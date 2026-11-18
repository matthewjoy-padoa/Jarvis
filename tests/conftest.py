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
# Configuration des clients : dossier temporaire vide, jamais config/ (les vrais clients du développeur).
os.environ.setdefault("JARVIS_CONFIG_DIR", tempfile.mkdtemp())

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

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


@dataclass
class Seen:
    """Requête reçue par le faux Airflow."""
    method: str
    path: str
    params: dict = field(default_factory=dict)  # paramètre → liste de valeurs
    body: dict | None = None
    headers: dict = field(default_factory=dict)


def airflow(dag=None, runs=(), tasks=()):
    """Réponses d'un Airflow sain, à ajuster : `dag` complète la fiche du DAG (ex. {"is_paused": True}), `runs` les runs en
    cours, `tasks` ses tâches. Un POST crée le run manual__1."""
    def handler(request: Seen):
        if request.method == "POST":
            return 200, {"dag_run_id": "manual__1", "state": "queued"}
        if request.path.endswith("/dagRuns"):
            return 200, {"dag_runs": list(runs), "total_entries": len(runs)}
        if request.path.endswith("/tasks"):
            return 200, {"tasks": list(tasks)}
        return 200, {"dag_id": request.path.rsplit("/", 1)[-1], "is_paused": False, "is_active": True, **(dag or {})}
    return handler


airflow_ok = airflow()


class FakeAirflow:
    """Vrai serveur HTTP local (127.0.0.1, port libre) : air.py, lancé en sous-processus, lui parle comme à l'Airflow de la VM.
    handler(Seen) -> (statut, corps JSON) ; chaque requête est notée dans `seen`."""

    def __init__(self, handler=airflow_ok):
        self.handler, self.seen = handler, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                url = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                req = Seen(self.command, url.path, parse_qs(url.query), json.loads(raw) if raw else None,
                           {k.lower(): v for k, v in self.headers.items()})
                fake.seen.append(req)
                status, body = fake.handler(req)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = _answer

            def log_message(self, *args):  # pas de bruit dans la sortie des tests
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def source(self, user="user", password="secret"):
        from deploy_hub.sources.airflow import AirflowSource
        return AirflowSource(self.url, user, password)

    def posted_runs(self, dag_id):
        return [r.body for r in self.seen if r.method == "POST" and r.path == f"/api/v1/dags/{dag_id}/dagRuns"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def airflow_server():
    """Fabrique de faux Airflow (voir FakeAirflow) ; tous arrêtés à la fin du test."""
    servers = []

    def start(handler=airflow_ok):
        servers.append(FakeAirflow(handler))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


@pytest.fixture
def fake_airflow(monkeypatch, tmp_path):
    """Airflow configuré mais factice, pour les tests du parcours dans Jarvis : tout lancement réussit (run manual__1) sans
    lancer air.py (testé contre un faux Airflow HTTP dans test_dags.py et test_stacks.py) ; le suivi du run ne lance rien."""
    from deploy_hub import main
    from deploy_hub.sources.airflow import AirflowSource
    from deploy_hub.sources.dag_watch import DagWatcher

    airflow = AirflowSource("http://airflow.test", "user", "secret")
    monkeypatch.setattr(airflow, "_air_run", lambda dag_id, padoa_env, launch, prod_authorized=False:
                        (True, f"DAG   : {dag_id}\n" + ("Lancé : run_id manual__1\n" if launch else "")))
    monkeypatch.setattr(main, "airflow", airflow)
    monkeypatch.setattr(main, "dag_watch", DagWatcher(tmp_path / "suivis", command=["true"]))
    return airflow
