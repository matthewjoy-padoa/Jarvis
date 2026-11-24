"""Demandes BAU : les actions de la base Notion « Equipes OPS - Actions internes » liées à une page projet ou BAU.

Pour l'équipe qui traite ces demandes plutôt que des déploiements. Réglage JARVIS_NOTION_BAU : le lien de la page (par
exemple « BAU équipe intégration - TECHNIQUE »). Les demandes suivent le passage de synchro des clients (sync_notion :
« preparer », « suite »), sous la clé KEY, et restent dans data/notion/_bau/ (hors git) :
- requetes/ : les trois requêtes prêtes à l'emploi (modèles sync/requetes/bau*.sql) ;
- cache-demandes.json : les demandes, que la page « BAU » lit telles quelles ;
- etat-synchro.json : page et colonnes du cache, jour de la dernière lecture complète.

Lecture seule : rien n'est écrit dans Notion. Le corps des pages (la description d'une demande) n'est pas lu par la
synchro, qui ne connaît que les colonnes de la base : il est lu à part, à l'ouverture d'une demande (sources/bau_details.py).
"""

import hashlib
import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from datetime import date, datetime
from pathlib import Path

from deploy_hub import sync_notion as sn
from deploy_hub.config import PROJECT_ROOT, data_dir
from deploy_hub.models import DONE, IN_PROGRESS, TODO, group_counts, notion_link, status_group
from deploy_hub.queries import TEMPLATES, ConfigError, page_id_of
from deploy_hub.safe_write import write_json
from deploy_hub.sources.fiches import FicheSource, _names
from deploy_hub.sync_notion import SyncError

KEY = "_bau"  # clé des demandes dans un passage et dossier data/notion/_bau : jamais un identifiant de client (pas de « _ »)
LABEL = "BAU"  # nom des demandes dans les comptes rendus et les messages de synchro
SETTING = "JARVIS_NOTION_BAU"
# Page « BAU équipe intégration - TECHNIQUE », la même pour toute l'équipe : suivie sans réglage ; JARVIS_NOTION_BAU en
# suit une autre.
DEFAULT_PAGE = "https://app.notion.com/p/padoa/BAU-quipe-int-gration-TECHNIQUE-5314976cdfac824da508018262a6fc9c"
SQUADS_SETTING = "JARVIS_BAU_SQUADS"
# Vues de squad de la page « BAU équipe intégration - TECHNIQUE » : Jarvis les lit telles quelles (mode view), avec leurs
# filtres (personnes de la squad, demandes à faire ou en cours). Une par ligne ou séparées par « ; » : « nom | lien de la vue ».
DEFAULT_SQUADS = "SPSTA | view://1684976c-dfac-830a-9523-088056550d68; Plateform | view://8454976c-dfac-83f5-b601-080ae7576566"
SQUADS = "squads.json"  # data/notion/_bau/squads.json : {nom de la squad: [références des demandes de sa vue]}
# Ce qui tient lieu de client aux demandes là où Jarvis en attend un (épingles, rappels, agents) : slug et nom seulement.
SPACE = SimpleNamespace(slug=KEY, client=LABEL)
TEMPLATE_NAMES = ("bau", "bau-delta", "bau-empreinte")
# Colonnes admises : tout autre champ arrête la fusion (comme pour les retours de recette).
COLUMNS = {"url", "intitule", "statut", "priorite", "echeance", "responsables", "referents", "cree_le", "modifie", "clients", "ressources"}
TRACKED = ("statut", "priorite", "echeance", "responsables")  # un changement compté dans le compte rendu de la synchro
FULL_SYNC_DAYS = 7  # clients et ressources liés viennent d'autres bases : leurs changements ne datent pas la demande
CACHE, STATE = "cache-demandes.json", "etat-synchro.json"
RESULTS = ("demandes", "demandes_delta")  # réponses de Notion d'un passage, retirées une fois fusionnées

