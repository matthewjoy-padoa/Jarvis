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
DEFAULT_PORT = 4004  # port par défaut de « main.py --start »
_starting = threading.Lock()  # deux clics rapprochés : le second attend le premier au lieu de lancer un second serveur


@dataclass(frozen=True)
class Launch:
    url: str = ""     # où aller quand tout va bien
    error: str = ""   # sinon, pourquoi


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


def start(wait: float = 20) -> Launch:
    """Démarre Flow Digger s'il ne répond pas, attend qu'il écoute ; ne lance jamais un second serveur."""
    with _starting:
        return _start(wait)


def _start(wait: float) -> Launch:
    if running():
        return Launch(url=url())
    repo = folder()
    if not repo:
        return Launch(error="Flow Digger introuvable : clone padoa/integration-flow-digger sur la VM "
                            f"(par exemple dans ~/flow-digger) ou indique son dossier dans FLOW_DIGGER_DIR (.env de Jarvis).")
    log = data_dir() / "flow-digger.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open("ab") as out:  # détaché : survit à un redémarrage de Jarvis
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
