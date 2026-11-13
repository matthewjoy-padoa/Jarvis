"""Suivi des runs lancés depuis Jarvis : après chaque « Lancer » réel, `air.py watch` (le script d'Arthur, deploy_hub/air.py)
tourne en tâche de fond. Il relance les tâches en échec passager (3 fois au plus), signale le reste, puis écrit un rapport.

Ses lignes « [notif] {"title", "msg"} » deviennent des notifications de Jarvis ; tout le reste va au journal du suivi.
État, journaux, rapports : data/airflow (AIR_STATE_DIR), hors de git et jamais dans le vault (valeurs client possibles).
Le suivi lit les logs des tâches qu'il suit : dérogation décidée par Matthew le 2026-11-12.

Redémarrage de Jarvis (rechargement compris) : shutdown() coupe les process sans changer l'état des suivis, puis le Jarvis
suivant les relance ; air.py reprend où il en était (son état watch__<dag>__<run>.json garde relances et notifications).
"""

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from deploy_hub.models import DONE, IN_PROGRESS, KO, status_group

WATCH_STATUSES = {"suivi en cours": IN_PROGRESS, "vert": DONE, "en échec ou incomplet": KO, "Airflow injoignable": KO,
                  "arrêté": KO, "interrompu": KO}
EXIT_STATUS = {0: "vert", 1: "en échec ou incomplet", 2: "Airflow injoignable"}  # codes de sortie de air.py watch
NOTIF = "[notif] "  # préfixe des lignes JSON imprimées par air.notify sous Jarvis (AIR_NOTIFY=print)


def watch_options() -> list[str]:
    """Réglages du suivi (page Réglages), relus à chaque lancement : JARVIS_DAG_MAX_CLEARS (relances automatiques,
    0 = aucune) et JARVIS_DAG_INTERVAL_S (secondes entre deux vérifications). Vides : valeurs par défaut d'air.py."""
    options = []
    for key, flag in (("JARVIS_DAG_MAX_CLEARS", "--max-clears"), ("JARVIS_DAG_INTERVAL_S", "--interval")):
        value = os.getenv(key, "").strip()
        if value.isdigit():
            options += [flag, value]
    return options


@dataclass
class DagWatch:
    id: int
    slug: str
    client: str
    customer: str  # client orchestrator du DAG (catalogue), passé à air.py --client
    dag_id: str
    run_id: str
    padoa_env: str
    card: str
    started_at: datetime
    status: str
    ended_at: datetime | None = None
    summary: str = ""  # dernière notification du suivi, ou cause de sa fin

    @property
    def group(self) -> str:
        return status_group(self.status, WATCH_STATUSES)

    @property
    def running(self) -> bool:
        return self.group == IN_PROGRESS


def report_name(dag_id: str, run_id: str) -> str:
    """Nom du rapport écrit par air.py (build_report) pour ce run, dans <AIR_STATE_DIR>/air/."""
    return re.sub(r"[^\w.-]", "_", f"{dag_id}__{run_id}") + ".md"


