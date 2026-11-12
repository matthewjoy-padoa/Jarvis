"""Suivi des runs lancés depuis Jarvis : air.py d'Arthur (adapté) en tâche de fond, notifications, rapport, pages Agents.
Aucun appel réseau : pour le suivi, air.py est remplacé par un faux script qui imprime ce qu'imprimerait un vrai suivi."""

import json
import os
import subprocess
import sys
import textwrap
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from deploy_hub import air, main
from deploy_hub.config import data_dir
from deploy_hub.models import Deployment
from deploy_hub.sources.airflow import AirflowSource
from deploy_hub.sources.dag_watch import DagWatcher, report_name
from deploy_hub.sources.notifications import NotificationStore

AIRFLOW_ENV = {"AIRFLOW_URL": "http://airflow.test", "AIRFLOW_USER": "user", "AIRFLOW_PASSWORD": "secret"}


def notif(msg):
    return "[notif] " + json.dumps({"title": "Airflow · acme-uegar", "msg": msg}, ensure_ascii=False)


def fake_air(tmp_path, lines, code=0, sleep=0.0):
    """Faux `python -m deploy_hub.air` : note chaque appel (arguments, environnement), imprime les lignes, écrit le rapport."""
    script = tmp_path / "fake_air.py"
    script.write_text(textwrap.dedent(f"""
        import json, os, sys, time
        from deploy_hub.sources.dag_watch import report_name
        state = os.environ["AIR_STATE_DIR"]
        with open(os.path.join(state, "appels.jsonl"), "a") as f:
            f.write(json.dumps({{"argv": sys.argv[1:], "env": {{k: os.environ.get(k) for k in
                    ("AIRFLOW_URL", "AIRFLOW_USER", "AIRFLOW_PASSWORD", "AIR_NOTIFY")}}}}) + "\\n")
        for line in {lines!r}:
            print(line, flush=True)
        time.sleep({sleep})
        os.makedirs(os.path.join(state, "air"), exist_ok=True)
        open(os.path.join(state, "air", report_name(sys.argv[2], sys.argv[3])), "w").write("## rapport de test")
        sys.exit({code})
    """))
    return [sys.executable, str(script)]


def calls(state_dir, at_least=1, timeout=10):
    """Appels notés par le faux air.py, en attendant qu'il en ait noté au moins `at_least` (démarrage de Python)."""
    path, end = state_dir / "appels.jsonl", time.time() + timeout
    while time.time() < end and (not path.is_file() or len(path.read_text().splitlines()) < at_least):
        time.sleep(0.05)
    return [json.loads(line) for line in path.read_text().splitlines()]


def wait_done(w, timeout=10):
    end = time.time() + timeout
    while w.running and time.time() < end:
        time.sleep(0.05)
    assert not w.running, "le suivi ne s'est pas terminé"


def wait_started(watcher, w, timeout=10):
    end = time.time() + timeout
    while w.id not in watcher._procs and time.time() < end:
        time.sleep(0.05)
    assert w.id in watcher._procs, "le suivi n'a pas démarré"


def start(watcher):
    return watcher.start("acme", "Acme", "acme-uegar", "acme-uegar-etl-firm", "manual__1", "recette", "Injection des firms")


# ---------------------------------------------------------------- air.py (logique d'Arthur, adaptations Jarvis)
@pytest.mark.parametrize(("log", "kind"), [
    ("[2026-11-12] {x.py:1} ERROR - psycopg2.OperationalError: could not connect to server", "transient"),
    ("Traceback (most recent call last):\n  File x\nKeyError: 'firm_id'", "fatal"),
    ("[2026-11-12] {x.py:1} INFO - Task exited with return code -9", "killed"),
    ("[2026-11-12] {x.py:1} ERROR - quelque chose d'inattendu", "unknown"),
])
def test_air_classifies_failures_like_arthur(log, kind):
    assert air.classify(log)[0] == kind


def test_air_watch_accepts_every_option_jarvis_and_arthur_use():
    """Le vrai parseur d'air.py : --client (Jarvis) n'a pas fait disparaître --max-clears, --interval, --clear-killed."""
    help_text = subprocess.run([sys.executable, "-m", "deploy_hub.air", "watch", "--help"], capture_output=True, text=True, check=True).stdout
    for option in ("--client", "--max-clears", "--interval", "--clear-killed"):
        assert option in help_text


