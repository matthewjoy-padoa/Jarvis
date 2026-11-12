"""Synchro Notion → fiche de déploiement, incrémentale et au plus juste.

La routine « deploy-hub-sync » exécute avec le connecteur Notion les requêtes que ce module lui donne et dépose
chaque résultat brut dans data/notion/<slug>/. Le module tient un cache par base (cache-actions.json,
cache-recette.json + contenu-recette.json), en fait la fiche data/deploiements/<slug>.md lue par l'app.

    uv run python -m deploy_hub.sync_notion preparer    # requêtes de chaque client + empreinte de tous les clients
    uv run python -m deploy_hub.sync_notion plan        # d'après l'empreinte : les seules requêtes à exécuter
    uv run python -m deploy_hub.sync_notion fusionner   # fusionne les résultats, écrit les fiches

Ce que le plan demande, base par base (la routine recopie chaque résultat : moins de lignes, moins de temps) :
- rien si l'empreinte (nombre de lignes, dernière modification ; signature des règles et de la fiche) n'a pas bougé ;
- delta : les seules lignes modifiées depuis la dernière modification connue du cache (ajouts compris) ;
- ids : la liste compacte des identifiants (une ligne) quand des lignes ont disparu, pour les retirer du cache ;
- recette sans textes (recette-meta) quand une colonne de métadonnées est ajoutée : les textes restent ceux du cache ;
- complet : pas de cache, colonnes des actions modifiées, ou actions relues entièrement il y a plus de FULL_SYNC_DAYS
  jours (leurs ressources et types viennent des cartes Bible, dont les changements ne datent pas la carte).
"""

import json
from dataclasses import asdict
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from deploy_hub.config import PROJECT_ROOT, data_dir
from deploy_hub import queries
from deploy_hub.queries import ConfigError, page_id_of
from deploy_hub.models import ACTION_STATUSES, DONE, INJECTION_STATUSES, KO, RETOUR_STATUSES, status_group
from deploy_hub.sources.notifications import NotificationStore

# Colonnes admises pour la base « Retours Recette » : tout autre champ arrête la synchro.
# Métadonnées → cache-recette.json et fiche (versionnés) ; contenu → contenu-recette.json (exclu de git, reste sur la machine).
RECETTE_META = {"url", "statut", "type_donnees", "logiciel", "responsables", "deadline", "cree_le", "modifie"}
RECETTE_CONTENT = {"ligne", "type_retour", "commentaire_client", "exemple", "analyse", "solutions", "discussion", "echanges"}
RECETTE_COLUMNS = RECETTE_META | RECETTE_CONTENT
PAGE_SIZE = 100  # lignes par réponse Notion
FULL_SYNC_DAYS = 7
BASES = ("actions", "recette")
MILESTONES = ["kickoff", "reception_donnees", "recette_1", "recette_2", "recette_3", "freeze", "formation", "deploiement", "deploiement_lot2"]


class SyncError(Exception):
    pass


def _pages(path: Path) -> list[Path]:
    return [path] + sorted(path.parent.glob(f"{path.stem}-*.json"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))


def _rows(path: Path) -> list[dict]:
    """Lignes de <nom>.json, puis des pages suivantes <nom>-2.json, <nom>-3.json… (réponses Notion limitées à 100 lignes)."""
    if not path.is_file():
        raise SyncError(f"Résultat manquant : {path}")
    pages = _pages(path)
    rows, last = [], []
    for page in pages:
        data = json.loads(page.read_text(encoding="utf-8"))
        if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("type") == "text":
            data = json.loads(data[0]["text"])  # réponse d'outil copiée telle quelle depuis son fichier, enveloppée
        last = data["results"] if isinstance(data, dict) else data
        rows += last
    # Avec un « LIMIT » dans la requête, Notion répond has_more: false même s'il reste des lignes :
    # une dernière page pleine veut dire que la pagination s'est arrêtée trop tôt, y compris quand
    # seule la première page est là (la page suivante, même vide, doit être écrite).
    if len(last) >= PAGE_SIZE:
        raise SyncError(f"{path.stem} : la dernière page ({pages[-1].name}) est pleine, des lignes manquent sans doute.")
    return rows


