"""Lecture directe de Notion par l'app (API REST, jeton NOTION_TOKEN), sans passer par Claude.

Même périmètre que les requêtes SQL de sync/requetes/ : pour chaque base, la liste des colonnes est fixée ici,
et l'API ne renvoie que ces propriétés (paramètre filter_properties). Pour « Retours Recette » :
- métadonnées et contenu utile à l'analyse (commentaire, exemple, analyse, solutions…) ; le contenu est rangé
  par sync_notion dans contenu-recette.json, exclu de git ;
- le corps des pages n'est jamais lu (aucun appel à /blocks) ; l'adresse est reconstruite depuis l'id ;
- un statut hors liste ou un type de plus de 40 caractères devient « Autre », comme dans recette-*.sql.
"""

import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import httpx

from deploy_hub.config import PROJECT_ROOT, data_dir
from deploy_hub.queries import ConfigError, page_id_of
from deploy_hub.sync_notion import (RECETTE_COLUMNS, RECETTE_CONTENT, SyncError, add_users, load_config, publish,
                                    summary, unknown_users, versions)

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"

ICON_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}  # pas de SVG (script possible)
ICON_MAX = 512 * 1024
CLIENTS_DS = "28d3f790-e4ca-404c-84de-8ea4087bce14"
ACTIONS_DS = "5055cf48-4d55-4606-8c83-92ae3dfaa8a2"
RECETTE_DS = "9e3440bc-941b-4c76-8b65-6fc15bbb02a7"
REGLES_DS = "3194976c-dfac-80e7-a241-000bf833be2c"
RESOURCES_DS = "3274976c-dfac-808c-bb6c-000bc601f8c4"  # « Resources » : nom orchestrator de chaque ressource

# alias → nom de la propriété Notion (identiques aux colonnes des requêtes SQL)
CLIENT_PROPERTIES = {
    "nom": "Nom",
    "kickoff": "Date de Kick-off",
    "reception_donnees": "Date réception des données",
    "recette_1": "Date recette 1",
    "recette_2": "Date recette 2",
    "recette_3": "Date recette 3",
    "freeze": "Date du Freeze",
    "formation": "Date formation",
    "deploiement": "Date déploiement",
    "deploiement_lot2": "Date déploiement Lot 2",
}
ACTIONS_PROPERTIES = {
    "intitule": "Intitulé",
    "statut": "Statut",
    "statut_injection": "Statut intégration - Injections",
    "deadline": "Deadline",
    "vigilance": "Point de vigilance",
    "evenement": "Événement",
    "actions_manuel": "Actions à mener (manuel)",
    "actions_bible": "Actions à mener - Bible",
    "ressources_manuel": "Ressources liées - Manuel",
    "ressources_bible": "Ressources liées - Bible",
}
INJECTION_ACTION = "INTE Injection"  # type d'action qui fait d'une carte une injection
RECETTE_PROPERTIES = {
    "statut": "Statut",
    "type_donnees": "Type de données",
    "logiciel": "Logiciel",
    "responsables": "Responsable",
    "deadline": "Deadline",
    "ligne": "Ligne du cahier de recette",
    "type_retour": "Type de retour",
    "commentaire_client": "Commentaire client (NE PAS MODIFIER)",
    "exemple": "Exemple Salarié / adhérents (NE PAS MODIFIER)",
    "analyse": "Analyse padoa",
    "solutions": "Solutions possibles",
    "discussion": "Discussion INTE/OPS",
    "echanges": "Échanges / Suivi Déploiement-Inté",
}
REGLES_PROPERTIES = {
    "intitule": "Intitulé de la règle",
    "type": "Type de règle",
    "application": "Application de la règle",
    "statut": "Statut de la règle",
}
# Statuts de recette recopiés tels quels ; tout autre texte devient « Autre » (même liste que recette-*.sql, vérifié par un test).
RECETTE_STATUTS = (
    "À faire", "INTE - Prio déploiement", "INTE - En cours", "INTE - Post déploiement", "INTE - En attente donnée client",
    "INTE - Fixed : à réinjecter", "INTE - En attente d'injection", "INTE - Vérifié en INTEGRATION", "INTE - Ancien retour - OK",
    "OPS - À vérifier avec le client", "OPS - En attente décision client", "OPS - À faire directement en front Padoa",
    "OPS - Validation à faire en INTEGRATION", "OPS - Validation à faire en SNAP", "OPS - Vérifié en INTEGRATION",
    "OPS - Vérifié en SNAP", "OPS - Vérifié et pas OK", "OPS - Paramétrage Admin Panel", "PRODUIT - Demande amélioration",
    "Non vérifié", "Non vérifié par le client", "Non vérifié par le client sans motif", "A revoir", "Action identifiée",
    "Validé", "Validé avec commentaire", "terminé", "Abandonné", "Doublon", "Non concerné",
)
TYPE_MAX = 40


