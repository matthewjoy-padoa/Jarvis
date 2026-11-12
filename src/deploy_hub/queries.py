"""Requêtes SQL Notion d'un client, générées depuis les modèles communs de sync/requetes/ et sa configuration.

Les modèles ne diffèrent d'un client à l'autre que par :
- {{page_id}} : id de la fiche du client dans « Clients en déploiement » (clé page_id de sa config) ;
- {{ou_clients_recette}} : retours de recette ajoutés par leur champ « Client » (clé recette_clients, facultative) ;
- {{slug}} : identifiant du client dans Jarvis (empreinte.sql, assemblée pour tous les clients par `empreintes`).

`write(slug)` écrit les requêtes prêtes à l'emploi dans data/notion/<slug>/requetes/, où la routine de synchro les lit.
"""

import hashlib
import re
from functools import lru_cache
from pathlib import Path

from deploy_hub.config import PROJECT_ROOT, data_dir

TEMPLATES = PROJECT_ROOT / "sync" / "requetes"
# nom du fichier généré → rôle dans la synchro (voir docs/routine-synchro.md)
NAMES = ("client", "actions", "actions-delta", "recette", "recette-meta", "recette-delta", "regles", "ids", "empreinte")
# Un id Notion : 32 caractères hexadécimaux, avec ou sans tirets (format UUID), qui ne touche aucun autre caractère hexa.
PAGE_ID = re.compile(r"(?<![0-9a-f])([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})(?![0-9a-f])")


class ConfigError(ValueError):
    pass


def page_id_of(value: str) -> str:
    """Id de 32 caractères depuis un id (avec ou sans tirets) ou une adresse Notion (le dernier id de l'adresse)."""
    ids = [i.replace("-", "") for i in PAGE_ID.findall((value or "").lower())]
    if not ids:
        raise ConfigError("Id ou adresse de page Notion attendu (32 caractères hexadécimaux).")
    return ids[-1]


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def templates_version(*names: str) -> str:
    """Empreinte de modèles de requêtes (tous par défaut) : change dès qu'une colonne est ajoutée ou retirée."""
    text = "".join(_template(n) for n in names or NAMES)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


@lru_cache(maxsize=len(NAMES))
def _template(name: str) -> str:
    return (TEMPLATES / f"{name}.sql").read_text(encoding="utf-8")


def render(config: dict) -> dict[str, str]:
    page_id = page_id_of(str(config.get("page_id", "")))
    extra = [c for c in config.get("recette_clients") or [] if str(c).strip()]
    ou = f"\n   OR \"Client\" IN ({', '.join(_quote(c) for c in extra)})" if extra else ""
    return {
        name: _template(name).replace("{{page_id}}", page_id).replace("{{ou_clients_recette}}", ou)
              .replace("{{slug}}", str(config.get("slug", "")).replace("'", ""))
        for name in NAMES
    }


def empreintes(configs: list[dict]) -> str:
    """Empreinte de tous les clients en une seule requête (une ligne par client) : la routine ne fait qu'un appel
    pour savoir quels clients et quelles bases ont changé."""
    return "\nUNION ALL\n".join(render(c)["empreinte"].strip() for c in configs)


def queries_dir(slug: str, root: Path = PROJECT_ROOT) -> Path:
    return data_dir(root) / "notion" / slug / "requetes"


def write(slug: str, config: dict, root: Path = PROJECT_ROOT) -> Path:
    out = queries_dir(slug, root)
    out.mkdir(parents=True, exist_ok=True)
    for name, sql in render(config).items():
        (out / f"{name}.sql").write_text(sql, encoding="utf-8")
    return out
