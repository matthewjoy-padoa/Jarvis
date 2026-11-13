"""Flow Digger (padoa/integration-flow-digger) lancé depuis Jarvis : un bouton qui démarre son interface web sur la VM
si elle ne tourne pas déjà, puis y emmène.

Jarvis ne lit ni ne modifie rien de Flow Digger (configs, secrets, fichiers de flux) : il lance seulement
`uv run --frozen --project <dossier> <dossier>/main.py --start <port> --no-browser`, détaché, comme l'alias `digg` du README.
"""

import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from deploy_hub.config import ENV_FILE_KEYS, data_dir
from deploy_hub.sources.claude_runner import SECRETS

CANDIDATES = ("~/Dev/Apps/integration-flow-digger", "~/Dev/Apps/flow-digger", "~/flow-digger", "~/Dev/integration-flow-digger",
              "~/integration-flow-digger", "~/Dev/flow-digger")
PULL_TIMEOUT = 30  # s
DEFAULT_PORT = 4004  # port par défaut de « main.py --start »
_starting = threading.Lock()  # deux clics rapprochés : le second attend le premier au lieu de lancer un second serveur


@dataclass(frozen=True)
class Launch:
    url: str = ""     # où aller quand tout va bien
    error: str = ""   # sinon, pourquoi
    can_force: bool = False  # échec du pull seulement : on peut lancer quand même la version actuelle


def folder() -> Path | None:
    """Dossier du dépôt : FLOW_DIGGER_DIR, sinon le premier emplacement habituel qui contient main.py."""
    paths = [os.environ["FLOW_DIGGER_DIR"]] if os.getenv("FLOW_DIGGER_DIR") else CANDIDATES
    return next((p for p in (Path(c).expanduser() for c in paths) if (p / "main.py").is_file()), None)


def port() -> int:
    return int(os.getenv("FLOW_DIGGER_PORT") or DEFAULT_PORT)


def url() -> str:
    """Adresse ouverte dans le navigateur ; FLOW_DIGGER_URL si le transfert de port de VS Code a pris un autre port."""
    return os.getenv("FLOW_DIGGER_URL") or f"http://localhost:{port()}"


def running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port()), timeout=0.5):
            return True
    except OSError:
        return False


def command(repo: Path) -> list[str]:
    return ["uv", "run", "--frozen", "--project", str(repo), str(repo / "main.py"), "--start", str(port()), "--no-browser"]


def _environment() -> dict[str, str]:
    """Environnement de Jarvis sans ses secrets ni ses réglages (.env) : Flow Digger a les siens (1Password, Google)."""
    return {k: v for k, v in os.environ.items() if k not in SECRETS and k not in ENV_FILE_KEYS}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # Jamais de question dans le terminal de Jarvis (identifiants, hôte inconnu) : la commande échoue au lieu d'attendre.
    env = {**_environment(), "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": os.getenv("GIT_SSH_COMMAND") or "ssh -o BatchMode=yes"}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=PULL_TIMEOUT, env=env,
                          stdin=subprocess.DEVNULL)


def update(repo: Path) -> tuple[bool, str]:
    """Met le dépôt à jour avant le démarrage : « git pull --ff-only », jamais de fusion ni de pull forcé.
    (True, "a1b2c3d → e4f5a6b") si des commits sont arrivés, (True, "") si rien de neuf ou pas de branche suivie,
    (False, raison) si le pull a échoué (réseau, branche divergente, modifications locales en conflit…)."""
    try:
        if _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}").returncode:
            return True, ""  # branche sans suivi distant (ou HEAD détachée) : rien à tirer
        before = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
        pull = _git(repo, "pull", "--ff-only", "--quiet")
    except subprocess.TimeoutExpired:
        return False, f"git pull n'a pas répondu en {PULL_TIMEOUT} s (réseau ?)"
    except OSError as e:
        return False, f"git introuvable ({e.strerror or e})"
    if pull.returncode:
        lines = [l for l in (pull.stderr or pull.stdout).strip().splitlines() if l.strip()]
        return False, "git pull a échoué : " + (lines[-1] if lines else f"code {pull.returncode}")[:300]
    after = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    return True, f"{before} → {after}" if before != after else ""


def start(wait: float = 20, force: bool = False) -> Launch:
    """Démarre Flow Digger s'il ne répond pas, attend qu'il écoute ; ne lance jamais un second serveur.
    Avant de le démarrer, met son dépôt à jour (git pull) ; si le pull échoue, ne lance rien sauf avec force=True."""
    with _starting:
        return _start(wait, force)


def _start(wait: float, force: bool = False) -> Launch:
    if running():
        return Launch(url=url())  # déjà lancé : pas de pull, on ne coupe pas un travail en cours
    repo = folder()
    if not repo:
        return Launch(error="Flow Digger introuvable : clone padoa/integration-flow-digger sur la VM "
                            f"(par exemple dans ~/flow-digger) ou indique son dossier dans FLOW_DIGGER_DIR (.env de Jarvis).")
    updated = ""
    if not force:
        ok, detail = update(repo)
        if not ok:
            return Launch(error=f"Mise à jour de Flow Digger impossible, il n'a pas été lancé. {detail}", can_force=True)
        updated = detail
    log = data_dir() / "flow-digger.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open("ab") as out:  # détaché : survit à un redémarrage de Jarvis
            note = f"mis à jour {updated}" if updated else ("sans mise à jour (lancé quand même)" if force else "déjà à jour")
            out.write(f"--- Jarvis : démarrage de Flow Digger, {note} ---\n".encode())
            out.flush()
            subprocess.Popen(command(repo), cwd=repo, env=_environment(), stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        return Launch(error=f"Impossible de lancer Flow Digger ({e.strerror or e}) : uv est-il installé sur la VM ?")
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if running():
            return Launch(url=url())
        time.sleep(0.3)
    return Launch(error=f"Flow Digger ne répond pas sur le port {port()} après {wait:.0f} s : son journal est dans {log}.")