def load_token(root: Path = PROJECT_ROOT) -> str | None:
    """NOTION_TOKEN de l'environnement, sinon du fichier .env de l'app (ignoré par git). Jamais affiché ni journalisé."""
    if os.getenv("NOTION_TOKEN"):
        return os.environ["NOTION_TOKEN"]
    env = root / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "NOTION_TOKEN" and value.strip():
                return value.strip().strip('"').strip("'")
    return None


def _uuid(page_id: str) -> str:
    h = page_id.replace("-", "")
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def page_url(page_id: str) -> str:
    """Adresse d'une page à partir de son seul id, au format des requêtes SQL (sans le titre ; clé des caches).
    L'affichage la rend ouvrable avec models.notion_link."""
    return f"https://app.notion.com/{page_id.replace('-', '')}"


def _text(parts: list) -> str:
    return "".join(p.get("plain_text", "") for p in parts or [])


def value(prop: dict):
    """Valeur simple d'une propriété Notion (texte, date ISO, booléen) ; None si vide ou non gérée."""
    kind = prop.get("type")
    v = prop.get(kind)
    if kind in ("title", "rich_text"):
        return _text(v) or None
    if kind in ("select", "status"):
        return v["name"] if v else None
    if kind == "multi_select":
        return ", ".join(o["name"] for o in v) or None
    if kind == "date":
        return v["start"] if v else None
    if kind == "checkbox":
        return bool(v)
    if kind in ("number", "url", "email", "phone_number", "created_time", "last_edited_time"):
        return v
    if kind == "formula":
        return v.get(v.get("type")) if v else None
    if kind == "rollup":
        if v and v.get("type") == "array":
            return ", ".join(str(value(x)) for x in v["array"] if value(x) is not None) or None
        return v.get(v.get("type")) if v else None
    if kind == "people":  # même forme que la synchro SQL ; les noms viennent de data/notion/utilisateurs.json
        return json.dumps([f"user://{u['id']}" for u in v or []]) if v else None
    if kind == "relation":  # ids des pages liées
        return ",".join(r["id"].replace("-", "") for r in v or []) or None
    return None  # files… : non lus


class NotionClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None, pause: float = 0.35):
        self.http = httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": VERSION},
            timeout=30,
            transport=transport,
        )
        self.transport = transport  # aussi pour télécharger les icônes (tests : faux Notion)
        self.pause = pause  # l'API accepte environ 3 requêtes par seconde
        self._schemas: dict[str, dict] = {}

    def _call(self, method: str, path: str, **kwargs) -> dict:
        for attempt in range(4):
            r = self.http.request(method, path, **kwargs)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("Retry-After", 1 + attempt)) if self.pause else 0)
                continue
            if r.status_code == 401:
                raise SyncError("Notion refuse le jeton (401) : vérifie NOTION_TOKEN.")
            if r.status_code in (403, 404):
                raise SyncError(f"Notion : accès refusé ou base introuvable ({r.status_code}) sur {path}. La base est-elle partagée avec l'intégration ?")
            r.raise_for_status()
            return r.json()
        raise SyncError(f"Notion indisponible sur {path} après 4 essais.")

    def name_users(self, root: Path = PROJECT_ROOT) -> None:
        """Noms des responsables encore inconnus de l'annuaire (GET /v1/users/<id>). Sans la capacité « informations
        utilisateur » de l'intégration, Notion refuse : ils restent « Inconnu », sans bloquer la synchro."""
        names = {}
        for user_id in unknown_users(root):
            try:
                names[user_id] = self._call("GET", f"/v1/users/{user_id}").get("name") or ""
            except SyncError as e:
                if "(404)" in str(e):  # compte supprimé : on passe au suivant
                    continue
                break  # 403 : l'intégration n'a pas le droit de lire les utilisateurs
        if names:
            add_users(names, root)

    def schema(self, data_source: str) -> dict[str, dict]:
        """Propriétés d'une base (nom → {id, type}) : structure seulement, aucune ligne."""
        if data_source not in self._schemas:
            props = self._call("GET", f"/data_sources/{data_source}")["properties"]
            self._schemas[data_source] = {name: {"id": p["id"], "type": p["type"]} for name, p in props.items()}
        return self._schemas[data_source]

    def _ids(self, data_source: str, properties: dict[str, str]) -> list[str]:
        schema = self.schema(data_source)
        missing = [name for name in properties.values() if name not in schema]
        if missing:
            raise SyncError(f"Propriétés absentes de la base {data_source} : {missing}")
        return [schema[name]["id"] for name in properties.values()]

    def filter_on(self, data_source: str, name: str, wanted: str) -> dict:
        """Filtre « la propriété vaut / contient wanted », selon son type dans le schéma."""
        kind = self.schema(data_source)[name]["type"]
        if kind == "relation":
            return {"property": name, "relation": {"contains": _uuid(wanted)}}
        if kind in ("select", "status"):
            return {"property": name, kind: {"equals": wanted}}
        if kind == "multi_select":
            return {"property": name, "multi_select": {"contains": wanted}}
        if kind in ("rich_text", "title"):
            return {"property": name, kind: {"equals": wanted}}
        raise SyncError(f"Filtre non géré pour {name} ({kind})")

    def query(self, data_source: str, properties: dict[str, str], filter: dict | None) -> list[dict]:
        """Toutes les pages qui passent le filtre, réduites à {alias: valeur} plus url, cree_le, modifie."""
        ids = self._ids(data_source, properties)
        by_id = {self.schema(data_source)[name]["id"]: alias for alias, name in properties.items()}
        params = [("filter_properties[]", i) for i in ids]
        rows, cursor = [], None
        while True:
            body = {"page_size": 100, **({"filter": filter} if filter else {}), **({"start_cursor": cursor} if cursor else {})}
            data = self._call("POST", f"/data_sources/{data_source}/query", params=params, json=body)
            for page in data["results"]:
                row = {"url": page_url(page["id"]), "cree_le": page.get("created_time"), "modifie": page.get("last_edited_time")}
                for prop in page.get("properties", {}).values():
                    if prop.get("id") in by_id:  # tout autre champ renvoyé est ignoré
                        row[by_id[prop["id"]]] = value(prop)
                rows.append(row)
            if not data.get("has_more"):
                return rows
            cursor = data["next_cursor"]
            if self.pause:
                time.sleep(self.pause)

    def page(self, page_id: str, data_source: str, properties: dict[str, str]) -> dict:
        ids = self._ids(data_source, properties)
        by_id = {self.schema(data_source)[name]["id"]: alias for alias, name in properties.items()}
        data = self._call("GET", f"/pages/{_uuid(page_id)}", params=[("filter_properties[]", i) for i in ids])
        row = {"url": page_url(data["id"])}
        for prop in data.get("properties", {}).values():
            if prop.get("id") in by_id:
                row[by_id[prop["id"]]] = value(prop)
        return row

    def page_icon(self, url: str, folder: Path) -> dict:
        """Icône d'une page (ici le dashboard d'intégration du client), seule chose lue de la page :
        {"emoji": "🦖", "icone": ""} ou, pour une image, {"emoji": "", "icone": "icone.png"} téléchargée dans folder.
        Rien (deux "") sans adresse, sans icône ou si la page n'est pas partagée avec l'intégration."""
        try:
            icon = self._call("GET", f"/pages/{_uuid(page_id_of(url))}", params=[("filter_properties[]", "title")]).get("icon") or {}
        except (ConfigError, SyncError):
            icon = {}
        kind = icon.get("type")
        if kind in ("file", "external", "custom_emoji"):  # image : Notion la signe pour une heure, on la garde sur disque
            name = self._download_icon((icon.get(kind) or {}).get("url") or "", folder)
            return {"emoji": "", "icone": name or _kept_icon(folder)}  # téléchargement raté : l'ancienne image reste
        for old in folder.glob("icone.*"):
            old.unlink()
        return {"emoji": icon.get("emoji", "") if kind == "emoji" else "", "icone": ""}

    def _download_icon(self, src: str, folder: Path) -> str:
        """Image d'icône (PNG, JPEG, GIF, WebP ; 512 Ko au plus) écrite en folder/icone.<ext> ; "" si refusée.
        Client HTTP à part : le jeton Notion ne part jamais vers l'hébergeur de l'image."""
        if not src.startswith("https://"):
            return ""
        try:
            with httpx.Client(timeout=10, follow_redirects=True, transport=self.transport) as http:
                r = http.get(src)
        except httpx.HTTPError:
            return ""
        ext = ICON_TYPES.get(r.headers.get("content-type", "").split(";")[0].strip())
        if r.status_code != 200 or not ext or len(r.content) > ICON_MAX:
            return ""
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("icone.*"):
            old.unlink()
        (folder / f"icone{ext}").write_bytes(r.content)
        return f"icone{ext}"


def _kept_icon(folder: Path) -> str:
    return next((p.name for p in sorted(folder.glob("icone.*"))), "")


