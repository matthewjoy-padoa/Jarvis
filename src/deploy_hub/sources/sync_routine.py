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
import uuid
from datetime import datetime
from pathlib import Path

from deploy_hub.config import ENV_FILE_KEYS, PROJECT_ROOT, data_dir
from deploy_hub.sources import ActionResult
from deploy_hub.sync_notion import SESSION_ENV

ROUTINE = PROJECT_ROOT / "sync" / "routine.md"
INSTRUCTION = "Suis la consigne reçue sur l'entrée standard (routine de synchro Notion de Jarvis)."
NOTION_SERVERS = ("mcp__claude_ai_Notion",)  # connecteur Notion de claude.ai (nom vu sur la VM le 2026-11-18)


def default_tools(data: Path) -> tuple[str, ...]:
    """Outils permis : les commandes de synchro (dépôt des grosses réponses compris : plus de « cp », que Claude Code refuse
    sur ~/.claude), la lecture et l'écriture du dossier de données (Edit : seule forme de règle à chemin que Claude Code
    applique aussi à Write ; Read pour un dossier de données placé hors du projet), Notion."""
    folder = f"/{data.resolve()}/**"
    return ("Bash(uv run python -m deploy_hub.sync_notion:*)", f"Read({folder})", f"Edit({folder})", *NOTION_SERVERS)


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
        self.folder.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

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
                *(["--session-id", session] if session else []), "--allowedTools", *self.allowed_tools]

    def _env(self) -> dict:
        """Environnement de l'app (mêmes dossiers, donc mêmes clients), sans les valeurs venues du .env : le processus de
        synchro relit le même .env lui-même (config.load_env_file), aucun secret ne transite par la ligne de commande."""
        return {k: v for k, v in os.environ.items() if k not in ENV_FILE_KEYS}

    def start(self) -> ActionResult:
        if (problem := self.problem()):
            return ActionResult(False, problem, False)
        with self._lock:
            if self.running:
                return ActionResult(False, "Une synchro est déjà en cours.", False)
            self.folder.mkdir(parents=True, exist_ok=True)
            log = self.log_path.open("w", encoding="utf-8")
            session = str(uuid.uuid4())
            self._proc = subprocess.Popen(self.command(session), cwd=PROJECT_ROOT, env={**self._env(), SESSION_ENV: session},
                                          stdin=subprocess.PIPE,
                                          stdout=log, stderr=subprocess.STDOUT, text=True)
            self._save(running=True, started=datetime.now().isoformat(timespec="seconds"))
        self._proc.stdin.write(routine_text())
        self._proc.stdin.close()
        threading.Thread(target=self._wait, args=(self._proc, log), name="jarvis-sync", daemon=True).start()
        return ActionResult(True, "Synchro Notion lancée : Claude lit Notion pour tous les clients de l'app. La page se mettra à jour à la fin.", False)

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
            return {"pct": 5, "label": "Empreinte de tous les clients…"}
        done = total = merged = 0
        for raw in (p for p in self.folder.iterdir() if p.is_dir()):
            plan = raw / "plan.json"
            if fresh(plan):
                try:
                    outputs = json.loads(plan.read_text(encoding="utf-8")).get("sorties", [])
                except (OSError, ValueError):
                    outputs = []
                total += len(outputs)
                done += sum((raw / f"{name}.json").is_file() for name in outputs)
            elif fresh(raw / "etat-synchro.json"):  # client fusionné : son plan est effacé
                merged += 1
        if merged:
            return {"pct": 92, "label": f"Fusion : {merged} client(s)…"}
        if not total:
            return {"pct": 15, "label": "Plan des requêtes…"}
        if done < total:
            return {"pct": 20 + 65 * done // total, "label": f"Requêtes : {done} / {total}"}
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
