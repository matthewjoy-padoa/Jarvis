"""Enchaînement de DAG : dans l'ordre, le suivant seulement si le précédent finit vert. Aucun appel réseau, données fictives."""

import json
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from deploy_hub import main
from deploy_hub.models import Deployment
from deploy_hub.sources.dag_chain import ChainRunner
from deploy_hub.sources.dag_watch import DagWatcher
from deploy_hub.sources.notifications import NotificationStore

DAGS = ["acme-etl-firm", "acme-etl-employee", "acme-etl-visit"]


class FakeLaunch:
    """Lancement de Jarvis remplacé : note les DAG partis, renvoie un suivi (ou rien en simulation, ou un refus)."""

    def __init__(self, watched=True, refuse=""):
        self.launched, self.watched, self.refuse = [], watched, refuse

    def __call__(self, dag_id, padoa_env, slug, card):
        if dag_id == self.refuse:
            return SimpleNamespace(ok=False, message="absent de cet Airflow"), None
        self.launched.append(dag_id)
        watch = SimpleNamespace(id=len(self.launched), status="suivi en cours", summary="", running=True) if self.watched else None
        return SimpleNamespace(ok=True, message="lancé"), watch


def end(runner, watch_id, status, summary=""):
    runner.watch_ended(SimpleNamespace(id=watch_id, status=status, summary=summary, running=False))


def test_the_next_dag_waits_for_a_green_one(tmp_path):
    launch, notes = FakeLaunch(), NotificationStore(tmp_path / "notifications.json")
    runner = ChainRunner(tmp_path / "chaines.json", launch, notes)
    chain = runner.start("acme", "Acme", "snap", DAGS)
    assert launch.launched == DAGS[:1] and [s.status for s in chain.steps] == ["en cours", "en attente", "en attente"]
    end(runner, 1, "vert")
    assert launch.launched == DAGS[:2] and chain.running
    end(runner, 2, "vert")
    end(runner, 3, "vert")
    assert launch.launched == DAGS and chain.status == "terminée" and chain.group == "done"
    assert [n.title for n in notes.all()] == ["Enchaînement #1 terminée · snap"]


def test_a_failed_dag_stops_the_chain(tmp_path):
    launch, notes = FakeLaunch(), NotificationStore(tmp_path / "notifications.json")
    runner = ChainRunner(tmp_path / "chaines.json", launch, notes)
    chain = runner.start("acme", "Acme", "snap", DAGS)
    end(runner, 1, "en échec ou incomplet", "❌ 2 tâches en échec")
    assert launch.launched == DAGS[:1] and chain.status == "arrêtée à l'étape 1" and chain.group == "ko"
    assert [s.status for s in chain.steps] == ["en échec ou incomplet", "non lancé", "non lancé"]
    note = notes.all()[0]
    assert note.level == "urgent" and "2 DAG suivant(s) non lancé(s)" in note.detail and "2 tâches en échec" in note.detail
    end(runner, 1, "vert")  # une fin rejouée ne relance rien
    assert launch.launched == DAGS[:1]


def test_a_refused_launch_stops_the_chain(tmp_path):
    launch = FakeLaunch(refuse=DAGS[1])
    runner = ChainRunner(tmp_path / "chaines.json", launch)
    chain = runner.start("acme", "Acme", "snap", DAGS)
    end(runner, 1, "vert")
    assert chain.status == "arrêtée à l'étape 2" and chain.steps[1].message == "absent de cet Airflow" and launch.launched == DAGS[:1]


def test_stopping_keeps_the_next_dags_from_starting(tmp_path):
    launch = FakeLaunch()
    runner = ChainRunner(tmp_path / "chaines.json", launch)
    chain = runner.start("acme", "Acme", "snap", DAGS)
    assert runner.stop(chain.id) and not runner.stop(chain.id)
    end(runner, 1, "vert")
    assert launch.launched == DAGS[:1] and chain.status == "arrêtée à la main" and chain.steps[2].status == "non lancé"


def test_simulated_launches_run_to_the_end(tmp_path):
    launch = FakeLaunch(watched=False)
    chain = ChainRunner(tmp_path / "chaines.json", launch).start("acme", "Acme", "snap", DAGS)
    assert launch.launched == DAGS and chain.status == "terminée" and chain.simulated


