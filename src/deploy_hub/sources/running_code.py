"""Le code qui tourne est-il encore celui du dossier ?

Jarvis se met à jour par « git pull » dans son dossier, puis il faut relancer le serveur. Entre les deux, Python garde
l'ancien code en mémoire alors que les gabarits et les fichiers statiques sont relus sur le disque : la mise à jour n'est
pas appliquée, et une page dont le gabarit appelle une fonction pas encore chargée s'affiche en erreur. Ce module compare
le commit extrait au lancement à celui du moment ; main.py en fait un bandeau « Jarvis est à relancer », qui cite les
numéros de version (pyproject.toml, monté à chaque PR sauf correction) et les commits seulement quand le numéro n'a pas bougé."""
import subprocess
import tomllib
from pathlib import Path

from deploy_hub.config import PROJECT_ROOT

APP_PATHS = ("src", "pyproject.toml", "uv.lock")  # ce qui fait le code qui tourne ; docs, tests, CHANGELOG : sans effet
GIT_TIMEOUT = 10  # secondes


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def version(root: Path = PROJECT_ROOT) -> str:
    """Numéro de version écrit dans le pyproject.toml du dossier ("" s'il est illisible). Lu sur le disque, pas dans les
    métadonnées du paquet installé : celles-ci ne changent qu'au « uv sync », pas au « git pull »."""
    try:
        return str(tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
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