def test_air_notify_prints_one_json_line_in_jarvis(monkeypatch, capsys):
    monkeypatch.setenv("AIR_NOTIFY", "print")
    monkeypatch.setattr(air.subprocess, "run", lambda *a, **k: pytest.fail("pas de notification macOS sous Jarvis"))
    air.notify("Airflow · acme-uegar", "✅ etl-firm vert · 3 min")
    assert capsys.readouterr().out == notif("✅ etl-firm vert · 3 min") + "\n"


def test_air_writes_state_and_journal_in_jarvis_data(tmp_path):
    code = "from deploy_hub import air; print(air.STATE); print(air.RUNS); print(air.JOURNAL)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={**os.environ, "AIR_STATE_DIR": str(tmp_path)}).stdout.split()
    assert out == [str(tmp_path), str(tmp_path / "air"), str(tmp_path / "airflow-runs.md")]


def test_report_name_follows_air():
    assert report_name("acme-uegar-etl-firm", "manual__2026-11-12T10:00:00+00:00") == "acme-uegar-etl-firm__manual__2026-11-12T10_00_00_00_00.md"


# ---------------------------------------------------------------- DagWatcher
def test_watch_runs_air_and_turns_its_notifications_into_jarvis_ones(tmp_path):
    notes = NotificationStore(tmp_path / "notifs.json")
    lines = ["Suivi de acme-uegar-etl-firm / manual__1", "Clear firm_load (1/3) : could not connect to server",
             notif("❌ etl-firm · firm_map : erreur de code/données, pas de relance. Va voir : KeyError"),
             notif("✅ etl-firm vert · 3 min 02 s · 10 créés, 2 maj")]
    watcher = DagWatcher(tmp_path, notes, AIRFLOW_ENV, command=fake_air(tmp_path, lines))
    w = start(watcher)
    wait_done(w)
    assert calls(tmp_path) == [{"argv": ["watch", "acme-uegar-etl-firm", "manual__1", "--client", "acme-uegar"],
                                "env": {**AIRFLOW_ENV, "AIR_NOTIFY": "print"}}]
    assert w.status == "vert" and w.summary.startswith("✅ etl-firm vert")
    added = list(reversed(notes.all()))
    assert [(n.level, n.link, n.kind) for n in added] == [("urgent", "/agents/suivis/1", "dag"), ("info", "/agents/suivis/1", "dag")]
    assert added[0].detail.startswith("❌ etl-firm · firm_map")
    assert "Clear firm_load (1/3)" in watcher.log(w) and watcher.report(w) == "## rapport de test"


@pytest.mark.parametrize(("code", "status"), [(1, "en échec ou incomplet"), (2, "Airflow injoignable")])
def test_watch_exit_codes_and_last_line_as_cause(tmp_path, code, status):
    watcher = DagWatcher(tmp_path, None, AIRFLOW_ENV, command=fake_air(tmp_path, ["401: identifiants refusés ou absents."], code=code))
    w = start(watcher)
    wait_done(w)
    assert w.status == status and w.summary == "401: identifiants refusés ou absents."


def test_stop_ends_the_watch_not_the_run(tmp_path):
    watcher = DagWatcher(tmp_path, None, AIRFLOW_ENV, command=fake_air(tmp_path, ["Suivi…"], sleep=30))
    w = start(watcher)
    wait_started(watcher, w)
    assert watcher.stop(w.id)
    wait_done(w)
    assert w.status == "arrêté" and "continue dans Airflow" in w.summary


def test_a_restart_resumes_running_watches(tmp_path):
    command = fake_air(tmp_path, ["Suivi…"], sleep=30)
    first = DagWatcher(tmp_path, None, AIRFLOW_ENV, command=command)
    w = start(first)
    calls(tmp_path, 1)  # le faux air.py a démarré
    first.shutdown()  # arrêt de Jarvis : le process est coupé, le suivi reste « en cours »
    time.sleep(0.2)
    assert w.status == "suivi en cours" and json.loads((tmp_path / "suivis.json").read_text())[0]["status"] == "suivi en cours"
    second = DagWatcher(tmp_path, None, AIRFLOW_ENV, command=command)  # le Jarvis suivant reprend le même run
    assert second.get(w.id).running
    assert [c["argv"][:3] for c in calls(tmp_path, 2)] == [["watch", "acme-uegar-etl-firm", "manual__1"]] * 2
    second.shutdown()