# Notion « Equipes OPS - Actions internes » → « Statut de l'action », dans l'ordre et les groupes de Notion.
STATUSES = {
    "À cadrer": TODO,
    "Pas commencé": TODO,
    "En cours": IN_PROGRESS,
    "En attente de retours internes": IN_PROGRESS,
    "A valider": IN_PROGRESS,
    "Plus tard": DONE,
    "Terminé": DONE,
    "Annulé": DONE,
}
PRIORITIES = ("P0", "P1", "P2")  # début des options de « Priorité » (« P0🔥 »…), de la plus à la moins urgente


@dataclass
class Demande:
    ref: str  # 8 derniers caractères de l'id Notion de la page
    title: str
    status: str = "À cadrer"
    priority: str = ""
    due: date | None = None
    created: date | None = None
    owners: list[str] = field(default_factory=list)  # « Responsable action »
    referents: list[str] = field(default_factory=list)  # « Référent inté »
    clients: list[str] = field(default_factory=list)  # « Client lié à l'action » : noms de domaine Padoa
    resources: list[str] = field(default_factory=list)  # « Resources » : noms orchestrator
    notion_url: str = ""
    modified: str = ""  # « Dernière modif » telle que Notion la donne : une description lue avant est à relire

    @property
    def group(self) -> str:
        return status_group(self.status, STATUSES)

    @property
    def is_open(self) -> bool:
        return self.group != DONE

    @property
    def priority_rank(self) -> int:
        """Rang de la priorité (0 : P0) ; les demandes sans priorité viennent après."""
        return next((i for i, p in enumerate(PRIORITIES) if self.priority.startswith(p)), len(PRIORITIES))

    def is_late(self, today: date) -> bool:
        return bool(self.due) and self.is_open and self.due < today


@dataclass
class Board:
    """Ce que la page « BAU » affiche : les demandes du cache, ou la raison pour laquelle il n'y en a pas."""
    configured: bool = False  # page suivie lisible (False : JARVIS_NOTION_BAU illisible)
    squads: dict[str, list[str]] = field(default_factory=dict)  # vue de squad → références de ses demandes (comme dans Notion)
    problem: str = ""  # réglage illisible
    notion_url: str = ""  # la page BAU dans Notion
    synced: bool = False  # au moins une synchro faite pour cette page
    checked_at: datetime | None = None  # dernière vérification dans Notion, même sans changement
    demandes: list[Demande] = field(default_factory=list)

    @property
    def open(self) -> list[Demande]:
        return [d for d in self.demandes if d.is_open]

    @property
    def groups(self) -> dict[str, int]:
        """Nombre de demandes par groupe de statut (à faire, en cours, terminé), pour la barre de répartition."""
        return group_counts([d.group for d in self.demandes])

    def squad(self, slug: str) -> tuple[str, list[Demande]] | None:
        """(nom, demandes) de la squad dont l'identifiant (squad_slug) est donné ; None si elle n'existe pas."""
        for name, refs in self.squads.items():
            if squad_slug(name) == slug:
                wanted = set(refs)
                return name, [d for d in self.demandes if d.ref in wanted]
        return None

    def get(self, ref: str) -> Demande | None:
        return next((d for d in self.demandes if d.ref == ref.upper()), None)


# ------------------------------------------------------------------------------------------------------------ réglage
def page_id() -> str:
    """Id de la page projet ou BAU suivie : celle du réglage, sinon la page BAU de l'équipe intégration. ConfigError si le
    réglage n'est ni un id ni une adresse Notion."""
    return page_id_of(os.getenv(SETTING, "").strip() or DEFAULT_PAGE)


def problem() -> str:
    """Pourquoi le réglage est inutilisable ; "" s'il est vide (page par défaut) ou correct."""
    try:
        page_id()
    except ConfigError:
        return f"{SETTING} n'est ni le lien ni l'id d'une page Notion (32 caractères hexadécimaux attendus)."
    return ""


def enabled() -> bool:
    try:
        return bool(page_id())
    except ConfigError:
        return False