def test_chains_survive_a_restart_and_missed_ends_are_caught_up(tmp_path):
    launch = FakeLaunch()
    ChainRunner(tmp_path / "chaines.json", launch).start("acme", "Acme", "snap", DAGS)
    again = ChainRunner(tmp_path / "chaines.json", launch)
    chain = again.get(1)
    assert chain.running and chain.steps[0].watch_id == 1
    again.reconcile(lambda watch_id: SimpleNamespace(id=watch_id, status="suivi en cours", summary="", running=True))
    assert launch.launched == DAGS[:1]  # suivi encore en cours : rien à rattraper
    again.reconcile(lambda watch_id: SimpleNamespace(id=watch_id, status="vert", summary="", running=False))
    assert launch.launched == DAGS[:2]
    again.reconcile(lambda watch_id: None)
    assert chain.status == "arrêtée à l'étape 2" and chain.steps[1].status == "suivi introuvable"


def test_a_real_watch_ending_green_starts_the_next_dag(tmp_path):
    """Avec le vrai DagWatcher : le faux air.py sort en 0 (vert) pour le premier DAG, en 1 pour le deuxième."""
    code = "import sys; sys.exit(0 if sys.argv[2].endswith('firm') else 1)"
    watcher = DagWatcher(tmp_path, None, {"AIRFLOW_URL": "http://airflow.test"}, command=[sys.executable, "-c", code])
    runs = iter(range(1, 9))

    def launch(dag_id, padoa_env, slug, card):
        return SimpleNamespace(ok=True, message=""), watcher.start(slug, "Acme", "acme", dag_id, f"manual__{next(runs)}", padoa_env, card)

    runner = ChainRunner(tmp_path / "chaines.json", launch)
    watcher.on_end = runner.watch_ended
    chain = runner.start("acme", "Acme", "snap", DAGS)
    limit = time.time() + 15
    while chain.running and time.time() < limit:
        time.sleep(0.05)
    assert chain.status == "arrêtée à l'étape 2"
    assert [s.status for s in chain.steps] == ["vert", "en échec ou incomplet", "non lancé"] and len(watcher.watches) == 2


# ---------------------------------------------------------------- Pages
@pytest.fixture
def app(tmp_path, monkeypatch):
    d = Deployment(slug="acme", client="Acme", software="Uegar", orchestrator=["acme"])
    catalogue = tmp_path / "catalogue.json"
    catalogue.write_text(json.dumps({"clients": {"acme": {"dags": {dag: {"type": "etl", "ressources": [dag.rsplit("-", 1)[1]]} for dag in DAGS}}}}))
    monkeypatch.setattr(main.dag_catalogue, "CATALOGUE", catalogue)
    monkeypatch.setattr(main.fiches, "get_deployment", lambda slug: d if slug == "acme" else None)
    monkeypatch.setattr(main.fiches, "list_deployments", lambda: [d])
    monkeypatch.setattr(main, "dag_chains", ChainRunner(tmp_path / "chaines.json", main.dag_chains.launch))
    return TestClient(main.app)


def test_the_injections_tab_offers_the_chain(app):
    html = app.get("/deploiements/acme/injections").text
    assert "Enchaîner plusieurs DAG" in html and 'hx-post="/actions/chaine/acme"' in html
    assert all(f'name="dag_id" value="{dag}"' in html for dag in DAGS)


def test_a_chain_follows_the_click_order_and_shows_on_the_agents_page(app):
    wanted = [DAGS[2], DAGS[0]]
    r = app.post("/actions/chaine/acme", data={"padoa_env": "snap", "dag_id": [DAGS[0], DAGS[2]], "ordre": ",".join(wanted)})
    assert "Enchaînement #1 simulé de bout en bout" in r.text and f"{DAGS[2]} → {DAGS[0]}" in r.text
    assert [s.dag_id for s in main.dag_chains.get(1).steps] == wanted
    page = app.get("/agents").text
    assert 'id="chaines"' in page and "terminée" in page and DAGS[2] in page and "/agents/chaines/1/arreter" not in page


@pytest.mark.parametrize(("data", "why"), [
    ({"padoa_env": "snap", "dag_id": [DAGS[0]]}, "au moins deux DAG"),
    ({"padoa_env": "prod", "dag_id": DAGS}, "jamais en prod"),
    ({"padoa_env": "ailleurs", "dag_id": DAGS}, "Stack inconnue"),
    ({"padoa_env": "snap", "dag_id": [DAGS[0], "autre-etl-firm"]}, "n&#39;est pas un DAG de Acme"),
])
def test_a_wrong_chain_starts_nothing(app, data, why):
    r = app.post("/actions/chaine/acme", data=data)
    assert why in r.text and not main.dag_chains.chains


def test_stopping_an_unknown_chain_is_a_404(app):
    assert app.post("/agents/chaines/9/arreter").status_code == 404
