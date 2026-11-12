"""Catalogue des DAGs d'injection (sync/dags/catalogue.json, généré par sync/dags/generer_catalogue.py
depuis la config de l'orchestrator) : quels DAGs peuvent injecter les ressources d'une carte."""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from deploy_hub.config import PROJECT_ROOT

CATALOGUE = PROJECT_ROOT / "sync" / "dags" / "catalogue.json"
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
    path = path or CATALOGUE
    return _load(str(path), path.stat().st_mtime) if path.is_file() else {"clients": {}}


def dags_for(customers: list[str], resources: list[str] | None = None, path: Path | None = None) -> list[DagChoice]:
    """DAGs des clients orchestrator donnés qui traitent au moins une des ressources de la carte.
    Sans liste de ressources (None) : tous leurs DAGs, pour la commande « >dag » de la palette."""
    choices = []
    for customer in customers:
        for dag_id, dag in ((catalogue(path)["clients"].get(customer) or {}).get("dags") or {}).items():
            hit = tuple(dag["ressources"]) if resources is None else tuple(r for r in dag["ressources"] if r in resources)
            if hit:
                choices.append(DagChoice(dag_id, customer, dag["type"], dag.get("groupe", ""), hit))
    return sorted(choices, key=lambda c: (TYPE_ORDER.get(c.type, 9), c.customer, c.dag_id))


def ced_for(customers: list[str], path: Path | None = None) -> list[dict]:
    """DAG CED de chaque client orchestrator : {customer, dag_id, ressources: {ressource: [{requete, niveau}]}}."""
    out = []
    for customer in customers:
        ced = (catalogue(path)["clients"].get(customer) or {}).get("ced")
        if ced:
            out.append({"customer": customer, **ced})
    return out


def known_dag(customers: list[str], dag_id: str, path: Path | None = None) -> bool:
    clients = catalogue(path)["clients"]
    return any(dag_id in (clients.get(c) or {}).get("dags", {}) or dag_id == ((clients.get(c) or {}).get("ced") or {}).get("dag_id")
               for c in customers)
