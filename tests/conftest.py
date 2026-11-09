import os
import tempfile

# Avant tout import de deploy_hub : le .env du développeur ne doit pas influencer les tests.
os.environ["JARVIS_ENV_FILE"] = os.devnull

from deploy_hub.config import PROJECT_ROOT  # noqa: E402

# Les tests lisent les fiches fictives, jamais data/ (fiches réelles synchronisées depuis Notion).
os.environ.setdefault("DEPLOY_HUB_DEPLOYMENTS_DIR", str(PROJECT_ROOT / "demo" / "deploiements"))
# Les notifications des tests vont dans un dossier temporaire, jamais dans demo/ ni data/.
os.environ.setdefault("DEPLOY_HUB_NOTIFICATIONS", str(__import__("pathlib").Path(tempfile.mkdtemp()) / "notifications.json"))
# Données écrites par l'app (historique et journaux des agents…) : dossier temporaire, jamais data/.
os.environ.setdefault("JARVIS_DATA_DIR", tempfile.mkdtemp())
