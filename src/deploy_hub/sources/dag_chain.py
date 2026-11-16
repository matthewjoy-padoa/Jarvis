"""Enchaînement de DAG : plusieurs DAG lancés dans l'ordre, le suivant seulement si le précédent finit vert.

Chaque étape passe par le lancement habituel (main._launch_watched) : c'est le suivi du run (sources/dag_watch.py) qui dit
quand il est fini et s'il est vert. Sans Airflow configuré, les lancements sont simulés et les étapes s'enchaînent aussitôt.
Jamais en prod. État : data/airflow/chaines.json (noms de DAG et de client seulement).
"""

import json
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from deploy_hub.models import DONE, IN_PROGRESS, KO, TODO

log = logging.getLogger(__name__)

WAITING, RUNNING, GREEN, SIMULATED, SKIPPED = "en attente", "en cours", "vert", "simulé", "non lancé"
STEP_STATUSES = {WAITING: TODO, RUNNING: IN_PROGRESS, GREEN: DONE, SIMULATED: DONE}  # tout autre état (échec, arrêté…) : KO
CHAIN_STATUSES = {"en cours": IN_PROGRESS, "terminée": DONE}  # « arrêtée… » : KO
MAX_STEPS = 20


@dataclass
class Step:
    dag_id: str
    status: str = WAITING
    watch_id: int | None = None  # suivi du run (Agents → Suivis de DAG) ; None en simulation
    message: str = ""  # pourquoi l'étape n'est pas verte

    @property
    def group(self) -> str:
        return STEP_STATUSES.get(self.status, KO)


@dataclass
class DagChain:
    id: int
    slug: str
    client: str
    padoa_env: str
    steps: list[Step]
    started_at: datetime
    status: str = "en cours"
    ended_at: datetime | None = None
    simulated: bool = False

    @property
    def group(self) -> str:
        return CHAIN_STATUSES.get(self.status, KO)

    @property
    def running(self) -> bool:
        return self.status == "en cours"


class ChainRunner:
    def __init__(self, state_file: Path, launch, notifications=None):
        """launch(dag_id, padoa_env, slug, card) → (ActionResult, suivi ou None) : le lancement habituel de Jarvis."""
        self.state_file = state_file
        self.launch = launch
        self.notifications = notifications
        self._lock = threading.RLock()
        self.chains: list[DagChain] = self._load()

    def _load(self) -> list[DagChain]:
        if not self.state_file.is_file():
            return []
        try:
            items = json.loads(self.state_file.read_text(encoding="utf-8"))
            return [DagChain(**{**i, "steps": [Step(**s) for s in i["steps"]], "started_at": datetime.fromisoformat(i["started_at"]),
                                "ended_at": datetime.fromisoformat(i["ended_at"]) if i.get("ended_at") else None}) for i in items]
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("chaines.json illisible : les enchaînements passés ne sont pas repris")
            return []

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(c) for c in self.chains], ensure_ascii=False, default=datetime.isoformat), encoding="utf-8")
        tmp.replace(self.state_file)

    def get(self, chain_id: int) -> DagChain | None:
        return next((c for c in self.chains if c.id == chain_id), None)

    def start(self, slug: str, client: str, padoa_env: str, dag_ids: list[str]) -> DagChain:
        with self._lock:
            chain = DagChain(max((c.id for c in self.chains), default=0) + 1, slug, client, padoa_env,
                             [Step(d) for d in dag_ids], datetime.now())
            self.chains.insert(0, chain)
            self._advance(chain)
        return chain

    def _advance(self, chain: DagChain) -> None:
        """Lance l'étape suivante ; en simulation (pas de suivi), enchaîne jusqu'au bout."""
        with self._lock:
            while chain.running:
                n, step = next(((n, s) for n, s in enumerate(chain.steps, 1) if s.status == WAITING), (0, None))
                if not step:
                    self._finish(chain, "terminée", "info", f"Les {len(chain.steps)} DAG sont passés, dans l'ordre.")
                    break
                result, watch = self.launch(step.dag_id, chain.padoa_env, chain.slug, f"Enchaînement #{chain.id}, étape {n}")
                if not result.ok:
                    self._stop_at(chain, n, step, "non lancé : refusé", result.message)
                elif watch:
                    step.status, step.watch_id = RUNNING, watch.id
                    break  # la suite viendra de watch_ended, à la fin du suivi
                else:
                    step.status, chain.simulated = SIMULATED, True
            self._save()

    def watch_ended(self, watch) -> None:
        """Appelé par le suivi d'un run à sa fin : vert → étape suivante ; sinon la chaîne s'arrête là."""
        with self._lock:
            for chain in self.chains:
                n, step = next(((n, s) for n, s in enumerate(chain.steps, 1) if s.status == RUNNING and s.watch_id == watch.id), (0, None))
                if not step or not chain.running:
                    continue
                if watch.status == GREEN:
                    step.status = GREEN
                    self._advance(chain)
                else:
                    self._stop_at(chain, n, step, watch.status, watch.summary)
                    self._save()
                return

    def reconcile(self, get_watch) -> None:
        """Au démarrage de Jarvis : une étape « en cours » dont le suivi est déjà fini (fin passée inaperçue) est rattrapée."""
        for chain in [c for c in self.chains if c.running]:
            step = next((s for s in chain.steps if s.status == RUNNING), None)
            watch = get_watch(step.watch_id) if step else None
            if step and (not watch or not watch.running):
                if watch:
                    self.watch_ended(watch)
                else:
                    with self._lock:
                        self._stop_at(chain, chain.steps.index(step) + 1, step, "suivi introuvable", "")
                        self._save()

    def stop(self, chain_id: int) -> bool:
        """Arrête la chaîne : le run en cours continue (et son suivi aussi), les étapes suivantes ne partent pas."""
        with self._lock:
            chain = self.get(chain_id)
            if not chain or not chain.running:
                return False
            self._skip_waiting(chain)
            chain.status, chain.ended_at = "arrêtée à la main", datetime.now()
            self._save()
            return True

    @staticmethod
    def _skip_waiting(chain: DagChain) -> None:
        for s in chain.steps:
            if s.status == WAITING:
                s.status = SKIPPED

    def _stop_at(self, chain: DagChain, n: int, step: Step, status: str, message: str) -> None:
        step.status, step.message = status, message
        self._skip_waiting(chain)
        left = sum(1 for s in chain.steps if s.status == SKIPPED)
        self._finish(chain, f"arrêtée à l'étape {n}", "urgent",
                     f"{step.dag_id} : {status}. {left} DAG suivant(s) non lancé(s)." + (f" {message}" if message else ""))

    def _finish(self, chain: DagChain, status: str, level: str, detail: str) -> None:
        chain.status, chain.ended_at = status, datetime.now()
        if self.notifications:
            self.notifications.add(f"chaine:{chain.id}:{chain.started_at.isoformat()}", "dag", level,
                                   f"Enchaînement #{chain.id} {status} · {chain.padoa_env}", detail[:400], "/agents#chaines", chain.client)
