"""Point d'entrée du skill /air (.claude/skills/air/SKILL.md) : air.py avec la configuration de Jarvis.

`uv run python -m deploy_hub.air_jarvis <commande air.py>` lit le .env de Jarvis (Réglages), résout les références
1Password (op://…) comme l'app, puis lance air.py avec :
- AIRFLOW_URL = AIRFLOW_BASE_URL, AIRFLOW_USER, AIRFLOW_PASSWORD ;
- AIR_STATE_DIR = <données>/airflow, le dossier des suivis lancés par l'app (client par défaut, rapports, journal),
  hors de git (valeurs client possibles).
air.py n'est pas modifié ; une variable déjà fixée dans l'environnement gagne.
"""

import os
import runpy
import sys

from deploy_hub.config import data_dir, load_env_file, secret


def jarvis_env() -> dict[str, str]:
    """Variables à donner à air.py ; vide pour celles que Jarvis n'a pas (air.py dit alors quoi régler)."""
    load_env_file()  # n'écrase pas l'environnement : identifiants déjà fixés gagnent, puis résolus (op://) par secret()
    env = {k: v for k, v in (("AIRFLOW_URL", os.getenv("AIRFLOW_BASE_URL", "").strip()),
                             ("AIR_STATE_DIR", str(data_dir() / "airflow"))) if v and not os.getenv(k)}
    env |= {k: v for k in ("AIRFLOW_USER", "AIRFLOW_PASSWORD") if (v := secret(k))}
    return env


def main() -> None:
    os.environ.update(jarvis_env())  # avant l'import : air.py lit AIRFLOW_URL et AIR_STATE_DIR au chargement
    sys.argv[0] = "air.py"
    runpy.run_module("deploy_hub.air", run_name="__main__")


if __name__ == "__main__":
    main()
