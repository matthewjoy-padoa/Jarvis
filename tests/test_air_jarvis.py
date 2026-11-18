"""Point d'entrée du skill /air : air.py lancé avec la configuration de Jarvis."""

import os
import subprocess
import sys

import pytest

from deploy_hub import air_jarvis, config
from deploy_hub.config import data_dir


@pytest.fixture(autouse=True)
def _isolated_environ(monkeypatch):
    """load_env_file écrit dans os.environ : chaque test travaille sur une copie."""
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setattr(config, "ENV_FILE_KEYS", set())


def test_jarvis_env_takes_airflow_settings_and_data_dir(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("AIRFLOW_BASE_URL=http://airflow.test:8081  # tunnel\nAIRFLOW_USER=u-test\nAIRFLOW_PASSWORD=p-test\n")
    monkeypatch.setenv("JARVIS_ENV_FILE", str(env_file))
    for key in ("AIRFLOW_BASE_URL", "AIRFLOW_USER", "AIRFLOW_PASSWORD", "AIRFLOW_URL", "AIR_STATE_DIR"):
        monkeypatch.delenv(key, raising=False)

    env = air_jarvis.jarvis_env()

    assert env == {"AIRFLOW_URL": "http://airflow.test:8081", "AIRFLOW_USER": "u-test", "AIRFLOW_PASSWORD": "p-test",
                   "AIR_STATE_DIR": str(data_dir() / "airflow")}


def test_jarvis_env_keeps_variables_already_set(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ENV_FILE", str(tmp_path / "absent.env"))
    for key in ("AIRFLOW_BASE_URL", "AIRFLOW_USER", "AIRFLOW_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AIRFLOW_URL", "http://deja-fixe.test")
    monkeypatch.setenv("AIR_STATE_DIR", str(tmp_path))

    assert air_jarvis.jarvis_env() == {}  # rien de configuré dans Jarvis, rien d'écrasé


def test_jarvis_env_resolves_credentials_from_process(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("AIRFLOW_USER", "u-proc")
    monkeypatch.setenv("AIRFLOW_PASSWORD", "op://coffre/airflow/password")
    monkeypatch.setattr(config, "resolve", lambda v: "p-resolu" if v.startswith("op://") else v)

    env = air_jarvis.jarvis_env()

    assert env["AIRFLOW_USER"] == "u-proc" and env["AIRFLOW_PASSWORD"] == "p-resolu"  # jamais la référence brute


def test_entry_point_runs_air_with_jarvis_state_dir(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AIRFLOW")}
    env |= {"JARVIS_ENV_FILE": str(tmp_path / "absent.env"), "JARVIS_DATA_DIR": str(tmp_path)}
    env.pop("AIR_STATE_DIR", None)

    out = subprocess.run([sys.executable, "-m", "deploy_hub.air_jarvis", "client", "client-test"],
                         check=False, capture_output=True, text=True, env=env, timeout=60)

    assert out.returncode == 0, out.stderr
    assert "client-test" in out.stdout
    assert (tmp_path / "airflow" / "air.json").read_text().count("client-test") == 1  # état dans data/airflow
