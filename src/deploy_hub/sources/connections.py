"""État des connexions au lancement de Jarvis : Airflow et Notion sont-ils vraiment joignables avec les identifiants donnés ?

Une requête de lecture chacun (Airflow : GET /dags?limit=1 ; Notion : GET /users/me), délai court ; rien n'est lancé
ni modifié, aucun secret n'est affiché. Le résultat s'écrit dans le terminal qui lance Jarvis.
"""

import httpx

from deploy_hub.sources.airflow import AirflowSource
from deploy_hub.sources.notion_api import API as NOTION_API, VERSION as NOTION_VERSION

TIMEOUT = 5


def airflow_status(airflow: AirflowSource, problem: str = "") -> tuple[bool, str]:
    if not airflow.live:
        if problem:
            return False, f"simulé : référence 1Password illisible ({problem})"
        return False, "simulé : AIRFLOW_BASE_URL, AIRFLOW_USER ou AIRFLOW_PASSWORD manquant dans .env"
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


def lines(airflow: tuple[bool, str], notion: tuple[bool, str]) -> list[str]:
    return [f"Jarvis · {label} : {'✓' if ok else '✗'} {message}" for label, (ok, message) in (("Airflow", airflow), ("Notion", notion))]
