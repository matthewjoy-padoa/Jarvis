"""État des connexions au lancement de Jarvis : Airflow et Notion sont-ils vraiment joignables avec les identifiants donnés,
et Claude Code est-il toujours connecté ?

Une requête de lecture chacun (Airflow : GET /dags?limit=1 ; Notion : GET /users/me ; Claude Code : « claude auth status »),
délai court ; rien n'est lancé ni modifié, aucun secret ni identité n'est affiché. Le résultat s'écrit dans le terminal qui
lance Jarvis.
"""

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from deploy_hub.sources.airflow import AirflowSource
from deploy_hub.sources.notion_api import API as NOTION_API, VERSION as NOTION_VERSION

TIMEOUT = 5


def airflow_status(airflow: AirflowSource, problem: str = "") -> tuple[bool, str]:
    if not airflow.live:
        if problem:
            return False, f"non configuré : référence 1Password illisible ({problem}) : connecte 1Password dans Réglages"
        return False, "non configuré : AIRFLOW_BASE_URL, AIRFLOW_USER ou AIRFLOW_PASSWORD à renseigner dans Réglages"
    try:
        r = airflow.http.get("/dags", params={"limit": 1}, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        return False, f"injoignable sur {airflow.base_url} ({type(e).__name__})"
    if r.status_code in (401, 403):
        return False, f"identifiants refusés par {airflow.base_url} ({r.status_code}) : vérifie AIRFLOW_USER / AIRFLOW_PASSWORD"
    if r.status_code != 200:
        return False, f"réponse inattendue de {airflow.base_url} ({r.status_code})"
    return True, f"connecté à {airflow.base_url}"


def notion_status(token: str | None, problem: str = "", transport: httpx.BaseTransport | None = None) -> tuple[bool, str]:
    if not token:
        if problem:
            return False, f"jeton illisible : référence 1Password ({problem})"
        return False, "pas de NOTION_TOKEN : bouton « Rafraîchir depuis Notion » inactif (la routine de synchro reste possible)"
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport) as http:
            r = http.get(f"{NOTION_API}/users/me", headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION})
    except httpx.HTTPError as e:
        return False, f"injoignable ({type(e).__name__})"
    if r.status_code == 401:
        return False, "jeton refusé par Notion (401) : vérifie NOTION_TOKEN"
    if r.status_code != 200:
        return False, f"réponse inattendue de Notion ({r.status_code})"
    name = (r.json().get("name") or "").strip()
    return True, f"connecté (intégration « {name} »)" if name else "connecté"


CLAUDE_TIMEOUT = 15  # secondes : « claude auth status » répond en moins d'une seconde
EXPIRY_WARNING_DAYS = 3  # connexion qui expire d'ici là : on prévient avant que agents et synchro ne s'arrêtent
LOGIN = "lance « claude auth login » sur cette machine"


@dataclass(frozen=True)
class ClaudeLogin:
    """Connexion de Claude Code sur cette machine. ok : les agents et la synchro Notion sans jeton peuvent tourner ;
    alert : à signaler (déconnecté, ou connexion qui expire bientôt) ; word : l'état en un mot, pour la page Sources."""
    ok: bool
    message: str
    alert: bool = False
    word: str = "connecté"

    @property
    def line(self) -> str:
        return f"Jarvis · Claude Code : {'✗' if not self.ok else '⚠' if self.alert else '✓'} {self.message}"


def claude_status(claude_bin: str = "claude", now: datetime | None = None) -> ClaudeLogin:
    """Demande son état à Claude Code (« claude auth status --json » : loggedIn, loginExpiresAt). Un état illisible (claude
    absent, version sans cette commande) n'est pas une alerte : on ne sait pas, on le dit dans le terminal seulement."""
    if not shutil.which(claude_bin):
        return ClaudeLogin(False, f"introuvable ({claude_bin}) : installe Claude Code ou renseigne CLAUDE_BIN dans Réglages", word="absent")
    try:
        out = subprocess.run([claude_bin, "auth", "status", "--json"], capture_output=True, text=True, timeout=CLAUDE_TIMEOUT).stdout
        status = json.loads(out)
        logged_in = status["loggedIn"]
        expires = datetime.fromisoformat(str(status["loginExpiresAt"]).replace("Z", "+00:00")) if status.get("loginExpiresAt") else None
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)  # sans fuseau : UTC, comme le reste de la réponse
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as e:
        return ClaudeLogin(False, f"état de connexion illisible ({type(e).__name__}) : vérifie avec « claude auth status »", word="inconnu")
    now = now or datetime.now(timezone.utc)
    if not logged_in or (expires and expires <= now):
        return ClaudeLogin(False, f"déconnecté : {LOGIN} (agents, synchro Notion sans jeton et annuaire des clients à l'arrêt)",
                           alert=True, word="déconnecté")
    if expires and expires - now <= timedelta(days=EXPIRY_WARNING_DAYS):
        return ClaudeLogin(True, f"connecté, mais la connexion expire le {expires.astimezone():%d/%m à %H:%M} : {LOGIN} avant", alert=True)
    return ClaudeLogin(True, f"connecté (jusqu'au {expires.astimezone():%d/%m/%Y})" if expires else "connecté")


def lines(airflow: tuple[bool, str], notion: tuple[bool, str]) -> list[str]:
    return [f"Jarvis · {label} : {'✓' if ok else '✗'} {message}" for label, (ok, message) in (("Airflow", airflow), ("Notion", notion))]