def squad_views() -> list[tuple[str, str]]:
    """(nom, lien de la vue) des squads : réglage JARVIS_BAU_SQUADS, sinon les vues de la page BAU de l'équipe intégration.
    Une ligne sans « | » prend son lien pour nom."""
    value = os.getenv(SQUADS_SETTING, "").strip() or DEFAULT_SQUADS
    out = []
    for part in value.replace("\n", ";").split(";"):
        name, _, url = (p.strip() for p in part.rpartition("|")) if "|" in part else ("", "", part.strip())
        if url:
            out.append((name or url, url))
    return out


def squad_slug(name: str) -> str:
    from deploy_hub.client_config import slugify
    return slugify(name) or "squad"


def folder(root: Path = PROJECT_ROOT) -> Path:
    return data_dir(root) / "notion" / KEY


# ------------------------------------------------------------------------------------------------------------ liens
LINKS_FILE = "bau.yaml"  # <dossier de configuration>/bau.yaml (config/, hors git) : les liens de l'équipe


@dataclass
class Links:
    """Liens de l'équipe BAU, qui n'a pas de client pour porter les siens : Google Drive, pages Notion, autres adresses.
    Jarvis ne fait que les ouvrir dans le navigateur, il ne lit jamais leur contenu."""
    drive: list[dict] = field(default_factory=list)  # [{label, url}] : drive.google.com ou docs.google.com
    notion: list[dict] = field(default_factory=list)  # [{label, url}] : pages Notion de l'équipe
    other: list[dict] = field(default_factory=list)  # [{label, url}] : toute autre adresse http(s)

    @property
    def all(self) -> list[dict]:
        return self.drive + self.notion + self.other

    @staticmethod
    def text(links: list[dict]) -> str:
        """Liens au format du formulaire : un par ligne, « libellé | adresse »."""
        return "\n".join(f"{l['label']} | {l['url']}" for l in links)


def links_path(root: Path = PROJECT_ROOT) -> Path:
    from deploy_hub.config import clients_dir
    return clients_dir(root).parent / LINKS_FILE


def parse_other_links(text: str) -> list[dict]:
    """Autres liens, un par ligne : « libellé | adresse » ou l'adresse seule ; seulement des adresses http(s)."""
    from deploy_hub.client_config import _labelled_links
    links = []
    for line, label, url in _labelled_links(text):
        if not url.startswith(("https://", "http://")):
            raise ConfigError(f"Lien « {line} » : adresse https://… attendue")
        links.append({"label": label or url.split("//", 1)[1].split("/", 1)[0], "url": url})
    return links


def links_from_form(form: dict) -> Links:
    """Liens saisis dans la page « Liens de l'équipe » ; ConfigError si une adresse ne convient pas."""
    from deploy_hub.client_config import parse_dashboards, parse_drive
    return Links(drive=parse_drive(str(form.get("drive") or "")), notion=parse_dashboards(str(form.get("notion") or "")),
                 other=parse_other_links(str(form.get("other") or "")))


