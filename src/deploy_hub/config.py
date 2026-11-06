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


def load_env_file(path: Path = PROJECT_ROOT / ".env") -> None:
    """Variables de .env (non versionné) ajoutées à l'environnement, sans écraser celles déjà définies."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key, value.split(" #")[0].strip().strip('"').strip("'"))


def load_settings() -> Settings:
    load_env_file()
    deployments_dir = os.getenv("DEPLOY_HUB_DEPLOYMENTS_DIR") or PROJECT_ROOT / "data" / "deploiements"
    return Settings(
        mode=os.getenv("DEPLOY_HUB_MODE", "demo"),
        deployments_dir=Path(deployments_dir).expanduser(),
        airflow_base_url=os.getenv("AIRFLOW_BASE_URL", "http://localhost:8080"),
        orchestrator_dir=Path(os.getenv("ORCHESTRATOR_DIR", "/home/padoa/Dev/integration-orchestrator")),
    )