def _date(value) -> date | None:
    return date.fromisoformat(str(value)[:10]) if value else None


def _page_id(url: str) -> str:
    """Id de 32 caractères d'une adresse Notion, ou "" (même analyse que la configuration des clients)."""
    try:
        return page_id_of(url)
    except ConfigError:
        return ""



def _yes(value) -> bool:
    return value in ("__YES__", True, 1, "1")


def _check_recette(rows: list[dict]) -> list[dict]:
    for row in rows:
        extra = set(row) - RECETTE_COLUMNS
        if extra:
            raise SyncError(f"Colonnes de recette non autorisées : {sorted(extra)}. Fiche non écrite.")
    return rows


def _load_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def _load_cache(raw: Path, base: str) -> dict | None:
    cache = _load_json(raw / f"cache-{base}.json", None)
    if cache is not None and base == "recette":  # recolle le contenu, rangé à part
        for url, content in _load_json(raw / "contenu-recette.json", {}).items():
            if url in cache:
                cache[url] = {**cache[url], **content}
    return cache


def write_caches(raw: Path, caches: dict[str, dict]) -> None:
    for base, cache in caches.items():
        if base == "recette":
            meta = {u: {k: v for k, v in r.items() if k in RECETTE_META} for u, r in cache.items()}
            content = {u: {k: r[k] for k in RECETTE_CONTENT if r.get(k)} for u, r in cache.items()}
            (raw / "cache-recette.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            (raw / "contenu-recette.json").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
        else:
            (raw / f"cache-{base}.json").write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def merge_base(raw: Path, base: str) -> tuple[dict[str, dict], str]:
    """Cache de la base après ce passage, et ce qui a été appliqué : complet, meta, delta, meta+delta ou cache."""
    cache = _load_cache(raw, base)
    full, meta, delta = raw / f"{base}.json", raw / f"{base}_meta.json", raw / f"{base}_delta.json"
    check = _check_recette if base == "recette" else (lambda rows: rows)
    if full.is_file():
        return {r["url"]: r for r in check(_rows(full))}, "complet"
    if cache is None:
        raise SyncError(f"{base} : ni résultat complet ni cache, lance une synchro complète.")
    how = []
    if meta.is_file():  # toutes les lignes sans les textes : les textes restent ceux du cache
        cache = {r["url"]: {**cache.get(r["url"], {}), **r} for r in check(_rows(meta))}
        how.append("meta")
    if delta.is_file():
        cache.update({r["url"]: r for r in check(_rows(delta))})
        how.append("delta")
    return cache, "+".join(how) or "cache"


def drop_missing(raw: Path, caches: dict[str, dict]) -> dict[str, int]:
    """Retire du cache les lignes absentes de ids.json (16 derniers caractères de l'id) ; nombre retiré par base."""
    if not (raw / "ids.json").is_file():
        return {}
    row = _rows(raw / "ids.json")[0]
    removed = {}
    for base, cache in caches.items():
        keep = set(filter(None, (row.get(base) or "").split(",")))
        gone = [u for u in cache if _page_id(u)[-16:] not in keep]
        for u in gone:
            del cache[u]
        removed[base] = len(gone)
    return removed


# Équipe = préfixe du type d'action (« INTE Injection » → INTE). Ordre d'affichage, puis libellé.
TEAMS = {"INTE": "INTE", "OPS": "OPS", "FORMATION": "Formation", "GESFIN": "Gesfin", "RM GESFIN": "Gesfin", "SRE": "SRE",
         "RUN": "RUN", "PRODUIT": "Produit", "STATS": "Stats", "BLC": "BLC", "CARE": "Care", "SPSTI": "SPSTI",
         "MARKETING": "Marketing", "COMPTA": "Compta", "EFEX": "EFEX"}
TEAM_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, TEAMS), key=len, reverse=True)) + r")\b", re.IGNORECASE)


