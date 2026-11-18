"""Catalogue des DAGs d'injection (généré par sync/dags/generer_catalogue.py depuis la config de l'orchestrator) :
quels DAGs peuvent injecter les ressources d'une carte. Celui de la branche locale choisie (data/dags/catalogue.json,
publié par sources/dag_source.py) ; à défaut, le catalogue versionné sync/dags/catalogue.json (copie figée de main)."""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from deploy_hub.config import PROJECT_ROOT, data_dir

CATALOGUE = PROJECT_ROOT / "sync" / "dags" / "catalogue.json"
LOCAL = data_dir() / "dags" / "catalogue.json"  # catalogue de la branche locale choisie, s'il a été généré
# Ordre de proposition : DAG de la ressource seule d'abord, puis DAG du groupe qui la contient.
TYPE_ORDER = {"etl": 0, "transform_and_load": 1, "etl_group": 2, "transform_and_load-group": 3}
TYPE_LABELS = {
    "etl": "ETL ressource",
    "transform_and_load": "Transform & load ressource",
    "etl_group": "ETL groupe",
    "transform_and_load-group": "Transform & load groupe",
}


@dataclass(frozen=True)
class DagChoice:
    dag_id: str
    customer: str
    type: str
    groupe: str
    resources: tuple[str, ...]  # ressources de la carte traitées par ce DAG

    @property
    def label(self) -> str:
        what = f"groupe {self.groupe}" if self.groupe else ", ".join(self.resources)
        return f"{TYPE_LABELS.get(self.type, self.type)} · {what} ({self.customer})"


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def catalogue(path: Path | None = None) -> dict:
    """Le catalogue demandé, sinon celui de la branche locale, sinon le versionné (relu seulement s'il a changé)."""
    for candidate in [path] if path else [LOCAL, CATALOGUE]:
        try:
            return _load(str(candidate), candidate.stat().st_mtime)
        except FileNotFoundError:
            continue
    return {"clients": {}}


def dags_for(customers: list[str], resources: list[str] | None = None, path: Path | None = None) -> list[DagChoice]:
    """DAGs des clients orchestrator donnés qui traitent au moins une des ressources de la carte.
    Sans liste de ressources (None) : tous leurs DAGs, pour la commande « >dag » de la palette."""
    choices, clients = [], catalogue(path)["clients"]
    for customer in customers:
        for dag_id, dag in ((clients.get(customer) or {}).get("dags") or {}).items():
            hit = tuple(dag["ressources"]) if resources is None else tuple(r for r in dag["ressources"] if r in resources)
            if hit:
                choices.append(DagChoice(dag_id, customer, dag["type"], dag.get("groupe", ""), hit))
    return sorted(choices, key=lambda c: (TYPE_ORDER.get(c.type, 9), c.customer, c.dag_id))


def ced_for(customers: list[str], path: Path | None = None) -> list[dict]:
    """DAG CED de chaque client orchestrator : {customer, dag_id, ressources: {ressource: [{requete, niveau}]}}."""
    out, clients = [], catalogue(path)["clients"]
    for customer in customers:
        ced = (clients.get(customer) or {}).get("ced")
        if ced:
            out.append({"customer": customer, **ced})
    return out


def known_dag(customers: list[str], dag_id: str, path: Path | None = None) -> bool:
    clients = catalogue(path)["clients"]
    return any(dag_id in (clients.get(c) or {}).get("dags", {}) or dag_id == ((clients.get(c) or {}).get("ced") or {}).get("dag_id")
               for c in customers)
