"""Faux Airflow de l'environnement local (demo/lancer.py) : un vrai serveur HTTP sur 127.0.0.1 qui répond à l'API REST v1
comme l'Airflow de la VM, pour que air.py (aperçu, lancement, suivi) tourne sans rien changer.

Les DAGs sont ceux du catalogue fictif (demo/dags/catalogue.json) et les dag_id des fiches fictives ; tout autre DAG
répond 404, comme sur la VM. Un run lancé avance tout seul avec le temps :
en file 2 s, puis extract, transform et load (DUREE_TACHE s chacune), puis succès. Rien n'est persisté.
"""
import json
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

TACHES = ("extract", "transform", "load")
DUREE_TACHE = 8  # secondes
ATTENTE = 2  # secondes en file avant la première tâche


def _iso(t: float | None) -> str | None:
    return datetime.fromtimestamp(t, timezone.utc).isoformat() if t else None


class FauxAirflow:
    def __init__(self, catalogue: Path, fiches: Path, port: int = 0):
        clients = json.loads(catalogue.read_text(encoding="utf-8"))["clients"]
        self.dags = ({d for c in clients.values() for d in (c.get("dags") or {})}
                     | {c["ced"]["dag_id"] for c in clients.values() if c.get("ced")}
                     | {m for f in fiches.glob("*.md") for m in re.findall(r"dag_id: *([\w-]+)", f.read_text(encoding="utf-8"))})
        self.active_clients = ",".join(sorted(clients))
        self.runs: list[dict] = []  # {dag_id, run_id, created, conf} ; list.append suffit entre threads (GIL)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                url = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"null") if length else None
                status, payload = fake.route(self.command, unquote(url.path), parse_qs(url.query), body)
                data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/plain" if isinstance(payload, str) else "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = _answer

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "FauxAirflow":
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        return self

    # --- runs qui avancent avec le temps ---------------------------------------------------------------------------
    def _task(self, run: dict, i: int, now: float) -> dict:
        begin = run["created"] + ATTENTE + i * DUREE_TACHE
        end = begin + DUREE_TACHE
        state = "success" if now >= end else "running" if now >= begin else None
        return {"task_id": TACHES[i], "dag_id": run["dag_id"], "dag_run_id": run["run_id"], "map_index": -1, "state": state,
                "try_number": 1 if state else 0, "start_date": _iso(begin if state else None),
                "end_date": _iso(end if state == "success" else None), "duration": DUREE_TACHE if state == "success" else None}

    def _run(self, run: dict, now: float | None = None) -> dict:
        now = now or time.time()
        done = run["created"] + ATTENTE + len(TACHES) * DUREE_TACHE
        state = "success" if now >= done else "running" if now >= run["created"] + ATTENTE else "queued"
        return {"dag_id": run["dag_id"], "dag_run_id": run["run_id"], "state": state, "conf": run["conf"],
                "execution_date": _iso(run["created"]), "logical_date": _iso(run["created"]),
                "start_date": _iso(run["created"] + ATTENTE) if state != "queued" else None,
                "end_date": _iso(done) if state == "success" else None, "run_type": "manual"}

    def _find(self, dag_id: str, run_id: str) -> dict | None:
        return next((r for r in self.runs if r["dag_id"] == dag_id and r["run_id"] == run_id), None)

    # --- API REST ---------------------------------------------------------------------------------------------------
    def route(self, method: str, path: str, query: dict, body):
        parts = [p for p in path.split("/") if p]
        if path == "/health":
            return 200, {"metadatabase": {"status": "healthy"}, "scheduler": {"status": "healthy"}}
        if parts[:2] != ["api", "v1"]:
            return 404, {"title": "Not Found"}
        rest = parts[2:]
        if rest[:2] == ["variables", "active_clients"]:
            if method == "PATCH" and body:
                self.active_clients = body.get("value", self.active_clients)
            return 200, {"key": "active_clients", "value": self.active_clients}
        if rest == ["dags"]:
            pattern = (query.get("dag_id_pattern") or [""])[0]
            dags = [self._dag(d) for d in sorted(self.dags) if pattern in d]
            return 200, {"dags": dags, "total_entries": len(dags)}
        if len(rest) < 2 or rest[0] != "dags":
            return 404, {"title": "Not Found"}
        dag_id, sub = rest[1], rest[2:]
        if dag_id == "~" and sub == ["dagRuns", "list"]:
            wanted = set((body or {}).get("dag_ids") or self.dags)
            runs = [self._run(r) for r in reversed(self.runs) if r["dag_id"] in wanted]
            return 200, {"dag_runs": runs, "total_entries": len(runs)}
        if dag_id != "~" and dag_id not in self.dags:
            return 404, {"title": "DAG not found", "detail": f"{dag_id} absent du faux Airflow"}
        if sub == ["dagRuns"] and method == "POST":
            run = {"dag_id": dag_id, "run_id": f"manual__{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
                   "created": time.time(), "conf": (body or {}).get("conf") or {}}
            self.runs.append(run)
            return 200, self._run(run)
        if sub == ["dagRuns"]:  # runs d'un DAG, ou de tous (~), du plus récent au plus ancien
            states = set(query.get("state") or [])
            runs = [self._run(r) for r in reversed(self.runs) if dag_id == "~" or r["dag_id"] == dag_id]
            runs = [r for r in runs if not states or r["state"] in states]
            limit = int((query.get("limit") or ["100"])[0])
            return 200, {"dag_runs": runs[:limit], "total_entries": len(runs)}
        if not sub:
            return 200, self._dag(dag_id)
        if sub == ["tasks"]:
            return 200, {"tasks": [{"task_id": t} for t in TACHES], "total_entries": len(TACHES)}
        if sub == ["clearTaskInstances"]:
            return 200, {"task_instances": []}
        run = self._find(dag_id, sub[1]) if len(sub) >= 2 and sub[0] == "dagRuns" else None
        if run is None:
            return 404, {"title": "DAG run not found"}
        now = time.time()
        if len(sub) == 2:
            return 200, self._run(run, now)
        tasks = [self._task(run, i, now) for i in range(len(TACHES))]
        if sub[2:] == ["taskInstances"]:
            return 200, {"task_instances": tasks, "total_entries": len(tasks)}
        task = next((t for t in tasks if len(sub) > 3 and t["task_id"] == sub[3]), None)
        if task is None:
            return 404, {"title": "Task instance not found"}
        if len(sub) > 4 and sub[4] == "logs":
            return 200, (f"[faux Airflow] {dag_id} / {task['task_id']} : journal fictif.\n"
                         "INFO - Lignes lues : 120\nINFO - Lignes écrites : 118\nINFO - Tâche terminée.\n")
        return 200, task

    def _dag(self, dag_id: str) -> dict:
        return {"dag_id": dag_id, "is_paused": False, "is_active": True, "tags": [{"name": "demo"}],
                "description": "DAG fictif de l'environnement local"}
