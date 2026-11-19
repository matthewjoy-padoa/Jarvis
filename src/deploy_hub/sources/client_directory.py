"""Annuaire des clients Notion : ajouter un client en le choisissant dans une liste, sans rien chercher.

Une fois par jour (ou à la demande), Claude Code (`claude -p`, connecteur Notion, lecture seule) lit deux choses : la page
« Liste des dashboards pour le déploiement des nouveaux clients » et la base « Clients en déploiement ». Il n'écrit rien :
Claude Code range ces grosses réponses dans des fichiers de sa session (MAX_MCP_OUTPUT_TOKENS bas), que Jarvis lit lui-même
ensuite. Python en tire l'annuaire (data/notion/annuaire.json) : pour chaque client, ses dashboards par famille et les fiches
« Clients en déploiement » qui portent son nom. Choisir un client pré-remplit le formulaire « Ajouter un client ».
"""
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from deploy_hub.client_config import is_notion, slugify
from deploy_hub.config import PROJECT_ROOT, data_dir
from deploy_hub.queries import ConfigError, page_id_of
from deploy_hub.sources import ActionResult
from deploy_hub.sources.notion_api import CLIENTS_DS
from deploy_hub.sources.sync_routine import MCP_OUTPUT_TOKENS, NOTION_SERVERS, app_env
from deploy_hub.sync_notion import CLAUDE_PROJECTS, saved_responses

DASHBOARDS_PAGE = "daf37a4f4c5b4288b0737f50442504c3"  # « Liste des dashboards pour le déploiement des nouveaux clients »
MAX_AGE = timedelta(days=1)  # au-delà, l'annuaire est relu en fond par Jarvis
FORMAT = 2  # version du contenu de annuaire.json : un annuaire d'une autre version est relu aussitôt (2 : liens complets)
WORKSPACE = os.getenv("JARVIS_NOTION_WORKSPACE") or "padoa"  # espace de travail Notion, dans les liens complets
FAMILIES = {"deploiement": "Déploiement", "integration": "Intégration", "formation": "Formation",
            "gestion_financiere": "Gestion financière"}
# Titre de section (h1) de la page → famille ; les sections non listées (tests, refresher) sont ignorées.
SECTIONS = (("refresher", None), ("test", None), ("delivery", "deploiement"), ("formation", "formation"),
            ("integration", "integration"), ("gesfin", "gestion_financiere"))
INSTRUCTION = "Suis la consigne reçue sur l'entrée standard (annuaire des clients Notion pour Jarvis)."
PROMPT = """Lecture seule : ne modifie rien dans Notion, n'écris aucun fichier, ne recopie aucun résultat.

1. Outil notion-fetch, id = {page}.
2. Outil notion-query-data-sources, mode sql, data_source_urls = ["collection://{clients}"], requête :
   SELECT url, "Nom", "élément parent" FROM "collection://{clients}" LIMIT 100 OFFSET 0
   Si la réponse a 100 lignes, relance avec OFFSET 100, puis 200, etc., jusqu'à une réponse de moins de 100 lignes.
3. Réponds seulement « fini » (ou « ÉCHEC : <raison> » si un outil a échoué).
"""
PAGE = re.compile(r'<page url="([^"]+)">\s*(.*?)\s*</page>', re.S)
HEADING = re.compile(r"<h1[^>]*>\s*(.*?)\s*</h1>", re.S)


def prompt(page: str = "") -> str:
    return PROMPT.format(page=page or os.getenv("JARVIS_NOTION_DASHBOARDS") or DASHBOARDS_PAGE, clients=CLIENTS_DS)


def name_key(text: str) -> str:
    """Clé d'un nom de client, qui sert aussi d'identifiant dans Jarvis et l'orchestrator : sans accents, majuscules,
    parenthèses ni séparateurs (« APST 26/07 », « APST26-07 » → apst2607)."""
    return slugify(re.sub(r"\(.*?\)", "", text or "")).replace("-", "")