def recette_row(row: dict) -> dict:
    """Ligne de recette réduite aux colonnes autorisées, statut et type ramenés à « Autre » si hors liste."""
    statut = row.get("statut")
    type_donnees = row.get("type_donnees")
    out = {
        "url": row["url"],
        "statut": statut if statut in RECETTE_STATUTS else ("Autre" if statut else None),
        "type_donnees": type_donnees if not type_donnees or len(type_donnees) <= TYPE_MAX else "Autre",
        "logiciel": row.get("logiciel"),
        "responsables": row.get("responsables"),
        "deadline": row.get("deadline"),
        "cree_le": row.get("cree_le"),
        "modifie": row.get("modifie"),
        **{k: row.get(k) for k in RECETTE_CONTENT},
    }
    if not set(out) <= RECETTE_COLUMNS:
        raise SyncError("Colonne de recette non autorisée")
    return out


def fetch(config: dict, notion: NotionClient) -> dict:
    """Lit dans Notion tout ce dont la fiche du client a besoin."""
    page_id = config["page_id"]
    client = notion.page(page_id, CLIENTS_DS, CLIENT_PROPERTIES)
    actions = notion.query(ACTIONS_DS, ACTIONS_PROPERTIES, notion.filter_on(ACTIONS_DS, "Client", page_id))
    names = {r["url"].rsplit("/", 1)[-1]: r["nom"] for r in notion.query(RESOURCES_DS, {"nom": "Integration name"}, None) if r.get("nom")}
    for a in actions:  # mêmes règles que les colonnes « injection » et « ressources » de actions-*.sql
        a["types_action"] = f"{a.pop('actions_manuel', None) or ''} {a.pop('actions_bible', None) or ''}".strip()
        a["injection"] = int(INJECTION_ACTION in a["types_action"])
        linked = a.pop("ressources_manuel", None) or a.pop("ressources_bible", None) or ""
        a.pop("ressources_bible", None)
        a["ressources"] = ",".join(names[i] for i in linked.split(",") if i in names) or None
    recette_filter = [notion.filter_on(RECETTE_DS, "🏕️ Clients en déploiement", page_id)]
    recette_filter += [notion.filter_on(RECETTE_DS, "Client", c) for c in config.get("recette_clients") or []]
    recette = notion.query(RECETTE_DS, RECETTE_PROPERTIES, {"or": recette_filter} if len(recette_filter) > 1 else recette_filter[0])
    regles = notion.query(REGLES_DS, REGLES_PROPERTIES, notion.filter_on(REGLES_DS, "Clients padoa ayant la règle", page_id))
    return {
        "client": client,
        "actions": actions,
        "recette": [recette_row(r) for r in recette],
        "regles": regles,
    }


def refresh(slug: str, notion: NotionClient, root: Path = PROJECT_ROOT) -> dict:
    """Synchro complète d'un client depuis Notion ; la fiche n'est réécrite que si toutes les lectures ont réussi."""
    config = load_config(slug, root)
    if not config.get("page_id"):
        raise SyncError(f"{slug} : page_id absent de la configuration du client")
    started = time.monotonic()
    data = fetch(config, notion)
    data["client"].update(notion.page_icon(config.get("notion_url") or "", data_dir(root) / "notion" / slug))
    caches = {base: {r["url"]: r for r in data[base]} for base in ("actions", "recette")}
    out = publish(slug, root, config, data["client"], data["regles"], caches)
    notion.name_users(root)
    state_path = data_dir(root) / "notion" / slug / "etat-synchro.json"
    state_path.write_text(json.dumps({"complet_le": date.today().isoformat(), "source": "api", "colonnes": versions()}), encoding="utf-8")
    result = summary(out["deployment"], out["blocked"], "api", out["changes"])
    result["secondes"] = round(time.monotonic() - started, 1)
    return result


def clients(root: Path = PROJECT_ROOT) -> list[str]:
    from deploy_hub.client_config import slugs
    return slugs(root)


if __name__ == "__main__":
    # uv run python -m deploy_hub.sources.notion_api [slug…]   (sans slug : tous les clients configurés)
    token = load_token()
    if not token:
        print("ÉCHEC : NOTION_TOKEN absent (environnement ou .env de l'app).", file=sys.stderr)
        sys.exit(1)
    notion = NotionClient(token)
    failed = False
    for slug in sys.argv[1:] or clients():
        try:
            print(json.dumps(refresh(slug, notion), ensure_ascii=False))
        except (SyncError, httpx.HTTPError, KeyError, ValueError) as e:
            print(f"ÉCHEC {slug} : {e}", file=sys.stderr)
            failed = True
    sys.exit(1 if failed else 0)
