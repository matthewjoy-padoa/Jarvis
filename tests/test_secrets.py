"""Secrets et données de Jarvis : dossiers hors de git, .env limité à son propriétaire, rien de secret donné aux processus lancés."""

import stat
import subprocess

import pytest

from deploy_hub import config


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


def test_processes_started_by_jarvis_get_neither_its_secrets_nor_a_password_manager_session(monkeypatch):
    monkeypatch.setenv("AIRFLOW_PASSWORD", "mot-de-passe")
    monkeypatch.setenv("OP_SESSION_exemple", "jeton-de-session")  # session héritée du terminal qui lance Jarvis
    given = config.child_environment()
    assert "AIRFLOW_PASSWORD" not in given and "OP_SESSION_exemple" not in given
