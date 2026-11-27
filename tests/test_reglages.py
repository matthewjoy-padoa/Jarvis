"""Page Réglages : .env écrit sans réafficher les secrets, rechargement à chaud, garde-fous."""

import os
import stat

import pytest
from fastapi.testclient import TestClient

from deploy_hub import config, env_file, shell
from deploy_hub import main

SAME = {"origin": "http://testserver"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """.env de test, requêtes du client de test considérées comme locales, réglages remis à zéro après le test."""
    file = tmp_path / ".env"
    file.write_text("# réglages\nCLAUDE_BIN=claude  # garde-moi\nAIRFLOW_PASSWORD=mdp-en-clair\n")
    file.chmod(0o644)
    monkeypatch.setenv("JARVIS_ENV_FILE", str(file))
    monkeypatch.setattr(shell, "LOCAL_HOSTS", shell.LOCAL_HOSTS | {"testclient"})
    before = {f.key: os.environ.get(f.key) for f in env_file.FIELDS}
    for f in env_file.FIELDS:
        monkeypatch.delenv(f.key, raising=False)
    monkeypatch.setattr(main, "airflow", main.airflow)  # remis en place après le test
    monkeypatch.setattr(main.dag_watch, "airflow_env", main.dag_watch.airflow_env)
    yield file
    for key, value in before.items():  # la page a pu écrire dans os.environ : rien ne doit déborder sur les autres tests
        if value is None:
            os.environ.pop(key, None)
            config.ENV_FILE_KEYS.discard(key)


def test_a_stored_secret_is_never_sent_back_to_the_page(env):
    page = TestClient(main.app).get("/reglages").text
    assert "mdp-en-clair" not in page and ">enregistré</span>" in page and "laisse vide pour le garder" in page


def test_an_old_reference_left_in_the_env_file_counts_as_unset_and_is_flagged(env, monkeypatch):
    """Un secret resté sous la forme « op://… » n'est plus lu : il vaut « non renseigné » (jamais envoyé tel quel à Airflow
    ni à Notion, la synchro repasse par la routine) et Réglages le marque à remplacer, sans l'afficher."""
    env.write_text("AIRFLOW_PASSWORD=op://Coffre/Airflow/password\n")
    monkeypatch.setenv("AIRFLOW_PASSWORD", "op://Coffre/Airflow/password")
    monkeypatch.setenv("NOTION_TOKEN", "op://Coffre/Notion/jeton")
    assert config.secret("AIRFLOW_PASSWORD") == "" and main.notion_api.load_token() is None
    page = TestClient(main.app).get("/reglages").text
    assert ">à remplacer</span>" in page and "Coffre/Airflow" not in page


def test_saving_keeps_other_lines_and_untyped_secrets_and_restricts_the_file(env):
    form = {"AIRFLOW_BASE_URL": "http://localhost:8888", "AIRFLOW_USER": "utilisateur-airflow", "AIRFLOW_PASSWORD": ""}
    r = TestClient(main.app).post("/reglages", data=form, headers=SAME)
    assert r.status_code == 200 and "Enregistré dans .env : AIRFLOW_BASE_URL, AIRFLOW_USER" in r.text
    text = env.read_text()
    assert "# réglages" in text and "CLAUDE_BIN=claude  # garde-moi" in text  # le reste du fichier est intact
    assert "AIRFLOW_PASSWORD=mdp-en-clair" in text  # secret laissé vide : inchangé
    assert "AIRFLOW_USER=utilisateur-airflow" in text
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert os.environ["AIRFLOW_BASE_URL"] == "http://localhost:8888"


def test_a_secret_can_be_erased(env):
    TestClient(main.app).post("/reglages", data={"effacer_AIRFLOW_PASSWORD": "on"}, headers=SAME)
    assert "AIRFLOW_PASSWORD=\n" in env.read_text()


@pytest.mark.parametrize(("key", "value", "why"), [
    ("AIRFLOW_BASE_URL", "localhost:8888", "http://"),
    ("NOTION_TOKEN", "ntn_x\nAIRFLOW_USER=pirate", "une seule ligne"),
])
def test_invalid_values_write_nothing(env, key, value, why):
    before = env.read_text()
    r = TestClient(main.app).post("/reglages", data={key: value}, headers=SAME)
    assert why in r.text and env.read_text() == before


def test_saving_reloads_airflow_without_restart(env):
    form = {"AIRFLOW_BASE_URL": "http://localhost:8888", "AIRFLOW_USER": "jarvis", "AIRFLOW_PASSWORD": "mdp"}
    TestClient(main.app).post("/reglages", data=form, headers=SAME)
    assert main.airflow.live and main.airflow.base_url == "http://localhost:8888"
    assert main.dag_watch.airflow_env["AIRFLOW_USER"] == "jarvis"


def test_the_test_button_reports_both_connections(env, monkeypatch):
    from deploy_hub.sources import connections
    monkeypatch.setattr(connections, "notion_status", lambda token: (False, "pas de NOTION_TOKEN"))
    r = TestClient(main.app).post("/reglages/tester", headers=SAME)
    assert "Airflow" in r.text and "Notion" in r.text and 'pas de <a href="/reglages#reglage-NOTION_TOKEN">NOTION_TOKEN</a>' in r.text


@pytest.mark.parametrize("path", ["/reglages", "/reglages/tester"])
def test_another_site_cannot_change_settings(env, path):
    before = env.read_text()
    client = TestClient(main.app)
    assert client.post(path, data={"AIRFLOW_USER": "pirate"}).status_code == 403
    assert client.post(path, data={"AIRFLOW_USER": "pirate"}, headers={"origin": "https://site-hostile.example"}).status_code == 403
    assert env.read_text() == before


def test_only_local_connections(env, monkeypatch):
    monkeypatch.setattr(shell, "LOCAL_HOSTS", {"127.0.0.1"})
    assert TestClient(main.app).post("/reglages", data={"AIRFLOW_USER": "x"}, headers=SAME).status_code == 403


def test_the_bar_links_to_the_settings():
    assert 'href="/reglages"' in TestClient(main.app).get("/").text


def test_a_value_also_set_outside_env_is_flagged(env, monkeypatch):
    monkeypatch.setattr(config, "PROCESS_KEYS", {"AIRFLOW_BASE_URL"})
    monkeypatch.setattr(env_file, "PROCESS_KEYS", config.PROCESS_KEYS)
    monkeypatch.setattr(main, "PROCESS_KEYS", config.PROCESS_KEYS)
    assert "définie hors .env" in TestClient(main.app).get("/reglages").text
    r = TestClient(main.app).post("/reglages", data={"AIRFLOW_BASE_URL": "http://localhost:8888"}, headers=SAME)
    assert "AIRFLOW_BASE_URL est aussi défini hors .env" in r.text


def test_claude_settings_are_in_the_page_and_applied_to_the_next_agents(env):
    page = TestClient(main.app).get("/reglages").text
    assert "Agents Claude" in page and 'name="JARVIS_AGENT_MODEL"' in page and '<select name="JARVIS_AGENT_PERMISSION_MODE">' in page
    form = {"JARVIS_AGENT_MODEL": "opus", "JARVIS_AGENT_MAX": "3", "JARVIS_AGENT_TIMEOUT_MIN": "30", "JARVIS_AGENT_PERMISSION_MODE": "plan"}
    r = TestClient(main.app).post("/reglages", data=form, headers=SAME)
    assert "Enregistré dans .env" in r.text
    claude = main.claude
    try:
        assert (claude.model, claude.max_running, claude.timeout, claude.permission_mode) == ("opus", 3, 1800, "plan")
        TestClient(main.app).post("/reglages", data={k: "" for k in form}, headers=SAME)  # vidés : valeurs par défaut
        assert (claude.model, claude.max_running, claude.timeout, claude.permission_mode) == ("", 2, 5400, "acceptEdits")
    finally:
        for key in form:
            os.environ.pop(key, None)
        claude.reconfigure()


@pytest.mark.parametrize(("key", "value", "why"), [
    ("JARVIS_AGENT_MAX", "deux", "un nombre entier, 1 au moins"),
    ("JARVIS_AGENT_TIMEOUT_MIN", "0", "un nombre entier, 1 au moins"),
    ("JARVIS_DAG_INTERVAL_S", "5", "un nombre entier, 10 au moins"),
    ("JARVIS_AGENT_PERMISSION_MODE", "bypassPermissions", "acceptEdits ou plan ou default"),
    ("JARVIS_AGENT_EFFORT", "extreme", "low ou medium ou high ou xhigh ou max"),
    ("JARVIS_AGENT_MAX_TURNS", "0", "un nombre entier, 1 au moins"),
])
def test_invalid_claude_settings_write_nothing(env, key, value, why):
    before = env.read_text()
    r = TestClient(main.app).post("/reglages", data={key: value}, headers=SAME)
    assert why in r.text and env.read_text() == before


def test_there_is_no_demo_mode_setting_anymore(env, tmp_path):
    """Plus de mode démo : pas de réglage « Mode » ; un réglage lu au démarrage (dossier de l'orchestrator) attend toujours."""
    page = TestClient(main.app).get("/reglages").text
    assert 'name="JARVIS_MODE"' not in page
    r = TestClient(main.app).post("/reglages", data={"ORCHESTRATOR_DIR": str(tmp_path)}, headers=SAME)
    assert "Au prochain démarrage de Jarvis : Dossier de integration-orchestrator" in r.text
    os.environ.pop("ORCHESTRATOR_DIR", None)


def test_dag_watch_settings_go_to_the_next_watches(env):
    from deploy_hub.sources import dag_watch
    assert dag_watch.watch_options() == []  # vides : valeurs par défaut d'air.py
    r = TestClient(main.app).post("/reglages", data={"JARVIS_DAG_MAX_CLEARS": "0", "JARVIS_DAG_INTERVAL_S": "120"}, headers=SAME)
    assert "Suivi des DAG" in r.text and "Enregistré dans .env" in r.text
    try:
        assert dag_watch.watch_options() == ["--max-clears", "0", "--interval", "120"]  # 0 permis : aucune relance
    finally:
        os.environ.pop("JARVIS_DAG_MAX_CLEARS", None)
        os.environ.pop("JARVIS_DAG_INTERVAL_S", None)


def test_mac_notifications_are_turned_on_from_the_settings_not_the_bell():
    client = TestClient(main.app)
    page, panel = client.get("/reglages").text, client.get("/notifications/panneau").text
    assert 'id="notifications" data-browser-notifs' in page and "data-enable-browser-notifs" in page
    assert "data-enable-browser-notifs" not in panel and 'href="/reglages#notifications"' in panel
    script = client.get("/static/notify.js").text
    assert "[data-browser-notifs]" in script and "Notification.requestPermission()" in script


def test_agent_engine_and_effort_go_to_the_claude_command(env):
    page = TestClient(main.app).get("/reglages").text
    assert '<select name="JARVIS_AGENT_EFFORT">' in page and 'name="JARVIS_AGENT_FALLBACK_MODEL"' in page
    form = {"JARVIS_AGENT_MODEL": "opus", "JARVIS_AGENT_FALLBACK_MODEL": "sonnet", "JARVIS_AGENT_EFFORT": "high",
            "JARVIS_AGENT_MAX_TURNS": "40", "JARVIS_AGENT_MAX_BUDGET_USD": "5"}
    claude = main.claude
    try:
        TestClient(main.app).post("/reglages", data=form, headers=SAME)
        cmd = claude.build_command()
        for option, value in (("--model", "opus"), ("--fallback-model", "sonnet"), ("--effort", "high"), ("--max-turns", "40"), ("--max-budget-usd", "5")):
            assert cmd[cmd.index(option) + 1] == value
        assert cmd.index("--allowedTools") > cmd.index("--max-budget-usd")  # --allowedTools prend tout ce qui suit
        TestClient(main.app).post("/reglages", data={k: "" for k in form}, headers=SAME)  # vidés : aucune de ces options
        assert not {"--model", "--fallback-model", "--effort", "--max-turns", "--max-budget-usd"} & set(claude.build_command())
    finally:
        for key in form:
            os.environ.pop(key, None)
        claude.reconfigure()


def test_settings_are_split_into_parts_with_a_summary(env):
    page = TestClient(main.app).get("/reglages").text
    assert {f.section for f in env_file.FIELDS} == set(env_file.SECTIONS)  # aucun réglage hors d'une partie affichée
    for name, part in env_file.SECTIONS.items():  # chaque partie : un lien dans le sommaire et sa carte
        assert f'href="#{part.anchor}"' in page and f'id="{part.anchor}"' in page and name in page
    assert 'href="#notifications"' in page
    for f in env_file.FIELDS:  # chaque réglage une seule fois, dans la carte de sa partie
        assert page.count(f'name="{f.key}"') == 1
        start = page.index(f'id="{env_file.SECTIONS[f.section].anchor}"')
        assert start < page.index(f'name="{f.key}"') < page.index("</section>", page.index(f'name="{f.key}"'))
        assert "</section>" not in page[start:page.index(f'name="{f.key}"')]
    agents = page[page.index('id="agents"'):page.index('id="dags"')]
    assert [g for g in ("Moteur", "Limites", "Permissions et dossiers") if f'<h3 class="settings-group">{g}</h3>' in agents] \
        == ["Moteur", "Limites", "Permissions et dossiers"]
    assert agents.index("Limites") < agents.index('name="JARVIS_AGENT_MAX_TURNS"') < agents.index("Permissions et dossiers")


def test_first_launch_notes_the_folders_found_on_the_machine_once(env, monkeypatch, tmp_path):
    """Premier lancement : chaque dossier pas encore réglé est cherché parmi les clones git de la machine (le plus récent
    d'abord) et noté dans .env, où Réglages l'affiche déjà rempli ; un dossier réglé n'est ni cherché ni remplacé, un dossier
    introuvable reste vide. Ensuite tout est réglé : plus aucune recherche."""
    from deploy_hub import first_launch
    orchestrator, old, game = tmp_path / "Dev" / "integration-orchestrator", tmp_path / "integration-orchestrator", tmp_path / "ailleurs" / "jeu"
    searched = []
    monkeypatch.setattr(first_launch, "git_clones", lambda repos: searched.append(repos) or {
        "integration-orchestrator": [orchestrator, old], "flow-digger": [], "intecg": [game]})
    assert first_launch.note_folders() == ["ORCHESTRATOR_DIR", "INTECG_DIR"]
    lines = env.read_text().splitlines()
    assert f"ORCHESTRATOR_DIR={orchestrator}" in lines and f"INTECG_DIR={game}" in lines and "CLAUDE_BIN=claude  # garde-moi" in lines
    assert not any(line.startswith("FLOW_DIGGER_DIR") for line in lines) and os.environ["ORCHESTRATOR_DIR"] == str(orchestrator)
    page = TestClient(main.app).get("/reglages").text
    assert f'name="ORCHESTRATOR_DIR" value="{orchestrator}"' in page and f'name="INTECG_DIR" value="{game}"' in page
    assert first_launch.note_folders() == [] and searched == [("integration-orchestrator", "flow-digger", "intecg"), ("flow-digger",)]
    monkeypatch.setenv("FLOW_DIGGER_DIR", str(tmp_path))  # tout est réglé : plus de recherche
    monkeypatch.setattr(first_launch, "git_clones", lambda repos: pytest.fail("tout est réglé : pas de recherche"))
    assert first_launch.note_folders() == []
