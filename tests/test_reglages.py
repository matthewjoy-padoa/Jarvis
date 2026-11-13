"""Page Réglages : .env écrit sans réafficher les secrets, rechargement à chaud, connexion à 1Password, garde-fous."""

import os
import stat

import pytest
from fastapi.testclient import TestClient

from deploy_hub import config, env_file, onepassword, shell
from deploy_hub import main

SAME = {"origin": "http://testserver"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """.env de test, requêtes du client de test considérées comme locales, réglages remis à zéro après le test."""
    file = tmp_path / ".env"
    file.write_text("# réglages\nJARVIS_MODE=demo  # garde-moi\nAIRFLOW_PASSWORD=mdp-en-clair\n")
    file.chmod(0o644)
    monkeypatch.setenv("JARVIS_ENV_FILE", str(file))
    monkeypatch.setattr(shell, "LOCAL_HOSTS", shell.LOCAL_HOSTS | {"testclient"})
    before = {f.key: os.environ.get(f.key) for f in env_file.FIELDS}
    for f in env_file.FIELDS:
        monkeypatch.delenv(f.key, raising=False)
    monkeypatch.setattr(main, "airflow", main.airflow)  # remis en place après le test
    monkeypatch.setattr(main.dag_watch, "airflow_env", main.dag_watch.airflow_env)
    yield file
    config.forget_secrets()
    for key, value in before.items():  # la page a pu écrire dans os.environ : rien ne doit déborder sur les autres tests
        if value is None:
            os.environ.pop(key, None)
            config.ENV_FILE_KEYS.discard(key)


@pytest.fixture
def fake_op(tmp_path, monkeypatch):
    """Faux op : signin accepte « bon-mdp » (session sur stdout), read répond seulement avec la session, whoami de même."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "op").write_text("""#!/bin/sh
case "$1" in
  signin) read pw; [ "$pw" = "bon-mdp" ] && echo 'export OP_SESSION_padoa="jeton-de-session"' && exit 0
          echo "[ERROR] 2026/11/13 10:00:00 Authentication: (401) Unauthorized" >&2; exit 1;;
  whoami) [ -n "$OP_SESSION_padoa" ] && echo '{"url":"padoa.1password.com","email":"moi@padoa.fr"}' && exit 0
          echo "[ERROR] 2026/11/13 10:00:00 account is not signed in" >&2; exit 1;;
  account) echo '[{"url":"padoa.1password.com","email":"moi@padoa.fr"}]';;
  read) [ -n "$OP_SESSION_padoa" ] || { echo "[ERROR] 2026/11/13 10:00:00 account is not signed in" >&2; exit 1; }
        case "$3" in *user) printf jarvis;; *password) printf mdp;; *) printf ntn_x;; esac;;
  signout) exit 0;;
esac
""")
    (bin_dir / "op").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(onepassword, "_session", {})
    config.forget_secrets()
    return bin_dir


def test_a_clear_secret_is_never_sent_back_to_the_page(env):
    page = TestClient(main.app).get("/reglages").text
    assert "mdp-en-clair" not in page and "en clair" in page and "laisse vide pour le garder" in page


def test_saving_keeps_other_lines_and_untyped_secrets_and_restricts_the_file(env):
    form = {"AIRFLOW_BASE_URL": "http://localhost:8888", "AIRFLOW_USER": "op://Coffre/Airflow/username", "AIRFLOW_PASSWORD": ""}
    r = TestClient(main.app).post("/reglages", data=form, headers=SAME)
    assert r.status_code == 200 and "Enregistré dans .env : AIRFLOW_BASE_URL, AIRFLOW_USER" in r.text
    text = env.read_text()
    assert "# réglages" in text and "JARVIS_MODE=demo  # garde-moi" in text  # le reste du fichier est intact
    assert "AIRFLOW_PASSWORD=mdp-en-clair" in text  # secret laissé vide : inchangé
    assert "AIRFLOW_USER=op://Coffre/Airflow/username" in text
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert os.environ["AIRFLOW_BASE_URL"] == "http://localhost:8888"


def test_a_secret_can_be_erased(env):
    TestClient(main.app).post("/reglages", data={"effacer_AIRFLOW_PASSWORD": "on"}, headers=SAME)
    assert "AIRFLOW_PASSWORD=\n" in env.read_text()


@pytest.mark.parametrize(("key", "value", "why"), [
    ("AIRFLOW_BASE_URL", "localhost:8888", "http://"),
    ("AIRFLOW_USER", "op:Coffre/x", "op://"),
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


def test_1password_signin_from_the_page_unlocks_the_references(env, fake_op):
    form = {"AIRFLOW_BASE_URL": "http://localhost:8888", "AIRFLOW_USER": "op://Coffre/Airflow/user", "AIRFLOW_PASSWORD": "op://Coffre/Airflow/password"}
    client = TestClient(main.app)
    client.post("/reglages", data=form, headers=SAME)
    assert not main.airflow.live  # pas encore connecté à 1Password
    bad = client.post("/reglages/1password/connexion", data={"password": "faux"}, headers=SAME)
    assert "Unauthorized" in bad.text and "faux" not in bad.text
    ok = client.post("/reglages/1password/connexion", data={"password": "bon-mdp"}, headers=SAME)
    assert "Connecté à 1Password" in ok.text and "moi@padoa.fr" in ok.text and "bon-mdp" not in ok.text
    assert main.airflow.live and (main.airflow.user, main.airflow.password) == ("jarvis", "mdp")
    assert "OP_SESSION_padoa" not in os.environ  # session gardée dans Jarvis seulement…
    assert not any(k.startswith("OP_SESSION_") for k in config.child_environment())  # … jamais donnée aux agents ni au shell
    client.post("/reglages/1password/deconnexion", headers=SAME)
    assert onepassword._session == {}


def test_the_test_button_reports_both_connections(env, monkeypatch):
    from deploy_hub.sources import connections
    monkeypatch.setattr(connections, "notion_status", lambda token, problem="": (False, "pas de NOTION_TOKEN"))
    r = TestClient(main.app).post("/reglages/tester", headers=SAME)
    assert "Airflow" in r.text and "Notion" in r.text and "pas de NOTION_TOKEN" in r.text


@pytest.mark.parametrize("path", ["/reglages", "/reglages/tester", "/reglages/1password/connexion", "/reglages/1password/deconnexion"])
def test_another_site_cannot_change_settings_or_sign_in(env, path):
    before = env.read_text()
    client = TestClient(main.app)
    assert client.post(path, data={"AIRFLOW_USER": "pirate", "password": "x"}).status_code == 403
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