def _settings(root: Path = PROJECT_ROOT) -> dict:
    """Contenu de config/bau.yaml (liens, synchro automatique) ; {} s'il manque ou est illisible (retouché à la main)."""
    import yaml
    try:
        data = yaml.safe_load(links_path(root).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_settings(data: dict, root: Path = PROJECT_ROOT) -> Path:
    import yaml
    from deploy_hub.safe_write import write_text
    path = links_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, "# Équipe BAU (page BAU de Jarvis) : configuration locale, hors git, modifiable depuis l'app.\n"
               + yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    return path


def auto_sync(root: Path = PROJECT_ROOT) -> bool:
    """Jarvis relit les demandes tout seul (bouton « Synchro auto » de la page BAU) ; non par défaut, comme un client."""
    return bool(_settings(root).get("synchro_auto"))


def set_auto_sync(on: bool, root: Path = PROJECT_ROOT) -> None:
    _write_settings({**_settings(root), "synchro_auto": on}, root)


def load_links(root: Path = PROJECT_ROOT) -> Links:
    """Liens enregistrés ; aucun si le fichier manque ou est illisible (retouché à la main) : la page reste utilisable."""
    data = _settings(root)
    keep = lambda key: [{"label": str(l.get("label") or "Lien"), "url": str(l["url"])} for l in data.get(key) or []
                        if isinstance(l, dict) and str(l.get("url") or "").startswith(("https://", "http://"))]
    return Links(drive=keep("drive"), notion=keep("notion"), other=keep("autres"))


def save_links(links: Links, root: Path = PROJECT_ROOT) -> Path:
    """Enregistre les liens ; le reste du fichier (synchro automatique) est gardé."""
    return _write_settings({**_settings(root), "drive": links.drive, "notion": links.notion, "autres": links.other}, root)


# ------------------------------------------------------------------------------------------------------------ synchro
def _template(name: str) -> str:
    return (TEMPLATES / f"{name}.sql").read_text(encoding="utf-8")


def version() -> str:
    """Empreinte des colonnes demandées : une colonne ajoutée ou retirée impose de tout relire."""
    return hashlib.sha256("".join(_template(n) for n in ("bau", "bau-delta")).encode()).hexdigest()[:12]


def write_queries(root: Path = PROJECT_ROOT) -> Path:
    """Écrit les requêtes de la page suivie dans data/notion/_bau/requetes/, où la routine de synchro les lit."""
    out = folder(root) / "requetes"
    out.mkdir(parents=True, exist_ok=True)
    for name in TEMPLATE_NAMES:
        (out / f"{name}.sql").write_text(_template(name).replace("{{page_id}}", page_id()).replace("{{cle}}", KEY), encoding="utf-8")
    return out


def empreinte(root: Path = PROJECT_ROOT) -> dict:
    """Ouvre un passage pour les demandes : leur requête d'empreinte (nombre de demandes, dernière modification), que
    « preparer » donne avec celle des clients. Les réponses d'un passage précédent, resté en plan, sont retirées."""
    queries_dir = write_queries(root)
    raw = folder(root)
    sn._clean(raw, ("empreinte", "plan") + RESULTS)
    for answer in raw.glob("squad-*.json"):
        answer.unlink()
    return sn._query(queries_dir / "bau-empreinte.sql", raw / "empreinte.json", [])


def plan(row: dict, root: Path = PROJECT_ROOT) -> tuple[list[tuple[str, str, list]], str]:
    """Requêtes à exécuter d'après la ligne d'empreinte : [(modèle, sortie, paramètres)] et la raison. Lecture complète sans
    cache, quand la page ou les colonnes ont changé, quand des demandes ont disparu, et tous les FULL_SYNC_DAYS jours ;
    sinon les seules demandes modifiées depuis la dernière connue ; rien si l'empreinte n'a pas bougé."""
    raw = folder(root)
    cache, state = sn._load_json(raw / CACHE, None), sn._load_json(raw / STATE, {})
    count, last = sn._signature(row.get("demandes"))
    last_full = state.get("complet_le")
    if cache is None:
        reason = "pas de cache"
    elif state.get("page") != page_id():
        reason = "page suivie changée"
    elif state.get("colonnes") != version():
        reason = "colonnes modifiées"
    elif count < len(cache) or state.get("complet_requis"):
        reason = "demandes disparues de la page"
    elif not last_full or (date.today() - date.fromisoformat(last_full)).days >= FULL_SYNC_DAYS:
        reason = f"relecture complète périodique (dernière : {last_full or 'jamais'})"
    else:
        reason = ""
    if reason:
        return [("bau", "demandes", [])], f"complet : {reason}"
    known = [t for r in cache.values() if (t := sn._stamp(r.get("modifie")))]
    since, current = max(known, default=None), sn._stamp(last)
    if count > len(cache) or (current and (since is None or current > since)):
        start = sn._sql_time(since) if since else "1970-01-01 00:00:00Z"
        return [("bau-delta", "demandes_delta", [start])], f"delta depuis {start}"
    return [], "inchangé"


def planned(root: Path = PROJECT_ROOT) -> list[dict]:
    """Requêtes attendues par « suite » d'après l'empreinte déposée, avec d'emblée toutes les pages d'une lecture complète
    (l'empreinte donne le nombre de demandes ; une page de plus que nécessaire : la dernière doit être incomplète)."""
    raw = folder(root)
    rows = sn._rows(raw / "empreinte.json")
    if len(rows) != 1:
        raise SyncError(f"empreinte : 1 ligne attendue, {len(rows)} reçue(s)")
    todo, why = plan(rows[0], root)
    write_json(raw / "plan.json", {"sorties": [name for _, name, _ in todo], "empreinte": rows[0], "raison": why})
    count = sn._signature(rows[0].get("demandes"))[0]
    out = []
    for template, name, params in todo:
        query = sn._query(raw / "requetes" / f"{template}.sql", raw / f"{name}.json", params)
        pages = min(count // sn.PAGE_SIZE + 1, sn.MAX_PAGES) if name == "demandes" else 1
        out += [sn._expected(KEY, query, page) for page in range(1, pages + 1)]
    # Vues de squad : relues à chaque passage (une page chacune) ; leurs filtres changent sans dater les demandes.
    return out + [sn.expected_view(KEY, url, raw / f"squad-{n}.json") for n, (_, url) in enumerate(squad_views(), start=1)]


def _checked(rows: list[dict]) -> list[dict]:
    for row in rows:
        if (extra := set(row) - COLUMNS):
            raise SyncError(f"Colonnes non autorisées : {sorted(extra)}. Demandes non mises à jour.")
    return rows


def _changes(before: dict[str, dict] | None, after: dict[str, dict]) -> int:
    """Demandes nouvelles, disparues ou dont un champ suivi a changé depuis le cache ; 0 au premier passage."""
    if before is None:
        return 0
    changed = sum(1 for url, row in after.items() if url not in before
                  or any(sn._norm(before[url].get(k)) != sn._norm(row.get(k)) for k in TRACKED))
    return changed + len(before.keys() - after.keys())


def merge(root: Path = PROJECT_ROOT) -> dict:
    """Fusionne dans le cache les réponses déposées selon plan.json et note l'heure de la vérification. Des comptes
    seulement dans le résultat : il va dans le compte rendu de la synchro."""
    raw = folder(root)
    planned_ = sn._load_json(raw / "plan.json", None)
    if planned_ is None:
        raise SyncError("pas de plan : relance la synchro")
    wanted = planned_["sorties"]
    cache = sn._load_json(raw / CACHE, None)
    if "demandes" in wanted:
        after, how = {r["url"]: r for r in _checked(sn._rows(raw / "demandes.json"))}, "complet"
    elif "demandes_delta" in wanted:
        after, how = {**(cache or {}), **{r["url"]: r for r in _checked(sn._rows(raw / "demandes_delta.json"))}}, "delta"
    else:
        after, how = cache or {}, "inchangé"
    state = sn._load_json(raw / STATE, {})
    count = sn._signature(planned_["empreinte"].get("demandes"))[0]
    if how != "inchangé":
        write_json(raw / CACHE, after)
    write_json(raw / STATE, {"page": page_id(), "colonnes": version(),
                             "complet_le": date.today().isoformat() if how == "complet" else state.get("complet_le"),
                             # autant de demandes que dans Notion, sinon tout est relu au passage suivant
                             "complet_requis": len(after) != count})
    squads = {}
    for n, (name, _) in enumerate(squad_views(), start=1):
        answer = raw / f"squad-{n}.json"
        if answer.is_file():  # une page de 100 lignes au plus : une vue de squad n'a que les demandes ouvertes de ses membres
            squads[name] = [sn._page_id(r.get("url", ""))[-8:].upper() for r in sn._notion_rows(answer.read_text(encoding="utf-8"), answer.name)]
    write_json(raw / SQUADS, squads)
    for answer in raw.glob("squad-*.json"):
        answer.unlink()
    sn._clean(raw, RESULTS + ("plan", "empreinte"))
    sn.mark_checked([KEY], root)
    before = cache if state.get("page") == page_id() else None  # cache d'une autre page : premier passage pour celle-ci
    return {"mode": how, "demandes": len(after), "changements": _changes(before, after)}


def report(result: dict) -> str:
    """Ligne du compte rendu de la synchro : un mode et des comptes, jamais de données."""
    if result["mode"] == "inchangé":
        return f"{LABEL} : inchangé"
    return f"{LABEL} : demandes {result['mode']} · {result['changements']} changement(s) · {result['demandes']} demandes"


def cached_user_ids(root: Path = PROJECT_ROOT) -> set[str]:
    """Responsables et référents des demandes du cache (uuids Notion) : ceux sans nom sont traduits en fin de synchro."""
    rows = sn._load_json(folder(root) / CACHE, {}).values() if enabled() else []
    return {u for row in rows for column in ("responsables", "referents") for u in sn.user_ids(row.get(column))}


# ------------------------------------------------------------------------------------------------------------ lecture
def _list(value) -> list[str]:
    return sorted({v.strip() for v in (value or "").split(",") if v.strip()})


def _demande(row: dict, users: dict[str, str]) -> Demande:
    names = lambda column: _names(sn.user_ids(row.get(column)), users)
    return Demande(
        ref=sn._page_id(row["url"])[-8:].upper(),
        title=row.get("intitule") or "(sans titre)",
        status=row.get("statut") or "À cadrer",
        priority=row.get("priorite") or "",
        due=sn._date(row.get("echeance")),
        created=sn._date(row.get("cree_le")),
        owners=names("responsables"),
        referents=names("referents"),
        clients=_list(row.get("clients")),
        resources=_list(row.get("ressources")),
        notion_url=notion_link(row["url"]),
        modified=str(row.get("modifie") or ""),
    )


_stamp = FicheSource._stamp  # date de modification d'un fichier, 0 s'il manque


_boards: dict[Path, tuple[tuple, Board]] = {}  # tableau gardé en mémoire tant que ni le réglage ni ses fichiers ne changent


def load(root: Path = PROJECT_ROOT) -> Board:
    """Demandes de la page suivie : les plus prioritaires d'abord, puis les échéances les plus proches, puis les plus
    récentes. Un cache fait pour une autre page (réglage changé) n'est pas affiché : il faut d'abord synchroniser."""
    if (why := problem()) or not (pid := page_id()):
        return Board(problem=why)
    raw, users_file, checks = folder(root), sn.users_path(root), data_dir(root) / "notion" / sn.VERIFICATIONS
    stamps = (pid, _stamp(raw / CACHE), _stamp(raw / STATE), _stamp(raw / SQUADS), _stamp(users_file), _stamp(checks))
    if (kept := _boards.get(root)) and kept[0] == stamps:
        return kept[1]
    board = Board(configured=True, notion_url=f"https://app.notion.com/p/{pid}")
    cache = sn._load_json(raw / CACHE, None)
    if cache is not None and sn._load_json(raw / STATE, {}).get("page") == pid:
        users = sn._load_json(users_file, {})
        board.synced = True
        board.squads = sn._load_json(raw / SQUADS, {})
        board.demandes = sorted((_demande(row, users) for row in cache.values()),
                                key=lambda d: (d.priority_rank, d.due or date.max, -(d.created or date.min).toordinal(), d.title))
        try:
            board.checked_at = datetime.strptime(sn._load_json(checks, {}).get(KEY, ""), sn.CHECKED_FORMAT)
        except (TypeError, ValueError):
            board.checked_at = None
    _boards[root] = (stamps, board)
    return board
