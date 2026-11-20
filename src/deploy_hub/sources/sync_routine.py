"""Routine de synchro Notion → Jarvis lancée depuis l'app : le bouton « Rafraîchir depuis Notion », quand l'app n'a pas
de jeton Notion, fait suivre sync/routine.md à Claude Code en headless (`claude -p`), avec son connecteur Notion.

Les clients synchronisés sont ceux de l'app : le processus hérite de l'environnement de Jarvis (JARVIS_CONFIG_DIR,
JARVIS_DATA_DIR, JARVIS_ENV_FILE…) et tourne dans le dossier du projet, donc « sync_notion preparer » lit la même
configuration de clients que l'app. Une seule synchro à la fois ; son journal et son état restent dans le dossier de données.
"""
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from deploy_hub.safe_write import write_json
from deploy_hub.config import ENV_FILE_KEYS, PROJECT_ROOT, data_dir
from deploy_hub.sources import ActionResult
from deploy_hub.queries import ConfigError
from deploy_hub.sync_notion import CLIENT_ENV, SESSION_ENV, SyncError, first_queries

ROUTINE = PROJECT_ROOT / "sync" / "routine.md"
INSTRUCTION = "Suis la consigne reçue sur l'entrée standard (routine de synchro Notion de Jarvis)."
# Jarvis ouvre lui-même le passage avant de lancer Claude : la première requête part avec la consigne, un aller-retour en moins.
PREPARED = "\n\nL'ÉTAPE 1 EST DÉJÀ FAITE par Jarvis, ne la relance pas. Voici ce qu'elle a affiché : commence à l'étape 2.\n"
# Seuil (en tokens) au-delà duquel Claude Code range une réponse d'outil dans un fichier au lieu de la rendre à Claude.
# Bas exprès : chaque page Notion est rangée, puis déposée par « sync_notion deposer » en quelques millisecondes. Sinon
# Claude recopiait lui-même chaque page de 45 Ko, 2 minutes par page (vu le 2026-11-18 sur la VM).
MCP_OUTPUT_TOKENS = "2000"
NOTION_SERVERS = ("mcp__claude_ai_Notion",)  # connecteur Notion de claude.ai (nom vu sur la VM le 2026-11-18)
# Le modèle ne fait qu'exécuter les requêtes que Python lui donne, et Python vérifie celles qu'il a exécutées : un modèle
# rapide suffit. Mesuré sur la VM le 2026-11-20 : 80 % du temps d'une synchro était le modèle par défaut qui écrivait.
MODEL, EFFORT = "haiku", "low"  # réglages JARVIS_SYNC_MODEL et JARVIS_SYNC_EFFORT
# Outils intégrés gardés (Write : dépôt de secours d'une réponse ; ToolSearch : charge l'outil Notion à la demande). Sans
# ToolSearch, Claude Code charge d'emblée les outils de tous les connecteurs du compte : un aller-retour en moins, mais
# 180 000 tokens de contexte au lieu de 40 000 (mesuré sur la VM le 2026-11-20), trop près de la limite pour une lecture complète.
BUILTIN_TOOLS = "Bash,Write,ToolSearch"


def default_tools(data: Path) -> tuple[str, ...]:
    """Outils permis : les commandes de synchro (dépôt des grosses réponses compris : plus de « cp », que Claude Code refuse
    sur ~/.claude), la lecture et l'écriture du dossier de données (Edit : seule forme de règle à chemin que Claude Code
    applique aussi à Write ; Read pour un dossier de données placé hors du projet), Notion."""
    folder = f"/{data.resolve()}/**"
    return ("Bash(uv run python -m deploy_hub.sync_notion:*)", f"Read({folder})", f"Edit({folder})", *NOTION_SERVERS)


def app_env() -> dict:
    """Environnement de l'app (mêmes dossiers, donc mêmes clients), sans les valeurs venues du .env : un processus lancé
    par Jarvis relit le même .env lui-même (config.load_env_file), aucun secret ne transite par la ligne de commande."""
    return {k: v for k, v in os.environ.items() if k not in ENV_FILE_KEYS}


def routine_text() -> str:
    """Consigne de sync/routine.md, sans son commentaire d'en-tête."""
    text = ROUTINE.read_text(encoding="utf-8")
    return text.split("-->", 1)[1].strip() if text.startswith("<!--") else text.strip()


