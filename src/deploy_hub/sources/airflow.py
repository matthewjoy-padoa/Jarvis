"""Airflow de l'orchestrator (par défaut celui de la VM), via l'API REST v1.

Lance un DAG avec le paramètre `padoa_env` (stack cible : integration, snap ou recette ; prod désactivée, contrôlé par
`handle_padoa_env_coherence` dans dags/dag_params/handle_functions.py de l'orchestrator) et lit l'état des runs.
Ne lit jamais les logs des DAGs (règle de l'équipe).

Réel dès que AIRFLOW_BASE_URL, AIRFLOW_USER et AIRFLOW_PASSWORD sont renseignés (environnement ou .env de l'app) ;
sinon simulé.
"""

import os
from dataclasses import dataclass
from datetime import datetime

import httpx

from deploy_hub.sources import ActionResult

# Stacks proposées. La prod existe côté orchestrator mais est désactivée ici (décision du 2026-11-05) :
# pour la réactiver, l'ajouter à cette liste (la confirmation « PROD » à taper est déjà en place).
PADOA_ENVS = ("integration", "snap", "recette")
CONFIRM_PROD = "PROD"  # à taper pour lancer sur la prod, si elle est réactivée


@dataclass
class DagRun:
    dag_id: str
    padoa_env: str
    client: str
    card: str
    started_at: datetime
    result: str
    simulated: bool


class AirflowSource:
    def __init__(self, base_url: str, user: str = "", password: str = "", transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.live = bool(base_url and user and password)
        self.runs: list[DagRun] = []
        self.http = httpx.Client(base_url=f"{self.base_url}/api/v1", auth=(user, password), timeout=20, transport=transport) if self.live else None

    @classmethod
    def from_env(cls) -> "AirflowSource":
        return cls(os.getenv("AIRFLOW_BASE_URL", ""), os.getenv("AIRFLOW_USER", ""), os.getenv("AIRFLOW_PASSWORD", ""))

    def _log(self, dag_id, padoa_env, client, card, result: ActionResult) -> ActionResult:
        self.runs.insert(0, DagRun(dag_id, padoa_env, client, card, datetime.now(), result.message, result.simulated))
        return result

    def trigger_dag(self, dag_id: str, padoa_env: str = "integration", client: str = "", card: str = "") -> ActionResult:
        if padoa_env == "prod" and "prod" not in PADOA_ENVS:
            return ActionResult(False, "Lancement en prod désactivé dans Jarvis.", simulated=False)
        if padoa_env not in PADOA_ENVS:
            return ActionResult(False, f"Environnement inconnu : {padoa_env}", simulated=False)
        if not self.live:
            return self._log(dag_id, padoa_env, client, card, ActionResult(
                True, f"Simulation : {dag_id} sur {padoa_env} (Airflow non configuré : AIRFLOW_BASE_URL, AIRFLOW_USER, AIRFLOW_PASSWORD)"))
        try:
            dag = self.http.get(f"/dags/{dag_id}")
            if dag.status_code in (401, 403):
                return ActionResult(False, f"Airflow refuse l'accès ({dag.status_code}) : vérifie AIRFLOW_USER / AIRFLOW_PASSWORD.", simulated=False)
            if dag.status_code == 404:
                return ActionResult(False, f"{dag_id} absent de cet Airflow (client inactif ou erreur d'import ?)", simulated=False)
            dag.raise_for_status()
            r = self.http.post(f"/dags/{dag_id}/dagRuns", json={"conf": {"padoa_env": padoa_env}})
            if r.status_code in (401, 403):
                return ActionResult(False, f"Airflow refuse l'accès ({r.status_code}) : vérifie AIRFLOW_USER / AIRFLOW_PASSWORD.", simulated=False)
            r.raise_for_status()
        except httpx.HTTPError as e:
            return ActionResult(False, f"Airflow injoignable ou en erreur : {e}", simulated=False)
        run = r.json()
        paused = " (attention : le DAG est en pause dans Airflow, le run attendra)" if dag.json().get("is_paused") else ""
        return self._log(dag_id, padoa_env, client, card, ActionResult(
            True, f"{dag_id} lancé sur {padoa_env} (run {run.get('dag_run_id')}, état {run.get('state')}){paused}", simulated=False))

    def last_run_state(self, dag_id: str) -> str:
        if not self.live:
            return "—"
        try:
            r = self.http.get(f"/dags/{dag_id}/dagRuns", params={"order_by": "-execution_date", "limit": 1})
            r.raise_for_status()
            runs = r.json().get("dag_runs") or []
            return runs[0]["state"] if runs else "jamais lancé"
        except httpx.HTTPError:
            return "inconnu"
