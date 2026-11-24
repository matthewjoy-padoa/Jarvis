"""Synchro Notion → fiche de déploiement, incrémentale et au plus juste.

La routine « deploy-hub-sync » exécute avec le connecteur Notion les requêtes que ce module lui donne et dépose
chaque résultat brut dans data/notion/<slug>/. Le module tient un cache par base (cache-actions.json,
cache-recette.json + contenu-recette.json), en fait la fiche data/deploiements/<slug>.md lue par l'app.

    uv run python -m deploy_hub.sync_notion preparer    # ouvre un passage : la requête d'empreinte à exécuter
    uv run python -m deploy_hub.sync_notion suite       # récolte les réponses, puis : requêtes suivantes, ou fusion et fin

« suite » lit les réponses de Notion dans la session Claude Code qui les a reçues (son transcript) : le modèle n'a ni à
relire les requêtes, ni à recopier ou déposer les réponses, et une requête qu'il aurait modifiée est refusée. Les
commandes d'avant (plan, deposer, fusionner) restent : « suite » les enchaîne, et « deposer » sert de secours.

Ce que le plan demande, base par base (la routine recopie chaque résultat : moins de lignes, moins de temps) :
- rien si l'empreinte (nombre de lignes, dernière modification ; signature des règles et de la fiche) n'a pas bougé ;
- delta : les seules lignes modifiées depuis la dernière modification connue du cache (ajouts compris) ;
- ids : la liste compacte des identifiants (une ligne) quand des lignes ont disparu, pour les retirer du cache ;
- recette sans textes (recette-meta) quand une colonne de métadonnées est ajoutée : les textes restent ceux du cache ;
- complet : pas de cache, colonnes des actions modifiées, ou actions relues entièrement il y a plus de FULL_SYNC_DAYS
  jours (leurs ressources et types viennent des cartes Bible, dont les changements ne datent pas la carte).
"""

import json
import os
from dataclasses import asdict
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from deploy_hub.safe_write import write_json, write_text
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
MAX_PAGES = 100  # plafond de la routine par requête (sync/routine.md) : 10 000 lignes, contre une pagination sans fin
FULL_SYNC_DAYS = 7
BASES = ("actions", "recette")
MILESTONES = ["kickoff", "reception_donnees", "recette_1", "recette_2", "recette_3", "freeze", "formation", "deploiement", "deploiement_lot2"]


class SyncError(Exception):
    pass


def _pages(path: Path) -> list[Path]:
    return [path] + sorted(path.parent.glob(f"{path.stem}-*.json"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))


def _unwrapped(data):
    """Réponse d'outil copiée telle quelle depuis son fichier, avec son enveloppe [{"type": "text", "text": "<JSON>"}]."""
    if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("type") == "text":
        return json.loads(data[0]["text"])
    return data


def _rows(path: Path) -> list[dict]:
    """Lignes de <nom>.json, puis des pages suivantes <nom>-2.json, <nom>-3.json… (réponses Notion limitées à 100 lignes)."""
    if not path.is_file():
        raise SyncError(f"Résultat manquant : {path}")
    pages = _pages(path)
    rows, last = [], []
    for page in pages:
        data = _unwrapped(json.loads(page.read_text(encoding="utf-8")))
        last = data["results"] if isinstance(data, dict) else data
        rows += last
    # Avec un « LIMIT » dans la requête, Notion répond has_more: false même s'il reste des lignes :
    # une dernière page pleine veut dire que la pagination s'est arrêtée trop tôt, y compris quand
    # seule la première page est là (la page suivante, même vide, doit être écrite).
    if len(last) >= PAGE_SIZE:
        if len(pages) >= MAX_PAGES:
            raise SyncError(f"{path.stem} : plus de {MAX_PAGES * PAGE_SIZE} lignes, plafond de {MAX_PAGES} pages atteint ; "
                            "données incomplètes, rien n'est fusionné pour ce client.")
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
            write_json(raw / "cache-recette.json", meta)
            write_json(raw / "contenu-recette.json", content)
        else:
            write_json(raw / f"cache-{base}.json", cache)


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
    """Responsables présents dans les caches de recette (et responsables et référents des demandes BAU) mais absents de
    l'annuaire : à faire traduire par la synchro."""
    from deploy_hub import bau
    known = _load_json(users_path(root), {})
    seen = {u for cache in (data_dir(root) / "notion").glob("*/cache-recette.json")
            for row in _load_json(cache, {}).values() for u in user_ids(row.get("responsables"))}
    return sorted((seen | bau.cached_user_ids(root)) - set(known))


def add_users(names: dict[str, str], root: Path = PROJECT_ROOT) -> None:
    path = users_path(root)
    known = _load_json(path, {})
    known.update({k.removeprefix("user://"): v for k, v in names.items() if v})
    write_json(path, known, indent=1, sort_keys=True)


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
        "icone": client.get("icone") or "",  # image de l'icône, dans data/notion/<slug>/
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