def teams_of(types: str | None) -> list[str]:
    """Équipes d'une carte, d'après ses types d'action (carte et carte Bible), sans doublon, dans l'ordre de TEAMS."""
    found = {TEAMS[m.upper()] for m in TEAM_RE.findall(types or "")}
    order = list(dict.fromkeys(TEAMS.values()))
    return sorted(found, key=order.index)


def _action(a: dict) -> dict:
    return {
        "title": a.get("intitule") or "(sans titre)",
        "status": a.get("statut") or "À faire",
        "injection_status": a.get("statut_injection") or "A faire",
        "injection": a.get("injection") in (1, True, "1", "__YES__"),  # « INTE Injection » dans Actions à mener
        "resources": sorted({r.strip() for r in (a.get("ressources") or "").split(",") if r.strip()}),  # noms orchestrator
        "teams": teams_of(a.get("types_action")),
        "due": _date(a.get("deadline")),
        "vigilance": _yes(a.get("vigilance")),
        "event": _yes(a.get("evenement")),
        "notion_url": a.get("url", ""),
    }


def user_ids(value) -> list[str]:
    """Personnes d'une colonne Notion (« ["user://<uuid>", …] » en SQL, liste avec l'API) → uuids, dans l'ordre."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = value.split(",")
    return [str(v).removeprefix("user://").strip() for v in value or [] if str(v).strip()]


def users_path(root: Path = PROJECT_ROOT) -> Path:
    """Annuaire uuid Notion → nom des responsables de recette (hors git, comme tout data/)."""
    return data_dir(root) / "notion" / "utilisateurs.json"


def unknown_users(root: Path = PROJECT_ROOT) -> list[str]:
    """Responsables présents dans les caches de recette mais absents de l'annuaire : à faire traduire par la synchro."""
    known = _load_json(users_path(root), {})
    seen = {u for cache in (data_dir(root) / "notion").glob("*/cache-recette.json")
            for row in _load_json(cache, {}).values() for u in user_ids(row.get("responsables"))}
    return sorted(seen - set(known))