def fiche_key(name: str) -> str:
    """Clé d'une fiche « Clients en déploiement », sans son préfixe : « Déploiement Acme » / « Deployment Acme » → acme."""
    key = name_key(name)
    return next((key.removeprefix(p) for p in ("deploiement", "deployment") if key.startswith(p)), key)


def client_of(title: str) -> str:
    """Client d'un titre de dashboard : « Dashboard de déploiement - Acme (lot 1) » → « Acme (lot 1) »."""
    parts = re.split(r"\s+[-—–]\s+", (title or "").strip(), maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def full_link(title: str, url: str) -> str:
    """Lien complet d'une page Notion, comme « Copier le lien » : https://app.notion.com/p/<espace>/<Titre>-<id> (Notion
    remplace dans le titre tout ce qui n'est pas lettre ou chiffre sans accent par « - »). La lecture ne donne que l'id."""
    try:
        page = page_id_of(url)
    except ConfigError:
        return url
    words = re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9]", "-", title or "")).strip("-")
    return f"https://app.notion.com/p/{WORKSPACE}/{words}-{page}" if words else f"https://app.notion.com/p/{WORKSPACE}/{page}"


def dashboards_of(page_text: str) -> list[dict]:
    """Dashboards de la page, dans l'ordre : {famille, client, titre, url}. Les modèles (« Nom du Client ») sont ignorés."""
    found, family = [], None
    # Les sections et les pages, dans l'ordre où elles apparaissent.
    for match in sorted([*HEADING.finditer(page_text), *PAGE.finditer(page_text)], key=lambda m: m.start()):
        if match.re is HEADING:
            heading = name_key(match.group(1))
            family = next((f for word, f in SECTIONS if word in heading), None)
            continue
        url, title = match.group(1), " ".join(match.group(2).split())
        client = client_of(title)
        if family and client and is_notion(url) and "nom du client" not in client.lower() and name_key(client):
            found.append({"famille": family, "client": client, "titre": title, "url": full_link(title, url)})
    return found


