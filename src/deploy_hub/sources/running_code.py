"""Le code qui tourne est-il encore celui du dossier ? Une version plus récente est-elle publiée ?

Jarvis se met à jour par « git pull » dans son dossier, puis il faut relancer le serveur. Entre les deux, Python garde
l'ancien code en mémoire alors que les gabarits et les fichiers statiques sont relus sur le disque : la mise à jour n'est
pas appliquée, et une page dont le gabarit appelle une fonction pas encore chargée s'affiche en erreur. Ce module compare
le commit extrait au lancement à celui du moment ; main.py en fait un bandeau « Jarvis est à relancer », qui cite les
numéros de version (pyproject.toml, monté à chaque PR sauf correction) et les commits seulement quand le numéro n'a pas bougé.

Il regarde aussi, par « git fetch », si la branche suivie (main sur GitHub) a du code que le dossier n'a pas encore
(published) : bandeau « Nouvelle version disponible » sans avoir fait de git pull. Le bouton du bandeau met le dossier à
jour (main.py), synchronise les dépendances (sync_dependencies) puis redémarre Jarvis (restart) : arrêt propre du serveur,
comme un Ctrl-C, puis le même processus relance la même commande (même terminal, même service systemd)."""
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path

from deploy_hub.config import PROJECT_ROOT, START_ENVIRON
from deploy_hub.sources.flow_digger import (  # même git fetch, sans question, que pour Flow Digger
    _last_line,
    behind,
)

APP_PATHS = ("src", "pyproject.toml", "uv.lock")  # ce qui fait le code qui tourne ; docs, tests, CHANGELOG : sans effet
GIT_TIMEOUT = 10  # secondes
FETCH_TIMEOUT = 20  # secondes : git fetch passe par le réseau
SYNC_TIMEOUT = 300  # secondes : uv sync peut télécharger des paquets


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _version_in(text: str) -> str:
    try:
        return str(tomllib.loads(text)["project"]["version"])
    except (KeyError, TypeError, tomllib.TOMLDecodeError):
        return ""


def version(root: Path = PROJECT_ROOT) -> str:
    """Numéro de version écrit dans le pyproject.toml du dossier ("" s'il est illisible). Lu sur le disque, pas dans les
    métadonnées du paquet installé : celles-ci ne changent qu'au « uv sync », pas au « git pull »."""
    try:
        return _version_in((root / "pyproject.toml").read_text(encoding="utf-8"))
    except OSError:
        return ""


def head(root: Path = PROJECT_ROOT) -> str:
    """Commit extrait dans le dossier de Jarvis ; "" si ce n'est pas un dépôt git (ou si git manque)."""
    r = _git(root, "rev-parse", "HEAD")
    return r.stdout.strip() if r and r.returncode == 0 else ""


def newer_code(started: str, root: Path = PROJECT_ROOT) -> str:
    """Commit maintenant extrait, quand le code de l'app n'est plus celui du commit « started » (celui du lancement) :
    Jarvis est à relancer. "" sinon : même commit, seuls des fichiers sans effet ont changé, ou dossier hors de git."""
    now = head(root)
    if not started or not now or now == started:
        return ""
    diff = _git(root, "diff", "--quiet", started, now, "--", *APP_PATHS)
    return now if diff is not None and diff.returncode == 1 else ""


@dataclass(frozen=True)
class Published:
    commit: str = ""  # commit court de la branche suivie, quand elle a du code de l'app que le dossier n'a pas ; "" sinon
    version: str = ""  # son numéro de version (pyproject.toml de ce commit), "" s'il est illisible
    error: str = ""  # pourquoi on n'a pas pu regarder (réseau, droits sur le dépôt)