def add_users(names: dict[str, str], root: Path = PROJECT_ROOT) -> None:
    path = users_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    known = _load_json(path, {})
    known.update({k.removeprefix("user://"): v for k, v in names.items() if v})
    path.write_text(json.dumps(known, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def _retour(r: dict) -> dict:
    return {
        "id": _page_id(r["url"])[-6:].upper(),
        "status": r.get("statut") or "À faire",
        "data_type": r.get("type_donnees") or "",
        "software": r.get("logiciel") or "",
        "owners": user_ids(r.get("responsables")),
        "due": _date(r.get("deadline")),
        "created": _date(r.get("cree_le")),
        "notion_url": r["url"],
    }


def build_deployment(config: dict, client: dict, regles_rows: list[dict], caches: dict[str, dict]) -> dict:

    actions = [_action(a) for a in caches["actions"].values()]
    actions.sort(key=lambda a: (a["due"] is None, a["due"] or date.max, a["title"]))  # datées d'abord, les plus proches en tête
    retours = sorted((_retour(r) for r in caches["recette"].values()), key=lambda r: (r["created"] or date.min, r["id"]))
    counts: dict[str, int] = {}
    for a in actions:
        if a["injection"]:  # seules les cartes d'injection comptent dans le suivi des injections
            counts[a["injection_status"]] = counts.get(a["injection_status"], 0) + 1
    regles = [
        {
            "title": r.get("intitule") or "(sans titre)",
            "type": r.get("type") or "",
            "application": r.get("application") or "",
            "status": r.get("statut") or "",
        }
        for r in regles_rows
    ]
    return {
        "client": config["client"],
        "emoji": client.get("emoji") or "",
        "software": config.get("software", "?"),
        "status": config.get("status", "en cours"),
        "notion_url": config.get("notion_url", ""),
        "synced_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "milestones": {k: _date(client.get(k)) for k in MILESTONES if client.get(k)},
        "injection_counts": counts,
        "actions": actions,
        "retours": retours,
        "regles": regles,
    }


def _fiche_emoji(fiche: Path) -> str:
    """Emoji de la fiche déjà écrite ; "" si elle manque ou est illisible (décoratif : ne bloque jamais la synchro)."""
    from deploy_hub.sources.fiches import _split_frontmatter
    try:
        return str(_split_frontmatter(fiche.read_text(encoding="utf-8")).get("emoji") or "")
    except (OSError, ValueError, yaml.YAMLError):
        return ""


def write_fiche(deployment: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    front = yaml.safe_dump(deployment, allow_unicode=True, sort_keys=False)
    out.write_text(
        "---\n# Généré par deploy_hub.sync_notion depuis Notion : ne pas modifier à la main.\n"
        f"{front}---\n",
        encoding="utf-8",
    )


def update_blocklist(retours: list[dict], blocklist: Path) -> int:
    """Ajoute les pages de recette au fichier lu par le hook ; ne retire jamais une page."""
    known = set(blocklist.read_text().split()) if blocklist.is_file() else set()
    known |= {pid for r in retours if (pid := _page_id(r["notion_url"]))}
    blocklist.write_text("\n".join(sorted(known)) + "\n")
    return len(known)


RESULTS = ("client", "regles", "actions", "actions_delta", "recette", "recette_meta", "recette_delta", "ids")
EMPREINTES = "empreintes"  # data/notion/empreintes.sql et .json : tous les clients en une requête


def versions() -> dict[str, str]:
    """Version des colonnes demandées, par base : une colonne ajoutée impose de relire la base concernée."""
    return {"actions": queries.templates_version("actions", "actions-delta"),
            "recette": queries.templates_version("recette-meta"),
            "contenu": ",".join(sorted(RECETTE_CONTENT))}


def _stamp(value) -> datetime | None:
    """Date de dernière modification Notion, au format SQL (« 2026-11-10 16:40:18Z ») ou API (ISO)."""
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))


def _sql_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%SZ")


def _signature(value) -> tuple[int, str]:
    n, _, rest = str(value or "").partition("|")
    return int(n or 0), rest


def _state(raw: Path) -> dict:
    state = _load_json(raw / "etat-synchro.json", {})
    if state and not isinstance(state.get("colonnes"), dict):  # état d'avant le 2026-11-10 (sans version par base).
        # Seuls les modèles de la recette ont changé depuis (colonne « Responsable ») : actions et textes restent valables.
        state["colonnes"] = {"actions": versions()["actions"], "contenu": versions()["contenu"]}
    return state


def plan_client(slug: str, row: dict, root: Path = PROJECT_ROOT) -> dict:
    """Requêtes à exécuter pour un client d'après sa ligne d'empreinte : [(requête, sortie, params)] et raisons."""
    raw = data_dir(root) / "notion" / slug
    state, v = _state(raw), versions()
    cols, todo, why = state.get("colonnes") or {}, [], {}
    ids = bool(state.get("ids_requis"))
    for base in BASES:
        cache = _load_json(raw / f"cache-{base}.json", None)
        n, last = _signature(row.get(base))
        last_full = state.get("complet_le")
        if cache is None:
            reason = "pas de cache"
        elif base in (state.get("complet_requis") or []):
            reason = "lignes manquantes au passage précédent"
        elif base == "actions" and cols.get("actions") != v["actions"]:
            reason = "colonnes modifiées"
        elif base == "actions" and (not last_full or (date.today() - date.fromisoformat(last_full)).days >= FULL_SYNC_DAYS):
            reason = f"relecture complète périodique (dernière : {last_full or 'jamais'})"
        elif base == "recette" and cols.get("contenu") != v["contenu"]:
            reason = "colonnes de texte modifiées"
        else:
            reason = None
        if reason:
            todo.append((base, base, []))
            why[base] = f"complet : {reason}"
            continue
        if base == "recette" and cols.get("recette") != v["recette"]:
            todo.append(("recette-meta", "recette_meta", []))
            why[base] = "métadonnées relues (colonnes modifiées), textes gardés"
        known = [t for r in cache.values() if (t := _stamp(r.get("modifie")))]
        since = max(known) if known else None
        current = _stamp(last)
        if current and (since is None or current > since):
            todo.append((f"{base}-delta", f"{base}_delta", [_sql_time(since) if since else "1970-01-01 00:00:00Z"]))
            why[base] = (why.get(base, "") + " + " if base in why else "") + f"delta depuis {_sql_time(since) if since else 'le début'}"
        if n < len(cache):
            ids = True
            why[base] = (why.get(base, "") + " + " if base in why else "") + f"{len(cache) - n} ligne(s) disparue(s)"
    if ids:
        todo.append(("ids", "ids", []))
    signatures = state.get("signatures") or {}
    if todo or row.get("fiche") != signatures.get("fiche") or not (raw / "client.json").is_file():
        todo.insert(0, ("client", "client", []))
    if row.get("regles") != signatures.get("regles") or not (raw / "regles.json").is_file():
        todo.insert(1, ("regles", "regles", []))
        why["regles"] = "règles modifiées"
    if todo == [("client", "client", [])] and row.get("fiche") != signatures.get("fiche"):
        why["fiche"] = "dates de la fiche modifiées"
    return {"requetes": todo, "raisons": why}


def _clean(raw: Path, names) -> None:
    """Retire les résultats bruts (et leurs pages) avant un passage, ou une fois fusionnés."""
    for name in names:
        head = raw / f"{name}.json"
        for page in ([head] if head.is_file() else []) + sorted(raw.glob(f"{name}-*.json")):
            page.unlink()


def prepare(root: Path = PROJECT_ROOT) -> dict:
    """Écrit les requêtes de chaque client et l'empreinte de tous les clients (une seule requête pour la routine)."""
    from deploy_hub import client_config
    configs = [asdict(c) for c in client_config.all_configs(root)]
    for config in configs:
        queries.write(config["slug"], config, root)
    folder = data_dir(root) / "notion"
    folder.mkdir(parents=True, exist_ok=True)
    sql = folder / f"{EMPREINTES}.sql"
    sql.write_text(queries.empreintes(configs), encoding="utf-8")
    _clean(folder, [EMPREINTES])
    return _query(sql, folder / f"{EMPREINTES}.json", [])


def _query(sql: Path, out: Path, params: list) -> dict:
    return {"sql": str(sql), "sortie": str(out), "params": params,
            "sources": sorted(set(re.findall(r"collection://[0-9a-f-]+", sql.read_text(encoding="utf-8")))) if sql.is_file() else []}


def plan_all(root: Path = PROJECT_ROOT) -> dict:
    """Plan de tous les clients d'après data/notion/empreintes.json : requêtes à exécuter, clients inchangés."""
    from deploy_hub import client_config
    folder = data_dir(root) / "notion"
    rows = {r.get("cle"): r for r in _rows(folder / f"{EMPREINTES}.json")}
    out = {"requetes": [], "inchanges": [], "raisons": {}, "erreurs": {}}
    for slug in client_config.slugs(root):
        raw = folder / slug
        if slug not in rows:
            out["erreurs"][slug] = "absent de l'empreinte : relance « preparer »"
            continue
        if not rows[slug].get("fiche"):  # aucune page « Clients en déploiement » à cet id : rien ne sert de lire le reste
            out["erreurs"][slug] = "fiche Notion introuvable : vérifie le page_id du client (Clients → Configurer)"
            continue
        p = plan_client(slug, rows[slug], root)
        raw.mkdir(parents=True, exist_ok=True)
        _clean(raw, RESULTS[2:] + ("plan",))
        if not p["requetes"]:
            out["inchanges"].append(slug)
            continue
        _clean(raw, [name for _, name, _ in p["requetes"]])
        (raw / "plan.json").write_text(json.dumps({"sorties": [name for _, name, _ in p["requetes"]], "empreinte": rows[slug]},
                                                  ensure_ascii=False), encoding="utf-8")
        out["requetes"] += [{"client": slug, **_query(raw / "requetes" / f"{q}.sql", raw / f"{name}.json", params)}
                            for q, name, params in p["requetes"]]
        out["raisons"][slug] = p["raisons"]
    return out


def merge_all(root: Path = PROJECT_ROOT) -> dict:
    """Fusionne les clients qui ont un plan ; un client en échec n'empêche pas les autres."""
    from deploy_hub import client_config
    results = {}
    for slug in client_config.slugs(root):
        if not (data_dir(root) / "notion" / slug / "plan.json").is_file():
            continue
        try:
            results[slug] = sync(slug, root)
        except (SyncError, ConfigError, OSError, KeyError, ValueError) as e:
            results[slug] = {"erreur": str(e)}
    return results


def load_config(slug: str, root: Path = PROJECT_ROOT) -> dict:
    """Configuration du client, lue et normalisée par client_config (même lecture que l'app)."""
    from deploy_hub import client_config  # client_config → queries ; import ici pour garder sync_notion léger
    cfg = client_config.load(slug, root)
    if cfg is None:
        raise SyncError(f"Client inconnu : {slug} (pas de configuration dans config/clients/)")
    return asdict(cfg)


# Champs suivis par le journal des changements (« Quoi de neuf »), par base.
TRACKED = {
    "actions": ("statut", "statut_injection", "deadline", "vigilance"),
    "recette": ("statut", "deadline", "commentaire_client", "exemple", "analyse", "solutions", "discussion", "echanges"),
}
JOURNAL_DAYS = 60  # au-delà, les événements sont oubliés


def _norm(value):
    return None if value in (None, "", "__NO__", False, 0) else value


def diff_caches(before: dict[str, dict] | None, after: dict[str, dict], base: str, when: str) -> list[dict]:
    """Événements entre deux états d'une base : nouveau, supprimé, ou champ suivi modifié (avant → après).
    Aucun événement au premier passage (pas d'état précédent)."""
    if before is None:
        return []
    events = []
    for url, row in after.items():
        old = before.get(url)
        if old is None:
            events.append({"quand": when, "base": base, "url": url, "type": "nouveau", "statut": row.get("statut")})
            continue
        for field in TRACKED[base]:
            a, b = _norm(old.get(field)), _norm(row.get(field))
            if a != b:
                events.append({"quand": when, "base": base, "url": url, "type": "modifie", "champ": field, "avant": a, "apres": b})
    events += [{"quand": when, "base": base, "url": url, "type": "supprime"} for url in before.keys() - after.keys()]
    return events


def append_journal(raw: Path, events: list[dict]) -> None:
    """journal.json : événements des 60 derniers jours. Il contient du contenu de recette : exclu de git."""
    path = raw / "journal.json"
    journal = _load_json(path, [])
    cutoff = (datetime.now() - timedelta(days=JOURNAL_DAYS)).isoformat(timespec="seconds")
    journal = [e for e in journal if e["quand"] >= cutoff] + events
    path.write_text(json.dumps(journal, ensure_ascii=False), encoding="utf-8")


URGENT_DAYS = 2  # une deadline déplacée à moins de 2 jours est urgente


def _soon(value) -> bool:
    day = _date(value)
    return day is not None and day <= date.today() + timedelta(days=URGENT_DAYS)


def sync_notes(slug: str, client: str, events: list[dict], caches: dict[str, dict], when: str) -> list[dict]:
    """Notifications d'un passage de synchro : une par changement urgent, plus un résumé des autres changements.
    Urgent : nouveau retour ouvert, retour passé en KO, injection passée en KO, point de vigilance levé,
    deadline (retour ouvert ou carte non terminée) amenée à moins de URGENT_DAYS jours."""
    notes, others = [], 0
    for e in events:
        row = caches.get(e["base"], {}).get(e["url"], {})
        if e["base"] == "recette":
            label = f"Retour {_page_id(e['url'])[-6:].upper()}" + (f" · {row.get('type_donnees')}" if row.get("type_donnees") else "")
            link = f"/deploiements/{slug}/recette?tous=true#{_page_id(e['url'])[-6:].upper()}"
            is_open = status_group(row.get("statut") or "À faire", RETOUR_STATUSES) != DONE
        else:
            label = row.get("intitule") or "Carte"
            link = f"/deploiements/{slug}/{'injections' if row.get('injection') in (1, True, '1') else 'actions?tous=true'}"
            is_open = status_group(row.get("statut") or "À faire", ACTION_STATUSES) != DONE
        field, after = e.get("champ"), e.get("apres")
        reason = None
        if e["type"] == "nouveau" and e["base"] == "recette" and is_open:
            reason = "nouveau retour de recette"
        elif field == "statut" and e["base"] == "recette" and status_group(after or "", RETOUR_STATUSES) == KO:
            reason = f"retour passé en {after}"
        elif field == "statut_injection" and status_group(after or "", INJECTION_STATUSES) == KO:
            reason = f"injection passée en {after}"
        elif field == "vigilance" and after:
            reason = "point de vigilance levé"
        elif field == "deadline" and after and is_open and _soon(after):
            reason = f"deadline amenée au {_date(after).strftime('%d/%m')}"
        if reason:
            notes.append({"key": f"sync:{e['url']}:{field or e['type']}:{after}", "kind": "sync", "level": "urgent",
                          "title": f"{client} · {label}", "detail": reason, "link": link, "client": client})
        else:
            others += 1
    if others:
        notes.append({"key": f"sync:{slug}:{when}", "kind": "sync", "level": "info", "title": f"{client} · synchro",
                      "detail": f"{others} autre(s) changement(s) depuis la synchro précédente", "link": "/#whats-new", "client": client})
    return notes


def publish(slug: str, root: Path, config: dict, client: dict, regles_rows: list[dict], caches: dict[str, dict]) -> dict:
    """Écrit la fiche, les caches, le journal des changements et la liste des pages bloquées ;
    commun à la routine et à la synchro directe."""
    raw = data_dir(root) / "notion" / slug
    raw.mkdir(parents=True, exist_ok=True)
    _check_recette(list(caches["recette"].values()))
    when = datetime.now().isoformat(timespec="seconds")
    events = [e for base in BASES for e in diff_caches(_load_cache(raw, base), caches[base], base, when)]
    fiche = data_dir(root) / "deploiements" / f"{slug}.md"
    if "emoji" not in client:  # la routine SQL ne voit pas les icônes : on garde celui de la synchro directe
        client = {**client, "emoji": _fiche_emoji(fiche)}
    deployment = build_deployment(config, client, regles_rows, caches)
    blocked = update_blocklist(deployment["retours"], data_dir(root) / "recette-pages.txt")
    write_fiche(deployment, fiche)
    write_caches(raw, caches)
    append_journal(raw, events)
    NotificationStore(data_dir(root) / "notifications.json").add_many(sync_notes(slug, config["client"], events, caches, when))
    return {"deployment": deployment, "blocked": blocked, "changes": len(events)}


def summary(deployment: dict, blocked: int, mode, changes: int = 0) -> dict:
    return {
        "changements": changes,
        "client": deployment["client"],
        "mode": mode,
        "jalons": len(deployment["milestones"]),
        "cartes": len(deployment["actions"]),
        "retours": len(deployment["retours"]),
        "regles": len(deployment["regles"]),
        "pages_recette_bloquees": blocked,
    }


def sync(slug: str, root: Path = PROJECT_ROOT) -> dict:
    """Passage de la routine : fusionne les résultats SQL déposés dans data/notion/<slug>/ selon plan.json."""
    config = load_config(slug, root)
    raw = data_dir(root) / "notion" / slug
    planned = _load_json(raw / "plan.json", None)
    if planned:
        missing = [n for n in planned["sorties"] if not (raw / f"{n}.json").is_file()]
        if missing:
            raise SyncError(f"Résultat manquant : {', '.join(n + '.json' for n in missing)}")
    merged = {base: merge_base(raw, base) for base in BASES}
    caches = {base: cache for base, (cache, _) in merged.items()}
    removed = drop_missing(raw, caches)
    client = _rows(raw / "client.json")
    if len(client) != 1:
        raise SyncError(f"client.json : 1 ligne attendue, {len(client)} reçues (page_id du client à vérifier)")
    out = publish(slug, root, config, client[0], _rows(raw / "regles.json"), caches)

    state, v = _state(raw), versions()
    cols = state.setdefault("colonnes", {})
    for base, (_, how) in merged.items():
        if how == "complet":
            cols.update({"actions": v["actions"]} if base == "actions" else {"recette": v["recette"], "contenu": v["contenu"]})
            if base == "actions":
                state["complet_le"] = date.today().isoformat()
        elif "meta" in how:
            cols["recette"] = v["recette"]
    state.pop("ids_requis", None)
    state.pop("complet_requis", None)
    if planned:
        row = planned["empreinte"]
        state["signatures"] = {"regles": row.get("regles"), "fiche": row.get("fiche")}
        for base in BASES:  # le cache doit compter autant de lignes que Notion ; sinon on vérifie au prochain passage
            n, _ = _signature(row.get(base))
            if len(caches[base]) > n:
                state["ids_requis"] = True
            elif len(caches[base]) < n:
                state.setdefault("complet_requis", []).append(base)
    (raw / "etat-synchro.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    _clean(raw, RESULTS[2:] + ("plan",))
    result = summary(out["deployment"], out["blocked"], {base: how for base, (_, how) in merged.items()}, out["changes"])
    if any(removed.values()):
        result["retirees"] = removed
    return result


if __name__ == "__main__":
    # uv run python -m deploy_hub.sync_notion preparer        → écrit les requêtes ; affiche la requête d'empreinte à exécuter
    # uv run python -m deploy_hub.sync_notion plan            → d'après l'empreinte : les seules requêtes à exécuter
    # uv run python -m deploy_hub.sync_notion fusionner       → fusionne les résultats déposés, écrit les fiches
    # uv run python -m deploy_hub.sync_notion utilisateurs     → uuids des responsables sans nom dans l'annuaire
    # uv run python -m deploy_hub.sync_notion utilisateurs '{"<uuid>": "<nom>"}'  → ajoute ces noms à l'annuaire
    # uv run python -m deploy_hub.sync_notion clients | requetes <slug> | <slug>   → outils unitaires
    from deploy_hub import client_config
    show = lambda value: print(json.dumps(value, ensure_ascii=False))
    try:
        command = sys.argv[1]
        if command == "preparer":
            show(prepare())
        elif command == "plan":
            show(plan_all())
        elif command == "fusionner":
            results = merge_all()
            show(results)
            sys.exit(1 if any("erreur" in r for r in results.values()) else 0)
        elif command == "clients":
            print("\n".join(client_config.slugs()))
        elif command == "requetes":
            print(queries.write(sys.argv[2], load_config(sys.argv[2])))
        elif command == "utilisateurs":
            if len(sys.argv) > 2:
                add_users(json.loads(sys.argv[2]))
            show(unknown_users())
        else:
            show(sync(command))
    except (SyncError, ConfigError, OSError, KeyError, ValueError, IndexError) as e:
        print(f"ÉCHEC synchro : {e}", file=sys.stderr)
        sys.exit(1)