class SyncRoutine:
    """Lance la routine et garde son dernier état (data/notion/routine.json) et son journal (routine.log)."""

    def __init__(self, notifications=None, folder: Path | None = None, claude_bin: str | None = None,
                 allowed_tools: tuple[str, ...] = (), timeout_min: int = 30):
        self.notifications = notifications
        self.folder = folder or data_dir() / "notion"
        self._claude_bin = claude_bin
        self._allowed_tools = allowed_tools
        self.timeout = timeout_min * 60
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    # Relus à chaque lancement : un changement dans Réglages s'applique sans redémarrer l'app.
    @property
    def claude_bin(self) -> str:
        return self._claude_bin or os.getenv("CLAUDE_BIN") or "claude"

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return self._allowed_tools or tuple(t.strip() for t in os.getenv("JARVIS_SYNC_ALLOWED_TOOLS", "").split(";") if t.strip()) \
            or default_tools(self.folder.parent)

    @property
    def model(self) -> str:
        return os.getenv("JARVIS_SYNC_MODEL", "").strip() or MODEL

    @property
    def effort(self) -> str:
        return os.getenv("JARVIS_SYNC_EFFORT", "").strip() or EFFORT

    # --- état ---------------------------------------------------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return self.folder / "routine.json"

    @property
    def log_path(self) -> Path:
        return self.folder / "routine.log"

    def state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, **state) -> None:
        write_json(self.state_path, state)

    def _other(self) -> dict | None:
        """Synchro lancée par un autre processus (autre Jarvis, test) et toujours vivante, d'après routine.json."""
        state = self.state()
        if not state.get("running") or not state.get("pid"):
            return None
        try:
            os.kill(int(state["pid"]), 0)
        except (OSError, ValueError):
            return None
        return state

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def problem(self) -> str | None:
        """Ce qui empêche de lancer la routine (None : rien)."""
        if not shutil.which(self.claude_bin):
            return f"Claude Code en ligne de commande introuvable ({self.claude_bin}) : installe-le ou renseigne CLAUDE_BIN dans Réglages."
        if not ROUTINE.is_file():
            return f"Consigne absente : {ROUTINE}"
        return None

    # --- lancement ----------------------------------------------------------------------------------------------
    def command(self, session: str = "") -> list[str]:
        """--session-id : Jarvis connaît la session, donc le dossier où Claude Code range ses grosses réponses (« deposer »).
        --permission-mode dontAsk : tout ce qui n'est pas permis est refusé, jamais mis en attente."""
        return [self.claude_bin, "-p", INSTRUCTION, "--output-format", "text", "--permission-mode", "dontAsk",
                "--model", self.model, "--effort", self.effort, "--tools", BUILTIN_TOOLS,
                *(["--session-id", session] if session else []), "--allowedTools", *self.allowed_tools]

    def _env(self) -> dict:
        return app_env()

    def start(self, slug: str = "", client: str = "") -> ActionResult:
        """slug : ce client seulement (bouton de sa fiche, client : son nom pour le message) ; vide : tous les clients."""
        if (problem := self.problem()):
            return ActionResult(False, problem, False)
        with self._lock:
            if self.running:
                return ActionResult(False, "Une synchro est déjà en cours.", False)
            if (other := self._other()):
                return ActionResult(False, f"Une synchro tourne déjà, lancée à {other.get('started', '?')} par un autre Jarvis : "
                                           "attends sa fin.", False)
            self.folder.mkdir(parents=True, exist_ok=True)
            try:
                first = self._prepare(slug)
            except (SyncError, ConfigError, OSError, KeyError, ValueError, IndexError) as e:  # comme la ligne de commande
                return ActionResult(False, f"Synchro impossible : {e}", False)
            log = self.log_path.open("w", encoding="utf-8")
            session = str(uuid.uuid4())
            env = {**self._env(), SESSION_ENV: session, CLIENT_ENV: slug, "MAX_MCP_OUTPUT_TOKENS": MCP_OUTPUT_TOKENS}
            self._proc = subprocess.Popen(self.command(session), cwd=PROJECT_ROOT, env=env,
                                          stdin=subprocess.PIPE,
                                          stdout=log, stderr=subprocess.STDOUT, text=True)
            self._save(running=True, started=datetime.now().isoformat(timespec="seconds"), pid=self._proc.pid, session=session,
                       client=slug)
        self._proc.stdin.write(routine_text() + (PREPARED + json.dumps(first, ensure_ascii=False) if first else ""))
        self._proc.stdin.close()
        threading.Thread(target=self._wait, args=(self._proc, log), name="jarvis-sync", daemon=True).start()
        scope = f"pour {client or slug}" if slug else "pour tous les clients de l'app"
        return ActionResult(True, f"Synchro Notion lancée : Claude lit Notion {scope}. La page se mettra à jour à la fin.", False)

    def _prepare(self, slug: str) -> dict | None:
        """Première requête du passage, préparée ici quand la routine écrit dans le dossier de données de l'app (le cas
        du bouton) ; None sinon : la routine fait alors son étape 1 elle-même."""
        return first_queries(only=slug) if self.folder == data_dir() / "notion" else None

    def _wait(self, proc: subprocess.Popen, log) -> None:
        try:
            code = proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = None
        log.close()
        failures = self.failures()
        ok = code == 0 and not failures
        if code is None:
            reason = "délai dépassé"
        elif failures:
            reason = " · ".join(failures)
        else:
            reason = self.summary() or f"code de sortie {code}"
        self._save(running=False, started=self.state().get("started"), finished=datetime.now().isoformat(timespec="seconds"),
                   ok=ok, summary=self.summary() if ok else reason)
        if not ok and self.notifications is not None:
            self.notifications.add(f"sync-routine-fail:{datetime.now().isoformat(timespec='minutes')}", "sync", "urgent",
                                   "Synchro Notion en échec", reason[:300], "/clients")

    def progress(self) -> dict:
        """Avancement de la synchro en cours, lu dans les fichiers que la routine dépose (data/notion/) depuis son lancement :
        empreinte, plan de chaque client (ses requêtes), résultats déposés, fusion. {"pct": 0-100, "label": "..."}."""
        try:
            since = datetime.fromisoformat(self.state()["started"]).timestamp()
        except (KeyError, TypeError, ValueError):
            return {"pct": 0, "label": "Démarrage…"}
        fresh = lambda path: path.is_file() and path.stat().st_mtime >= since  # noqa: E731
        if not fresh(self.folder / "empreintes.json"):
            return {"pct": 5, "label": "Empreinte…"}
        done = total = merged = 0
        latest = None  # dernier résultat déposé : on montre sa page quand une requête en a plusieurs
        for raw in (p for p in self.folder.iterdir() if p.is_dir()):
            plan = raw / "plan.json"
            if fresh(plan):
                try:
                    outputs = json.loads(plan.read_text(encoding="utf-8")).get("sorties", [])
                except (OSError, ValueError):
                    outputs = []
                total += len(outputs)
                done += sum((raw / f"{name}.json").is_file() for name in outputs)
                for name in outputs:
                    for page in raw.glob(f"{name}*.json"):
                        if fresh(page) and (latest is None or page.stat().st_mtime > latest.stat().st_mtime):
                            latest = page
            elif fresh(raw / "etat-synchro.json"):  # client fusionné : son plan est effacé
                merged += 1
        if merged:
            return {"pct": 92, "label": f"Fusion : {merged} client(s)…"}
        if not total:
            return {"pct": 15, "label": "Plan des requêtes…"}
        number = latest.stem.rsplit("-", 1)[-1] if latest and "-" in latest.stem else ""
        paging = number.isdigit() and time.time() - latest.stat().st_mtime < 120  # une page vient d'arriver : la lecture continue
        if done < total or paging:
            label = f"Requêtes : {min(done, total - 1) if paging else done} / {total}"
            if paging:  # requête lue page par page (100 lignes) : la page en cours
                label += f" · {latest.stem.rsplit('-', 1)[0]}, page {int(number) + 1}"
            return {"pct": 20 + 65 * min(done, total - 1 if paging else done) // total, "label": label}
        return {"pct": 88, "label": "Fusion…"}

    def failures(self) -> list[str]:
        """Échecs de la dernière synchro, où qu'ils soient : erreurs par client du « plan » (fichier écrit par Python, sûr)
        et lignes « ÉCHEC » du compte rendu de Claude, en entier (pas seulement la fin)."""
        out = []
        try:
            started = datetime.fromisoformat(self.state()["started"]).timestamp()
            errors_file = self.folder / "plan-erreurs.json"
            if errors_file.is_file() and errors_file.stat().st_mtime >= started:
                out += [f"{slug} : {why}" for slug, why in json.loads(errors_file.read_text(encoding="utf-8")).items()]
        except (KeyError, TypeError, ValueError, OSError):
            pass
        try:
            for line in self.log_path.read_text(encoding="utf-8").splitlines():
                if "ÉCHEC" in line and not out:  # le fichier du plan, quand il existe, dit déjà la même chose, mieux
                    out.append(line.replace("**", "").strip(" -*·"))
        except OSError:
            pass
        return out

    def summary(self, lines: int = 8) -> str:
        """Fin du compte rendu de Claude (des comptes, jamais de lignes de données : voir la consigne)."""
        try:
            text = self.log_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])
