"""Sources de données et d'actions (Notion, Airflow, agents Claude, GitHub…)."""

from dataclasses import dataclass


@dataclass
class ActionResult:
    ok: bool
    message: str
    simulated: bool = False  # vrai seulement pour l'historique d'avant (plus de mode démo)
    run_id: str = ""  # run Airflow créé par un lancement réel : le suivi (dag_watch) s'y accroche
