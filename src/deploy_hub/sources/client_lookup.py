"""Ajouter un client par son seul nom : Claude Code (`claude -p`, connecteur Notion, lecture seule) lit la page « Liste des
dashboards pour le déploiement des nouveaux clients », garde les dashboards du client et trouve sa fiche dans « Clients en
déploiement ». Jarvis en tire une configuration à relire avant l'ajout (page « Ajouter un client »).

Une recherche à la fois ; son état et sa réponse restent dans data/notion/recherche-client.json.
"""
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from deploy_hub.client_config import is_notion, slugify
from deploy_hub.config import PROJECT_ROOT, data_dir
from deploy_hub.queries import ConfigError, page_id_of
from deploy_hub.sources import ActionResult
from deploy_hub.sources.notion_api import CLIENTS_DS  # « Clients en déploiement » : la fiche du client (page_id)
from deploy_hub.sources.sync_routine import NOTION_SERVERS, app_env

DASHBOARDS_PAGE = "daf37a4f4c5b4288b0737f50442504c3"  # « Liste des dashboards pour le déploiement des nouveaux clients »
INSTRUCTION = "Suis la consigne reçue sur l'entrée standard (recherche d'un client dans Notion pour Jarvis)."
FAMILIES = {"deploiement": "Déploiement", "integration": "Intégration", "formation": "Formation",
            "gestion_financiere": "Gestion financière"}
ANSWER = re.compile(r"<client>\s*(.*?)\s*</client>", re.S)

PROMPT = """Tu aides Jarvis à configurer un client à partir de son seul nom. Lecture seule : ne modifie rien dans Notion.

Client cherché : « {name} »

1. Lis la page Notion {page} (« Liste des dashboards pour le déploiement des nouveaux clients ») avec notion-fetch. Elle range
   des liens vers des dashboards par famille : Déploiement (« Dashboard de déploiement - X », « Deployment dashboard - X »),
   Formation (« Dashboard de formation - X »), Intégration (« Dashboard intégration - X », « Data Migration Dashboard - X »),
   Gestion financière (« Dashboard de Gestion financière - X »). Ignore les dashboards de test et les modèles (« Nom du Client »).
2. Garde les dashboards dont le client X est « {name} ». Compare sans tenir compte des majuscules, accents, espaces, tirets,
   barres obliques ni de ce qui est entre parenthèses : « Acme 12-34 », « ACME12-34 » et « Acme 12/34 » sont le même client.
   Si aucun client ne correspond, ou si plusieurs clients différents correspondent, n'invente rien : laisse "dashboards" vide
   et mets les noms de clients proches dans "candidats".
3. Fiche du client dans « Clients en déploiement » (collection://{clients}) : lis son dashboard de déploiement ; sa première
   base est une vue de cette base, filtrée sur le nom du client (notion-fetch sur l'adresse de la vue pour lire le filtre).
   Cherche la ligne correspondante avec notion-query-data-sources (SELECT url, "Nom" FROM "collection://{clients}"
   WHERE "Nom" LIKE …). Sans dashboard de déploiement, cherche une ligne dont le « Nom » désigne le client. S'il y en a
   plusieurs, garde celle qui n'a pas d'« élément parent ». Si tu n'en trouves pas, laisse "fiche" vide et dis-le.
4. Réponds uniquement par un bloc <client>…</client> qui ne contient que du JSON valide :
<client>
{{"nom": "nom du client tel qu'écrit dans Notion", "fiche": "adresse de sa fiche dans Clients en déploiement, ou vide",
 "dashboards": [{{"famille": "deploiement | integration | formation | gestion_financiere", "titre": "titre de la page",
 "url": "adresse de la page"}}], "candidats": [], "remarque": "ce qui a posé problème, sinon vide"}}
</client>
"""