def published(root: Path = PROJECT_ROOT, timeout: float = FETCH_TIMEOUT) -> Published:
    """Après un « git fetch » : la branche suivie a-t-elle du code de l'app (APP_PATHS) que le dossier n'a pas ? Rien si
    le dossier ne suit aucune branche distante (HEAD détachée, dépôt local) ou si seuls des fichiers sans effet ont changé."""
    count, error = behind(root, timeout=timeout)
    if not count:
        return Published(error=error)
    diff = _git(root, "diff", "--quiet", "HEAD...@{upstream}", "--", *APP_PATHS)  # depuis l'ancêtre commun : ses commits à lui
    if diff is None or diff.returncode != 1:
        return Published()
    commit = _git(root, "rev-parse", "--short", "@{upstream}")
    toml = _git(root, "show", "@{upstream}:pyproject.toml")
    return Published(commit=commit.stdout.strip() if commit else "",
                      version=_version_in(toml.stdout) if toml and toml.returncode == 0 else "")


def sync_dependencies(started: str, root: Path = PROJECT_ROOT) -> str:
    """« uv sync --frozen » quand uv.lock ou pyproject.toml ont changé depuis le commit « started » (celui du lancement) :
    le redémarrage relance Python directement, sans repasser par « uv run ». "" si c'est fait, inutile ou impossible
    ici (sans uv, hors environnement virtuel) ; sinon la raison de l'échec."""
    unchanged = _git(root, "diff", "--quiet", started, "HEAD", "--", "uv.lock", "pyproject.toml") if started else None
    if unchanged is not None and unchanged.returncode == 0:
        return ""
    uv = os.getenv("UV") or shutil.which("uv")  # UV : posé par « uv run »
    if not uv or sys.prefix == sys.base_prefix:
        return ""
    try:  # --active : l'environnement de ce Python, celui que le redémarrage relance (le .venv du dossier avec « uv run »)
        r = subprocess.run([uv, "sync", "--frozen", "--active"], cwd=root, capture_output=True, text=True, timeout=SYNC_TIMEOUT,
                           check=False, stdin=subprocess.DEVNULL, env={**os.environ, "VIRTUAL_ENV": sys.prefix})
    except subprocess.TimeoutExpired:
        return f"uv sync n'a pas fini en {SYNC_TIMEOUT} s"
    except OSError as e:
        return f"uv introuvable ({e.strerror or e})"
    return "uv sync a échoué : " + _last_line(r) if r.returncode else ""


def reloads_itself() -> bool:
    """Lancé avec « uvicorn --reload » : ce processus est un enfant du surveillant, qui le relance seul quand src/ change."""
    return multiprocessing.parent_process() is not None


_requested = threading.Event()


def restart(delay: float = 0.5) -> None:
    """Redémarre Jarvis : dans « delay » secondes (le temps que la réponse parte), le même signal qu'un Ctrl-C arrête le
    serveur proprement (requêtes finies, puis la sortie de lifespan : suivis des DAG coupés pour être repris), et la fin
    de lifespan appelle exec_if_requested. Une seule fois (main.jarvis_update vérifie restarting) : un second signal
    forcerait l'arrêt sans relance."""
    _requested.set()
    timer = threading.Timer(delay, os.kill, (os.getpid(), signal.SIGINT))
    timer.daemon = True
    timer.start()


def exec_if_requested() -> None:
    """Fin de lifespan : après restart(), le processus se remplace par la même commande que celle qui l'a lancé
    (sys.orig_argv), avec l'environnement d'origine (config.START_ENVIRON). Même PID : le terminal, « uv run » ou systemd
    qui l'attendent ne voient rien partir. Le port est déjà libéré (uvicorn ferme son écoute avant lifespan)."""
    if not _requested.is_set():
        return
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execve(sys.executable, sys.orig_argv, START_ENVIRON)
    except OSError as e:
        print(f"Jarvis : redémarrage impossible ({e}) : relance-le à la main.", file=sys.stderr, flush=True)


def restarting() -> bool:
    """Redémarrage déjà demandé : le serveur est en train de s'arrêter."""
    return _requested.is_set()
