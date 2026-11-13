"""Secrets en référence 1Password (op://…), dossiers de données hors de git, .env limité à son propriétaire."""

import os
import stat
import subprocess

import pytest

from deploy_hub import config
from deploy_hub.sources import notion_api
from deploy_hub.sources.airflow import AirflowSource

REF = "op://Padoa-integration/Jarvis/notion"


@pytest.fixture
def fake_op(tmp_path, monkeypatch):
    """Faux 1Password CLI : répond la valeur de la référence connue, « not signed in » sinon ; compte ses appels."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "appels"
    op = bin_dir / "op"
    op.write_text(f"""#!/bin/sh
echo x >> {calls}
[ "$1 $2 $3" = "read --no-newline {REF}" ] && printf 'ntn_secret' && exit 0
[ "$3" = "op://Padoa-integration/Airflow/user" ] && printf 'jarvis' && exit 0
[ "$3" = "op://Padoa-integration/Airflow/password" ] && printf 'mdp' && exit 0
echo "[ERROR] 2026/11/13 account is not signed in" >&2
exit 1
""")
    op.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(config, "_resolved", {})
    monkeypatch.setattr(config, "SECRET_ERRORS", {})
    monkeypatch.setattr(config, "_failed_at", {})
    return lambda: len(calls.read_text().splitlines()) if calls.exists() else 0


def test_a_plain_value_is_kept_as_is(fake_op):
    assert config.resolve("ntn_en_clair") == "ntn_en_clair" and fake_op() == 0


def test_a_1password_reference_is_read_once_and_kept_in_memory_only(fake_op, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", REF)
    assert config.secret("NOTION_TOKEN") == "ntn_secret"
    assert config.secret("NOTION_TOKEN") == "ntn_secret" and fake_op() == 1  # un seul op read
    assert os.environ["NOTION_TOKEN"] == REF  # l'environnement (transmis aux agents, au shell) ne garde que la référence


def test_an_unreadable_reference_gives_nothing_and_says_why(fake_op, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "op://Padoa-integration/Inconnu/champ")
    assert notion_api.load_token() is None
    assert "not signed in" in config.SECRET_ERRORS["op://Padoa-integration/Inconnu/champ"]
    assert config.secret("NOTION_TOKEN") == "" and fake_op() == 1  # pas de nouvel op read à chaque page…
    monkeypatch.setattr(config, "RETRY_AFTER", 0)
    assert config.secret("NOTION_TOKEN") == "" and fake_op() == 2  # … mais on retente après RETRY_AFTER


def test_without_op_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))  # aucun op
    monkeypatch.setattr(config, "_resolved", {})
    monkeypatch.setattr(config, "SECRET_ERRORS", {})
    monkeypatch.setattr(config, "_failed_at", {})
    assert config.resolve(REF) == "" and config.SECRET_ERRORS[REF] == "1Password CLI (op) absent"


def test_the_notion_token_can_be_a_reference_in_the_env_file(fake_op, monkeypatch, tmp_path):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    (tmp_path / ".env").write_text(f"NOTION_TOKEN={REF}  # dans 1Password\n")
    assert notion_api.load_token(tmp_path) == "ntn_secret"


def test_airflow_credentials_can_be_references(fake_op, monkeypatch):
    monkeypatch.setenv("AIRFLOW_BASE_URL", "http://localhost:8888")
    monkeypatch.setenv("AIRFLOW_USER", "op://Padoa-integration/Airflow/user")
    monkeypatch.setenv("AIRFLOW_PASSWORD", "op://Padoa-integration/Airflow/password")
    airflow = AirflowSource.from_env()
    assert airflow.live and (airflow.user, airflow.password) == ("jarvis", "mdp")


def git_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_a_data_folder_git_could_push_is_refused(tmp_path):
    repo = git_repo(tmp_path / "depot")
    problem = config.private_dir_problem(repo / "donnees")  # n'existe pas encore : on part de son parent
    assert "sans y être ignoré" in problem and str(repo.resolve()) in problem
    (repo / ".gitignore").write_text("donnees/\n")
    assert config.private_dir_problem(repo / "donnees") == ""


def test_a_folder_outside_any_repository_is_fine(tmp_path):
    assert config.private_dir_problem(tmp_path / "hors-git" / "data") == ""


def test_jarvis_refuses_to_start_with_pushable_data(tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "depot")
    (repo / ".gitignore").write_text("config/\n")  # la configuration est ignorée, les données non
    with pytest.raises(SystemExit, match="git pourrait pousser des données client"):
        config.ensure_private_dirs(repo)
    (repo / ".gitignore").write_text("config/\ndata/\n")
    config.ensure_private_dirs(repo)  # tout est ignoré : démarrage normal


def test_jarvis_own_data_and_config_are_ignored():
    assert config.private_dir_problem(config.PROJECT_ROOT / "data") == ""
    assert config.private_dir_problem(config.PROJECT_ROOT / "config") == ""


def test_an_env_file_readable_by_others_is_restricted(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("JARVIS_TEST_RESTRICT=1\n")
    env.chmod(0o644)
    monkeypatch.delenv("JARVIS_TEST_RESTRICT", raising=False)
    config.load_env_file(env)
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert "droits ramenés à 600" in capsys.readouterr().err
    assert config.restrict(env) is False  # déjà limité : rien à faire
    monkeypatch.delenv("JARVIS_TEST_RESTRICT", raising=False)


def test_the_sources_page_says_why_the_reference_is_unreadable(fake_op, monkeypatch):
    from fastapi.testclient import TestClient
    from deploy_hub.main import app
    monkeypatch.setenv("NOTION_TOKEN", "op://Padoa-integration/Inconnu/champ")
    page = TestClient(app).get("/sources").text
    assert "référence 1Password illisible" in page and "not signed in" in page


def test_the_sources_page_says_why_airflow_credentials_are_unreadable(fake_op, monkeypatch):
    from fastapi.testclient import TestClient
    from deploy_hub import config as cfg
    from deploy_hub.main import app
    monkeypatch.setenv("AIRFLOW_USER", "op://Padoa-integration/Inconnu/user")
    cfg.secret("AIRFLOW_USER")  # lu au démarrage par AirflowSource.from_env
    page = TestClient(app).get("/sources").text
    assert "AIRFLOW_USER : " in page and "not signed in" in page