def prompt(name: str, page: str = "") -> str:
    return PROMPT.format(name=name, page=page or os.getenv("JARVIS_NOTION_DASHBOARDS") or DASHBOARDS_PAGE, clients=CLIENTS_DS)


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def proposal(answer: str) -> dict:
    """Configuration proposée depuis la réponse de Claude (dernier bloc <client>) : valeurs du formulaire « Ajouter un client »
    (client, slug, page_id, notion_url, dashboards au format « libellé | adresse ») plus candidats et remarque.
    Lève ConfigError si la réponse est absente ou illisible. Seules les adresses Notion sont gardées."""
    blocks = ANSWER.findall(answer or "")
    try:
        data = json.loads(re.sub(r"^```(?:json)?|```$", "", blocks[-1].strip()).strip()) if blocks else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ConfigError("Claude n'a pas rendu de réponse lisible : relance la recherche, ou remplis le formulaire à la main.")
    def listed(key: str) -> list:
        return value if isinstance(value := data.get(key), list) else []

    found: dict[str, list[tuple[str, str]]] = {}  # famille → (titre, adresse)
    for d in listed("dashboards"):
        if isinstance(d, dict) and _text(d.get("famille")) in FAMILIES and is_notion(_text(d.get("url"))):
            found.setdefault(_text(d["famille"]), []).append((_text(d.get("titre")), _text(d["url"])))
    integration = found.get("integration", [])
    # Le premier dashboard intégration devient le dashboard du client (notion_url) ; les autres, des liens. Une famille à une
    # seule page prend le nom de la famille, une famille à plusieurs pages (lots) garde leurs titres.
    links = [(FAMILIES[f] if len(pages) == 1 and f != "integration" else title or FAMILIES[f], url)
             for f, pages in found.items() for title, url in (pages[1:] if f == "integration" else pages)]
    try:
        page_id = page_id_of(_text(data.get("fiche"))) if _text(data.get("fiche")) else ""
    except ConfigError:
        page_id = ""
    name = _text(data.get("nom"))
    return {"client": name, "slug": slugify(name) if name else "", "page_id": page_id,
            "notion_url": integration[0][1] if integration else "",
            "dashboards": "\n".join(f"{label} | {url}" for label, url in links),
            "candidats": [c for c in map(_text, listed("candidats")) if c],
            "remarque": _text(data.get("remarque"))}


def result_of(out: str) -> str:
    """Réponse de Claude dans la sortie de `--output-format json` (dernière ligne JSON, après d'éventuels avertissements)."""
    for line in reversed((out or "").splitlines()):
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and isinstance(data.get("result"), str):
                return data["result"]
    return out or ""


class ClientLookup:
    """Lance la recherche et garde son dernier état ; mêmes réglages que la routine de synchro (CLAUDE_BIN, dossier, .env)."""

    def __init__(self, folder: Path | None = None, claude_bin: str | None = None, timeout_min: int = 6):
        self.folder = folder or data_dir() / "notion"
        self._claude_bin = claude_bin
        self.timeout = timeout_min * 60
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    @property
    def claude_bin(self) -> str:
        return self._claude_bin or os.getenv("CLAUDE_BIN") or "claude"

    @property
    def state_path(self) -> Path:
        return self.folder / "recherche-client.json"

    def state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if data.get("running") and self._proc is None:  # lancée avant un redémarrage de Jarvis : plus personne ne l'attend
            data.update(running=False, ok=False, error="Recherche interrompue : Jarvis a redémarré pendant qu'elle tournait. Relance-la.")
        return data

    def _save(self, **state) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @staticmethod
    def found(state: dict) -> bool:
        """Recherche finie, qui a trouvé le client : le formulaire peut être pré-rempli."""
        return bool(state.get("ok")) and not state.get("running")

    def command(self) -> list[str]:
        """Seulement le connecteur Notion ; dontAsk : tout le reste est refusé, jamais mis en attente."""
        return [self.claude_bin, "-p", INSTRUCTION, "--output-format", "json", "--permission-mode", "dontAsk",
                "--allowedTools", *NOTION_SERVERS]

    def start(self, name: str) -> ActionResult:
        name = (name or "").strip()
        if not name:
            return ActionResult(False, "Écris le nom du client.", False)
        if not shutil.which(self.claude_bin):
            return ActionResult(False, f"Claude Code en ligne de commande introuvable ({self.claude_bin}) : installe-le ou "
                                       "renseigne CLAUDE_BIN dans Réglages, ou remplis le formulaire à la main.", False)
        with self._lock:
            if self.running:
                return ActionResult(False, "Une recherche est déjà en cours : attends sa fin.", False)
            self._proc = subprocess.Popen(self.command(), cwd=PROJECT_ROOT, env=app_env(), stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self._save(running=True, name=name, started=datetime.now().isoformat(timespec="seconds"))
        threading.Thread(target=self._wait, args=(self._proc, name, self.state().get("started")), name="jarvis-client-lookup",
                         daemon=True).start()
        return ActionResult(True, f"Claude cherche « {name} » dans Notion…", False)

    def _wait(self, proc: subprocess.Popen, name: str, started: str) -> None:
        try:
            out, _ = proc.communicate(prompt(name), timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            return self._save(running=False, name=name, started=started, ok=False, error="Délai dépassé : relance la recherche.")
        answer = result_of(out)
        try:
            found = proposal(answer)
        except ConfigError as e:
            return self._save(running=False, name=name, started=started, ok=False, error=f"{e} ({(answer or '').strip()[-200:]})")
        self._save(running=False, name=name, started=started, ok=bool(found["client"] and (found["page_id"] or found["dashboards"]
                                                                                             or found["notion_url"])),
                   proposal=found)
