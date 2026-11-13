import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    mode: str
    deployments_dir: Path
    airflow_base_url: str
    orchestrator_dir: Path


def clients_dir(root: Path = PROJECT_ROOT) -> Path:
    """Configuration des clients de l'utilisateur (un yaml par client). Hors git : chacun configure les siens.
    JARVIS_CONFIG_DIR la place ailleurs (seulement pour le dossier du projet)."""
    if root == PROJECT_ROOT and os.getenv("JARVIS_CONFIG_DIR"):
        return Path(os.environ["JARVIS_CONFIG_DIR"]).expanduser() / "clients"
    return root / "config" / "clients"


def data_dir(root: Path = PROJECT_ROOT) -> Path:
    """Données synchronisées (fiches, caches, journal, notifications). Hors git. JARVIS_DATA_DIR la place ailleurs."""
    if root == PROJECT_ROOT and os.getenv("JARVIS_DATA_DIR"):
        return Path(os.environ["JARVIS_DATA_DIR"]).expanduser()
    return root / "data"


def deployments_dir(root: Path = PROJECT_ROOT) -> Path:
    """Fiches de déploiement lues par l'app (DEPLOY_HUB_DEPLOYMENTS_DIR : fiches de démo des tests)."""
    if root == PROJECT_ROOT and os.getenv("DEPLOY_HUB_DEPLOYMENTS_DIR"):
        return Path(os.environ["DEPLOY_HUB_DEPLOYMENTS_DIR"]).expanduser()
    return data_dir(root) / "deploiements"


ENV_FILE_KEYS: set[str] = set()  # variables venues du .env : réglages de Jarvis, jamais transmis aux agents


def load_env_file(path: Path | None = None) -> None:
    """Variables de .env (non versionné) ajoutées à l'environnement, sans écraser celles déjà définies."""
    path = path or Path(os.getenv("JARVIS_ENV_FILE") or PROJECT_ROOT / ".env")
    if not path.is_file():
        return
    restrict(path)
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key and not key.startswith("#") and key not in os.environ:  # l'environnement garde la main
            ENV_FILE_KEYS.add(key)
            os.environ[key] = value.split(" #")[0].strip().strip('"').strip("'")


def restrict(path: Path) -> bool:
    """Fichier de secrets lisible par d'autres utilisateurs (groupe, autres) : ramené à 600. True s'il a fallu le faire."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077 and path.is_file() and not path.is_char_device():
            path.chmod(0o600)
            print(f"Jarvis : {path.name} était lisible par d'autres utilisateurs ({mode:o}), droits ramenés à 600.", file=sys.stderr)
            return True
    except OSError:
        pass
    return False


# Secrets : une valeur « op://coffre/élément/champ » est une référence 1Password, lue avec « op read » à la première
# demande puis gardée en mémoire seulement (jamais écrite, jamais remise dans l'environnement transmis aux agents).
OP_PREFIX = "op://"
_resolved: dict[str, str] = {}
SECRET_ERRORS: dict[str, str] = {}  # référence → raison du dernier échec, pour la page Sources
_failed_at: dict[str, float] = {}
RETRY_AFTER = 60  # s : un échec n'est retenté qu'ensuite (chaque page lit le jeton ; op peut attendre jusqu'au délai)


def resolve(value: str) -> str:
    """Valeur telle quelle, ou celle de la référence 1Password ; "" si 1Password ne la donne pas (raison dans SECRET_ERRORS)."""
    value = (value or "").strip()
    if not value.startswith(OP_PREFIX):
        return value
    if value in _resolved:
        return _resolved[value]
    if time.monotonic() - _failed_at.get(value, -RETRY_AFTER) < RETRY_AFTER:
        return ""
    value_ok = _read_op(value)
    if not value_ok:
        _failed_at[value] = time.monotonic()
    return value_ok


def _read_op(value: str) -> str:
    try:
        out = subprocess.run(["op", "read", "--no-newline", value], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        SECRET_ERRORS[value] = "1Password CLI (op) absent"
        return ""
    except (OSError, subprocess.TimeoutExpired) as e:
        SECRET_ERRORS[value] = f"op read a échoué ({type(e).__name__})"
        return ""
    if out.returncode or not out.stdout:
        # Message de op (jamais la valeur : elle n'est pas lue) ; le plus souvent « not signed in ».
        SECRET_ERRORS[value] = (out.stderr.strip().splitlines() or ["op read a échoué"])[-1][:200]
        return ""
    SECRET_ERRORS.pop(value, None)
    _resolved[value] = out.stdout
    return out.stdout


def secret(name: str) -> str:
    """Secret de Jarvis (NOTION_TOKEN, AIRFLOW_USER, AIRFLOW_PASSWORD) : valeur en clair ou référence op://."""
    return resolve(os.getenv(name, ""))


def secret_problem(name: str) -> str:
    """Pourquoi la référence 1Password du secret n'a pas pu être lue ; "" si elle l'a été ou n'en est pas une."""
    return SECRET_ERRORS.get(os.getenv(name, "").strip(), "")


def private_dir_problem(path: Path) -> str:
    """Raison de refuser un dossier de données ou de configuration : dans un dépôt git sans y être ignoré, git pourrait le pousser.
    "" s'il est hors de tout dépôt ou ignoré. Le dossier n'a pas besoin d'exister (on part de son premier parent existant)."""
    path = path.expanduser().resolve()
    existing = next((p for p in (path, *path.parents) if p.is_dir()), None)
    if existing is None:
        return ""
    try:
        top = subprocess.run(["git", "-C", str(existing), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10)
        if top.returncode:
            return ""  # hors de tout dépôt git
        root = Path(top.stdout.strip()).resolve()
        ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", str(path.relative_to(root)) + "/"],
                                 capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""  # git absent : rien à pousser
    if ignored.returncode == 0:
        return ""
    return f"{path} est dans le dépôt git {root} sans y être ignoré : git pourrait pousser des données client. Ajoute-le au .gitignore ou choisis un dossier hors du dépôt."


def ensure_private_dirs(root: Path = PROJECT_ROOT) -> None:
    """Refuse de démarrer si les données ou la configuration des clients atterriraient dans un dossier versionné."""
    problems = [p for p in (private_dir_problem(data_dir(root)), private_dir_problem(clients_dir(root))) if p]
    if problems:
        raise SystemExit("Jarvis ne démarre pas : " + " ".join(problems))


def load_settings() -> Settings:
    return Settings(
        mode=os.getenv("JARVIS_MODE") or os.getenv("DEPLOY_HUB_MODE", "demo"),
        deployments_dir=deployments_dir(),
        airflow_base_url=os.getenv("AIRFLOW_BASE_URL", "http://localhost:8080"),
        orchestrator_dir=Path(os.getenv("ORCHESTRATOR_DIR", "~/integration-orchestrator")).expanduser(),
    )


# Lu dès l'import : l'app comme la synchro en ligne de commande (routine) voient les mêmes réglages.
load_env_file()