class DagWatcher:
    def __init__(self, state_dir: Path, notifications=None, airflow_env: dict[str, str] | None = None,
                 command: list[str] | None = None, on_end=None):
        """airflow_env : AIRFLOW_URL, AIRFLOW_USER, AIRFLOW_PASSWORD (vide en mode simulé : aucun suivi à reprendre)."""
        self.state_dir = state_dir
        self.state_file = state_dir / "suivis.json"
        self.notifications = notifications
        self.airflow_env = airflow_env or {}
        self.command = command or [sys.executable, "-m", "deploy_hub.air"]  # remplacé par un faux script dans les tests
        self.on_end = on_end  # appelé avec le suivi quand il se termine (enchaînements : sources/dag_chain.py)
        self._lock = threading.RLock()
        self._procs: dict[int, subprocess.Popen] = {}
        self._stopped: set[int] = set()  # arrêtés par la personne (bouton) : « arrêté »
        self._closing = False  # Jarvis s'arrête : les suivis gardent leur état, le Jarvis suivant les reprend
        self.watches: list[DagWatch] = self._load()
        for w in self.watches:
            if w.running:
                self._spawn(w)

    # --- historique (écriture atomique, comme ClaudeRunner) -----------------------------------------------------
    def _load(self) -> list[DagWatch]:
        if not self.state_file.is_file():
            return []
        try:
            items = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state_file.replace(self.state_file.with_suffix(f".illisible-{datetime.now():%Y%m%d-%H%M%S}"))
            return []
        watches = []
        for item in items:
            for key in ("started_at", "ended_at"):
                item[key] = datetime.fromisoformat(item[key]) if item.get(key) else None
            w = DagWatch(**{k: v for k, v in item.items() if k in DagWatch.__dataclass_fields__})
            if w.running and not self.airflow_env:  # plus d'Airflow configuré : impossible de reprendre
                w.status, w.ended_at = "interrompu", datetime.now()
                w.summary = "Jarvis a redémarré sans Airflow configuré : le run continue dans Airflow, sans relances."
            watches.append(w)
        return watches

    def _save(self) -> None:
        with self._lock:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps([asdict(w) for w in self.watches], ensure_ascii=False, default=datetime.isoformat), encoding="utf-8")
            tmp.replace(self.state_file)

    def get(self, watch_id: int) -> DagWatch | None:
        return next((w for w in self.watches if w.id == watch_id), None)

    def log_path(self, w: DagWatch) -> Path:
        return self.state_dir / f"suivi-{w.id}.log"

    def log(self, w: DagWatch) -> str:
        path = self.log_path(w)
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""

    def report(self, w: DagWatch) -> str:
        """Rapport de fin écrit par air.py (durées, loader, compare, cleaner, mappings manquants), s'il existe."""
        path = self.state_dir / "air" / report_name(w.dag_id, w.run_id)
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""

    # --- suivi --------------------------------------------------------------------------------------------------
    def start(self, slug: str, client: str, customer: str, dag_id: str, run_id: str, padoa_env: str, card: str) -> DagWatch:
        with self._lock:
            w = DagWatch(max((x.id for x in self.watches), default=0) + 1, slug, client, customer, dag_id, run_id,
                         padoa_env, card, datetime.now(), "suivi en cours")
            self.watches.insert(0, w)
            self._save()
        self._spawn(w)
        return w

    def _spawn(self, w: DagWatch) -> None:
        env = {**os.environ, **self.airflow_env, "AIR_STATE_DIR": str(self.state_dir), "AIR_NOTIFY": "print", "PYTHONUNBUFFERED": "1"}
        cmd = [*self.command, "watch", w.dag_id, w.run_id, "--client", w.customer, *watch_options()]
        threading.Thread(target=self._work, args=(w, cmd, env), name=f"suivi-{w.id}", daemon=True).start()

    def _work(self, w: DagWatch, cmd: list[str], env: dict[str, str]) -> None:
        notes, last = 0, ""
        try:
            with self.log_path(w).open("a", encoding="utf-8") as out:  # « a » : une reprise continue le même journal
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
                self._procs[w.id] = proc
                for line in proc.stdout:
                    out.write(line)
                    out.flush()
                    last = line.strip() or last
                    if line.startswith(NOTIF):
                        notes += 1
                        self._notify(w, json.loads(line[len(NOTIF):]).get("msg", ""))
                code = proc.wait()
        except Exception as e:  # noqa: BLE001 : tout échec doit finir en état visible, pas en thread mort
            w.summary, code = f"Suivi impossible : {e}", None
        finally:
            self._procs.pop(w.id, None)
        if self._closing and w.id not in self._stopped:
            return  # Jarvis s'arrête : le suivi reste « en cours », le Jarvis suivant le reprend
        if w.id in self._stopped:
            status, w.summary = "arrêté", "Suivi arrêté depuis Jarvis : le run continue dans Airflow, sans relances automatiques."
        else:
            status = EXIT_STATUS.get(code, "en échec ou incomplet")
            if code and not notes:  # sorti sans rien notifier (401, run introuvable…) : sa dernière ligne dit pourquoi
                w.summary = last[:300] or f"Code de sortie {code} : voir le journal du suivi."
        w.status, w.ended_at = status, datetime.now()
        self._save()
        if self.on_end:
            try:
                self.on_end(w)
            except Exception as e:  # noqa: BLE001 : la fin du suivi est déjà enregistrée, la suite ne doit pas tuer le thread
                w.summary = f"{w.summary} · suite de l'enchaînement impossible : {e}".strip(" ·")
                self._save()

    def _notify(self, w: DagWatch, msg: str) -> None:
        w.summary = msg
        if not self.notifications:
            return
        level = "info" if msg.startswith("✅") else "urgent" if msg.startswith(("❌", "⛔", "🔌")) else "warning"
        # Clé tirée du message : après une reprise, air.py ne renvoie pas ce qu'il a déjà notifié, et rien n'est doublé.
        self.notifications.add(f"suivi:{w.id}:{w.started_at.isoformat()}:{msg}", "dag", level, f"{w.dag_id} · {w.padoa_env}",
                               msg, f"/agents/suivis/{w.id}", w.client)

    @staticmethod
    def _end(proc: subprocess.Popen, timeout: float = 5) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()

    def stop(self, watch_id: int) -> bool:
        """Arrête le suivi (pas le run : il continue dans Airflow, sans relances automatiques)."""
        proc = self._procs.get(watch_id)
        if not proc:
            return False
        self._stopped.add(watch_id)
        threading.Thread(target=self._end, args=(proc,), daemon=True).start()
        return True

    def shutdown(self) -> None:
        """Arrêt de Jarvis : coupe les process (sinon ils survivraient) sans changer l'état ; le Jarvis suivant reprend."""
        self._closing = True
        for proc in list(self._procs.values()):
            self._end(proc)