ICON_FILE = "icone.json"  # data/notion/<slug>/icone.json : icône du client lue par l'annuaire (sans jeton Notion)


def icon_fields(data) -> dict:
    """{"emoji", "icone"} d'une fiche ou d'un icone.json (vides s'ils manquent)."""
    data = data if isinstance(data, dict) else {}
    return {k: str(data.get(k) or "") for k in ("emoji", "icone")}


def saved_icon(folder: Path) -> dict:
    """Emoji et image du client lus par l'annuaire (data/notion/<slug>/icone.json) ; vides s'il n'y en a pas."""
    try:
        return icon_fields(json.loads((folder / ICON_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return icon_fields({})


def _fiche_icon(fiche: Path) -> dict:
    """Emoji et image de la fiche déjà écrite ; vides si elle manque ou est illisible (décoratif : ne bloque jamais la synchro)."""
    from deploy_hub.sources.fiches import _split_frontmatter
    try:
        data = _split_frontmatter(fiche.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        data = {}
    return icon_fields(data)


def write_fiche(deployment: dict, out: Path) -> None:
    front = yaml.safe_dump(deployment, allow_unicode=True, sort_keys=False)
    write_text(out, f"---\n# Généré par deploy_hub.sync_notion depuis Notion : ne pas modifier à la main.\n{front}---\n")


def update_blocklist(retours: list[dict], blocklist: Path) -> int:
    """Ajoute les pages de recette au fichier lu par le hook ; ne retire jamais une page."""
    known = set(blocklist.read_text().split()) if blocklist.is_file() else set()
    known |= {pid for r in retours if (pid := _page_id(r["notion_url"]))}
    write_text(blocklist, "\n".join(sorted(known)) + "\n")
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


def running_elsewhere(root: Path = PROJECT_ROOT) -> dict | None:
    """Synchro lancée par Jarvis qui tourne encore dans un autre processus (data/notion/routine.json : session et pid de
    claude vivant), ou None. Sa propre session (JARVIS_SYNC_SESSION) ne compte pas."""
    state = _load_json(data_dir(root) / "notion" / "routine.json", {})
    pid, session = state.get("pid"), state.get("session")
    if not state.get("running") or not pid or session == os.environ.get(SESSION_ENV):
        return None
    try:
        os.kill(int(pid), 0)  # signal 0 : le processus existe-t-il encore ?
    except (OSError, ValueError):
        return None
    return state


PASSAGE = "passage.json"  # data/notion/passage.json : le passage en cours (début, clients, étape, requêtes attendues)
CLIENT_ENV = "JARVIS_SYNC_CLIENT"  # synchro d'un seul client (bouton de sa fiche) : son slug ; vide : tous les clients


def _passage(root: Path) -> Path:
    return data_dir(root) / "notion" / PASSAGE


def _followed(root: Path, slugs: list[str] | None = None) -> list[str]:
    """Clients suivis (non archivés) ; slugs : seulement ceux-là (les clients d'un passage)."""
    from deploy_hub import client_config
    active = client_config.active_slugs(root)
    return active if slugs is None else [slug for slug in active if slug in slugs]


def prepare(root: Path = PROJECT_ROOT, only: str | None = None) -> dict:
    """Ouvre un passage : écrit les requêtes de chaque client et l'empreinte (une seule requête, une ligne par client).
    only (ou JARVIS_SYNC_CLIENT) : ce client seulement, ou ceux-là (slugs séparés par des virgules). Refuse si une autre synchro lancée par Jarvis tourne : deux
    passages écriraient les mêmes fichiers (vu le 2026-11-18).
    Les demandes BAU (réglage JARVIS_NOTION_BAU, voir bau.py) sont du passage quand tout est synchronisé, ou quand
    « only » les nomme (bau.KEY) : elles ont leur propre requête d'empreinte, donnée avec celle des clients."""
    if (other := running_elsewhere(root)):
        raise SyncError(f"Une autre synchro tourne déjà (lancée à {other.get('started', '?')}) : attends sa fin, puis relance")
    from deploy_hub import bau, client_config
    configs = [asdict(c) for c in client_config.active_configs(root)]  # un client archivé n'est plus synchronisé
    with_bau = bau.enabled()
    if (only := os.environ.get(CLIENT_ENV, "") if only is None else only):
        wanted = [slug.strip() for slug in only.split(",")]  # un client (sa fiche) ou plusieurs (synchro automatique)
        configs = [c for c in configs if c["slug"] in wanted]
        if bau.KEY in wanted and not with_bau:
            raise SyncError(bau.problem() or f"{bau.SETTING} est vide : aucune page BAU à synchroniser")
        with_bau = with_bau and bau.KEY in wanted
        if not configs and not with_bau:
            raise SyncError(f"{only} : client inconnu ou archivé, rien à synchroniser")
    elif not configs and not with_bau:
        raise SyncError("Aucun client suivi : ajoute un client avant de synchroniser")
    for config in configs:
        queries.write(config["slug"], config, root)
    folder = data_dir(root) / "notion"
    folder.mkdir(parents=True, exist_ok=True)
    _clean(folder, [EMPREINTES])
    for old in folder.glob(f"{EMPREINTES}*.sql"):
        old.unlink()
    # Une requête d'empreinte par paquet de clients : chacune doit tenir dans ce qu'une commande peut rendre au modèle
    # (BATCH_CHARS). Les réponses vont dans empreintes.json, empreintes-2.json… que _rows lit comme les pages d'un résultat.
    packs: list[list[dict]] = []
    for config in configs:
        if not packs or len(queries.empreintes(packs[-1] + [config])) > BATCH_CHARS // 2:
            packs.append([])
        packs[-1].append(config)
    asked = []
    for n, pack in enumerate(packs, start=1):
        name = EMPREINTES if n == 1 else f"{EMPREINTES}-{n}"
        (folder / f"{name}.sql").write_text(queries.empreintes(pack), encoding="utf-8")
        asked.append(_query(folder / f"{name}.sql", folder / f"{name}.json", []))
    if with_bau:
        asked.append(bau.empreinte(root))
    write_json(_passage(root), {"debut": datetime.now(timezone.utc).isoformat(timespec="seconds"), "etape": "empreinte",
                                "clients": [c["slug"] for c in configs], "bau": with_bau, "demandes": 0,
                                "attendues": [_expected(None, query) for query in asked]})
    return asked[0]


def _query(sql: Path, out: Path, params: list) -> dict:
    return {"sql": str(sql), "sortie": str(out), "params": params,
            "sources": sorted(set(re.findall(r"collection://[0-9a-f-]+", sql.read_text(encoding="utf-8")))) if sql.is_file() else []}


CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"  # où Claude Code range les réponses trop grosses (tool-results/)
SESSION_ENV = "JARVIS_SYNC_SESSION"  # session de la routine lancée par Jarvis (claude -p --session-id) : ses fichiers seulement
SAVED_NOTION = "mcp-*Notion*-notion-query-data-sources-*.txt"


def saved_responses(session: str = "", name: str = SAVED_NOTION, projects: Path | None = None) -> list[Path]:
    """Réponses d'outils que Claude Code a rangées dans des fichiers (grosses réponses), de la plus ancienne à la plus récente :
    celles d'une session si on la connaît, sinon de toutes. Seul endroit qui connaît leur emplacement dans ~/.claude."""
    pattern = f"*/{session or '*'}/tool-results/{name}"
    return sorted((projects or CLAUDE_PROJECTS).glob(pattern), key=lambda p: p.stat().st_mtime)


def saved_response(session: str = "", within: timedelta = timedelta(minutes=10)) -> Path:
    """Dernière réponse Notion rangée par Claude Code dans un fichier : celle de la session de la routine si on la connaît,
    sinon la plus récente des 10 dernières minutes."""
    found = saved_responses(session)
    if not found or datetime.now() - datetime.fromtimestamp(found[-1].stat().st_mtime) > within:
        raise SyncError("Aucune réponse Notion rangée par Claude Code dans les 10 dernières minutes")
    return found[-1]


def deposit(output: str, source: str = "", root: Path = PROJECT_ROOT) -> dict:
    """Dépose dans « sortie » une réponse Notion que Claude Code a rangée dans un fichier (grosse réponse). Remplace le
    « cp » de la routine : Claude Code classe ~/.claude comme sensible et refuse d'y toucher (vu le 2026-11-17 sur la VM).
    La réponse est vérifiée (JSON avec « results ») et copiée telle quelle ; rien n'est lu ni affiché à part son nombre de lignes."""
    out = Path(output).expanduser()
    folder = (data_dir(root) / "notion").resolve()
    if folder not in out.resolve().parents:
        raise SyncError(f"Sortie hors du dossier de données : {out}")
    src = Path(source).expanduser() if source else saved_response(current_session())
    text = _saved_text(src)
    rows = _notion_rows(text, src.name)
    write_text(out, text)
    return {"sortie": str(out), "lignes": len(rows), "source": src.name}


def _saved_text(src: Path) -> str:
    """Contenu d'une réponse que Claude Code a rangée dans un fichier : seulement sous ~/.claude/projects."""
    if CLAUDE_PROJECTS.resolve() not in src.resolve().parents or not src.is_file():
        raise SyncError(f"Pas une réponse rangée par Claude Code : {src.name}")
    return src.read_text(encoding="utf-8")


def _notion_rows(text: str, name: str = "la réponse") -> list:
    """Lignes d'une réponse de l'outil Notion (JSON avec « results ») ; SyncError si ce n'en est pas une."""
    try:
        data = _unwrapped(json.loads(text))  # dépôt à la main : l'enveloppe est admise, comme dans _rows
    except (ValueError, KeyError, TypeError):
        raise SyncError(f"{name} n'est pas du JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise SyncError(f"{name} n'est pas une réponse Notion (pas de « results »)")
    return data["results"]


# ------------------------------------------------------------------------------------------------------------------
# Passage piloté par « suite » : Python donne les requêtes, le modèle les exécute, Python récolte les réponses.
NOTION_QUERY = "notion-query-data-sources"  # fin du nom de l'outil du connecteur (mcp__claude_ai_Notion__…)
SAVED_TO = re.compile(r"Output has been saved to (\S+?\.(?:txt|json))")  # avis de Claude Code quand il range une grosse réponse
MAX_ASKS = 3  # « suite » redemande une requête restée sans réponse ; à la troisième fois, le passage s'arrête
NEXT = "uv run python -m deploy_hub.sync_notion suite"
# Requêtes données par lot : au-delà de 30 000 caractères, Claude Code range la sortie d'une commande dans un fichier que
# le modèle ne lit pas (vu le 2026-11-20 sur une lecture complète : 12 pages, 31,7 Ko), et un lot est aussi ce que le
# modèle réécrit en un seul message. Le reste du plan part aux « suite » suivants.
BATCH_CHARS = 16000
PAGED = {"actions": "actions", "recette": "recette", "recette_meta": "recette"}  # lectures complètes : résultat → base comptée


def _sql_key(sql: str, params) -> tuple:
    """Ce qui identifie une requête exécutée : son texte aux blancs près, et ses paramètres. Selon la façon dont l'outil
    lui est présenté, le modèle passe les paramètres en liste ou en texte JSON (« ["…"] », vu le 2026-11-20) : même requête."""
    if isinstance(params, str) and params.strip():
        try:
            params = json.loads(params)
        except ValueError:
            params = [params]
    params = params if isinstance(params, (list, tuple)) else [params] if params else []
    return " ".join(str(sql).split()), tuple(str(p) for p in params)


VIEW_PREFIX = "vue:"  # « requete » d'une vue Notion lue telle quelle (mode view) : son adresse, ainsi préfixée


def view_key(url: str) -> tuple:
    """Ce qui identifie la lecture d'une vue Notion (mode view) dans le transcript : son adresse."""
    return _sql_key(VIEW_PREFIX + url.strip(), [])


def expected_view(client: str, url: str, out: Path) -> dict:
    """Vue Notion à lire telle quelle (ses filtres et son tri, ceux de Notion) : une seule page de 100 lignes."""
    return {"client": client, "nom": out.stem, "page": 1, "requete": VIEW_PREFIX + url, "params": [], "sources": [],
            "sql": "", "sortie": str(out), "vue": url}


def _expected(client: str | None, query: dict, page: int = 1) -> dict:
    """Requête attendue par « suite » : son texte exact, et où ranger sa réponse. Les pages suivantes reprennent la requête
    avec « LIMIT 100 OFFSET n » et vont dans <nom>-2.json, <nom>-3.json… (voir _rows)."""
    out = Path(query["sortie"])
    sql = Path(query["sql"]).read_text(encoding="utf-8").rstrip()
    if page > 1:
        sql += f"\nLIMIT {PAGE_SIZE} OFFSET {PAGE_SIZE * (page - 1)}"
        out = out.with_name(f"{out.stem}-{page}.json")
    return {"client": client, "nom": Path(query["sortie"]).stem, "page": page, "requete": sql, "params": query["params"],
            "sources": query["sources"], "sql": query["sql"], "sortie": str(out)}


def current_session() -> str:
    """Session Claude Code qui lance la commande : celle de la routine lancée par Jarvis, sinon celle que Claude Code
    annonce aux commandes qu'il lance (/jarvis-sync, tâche planifiée) ; "" si on ne la connaît pas."""
    return os.environ.get(SESSION_ENV) or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""


def _transcripts(since: datetime, session: str = "") -> list[Path]:
    """Transcripts de Claude Code où chercher les réponses : celui de la session donnée, sinon de celle qui lance la
    commande si on la connaît (routine lancée par Jarvis, ou variable posée par Claude Code), sinon ceux écrits depuis
    le début du passage."""
    session = session or current_session()
    if session and (found := list(CLAUDE_PROJECTS.glob(f"*/{session}.jsonl"))):
        return found
    return [p for p in CLAUDE_PROJECTS.glob("*/*.jsonl") if p.stat().st_mtime >= since.timestamp()]


def _answer(content) -> str | None:
    """Texte brut d'une réponse Notion valable, rendue en ligne ou rangée dans un fichier par Claude Code (grosse réponse) ;
    None pour tout le reste (erreur Notion, délai dépassé, fichier disparu) : la requête sera redemandée."""
    text = content if isinstance(content, str) else "".join(p.get("text", "") for p in content or [] if isinstance(p, dict))
    try:
        if not text.lstrip().startswith("{") and (m := SAVED_TO.search(text[:2000])):
            text = _saved_text(Path(m[1]))
        _notion_rows(text)
    except (SyncError, OSError):
        return None
    return text


def notion_answers(since: datetime, session: str = "") -> dict[tuple, str]:
    """Réponses de l'outil Notion reçues depuis le début du passage, lues dans le transcript de la session Claude Code :
    {(requête, paramètres): texte brut de la dernière réponse valable}. Le format du transcript est interne à Claude Code :
    tout ce qui n'a pas la forme attendue est ignoré, et « suite » fait alors déposer les réponses par le modèle."""
    answers: dict[tuple, str] = {}
    for path in _transcripts(since, session):
        asked: dict[str, tuple] = {}
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
                content = row["message"]["content"]
                when = _stamp(row["timestamp"])
            except (ValueError, KeyError, TypeError):
                continue
            if not isinstance(content, list) or when is None or when < since:
                continue  # réponses d'un passage précédent (même requête d'empreinte) : jamais reprises
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use" and str(part.get("name", "")).endswith(NOTION_QUERY):
                    data = part.get("input") or {}
                    data = data.get("data") if isinstance(data.get("data"), dict) else data
                    asked[part.get("id")] = (view_key(data["view_url"]) if data.get("mode") == "view" and data.get("view_url")
                                             else _sql_key(data.get("query", ""), data.get("params")))
                elif part.get("type") == "tool_result" and part.get("tool_use_id") in asked:
                    if (text := _answer(part.get("content"))) is not None:
                        answers[asked[part["tool_use_id"]]] = text
    return answers


def _shown(q: dict, lost: bool = False) -> dict:
    if q.get("vue"):
        return {"vue": q["vue"], **({"sortie": q["sortie"]} if lost else {})}
    return {"requete": q["requete"], "sources": q["sources"], **({"params": q["params"]} if q["params"] else {}),
            **({"sortie": q["sortie"]} if lost else {})}


def _ask(asked: list[dict], lost: bool = False) -> dict:
    """Ce que « preparer » et « suite » affichent : les requêtes à exécuter, puis la commande à relancer. lost : réponses
    introuvables dans le transcript ; chaque requête porte alors sa « sortie », que le modèle dépose lui-même (secours)."""
    out = {"requetes": [_shown(q, lost) for q in asked], "ensuite": NEXT}
    if lost:
        out["note"] = "Réponses introuvables : réexécute ces requêtes, dépose chaque réponse dans sa « sortie », puis relance la commande."
    return out


def _hand(state: dict) -> list[dict]:
    """Prochain lot de requêtes à donner au modèle (marquées « donnee »), dans l'ordre du plan : au moins une, puis tant
    que le lot tient dans BATCH_CHARS."""
    batch, size = [], 0
    for q in state["attendues"]:
        if q.get("donnee"):
            continue
        size += len(json.dumps(_shown(q), ensure_ascii=False))
        if batch and size > BATCH_CHARS:
            break
        q["donnee"] = True
        batch.append(q)
    return batch


def _collect(state: dict) -> list[dict]:
    """Range dans leur « sortie » les réponses des requêtes attendues ; renvoie celles qui manquent encore. Une sortie
    déposée à la main depuis le début du passage (secours) compte comme reçue."""
    since = datetime.fromisoformat(state["debut"])
    answers = notion_answers(since)
    missing = []
    for q in state["attendues"]:
        if q.get("recue") or not q.get("donnee"):
            continue
        out = Path(q["sortie"])
        if (text := answers.get(_sql_key(q["requete"], q["params"]))) is not None:
            write_text(out, text)
        elif not (out.is_file() and out.stat().st_mtime >= since.timestamp()):
            missing.append(q)
            continue
        q["recue"] = True
    return missing


def passage_progress(state: dict, session: str = "") -> tuple[str, int, int]:
    """Où en est un passage, pour le bouton de l'app : (étape, réponses déjà reçues de Notion, requêtes du plan). Les
    réponses sont comptées dans le transcript de la session au fur et à mesure : « suite » ne les range qu'ensuite, toutes
    ensemble, et l'avancement sautait du début à la fin (vu le 2026-11-20)."""
    answers = notion_answers(datetime.fromisoformat(state["debut"]), session)
    asked = state.get("attendues") or []
    return state.get("etape", ""), sum(1 for q in asked if q.get("recue") or _sql_key(q["requete"], q["params"]) in answers), len(asked)


def _next_pages(state: dict) -> list[dict]:
    """Pages suivantes à demander : une requête dont la dernière page reçue est pleine (100 lignes) a peut-être une suite.
    Au plafond de pages, on n'insiste pas : la fusion refusera ce client (_rows), avec un message clair."""
    last: dict[tuple, dict] = {}
    for q in state["attendues"]:
        key = (q["client"], q["nom"])
        if key not in last or q["page"] > last[key]["page"]:
            last[key] = q
    more = []
    for q in last.values():
        if q.get("vue"):  # une vue se lit en une page : pas de suite par OFFSET
            continue
        rows = _notion_rows(Path(q["sortie"]).read_text(encoding="utf-8"), Path(q["sortie"]).name)
        if len(rows) >= PAGE_SIZE and q["page"] < MAX_PAGES:
            first = next(x for x in state["attendues"] if (x["client"], x["nom"], x["page"]) == (q["client"], q["nom"], 1))
            more.append(_expected(q["client"], first, q["page"] + 1))
    return more


def _planned(plan: dict, root: Path) -> list[dict]:
    """Requêtes du plan, avec d'emblée toutes les pages d'une lecture complète : l'empreinte donne le nombre de lignes, les
    pages partent ensemble au lieu d'une par aller-retour (une de plus que nécessaire : la dernière doit être incomplète)."""
    rows = {r.get("cle"): r for r in _rows(data_dir(root) / "notion" / f"{EMPREINTES}.json")}
    out = []
    for query in plan["requetes"]:
        slug, name = query["client"], Path(query["sortie"]).stem
        count = _signature(rows.get(slug, {}).get(PAGED[name]))[0] if name in PAGED else 0
        out += [_expected(slug, query, page) for page in range(1, min(count // PAGE_SIZE + 1, MAX_PAGES) + 1)]
    return out


def _report(slugs: list[str], plan_errors: dict, merged: dict) -> list[str]:
    """Une ligne par client du passage : des comptes et des modes, jamais de données. « ÉCHEC » : Jarvis le relève."""
    lines = []
    for slug in slugs:
        if slug in plan_errors:
            lines.append(f"ÉCHEC {slug} : plan : {plan_errors[slug]}")
        elif "erreur" in merged.get(slug, {}):
            lines.append(f"ÉCHEC {slug} : fusion : {merged[slug]['erreur']}")
        elif slug in merged:
            r = merged[slug]
            modes = ", ".join(f"{base} {how}" for base, how in (r.get("mode") or {}).items()) or "fiche relue"
            lines.append(f"{slug} : {modes} · {r.get('changements', 0)} changement(s) · {r.get('cartes', 0)} cartes, {r.get('retours', 0)} retours")
        else:
            lines.append(f"{slug} : inchangé")
    return lines


def first_queries(root: Path = PROJECT_ROOT, only: str | None = None) -> dict:
    """Commande « preparer » : ouvre un passage et donne sa première requête (l'empreinte), au format de « suite »."""
    prepare(root, only)
    state = _load_json(_passage(root), {})
    batch = _hand(state)
    write_json(_passage(root), state)
    return _ask(batch)


def advance(root: Path = PROJECT_ROOT) -> dict:
    """Commande « suite » : récolte les réponses des requêtes attendues, puis dit quoi faire. Soit de nouvelles requêtes
    (réponses manquantes, pages suivantes, plan d'après l'empreinte), soit la fin : fusion faite, compte rendu, et les
    identifiants de responsables encore sans nom."""
    path = _passage(root)
    state = _load_json(path, None)
    if not state or state.get("etape") == "fini":
        raise SyncError("Aucun passage en cours : lance d'abord « preparer »")
    slugs = _followed(root, state["clients"])  # un client archivé en cours de passage n'est plus traité
    if (missing := _collect(state)):
        state["demandes"] += 1
        write_json(path, state)
        if state["demandes"] >= MAX_ASKS:
            names = ", ".join(sorted({f"{q['client'] or 'tous'}/{q['nom']}" for q in missing}))
            raise SyncError(f"réponses de Notion introuvables après {MAX_ASKS} demandes ({names}) : rien n'est fusionné")
        return _ask(missing, lost=state["demandes"] > 1)  # une première fois telle quelle (erreur passagère de Notion)
    state["demandes"] = 0
    if not (batch := _hand(state)):  # tout le plan est donné et reçu : des pages pleines appellent leur suite
        state["attendues"] += _next_pages(state)
        batch = _hand(state)
    if not batch and state["etape"] == "empreinte":
        if state["clients"]:
            planned = _planned(plan_all(root, slugs), root)
        else:  # passage des seules demandes BAU : pas d'empreinte des clients, et aucune erreur de plan d'avant à garder
            planned = []
            write_json(data_dir(root) / "notion" / PLAN_ERRORS, {})
        state.update(etape="requetes", attendues=planned + _bau_planned(state, root))
        batch = _hand(state)
    if batch:
        write_json(path, state)
        return _ask(batch)
    plan_errors = _load_json(data_dir(root) / "notion" / PLAN_ERRORS, {})  # écrites par plan_all
    merged = merge_all(root, slugs)
    errors = {**plan_errors, **{slug: f"fusion : {r['erreur']}" for slug, r in merged.items() if "erreur" in r}}
    bau_line = _bau_merged(state, root, errors)
    write_json(data_dir(root) / "notion" / PLAN_ERRORS, errors)  # Jarvis les affiche, quel que soit le compte rendu du modèle
    state.update(etape="fini", attendues=[])
    write_json(path, state)
    return {"termine": True, "compte_rendu": _report(slugs, plan_errors, merged) + bau_line, "utilisateurs": unknown_users(root)}


BAU_ERRORS = (SyncError, ConfigError, OSError, KeyError, ValueError)  # un échec des demandes BAU n'arrête pas les clients


def _bau_planned(state: dict, root: Path) -> list[dict]:
    """Requêtes des demandes BAU d'après leur empreinte, si elles sont du passage. Un plan en échec est gardé pour le
    compte rendu (state["bau_erreur"]) : les clients, eux, continuent."""
    if not state.get("bau"):
        return []
    from deploy_hub import bau
    try:
        return bau.planned(root)
    except BAU_ERRORS as e:
        state["bau_erreur"] = f"plan : {e}"
        return []


def _bau_merged(state: dict, root: Path, errors: dict) -> list[str]:
    """Fusionne les demandes BAU du passage ; renvoie leur ligne de compte rendu ([] si elles n'en sont pas). Un échec
    est aussi noté dans errors (plan-erreurs.json), que Jarvis affiche sous le bouton."""
    if not state.get("bau"):
        return []
    from deploy_hub import bau
    error = state.get("bau_erreur")
    if not error:
        try:
            return [bau.report(bau.merge(root))]
        except BAU_ERRORS as e:
            error = f"fusion : {e}"
    errors[bau.LABEL] = error
    return [f"ÉCHEC {bau.LABEL} : {error}"]


PLAN_ERRORS = "plan-erreurs.json"  # data/notion/plan-erreurs.json : erreurs par client du dernier « plan »
VERIFICATIONS = "verifications.json"  # data/notion/verifications.json : dernière vérification de chaque client


CHECKED_FORMAT = "%d/%m/%Y %H:%M"


def next_check(minutes: int, slugs: list[str], root: Path = PROJECT_ROOT) -> datetime | None:
    """Moment à partir duquel l'un de ces clients est à revérifier dans Notion : sa dernière vérification (mark_checked)
    plus « minutes » ; datetime.min, c'est-à-dire tout de suite, s'il n'a jamais été vérifié. None : aucun client donné.
    C'est l'échéance de la synchro automatique."""
    seen = _load_json(data_dir(root) / "notion" / VERIFICATIONS, {})

    def due(slug: str) -> datetime:
        try:
            return datetime.strptime(seen[slug], CHECKED_FORMAT) + timedelta(minutes=minutes)
        except (KeyError, TypeError, ValueError):
            return datetime.min
    return min(map(due, slugs), default=None)


def mark_checked(slugs: list[str], root: Path = PROJECT_ROOT) -> None:
    """Note l'heure à laquelle ces clients ont été vérifiés dans Notion, même s'il n'y avait rien de neuf : la fiche n'est
    réécrite (et synced_at mis à jour) que quand quelque chose change, la « dernière synchro » affichée ne doit pas dater."""
    if not slugs:
        return
    path = data_dir(root) / "notion" / VERIFICATIONS
    seen = _load_json(path, {})
    seen.update({slug: datetime.now().strftime(CHECKED_FORMAT) for slug in slugs})
    write_json(path, seen)


def plan_all(root: Path = PROJECT_ROOT, slugs: list[str] | None = None) -> dict:
    """Plan des clients du passage d'après data/notion/empreintes.json : requêtes à exécuter, clients inchangés."""
    folder = data_dir(root) / "notion"
    rows = {r.get("cle"): r for r in _rows(folder / f"{EMPREINTES}.json")}
    out = {"requetes": [], "inchanges": [], "raisons": {}, "erreurs": {}}
    for slug in _followed(root, slugs):
        raw = folder / slug
        if slug not in rows:
            out["erreurs"][slug] = "absent de l'empreinte : relance « preparer »"
            continue
        if not rows[slug].get("fiche"):  # aucune page « Clients en déploiement » à cet id : rien ne sert de lire le reste
            out["erreurs"][slug] = "fiche Notion introuvable : vérifie le lien de sa fiche « Clients en déploiement » (Clients → Configurer) et que tu y as accès dans Notion"
            continue
        p = plan_client(slug, rows[slug], root)
        raw.mkdir(parents=True, exist_ok=True)
        _clean(raw, RESULTS[2:] + ("plan",))
        if not p["requetes"]:
            out["inchanges"].append(slug)
            continue
        _clean(raw, [name for _, name, _ in p["requetes"]])
        write_json(raw / "plan.json", {"sorties": [name for _, name, _ in p["requetes"]], "empreinte": rows[slug]})
        out["requetes"] += [{"client": slug, **_query(raw / "requetes" / f"{q}.sql", raw / f"{name}.json", params)}
                            for q, name, params in p["requetes"]]
        out["raisons"][slug] = p["raisons"]
    mark_checked(out["inchanges"], root)  # vérifiés, rien de neuf
    # Erreurs par client : Jarvis les affiche à la fin de la synchro, quel que soit le compte rendu de Claude.
    write_json(folder / PLAN_ERRORS, out["erreurs"])
    return out


def merge_all(root: Path = PROJECT_ROOT, slugs: list[str] | None = None) -> dict:
    """Fusionne les clients qui ont un plan ; un client en échec n'empêche pas les autres."""
    results = {}
    for slug in _followed(root, slugs):
        if not (data_dir(root) / "notion" / slug / "plan.json").is_file():
            continue
        try:
            results[slug] = sync(slug, root)
        except (SyncError, ConfigError, OSError, KeyError, ValueError) as e:
            results[slug] = {"erreur": str(e)}
    mark_checked([slug for slug, r in results.items() if "erreur" not in r], root)
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
    write_json(path, journal)


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
    if "emoji" not in client:  # la routine SQL ne voit pas les icônes : celle lue par l'annuaire, sinon celle déjà dans la fiche
        icon = saved_icon(raw)
        client = {**client, **(icon if any(icon.values()) else _fiche_icon(fiche))}
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
    write_json(raw / "etat-synchro.json", state)
    _clean(raw, RESULTS[2:] + ("plan",))
    result = summary(out["deployment"], out["blocked"], {base: how for base, (_, how) in merged.items()}, out["changes"])
    if any(removed.values()):
        result["retirees"] = removed
    return result


if __name__ == "__main__":
    from deploy_hub.config import ensure_private_dirs
    ensure_private_dirs()
    # uv run python -m deploy_hub.sync_notion preparer [<slug>] → ouvre un passage ; affiche la requête d'empreinte à exécuter
    # uv run python -m deploy_hub.sync_notion suite           → récolte les réponses ; requêtes suivantes, ou fusion et compte rendu
    # uv run python -m deploy_hub.sync_notion plan            → d'après l'empreinte : les seules requêtes à exécuter
    # uv run python -m deploy_hub.sync_notion fusionner       → fusionne les résultats déposés, écrit les fiches
    # uv run python -m deploy_hub.sync_notion utilisateurs     → uuids des responsables sans nom dans l'annuaire
    # uv run python -m deploy_hub.sync_notion utilisateurs '{"<uuid>": "<nom>"}'  → ajoute ces noms à l'annuaire
    # uv run python -m deploy_hub.sync_notion clients | requetes <slug> | <slug>   → outils unitaires
    from deploy_hub import client_config
    show = lambda value: print(json.dumps(value, ensure_ascii=False))
    try:
        command = sys.argv[1]
        if command == "preparer":  # preparer [<slug>] : ce client seulement
            show(first_queries(only=sys.argv[2] if len(sys.argv) > 2 else None))
        elif command == "suite":
            show(advance())
        elif command == "plan":
            show(plan_all())
        elif command == "fusionner":
            results = merge_all()
            show(results)
            sys.exit(1 if any("erreur" in r for r in results.values()) else 0)
        elif command == "clients":
            print("\n".join(client_config.active_slugs()))
        elif command == "requetes":
            print(queries.write(sys.argv[2], load_config(sys.argv[2])))
        elif command == "deposer":  # deposer <sortie> [<fichier rangé par Claude Code>]
            show(deposit(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""))
        elif command == "utilisateurs":
            if len(sys.argv) > 2:
                add_users(json.loads(sys.argv[2]))
            show(unknown_users())
        else:
            show(sync(command))
    except (SyncError, ConfigError, OSError, KeyError, ValueError, IndexError) as e:
        print(f"ÉCHEC synchro : {e}", file=sys.stderr)
        sys.exit(1)