def test_a_restart_without_airflow_marks_watches_interrupted(tmp_path):
    first = DagWatcher(tmp_path, None, AIRFLOW_ENV, command=fake_air(tmp_path, ["Suivi…"], sleep=30))
    w = start(first)
    wait_started(first, w)
    first.shutdown()
    again = DagWatcher(tmp_path).get(w.id)
    assert again.status == "interrompu" and "sans Airflow configuré" in again.summary


def test_notification_keys_survive_a_lost_history(tmp_path):
    notes = NotificationStore(tmp_path / "notifs.json")
    for i in range(2):  # même id #1 deux fois (historique perdu entre les deux) : deux notifications
        watcher = DagWatcher(tmp_path / str(i), notes, AIRFLOW_ENV, command=fake_air(tmp_path, [notif("✅ vert")]))
        wait_done(start(watcher))
    assert len(notes.all()) == 2


# ---------------------------------------------------------------- « Lancer » → suivi, pages Agents
@pytest.fixture
def live_app(tmp_path, monkeypatch):
    # Deux clés orchestrator dont l'une préfixe l'autre : le client du suivi vient du catalogue, pas du préfixe du DAG.
    d = Deployment(slug="acme", client="Acme", software="Uegar", orchestrator=["acme", "acme-uegar"])
    catalogue = tmp_path / "catalogue.json"
    catalogue.write_text(json.dumps({"clients": {"acme-uegar": {"dags": {"acme-uegar-etl-firm": {"type": "etl", "ressources": ["firm"]}}}}}))
    monkeypatch.setattr(main.dag_catalogue, "CATALOGUE", catalogue)
    monkeypatch.setattr(main.fiches, "get_deployment", lambda slug: d if slug == "acme" else None)
    monkeypatch.setattr(main.fiches, "list_deployments", lambda: [d])

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"dag_id": "acme-uegar-etl-firm", "is_paused": False})
        return httpx.Response(200, json={"dag_run_id": "manual__1", "state": "queued"})

    monkeypatch.setattr(main, "airflow", AirflowSource("http://airflow.test", "user", "secret", transport=httpx.MockTransport(handler)))
    watcher = DagWatcher(tmp_path / "airflow", None, AIRFLOW_ENV, command=fake_air(tmp_path, [notif("✅ etl-firm vert")]))
    monkeypatch.setattr(main, "dag_watch", watcher)
    return TestClient(main.app), watcher


def test_a_real_launch_starts_the_watch_and_the_agents_pages_show_it(live_app):
    app, watcher = live_app
    page = app.post("/actions/injection/acme", data={"dag_id": "acme-uegar-etl-firm", "padoa_env": "snap", "card": "Injection des firms"})
    assert "manual__1" in page.text and "suivi #1 lancé" in page.text
    w = watcher.get(1)
    wait_done(w)
    assert (w.customer, w.padoa_env, w.status) == ("acme-uegar", "snap", "vert")
    listing = app.get("/agents").text
    assert "Suivis de DAG" in listing and 'href="/agents/suivis/1"' in listing
    detail = app.get("/agents/suivis/1").text
    assert "Rapport de fin" in detail and "## rapport de test" in detail and "Arrêter le suivi" not in detail
    assert app.get("/agents/suivis/1/journal").status_code == 200 and app.get("/agents/suivis/9").status_code == 404


def test_a_simulated_launch_starts_no_watch(monkeypatch, tmp_path):
    watcher = DagWatcher(tmp_path, command=["false"])
    monkeypatch.setattr(main, "dag_watch", watcher)
    monkeypatch.setattr(main, "airflow", AirflowSource(""))
    monkeypatch.setattr(main.fiches, "get_deployment", lambda slug: Deployment(slug="acme", client="Acme", software="Uegar", orchestrator=["acme-uegar"]))
    monkeypatch.setattr(main.dag_catalogue, "known_dag", lambda customers, dag_id: True)
    page = TestClient(main.app).post("/actions/injection/acme", data={"dag_id": "acme-uegar-etl-firm", "padoa_env": "snap"})
    assert "Simulation" in page.text and "suivi" not in page.text and watcher.watches == []


def test_watch_state_stays_in_jarvis_data_not_the_vault():
    """Rapport et journal contiennent des valeurs client : sous Jarvis, ils vont dans data/airflow."""
    assert main.dag_watch.state_dir == data_dir() / "airflow" and "Claude_vault" not in str(main.dag_watch.state_dir)


def test_sources_page_no_longer_promises_no_logs():
    assert "jamais les logs" not in TestClient(main.app).get("/sources").text
