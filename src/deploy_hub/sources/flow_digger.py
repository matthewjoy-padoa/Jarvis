"""Flow Digger (padoa/integration-flow-digger) lancé depuis Jarvis : un bouton qui démarre son interface web sur la VM
si elle ne tourne pas déjà, puis y emmène. À chaque clic, Jarvis regarde d'abord si la branche suivie a des commits
qu'il n'a pas (git fetch) : arrêté, il ne tire que s'il y en a ; déjà lancé, il propose de mettre à jour et relancer,
sans jamais couper seul un Flow Digger en cours d'utilisation.

Jarvis ne lit ni ne modifie rien de Flow Digger (configs, secrets, fichiers de flux) : il lance seulement
`uv run --frozen --project <dossier> <dossier>/main.py --start <port> --no-browser`, détaché, comme l'alias `digg` du README.
"""

import os
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from deploy_hub.config import child_environment, data_dir
from deploy_hub.safe_write import write_text

CANDIDATES = ("~/Dev/Apps/integration-flow-digger", "~/Dev/Apps/flow-digger", "~/flow-digger", "~/Dev/integration-flow-digger",
              "~/integration-flow-digger", "~/Dev/flow-digger")
PULL_TIMEOUT = 30  # s, pour chaque commande git (fetch, mise à jour)
CHECK_TIMEOUT = 8  # s : fetch quand Flow Digger tourne déjà ; trop lent (réseau), on ouvre la version en service
PID_FILE = "flow-digger.pid"  # data/flow-digger.pid : processus lancé par Jarvis, pour pouvoir le relancer
DEFAULT_PORT = 4004  # port par défaut de « main.py --start »
_starting = threading.Lock()  # deux clics rapprochés : le second attend le premier au lieu de lancer un second serveur


@dataclass(frozen=True)
class Launch:
    url: str = ""     # où aller quand tout va bien
    error: str = ""   # sinon, pourquoi
    can_force: bool = False  # échec du pull seulement : on peut lancer quand même la version actuelle
    behind: int = 0  # déjà lancé, mais la branche suivie a ce nombre de commits de plus : mise à jour proposée
    can_restart: bool = False  # ce Flow Digger a été lancé par Jarvis : il peut l'arrêter pour le relancer à jour


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
    return child_environment()


def _git(repo: Path, *args: str, timeout: float = PULL_TIMEOUT) -> subprocess.CompletedProcess:
    # Jamais de question dans le terminal de Jarvis (identifiants, hôte inconnu) : la commande échoue au lieu d'attendre.
    env = {**_environment(), "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": os.getenv("GIT_SSH_COMMAND") or "ssh -o BatchMode=yes"}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout, env=env,
                          stdin=subprocess.DEVNULL)


def _last_line(result: subprocess.CompletedProcess) -> str:
    lines = [l for l in (result.stderr or result.stdout).strip().splitlines() if l.strip()]
    return (lines[-1] if lines else f"code {result.returncode}")[:300]


def behind(repo: Path, timeout: float = PULL_TIMEOUT) -> tuple[int | None, str]:
    """Commits de la branche suivie que le dossier n'a pas encore, après un « git fetch » : (n, ""). (None, "") si la
    branche ne suit aucune branche distante (ou HEAD détachée) ; (None, raison) si le fetch a échoué."""
    try:
        if _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}").returncode:
            return None, ""
        fetch = _git(repo, "fetch", "--quiet", timeout=timeout)
        if fetch.returncode:
            return None, "git fetch a échoué : " + _last_line(fetch)
        count = _git(repo, "rev-list", "--count", "HEAD..@{upstream}")
    except subprocess.TimeoutExpired:
        return None, f"git fetch n'a pas répondu en {timeout:.0f} s (réseau ?)"
    except OSError as e:
        return None, f"git introuvable ({e.strerror or e})"
    return int(count.stdout.strip() or 0), ""


