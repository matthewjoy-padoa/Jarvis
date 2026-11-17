import os
import tempfile

# Avant tout import de deploy_hub : le .env du développeur ne doit pas influencer les tests.
os.environ["JARVIS_ENV_FILE"] = os.devnull

from deploy_hub.config import PROJECT_ROOT  # noqa: E402

# Les tests lisent les fiches fictives, jamais data/ (fiches réelles synchronisées depuis Notion).
os.environ.setdefault("DEPLOY_HUB_DEPLOYMENTS_DIR", str(PROJECT_ROOT / "demo" / "deploiements"))
# Les notifications des tests vont dans un dossier temporaire, jamais dans demo/ ni data/.
os.environ.setdefault("DEPLOY_HUB_NOTIFICATIONS", str(__import__("pathlib").Path(tempfile.mkdtemp()) / "notifications.json"))
# Données écrites par l'app (historique et journaux des agents…) : dossier temporaire, jamais data/.
os.environ.setdefault("JARVIS_DATA_DIR", tempfile.mkdtemp())

import httpx  # noqa: E402
import pytest  # noqa: E402


def instant_agents(runner):
    """Doublure de test des agents : ni claude ni worktree, l'agent finit aussitôt (« terminé »). L'app, elle, lance toujours
    de vrais agents : il n'y a plus de mode démo."""
    runner.problem = lambda: None
    runner._start = lambda run: runner.finished(run, "terminé")
    return runner


@pytest.fixture(autouse=True)
def _agents_finish_at_once(monkeypatch):
    from deploy_hub import main
    monkeypatch.setattr(main.claude, "problem", lambda: None)
    monkeypatch.setattr(main.claude, "_start", lambda run: main.claude.finished(run, "terminé"))


@pytest.fixture
def fake_airflow(monkeypatch, tmp_path):
    """Airflow configuré mais factice : tout lancement réussit (run manual__1) ; le suivi du run ne lance rien."""
    from deploy_hub import main
    from deploy_hub.sources.airflow import AirflowSource
    from deploy_hub.sources.dag_watch import DagWatcher

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"dag_id": "x", "is_paused": False})
        return httpx.Response(200, json={"dag_run_id": "manual__1", "state": "queued"})

    airflow = AirflowSource("http://airflow.test", "user", "secret", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(main, "airflow", airflow)
    monkeypatch.setattr(main, "dag_watch", DagWatcher(tmp_path / "suivis", command=["true"]))
    return airflow
