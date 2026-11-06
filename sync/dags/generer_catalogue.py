"""Catalogue des DAGs d'injection de l'orchestrator : pour chaque client, les DAGs, les ressources qu'ils traitent,
et le contenu de son DAG CED (requêtes de fichiers par ressource).

Lit la config du dépôt integration-orchestrator (dags/config_deployment.py, groupes de ressources des réglages client,
requêtes CED du composant d'extraction) et écrit sync/dags/catalogue.json, lu par l'app. Airflow et les connecteurs
(Azure, SFTP, bases…) sont remplacés par des modules vides : le script ne fait que lire des constantes.

    cd ~/Documents/Jarvis
    AIRFLOW__CORE__DAGS_FOLDER=<dépôt orchestrator> uv run --no-project --python 3.12 \\
        --with 'numpy<2' --with 'pandas<2.2' --with pendulum --with pydantic --with pyyaml --with python-dateutil \\
        --with requests --with phonenumbers --with schwifty --with beautifulsoup4 --with striprtf \\
        python sync/dags/generer_catalogue.py <dépôt orchestrator>

Formats d'identifiant (dags/*.py et dags/dag_builder/dependencies/*.py de l'orchestrator) :
  {client}-etl-{ressource}, {client}-transform_and_load-{ressource},
  {client}-etl_group-{groupe}, {client}-transform_and_load-group-{groupe}, {client}-ced (dags/dag_ced.py:42)
"""

import importlib.abc
import importlib.machinery
import json
import subprocess
import sys
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

OUT = Path(__file__).with_name("catalogue.json")
# Modules remplacés par des modules vides : Airflow et les connecteurs, inutiles pour lire la config.
STUBS = {
    "airflow", "azure", "paramiko", "pysftp", "snowflake", "sqlalchemy", "psycopg2", "pymysql", "pyodbc", "pymssql",
    "slack_sdk", "googleapiclient", "google", "boto3", "smbclient", "requests_toolbelt", "openpyxl", "xlrd", "pyarrow",
    "cx_Oracle", "oracledb", "fdb", "jaydebeapi", "pyzipper", "pgpy", "gnupg", "sshtunnel", "msal", "office365", "docx",
    "PyPDF2", "pypdf", "fitz", "elasticsearch", "unidecode",
}


class _Stub(types.ModuleType):
    __path__: list = []

    def __getattr__(self, name):
        if name == "__version__":
            return "2.10.0"
        if name.startswith("__"):
            raise AttributeError(name)
        return MagicMock


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path, target=None):
        return importlib.machinery.ModuleSpec(name, self, is_package=True) if name.split(".")[0] in STUBS else None

    def create_module(self, spec):
        return _Stub(spec.name)

    def exec_module(self, module):
        pass


def _ced(customer, config, repo: Path) -> dict | None:
    from src.dag_builder_reference.dag_reference_extract import get_extract_component

    component = get_extract_component(customer, config["software"], version=config.get("software_version"))()
    queries = component.ced_queries or {}
    if not queries:
        return None

    def origin(q):
        path = Path(str(getattr(q, "query_path", "") or ""))
        try:
            path = path.relative_to(repo)
        except ValueError:
            pass
        return "client" if "/customers/" in str(path) else "logiciel"

    return {
        "dag_id": f"{customer}-ced",
        "ressources": {str(r): [{"requete": str(q.name), "niveau": origin(q)} for q in qs] for r, qs in queries.items()},
    }


def main(repo: Path) -> dict:
    sys.meta_path.insert(0, _StubFinder())
    sys.path.insert(0, str(repo))
    from dags.config_deployment import CONFIG_DEPLOYMENT
    from src.lib.constants.dag_name import DagName
    from src.settings.settings_by_customer import SETTINGS_BY_CUSTOMER
    from src.shared.constants.settings import SETTING_NAMES

    clients = {}
    for customer, config in CONFIG_DEPLOYMENT.items():
        active = config.get("active_dags") or {}
        groups = (SETTINGS_BY_CUSTOMER.get(customer) or {}).get(SETTING_NAMES.RESOURCE_GROUPS) or {}
        dags = {}
        for kind in (DagName.ETL, DagName.TRANSFORM_AND_LOAD):
            for resource in active.get(kind) or []:
                dags[f"{customer}-{kind}-{resource}"] = {"type": str(kind), "ressources": [str(resource)]}
        for kind in (DagName.ETL_GROUP, DagName.TRANSFORM_AND_LOAD_GROUP):
            for group in active.get(kind) or []:
                dags[f"{customer}-{kind}-{group}"] = {
                    "type": str(kind),
                    "groupe": str(group),
                    "ressources": [str(r) for r in groups.get(group, ())],
                }
        entry = {"logiciel": str(config.get("software_version") or config.get("software")), "dags": dags}
        if active.get(DagName.CED) is True:
            try:
                entry["ced"] = _ced(customer, config, repo)
            except Exception as e:  # un client mal configuré ne bloque pas le catalogue
                entry["ced_erreur"] = f"{type(e).__name__}: {e}"[:300]
        if dags or entry.get("ced"):
            clients[str(customer)] = entry

    commit = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%h %cs"], capture_output=True, text=True).stdout.strip()
    return {"genere_le": datetime.now().strftime("%Y-%m-%d %H:%M"), "commit_orchestrator": commit, "clients": clients}


if __name__ == "__main__":
    catalogue = main(Path(sys.argv[1]).resolve())
    OUT.write_text(json.dumps(catalogue, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    n = sum(len(c["dags"]) for c in catalogue["clients"].values())
    ced = sum(1 for c in catalogue["clients"].values() if c.get("ced"))
    print(f"{len(catalogue['clients'])} clients, {n} DAGs, {ced} DAGs CED → {OUT} (orchestrator {catalogue['commit_orchestrator']})")
