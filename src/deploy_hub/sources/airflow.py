"""Airflow de l'orchestrator (par défaut celui de la VM), via l'API REST v1.

Lancement d'un DAG : par air.py (le script du skill /air, deploy_hub/air.py), comme /air :
- aperçu (`air.py run DAG STACK`) : DAG, stack, ressources, avertissements (run déjà en cours…), montré dans la confirmation ;
- lancement (`air.py run DAG STACK --yes`) : refuse un DAG qui n'est plus chargé par Airflow, sort de pause un DAG en pause
  (sinon le run attendrait), puis crée le run avec le paramètre `padoa_env`.
Stacks : integration, snap ou recette ; prod désactivée (contrôlé aussi par `handle_padoa_env_coherence` dans
dags/dag_params/handle_functions.py de l'orchestrator). Ce module lit aussi l'état des runs (httpx, sans air.py).
Ni ce module ni air.py ne lisent de fichier de logs : le suivi (dag_watch.py → air.py) lit ceux de ses tâches par l'API REST.

Actif dès que AIRFLOW_BASE_URL, AIRFLOW_USER et AIRFLOW_PASSWORD sont renseignés (environnement ou .env de l'app) ;
sinon aucun DAG n'est lancé et le message dit quoi régler.
"""

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime

import httpx

from deploy_hub.config import child_environment, data_dir, secret
from deploy_hub.sources import ActionResult

