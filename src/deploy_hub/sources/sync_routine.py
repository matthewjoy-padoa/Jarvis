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
from deploy_hub.sync_notion import CLIENT_ENV, PASSAGE, SESSION_ENV, SyncError, first_queries, passage_progress

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

    def start(self, slugs: str | list[str] = "", client: str = "", auto: bool = False) -> ActionResult:
        """slugs : ce client seulement (bouton de sa fiche, client : son nom pour le message), ou ceux de la liste (synchro
        automatique) ; vide : tous les clients. La liste n'est jointe qu'ici, pour la variable lue par « preparer ».
        auto : synchro lancée par l'app elle-même (toutes les heures) ; ses échecs ne sont signalés qu'une fois par jour."""
        if (problem := self.problem()):
            return ActionResult(False, problem, False)
        slugs = [slugs] if isinstance(slugs, str) and slugs else list(slugs)
        slug = ",".join(slugs)
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
        threading.Thread(target=self._wait, args=(self._proc, log, auto), name="jarvis-sync", daemon=True).start()
        scope = f"pour {client or ', '.join(slugs)}" if slugs else "pour tous les clients de l'app"
        return ActionResult(True, f"Synchro Notion lancée : Claude lit Notion {scope}. La page se mettra à jour à la fin.", False)

    def _prepare(self, slug: str) -> dict | None:
        """Première requête du passage, préparée ici quand la routine écrit dans le dossier de données de l'app (le cas
        du bouton) ; None sinon : la routine fait alors son étape 1 elle-même."""
        return first_queries(only=slug) if self.folder == data_dir() / "notion" else None

    def _wait(self, proc: subprocess.Popen, log, auto: bool = False) -> None:
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
            when = f"auto:{datetime.now():%Y-%m-%d}" if auto else datetime.now().isoformat(timespec="minutes")
            self.notifications.add(f"sync-routine-fail:{when}", "sync", "urgent",
                                   "Synchro Notion en échec", reason[:300], "/clients")

    def progress(self) -> dict:
        """Avancement de la synchro en cours, {"pct": 0-100, "label": "..."}, d'après son passage (data/notion/passage.json,
        ouvert juste avant le lancement) : les réponses de Notion sont comptées dans la session de la routine dès qu'elles
        arrivent. « suite » ne les range qu'ensuite, toutes ensemble : lu dans les fichiers déposés, l'avancement restait
        à 20 % puis passait à « terminé » (vu le 2026-11-20)."""
        state = self.state()
        path = self.folder / PASSAGE
        try:
            since = datetime.fromisoformat(state["started"]).timestamp()
            if not state.get("session") or path.stat().st_mtime < since - 30:  # pas de passage de cette synchro
                return {"pct": 3, "label": "Démarrage…"}
            step, done, total = passage_progress(json.loads(path.read_text(encoding="utf-8")), state["session"])
        except (OSError, ValueError, KeyError, TypeError):
            return {"pct": 3, "label": "Démarrage…"}
        if step == "fini":
            return {"pct": 97, "label": "Terminé : la page va se recharger…"}
        if step == "empreinte":
            return {"pct": 15, "label": "Plan des requêtes…"} if total and done >= total else {"pct": 8, "label": "Empreinte…"}
        if done < total:
            return {"pct": 20 + 65 * done // total, "label": f"Requêtes : {done} / {total}"}
        return {"pct": 90, "label": "Fusion…"}

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