def _has_parent(value) -> bool:
    """« élément parent » : liste d'adresses, en JSON (texte) ou déjà décodée ; vide ou absent : fiche principale."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip().startswith("[") else value.strip()
        except ValueError:
            return bool(value.strip())
    return bool(value)


def fiches_of(rows: list) -> list[dict]:
    """Lignes de « Clients en déploiement » : {nom, url, parent} (parent : sous-élément d'une autre fiche)."""
    return [{"nom": str(r.get("Nom") or "").strip(), "url": full_link(str(r.get("Nom") or ""), r["url"]),
             "parent": _has_parent(r.get("élément parent"))}
            for r in rows if isinstance(r, dict) and isinstance(r.get("url"), str) and str(r.get("Nom") or "").strip()]


def build(dashboards: list[dict], fiches: list[dict]) -> list[dict]:
    """Annuaire : un client par nom (name_key), avec ses dashboards et les fiches qui portent son nom.
    Nom affiché : celui de son dashboard de déploiement s'il en a un."""
    clients: dict[str, dict] = {}
    for d in dashboards:
        key = name_key(d["client"])
        entry = clients.setdefault(key, {"cle": key, "client": d["client"], "dashboards": []})
        if d["famille"] == "deploiement":
            entry["client"] = d["client"]
        entry["dashboards"].append({k: d[k] for k in ("famille", "titre", "url")})
    for entry in clients.values():
        # Le nom de la fiche commence par celui du client, éventuellement après « Déploiement » (« Déploiement Acme lot 2 ») :
        # « ab » ne prend pas « Lab… ».
        matching = [f for f in fiches if fiche_key(f["nom"]).startswith(entry["cle"])]
        entry["fiches"] = sorted(matching, key=lambda f: (f["parent"], len(f["nom"])))  # fiches principales d'abord
    return sorted(clients.values(), key=lambda e: e["client"].lower())


def form_of(entry: dict, orchestrator_keys=()) -> dict:
    """Valeurs du formulaire « Ajouter un client » pour un client de l'annuaire. Identifiant : le nom réduit à ses lettres et
    chiffres (« APST 26-07 » → apst2607), comme les clés de l'orchestrator ; clés orchestrator : celles du catalogue des DAGs
    qui commencent par lui (apst2607, apst2607-uegar…). Fiche : seulement si une seule fiche principale porte son nom (sinon
    c'est à choisir). Premier dashboard intégration : le dashboard du client ; les autres : des liens."""
    by_family: dict[str, list[dict]] = {}
    for d in entry["dashboards"]:
        by_family.setdefault(d["famille"], []).append(d)
    integration = by_family.get("integration", [])
    links = [(FAMILIES[f] if len(pages) == 1 and f != "integration" else d["titre"], d["url"])
             for f, pages in by_family.items() for d in (pages[1:] if f == "integration" else pages)]
    main = [f for f in entry.get("fiches", []) if not f["parent"]]
    exact = [f for f in main if fiche_key(f["nom"]) == entry["cle"]]  # « Déploiement Acme » pour « Acme »
    chosen = exact if len(exact) == 1 else main
    page_id = chosen[0]["url"] if len(chosen) == 1 else ""  # lien complet : le formulaire n'en garde que l'id
    keys = [k for k in sorted(orchestrator_keys) if name_key(k).startswith(entry["cle"])]
    return {"client": entry["client"], "slug": entry["cle"][:40], "page_id": page_id, "orchestrator": ", ".join(keys),
            "notion_url": integration[0]["url"] if integration else "",
            "dashboards": "\n".join(f"{label} | {url}" for label, url in links)}


def _notion_text(raw: str) -> str:
    """Texte d'une réponse de notion-fetch rangée par Claude Code : objet {"text": …}, liste de blocs {"text": …}, ou brut."""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(data, dict):
        return str(data.get("text") or raw)
    if isinstance(data, list):
        return "\n".join(_notion_text(b["text"]) for b in data if isinstance(b, dict) and isinstance(b.get("text"), str))
    return raw


def read_session(session: str, projects: Path = CLAUDE_PROJECTS) -> tuple[list[dict], list[dict]]:
    """(dashboards, fiches) lus dans les réponses Notion que Claude Code a rangées pour cette session."""
    files = saved_responses(session, "mcp-*Notion*.txt", projects)
    page = next((f for f in reversed(files) if "notion-fetch" in f.name), None)
    rows = []
    for f in files:
        if "notion-query-data-sources" in f.name:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            rows += data.get("results", []) if isinstance(data, dict) else []
    if page is None:
        raise ConfigError("Claude n'a pas lu la page des dashboards (aucune réponse Notion rangée).")
    return dashboards_of(_notion_text(page.read_text(encoding="utf-8"))), fiches_of(rows)


def _age(iso) -> timedelta | None:
    """Âge d'une date ISO ; None si elle manque ou est illisible."""
    try:
        return datetime.now() - datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


class ClientDirectory:
    """Annuaire (data/notion/annuaire.json) et sa relecture par Claude, une à la fois (annuaire-etat.json)."""

    def __init__(self, folder: Path | None = None, claude_bin: str | None = None, timeout_min: int = 6,
                 projects: Path = CLAUDE_PROJECTS):
        self.folder = folder or data_dir() / "notion"
        self._claude_bin = claude_bin
        self.timeout = timeout_min * 60
        self.projects = projects
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    @property
    def claude_bin(self) -> str:
        return self._claude_bin or os.getenv("CLAUDE_BIN") or "claude"

    def _read(self, name: str) -> dict:
        try:
            data = json.loads((self.folder / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, name: str, data: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def directory(self) -> dict:
        """{"lu": date ISO, "clients": [...], "fiches": [...]} ; vide tant qu'il n'a jamais été lu."""
        return self._read("annuaire.json")

    @staticmethod
    def entry(directory: dict, key: str) -> dict | None:
        return next((e for e in directory.get("clients", []) if e.get("cle") == key), None)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def state(self) -> dict:
        state = self._read("annuaire-etat.json")
        if state.get("running") and self._proc is None:  # lancée avant un redémarrage de Jarvis
            state.update(running=False, error="Lecture interrompue : Jarvis a redémarré pendant qu'elle tournait.")
        return state

    def should_refresh(self, directory: dict) -> bool:
        """Annuaire absent ou de plus d'un jour, aucune lecture en cours, et pas de tentative dans les 10 dernières minutes
        (une lecture qui échoue, ou Claude Code absent, n'est pas relancée chaque minute)."""
        read = _age(directory.get("lu")) if directory.get("format") == FORMAT else None  # autre format : comme jamais lu
        if self.running or (read is not None and read <= MAX_AGE):
            return False
        tried = _age(self.state().get("started"))  # lu seulement quand l'annuaire est à relire
        return tried is None or tried > timedelta(minutes=10)

    def refresh_if_stale(self, directory: dict | None = None) -> dict:
        """Relance la lecture si l'annuaire est à relire (boucle de fond de Jarvis, et page « Ajouter un client » en secours) ;
        renvoie l'annuaire tel qu'il est."""
        directory = self.directory() if directory is None else directory
        if self.should_refresh(directory):
            self.refresh()
        return directory

    def refresh(self) -> ActionResult:
        """Relit l'annuaire en arrière-plan (une vingtaine de secondes)."""
        if not shutil.which(self.claude_bin):
            message = (f"Claude Code en ligne de commande introuvable ({self.claude_bin}) : installe-le ou renseigne CLAUDE_BIN "
                       "dans Réglages, ou remplis le formulaire à la main.")
            # noté comme une tentative : pas de nouvel essai avant 10 minutes, et la page dit pourquoi
            self._write("annuaire-etat.json", {"running": False, "started": datetime.now().isoformat(timespec="seconds"), "error": message})
            return ActionResult(False, message, False)
        with self._lock:
            if self.running:
                return ActionResult(False, "La liste des clients est déjà en cours de lecture.", False)
            session = str(uuid.uuid4())
            env = {**app_env(), "MAX_MCP_OUTPUT_TOKENS": MCP_OUTPUT_TOKENS}  # bas : les réponses sont rangées dans des fichiers
            self._proc = subprocess.Popen(
                [self.claude_bin, "-p", INSTRUCTION, "--output-format", "text", "--permission-mode", "dontAsk",
                 "--session-id", session, "--allowedTools", *NOTION_SERVERS],
                cwd=PROJECT_ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            started = datetime.now().isoformat(timespec="seconds")
            self._write("annuaire-etat.json", {"running": True, "started": started})
        threading.Thread(target=self._wait, args=(self._proc, session, started), name="jarvis-annuaire", daemon=True).start()
        return ActionResult(True, "Lecture de la liste des clients dans Notion…", False)

    def _wait(self, proc: subprocess.Popen, session: str, started: str) -> None:
        try:
            out, _ = proc.communicate(prompt(), timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            return self._write("annuaire-etat.json", {"running": False, "started": started, "error": "Délai dépassé."})
        try:
            dashboards, fiches = read_session(session, self.projects)
        except ConfigError as e:
            return self._write("annuaire-etat.json", {"running": False, "started": started, "error": f"{e} {out.strip()[-200:]}"})
        if not dashboards:
            return self._write("annuaire-etat.json", {"running": False, "started": started,
                                                      "error": "Aucun dashboard client trouvé sur la page Notion."})
        self._write("annuaire.json", {"format": FORMAT, "lu": datetime.now().isoformat(timespec="seconds"), "clients": build(dashboards, fiches),
                                      "fiches": [f for f in fiches if not f["parent"]]})
        self._write("annuaire-etat.json", {"running": False, "started": started, "ok": True,
                                           "resume": f"{len({name_key(d['client']) for d in dashboards})} clients, {len(fiches)} fiches"})