# Stacks proposées. La prod existe côté orchestrator mais est désactivée ici (décision du 2026-11-05) :
# pour la réactiver, l'ajouter à cette liste (la confirmation « PROD » à taper est déjà en place).
PADOA_ENVS = ("integration", "snap", "recette")
CONFIRM_PROD = "PROD"  # à taper pour lancer sur la prod, si elle est réactivée
# `air.py run --yes` fait au plus 5 appels (DAG, runs en cours, tâches, sortie de pause, création du run) de 60 s chacun :
# ne jamais le couper avant, sinon un run créé passerait pour un échec (pas de suivi, et un second clic le doublerait).
AIR_TIMEOUT_S = 5 * 60 + 30
PREVIEW_TIMEOUT_S = 60  # aperçu : lecture seule, le couper ne laisse rien à moitié fait
AIR_COMMAND = [sys.executable, "-m", "deploy_hub.air"]  # air.py, pour le lancement et pour le suivi (dag_watch.py)
LAUNCHED = re.compile(r"^Lancé : run_id (\S+)", re.MULTILINE)  # ligne imprimée par `air.py run --yes`
PREVIEW_END = "(aperçu"  # dernière ligne de l'aperçu (« relancer avec --yes ») : sans intérêt dans Jarvis


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
        self.user, self.password = user, password  # transmis à air.py par l'environnement, jamais affichés
        self.runs: list[DagRun] = []
        self.http = httpx.Client(base_url=f"{self.base_url}/api/v1", auth=(user, password), timeout=20, transport=transport) if self.live else None

    @classmethod
    def from_env(cls) -> "AirflowSource":
        return cls(os.getenv("AIRFLOW_BASE_URL", ""), secret("AIRFLOW_USER"), secret("AIRFLOW_PASSWORD"))  # en clair ou op://…

    def air_env(self) -> dict[str, str]:
        """Réglages Airflow donnés à air.py (lancement et suivi) ; vide sans Airflow configuré."""
        return {"AIRFLOW_URL": self.base_url, "AIRFLOW_USER": self.user, "AIRFLOW_PASSWORD": self.password} if self.live else {}

    def _log(self, dag_id, padoa_env, client, card, result: ActionResult) -> ActionResult:
        self.runs.insert(0, DagRun(dag_id, padoa_env, client, card, datetime.now(), result.message, result.simulated))
        return result

    def _refused(self, padoa_env: str) -> ActionResult | None:
        """Ce qui empêche tout appel à Airflow (stack ou réglages) ; None si rien."""
        if padoa_env == "prod" and "prod" not in PADOA_ENVS:
            return ActionResult(False, "Lancement en prod désactivé dans Jarvis.", simulated=False)
        if padoa_env not in PADOA_ENVS:
            return ActionResult(False, f"Environnement inconnu : {padoa_env}", simulated=False)
        if not self.live:
            return ActionResult(False, "Airflow non configuré : renseigne AIRFLOW_BASE_URL, AIRFLOW_USER et AIRFLOW_PASSWORD dans Réglages.",
                                simulated=False)
        return None

    def _air_run(self, dag_id: str, padoa_env: str, launch: bool, prod_authorized: bool = False) -> tuple[bool, str]:
        """`air.py run` (aperçu, ou lancement avec --yes) : (réussi, sortie). Identifiants par l'environnement, jamais en argument.
        prod_authorized : la personne a tapé PROD (vérifié par la route) ; sans lui, air.py refuse la prod."""
        flags = (["--yes"] + (["--prod-authorized"] if prod_authorized else [])) if launch else []
        args = [*AIR_COMMAND, "run", *flags, "--", dag_id, padoa_env]  # « -- » : un dag_id en « -… » n'est pas une option
        env = {**child_environment(), **self.air_env(), "AIR_STATE_DIR": str(data_dir() / "airflow"), "AIR_NOTIFY": "print"}
        timeout = AIR_TIMEOUT_S if launch else PREVIEW_TIMEOUT_S
        try:
            out = subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return False, f"Airflow ne répond pas ({self.base_url}) après {timeout} s."
        except OSError as e:
            return False, f"air.py n'a pas pu être lancé : {e}"
        if out.returncode:
            return False, self._air_error(dag_id, out.stderr.strip() or out.stdout.strip())
        return True, out.stdout

    def _air_error(self, dag_id: str, text: str) -> str:
        """Message d'échec d'air.py, dit avec les mots de Jarvis (réglages dans Réglages, pas dans .claude/air.env)."""
        if denied := re.match(r"(401)\b", text) or re.search(r"HTTP (40[13]) (GET|POST)", text):
            return f"Airflow refuse l'accès ({denied.group(1)}) : vérifie AIRFLOW_USER / AIRFLOW_PASSWORD dans Réglages."
        if re.search(r"HTTP 40[13] PATCH", text):
            return (f"{dag_id} est en pause et Airflow refuse de l'en sortir : il faut le droit de modifier les DAGs, "
                    "ou le sortir de pause dans l'interface.")
        if f"HTTP 404 GET /api/v1/dags/{dag_id}" in text:
            return f"{dag_id} absent de cet Airflow (client inactif ou erreur d'import ?)"
        if text.startswith("🔌"):
            return f"Airflow injoignable ({self.base_url}) : vérifie le tunnel, les VPN et qu'Airflow tourne."
        lines = [line for line in text.splitlines() if line.strip()]
        return lines[-1] if lines else "air.py a échoué sans message."

    def preview(self, dag_id: str, padoa_env: str) -> ActionResult:
        """Aperçu d'air.py avant la confirmation : lignes DAG, Stack, Ressources et avertissements. Ne lance rien."""
        if refused := self._refused(padoa_env):
            return refused
        ok, text = self._air_run(dag_id, padoa_env, launch=False)
        if ok:
            text = "\n".join(line for line in text.splitlines() if line.strip() and not line.startswith(PREVIEW_END))
        return ActionResult(ok, text, simulated=False)

    def trigger_dag(self, dag_id: str, padoa_env: str = "integration", client: str = "", card: str = "",
                    prod_authorized: bool = False) -> ActionResult:
        if refused := self._refused(padoa_env):
            return refused
        ok, text = self._air_run(dag_id, padoa_env, launch=True, prod_authorized=prod_authorized)
        if not ok:
            return ActionResult(False, text, simulated=False)
        run = LAUNCHED.search(text)
        if not run:
            return ActionResult(False, f"air.py n'a pas donné de run_id : {text.strip()[-300:]}", simulated=False)
        warnings = "".join(f" · {line.strip()}" for line in text.splitlines() if line.startswith("⚠️"))
        return self._log(dag_id, padoa_env, client, card, ActionResult(
            True, f"{dag_id} lancé sur {padoa_env} (run {run.group(1)}){warnings}", simulated=False, run_id=run.group(1)))

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
