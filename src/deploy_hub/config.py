import os
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


def load_env_file(path: Path | None = None) -> None:
    """Variables de .env (non versionné) ajoutées à l'environnement, sans écraser celles déjà définies."""
    path = path or Path(os.getenv("JARVIS_ENV_FILE") or PROJECT_ROOT / ".env")
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key, value.split(" #")[0].strip().strip('"').strip("'"))


def load_settings() -> Settings:
    return Settings(
        mode=os.getenv("JARVIS_MODE") or os.getenv("DEPLOY_HUB_MODE", "demo"),
        deployments_dir=deployments_dir(),
        airflow_base_url=os.getenv("AIRFLOW_BASE_URL", "http://localhost:8080"),
        orchestrator_dir=Path(os.getenv("ORCHESTRATOR_DIR", "~/integration-orchestrator")).expanduser(),
    )


# Lu dès l'import : l'app comme la synchro en ligne de commande (routine) voient les mêmes réglages.
load_env_file()