def update(repo: Path) -> tuple[bool, str]:
    """Met le dépôt à jour avant le démarrage, seulement si la branche suivie a des commits qu'il n'a pas : avance
    rapide (« git merge --ff-only »), jamais de fusion ni de mise à jour forcée. (True, "a1b2c3d → e4f5a6b") si des
    commits sont arrivés, (True, "") si rien de neuf ou pas de branche suivie, (False, raison) en cas d'échec (réseau,
    branche divergente, modifications locales en conflit…). Sans rien de neuf, des modifications locales ne gênent pas."""
    count, error = behind(repo)
    if error:
        return False, error
    if not count:
        return True, ""
    try:
        before, after = (_git(repo, "rev-parse", "--short", ref).stdout.strip() for ref in ("HEAD", "@{upstream}"))
        merge = _git(repo, "merge", "--ff-only", "--quiet", "@{upstream}")
    except subprocess.TimeoutExpired:
        return False, f"git merge n'a pas répondu en {PULL_TIMEOUT} s"
    except OSError as e:
        return False, f"git introuvable ({e.strerror or e})"
    if merge.returncode:
        return False, "mise à jour impossible sans fusion (git merge --ff-only) : " + _last_line(merge)
    return True, f"{before} → {after}"


def _pid_path() -> Path:
    return data_dir() / PID_FILE


def _launched_pid(repo: Path) -> int | None:
    """Processus du Flow Digger lancé par Jarvis, s'il tourne toujours : le numéro noté au lancement, vérifié sur sa
    commande (un numéro réutilisé par un autre programme n'est jamais pris pour Flow Digger). None sinon (lancé à la
    main, par l'alias digg, ou arrêté)."""
    try:
        pid = int(_pid_path().read_text())
        args = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return pid if "--start" in args and str(repo / "main.py") in args else None


def stop(repo: Path, wait: float = 10) -> bool:
    """Arrête le Flow Digger lancé par Jarvis (tout son groupe de processus) et attend que son port se libère."""
    pid = _launched_pid(repo)
    if pid is None:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return False
    end = time.monotonic() + wait
    while running() and time.monotonic() < end:
        time.sleep(0.2)
    if running():
        return False
    _pid_path().unlink(missing_ok=True)
    return True


def start(wait: float = 20, force: bool = False, restart: bool = False, current: bool = False) -> Launch:
    """Démarre Flow Digger s'il ne répond pas, attend qu'il écoute ; ne lance jamais un second serveur.
    Avant de le démarrer, met son dépôt à jour s'il y a du nouveau ; si la mise à jour échoue, ne lance rien sauf avec
    force=True. Déjà lancé : y emmène, sauf si la branche suivie a avancé (Launch.behind : mise à jour proposée).
    restart=True (choix sur cette proposition) : arrête celui lancé par Jarvis, met à jour et relance ;
    current=True : y emmène sans rien vérifier."""
    if running() and not restart:  # déjà lancé : on y emmène, hors du verrou (le fetch ne retient pas un second clic)
        return _open(folder(), check=not current)
    with _starting:
        return _start(wait, force, restart)


def _open(repo: Path | None, check: bool) -> Launch:
    """Flow Digger déjà lancé : y emmène, avec la mise à jour proposée si sa branche a avancé. Fetch trop lent ou en
    échec (réseau) : on ouvre la version en service, sans bloquer."""
    count = behind(repo, timeout=CHECK_TIMEOUT)[0] if check and repo else 0
    return Launch(url=url(), behind=count or 0, can_restart=bool(count) and _launched_pid(repo) is not None)


def _start(wait: float, force: bool = False, restart: bool = False) -> Launch:
    repo = folder()
    if running():
        if not restart or not repo:
            return Launch(url=url())  # lancé pendant qu'on attendait le verrou
        if not stop(repo):
            return Launch(error="Flow Digger n'a pas pu être arrêté pour la mise à jour : il a sans doute été lancé hors de "
                                "Jarvis (alias digg). Arrête-le (Ctrl+C dans son terminal), puis reclique sur Flow Digger.")
    if not repo:
        return Launch(error="Flow Digger introuvable : clone padoa/integration-flow-digger sur la VM "
                            f"(par exemple dans ~/flow-digger) ou indique son dossier dans FLOW_DIGGER_DIR (Réglages).")
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
            proc = subprocess.Popen(command(repo), cwd=repo, env=_environment(), stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        write_text(_pid_path(), str(proc.pid))  # pour pouvoir le relancer à jour (stop)
    except OSError as e:
        return Launch(error=f"Impossible de lancer Flow Digger ({e.strerror or e}) : uv est-il installé sur la VM ?")
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if running():
            return Launch(url=url())
        time.sleep(0.3)
    return Launch(error=f"Flow Digger ne répond pas sur le port {port()} après {wait:.0f} s : son journal est dans {log}.")
