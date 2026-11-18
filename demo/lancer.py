"""Environnement local de Jarvis, seulement avec du faux : voir les changements de l'interface sur le Mac sans pull ni VM.

    uv run python demo/lancer.py            # http://localhost:8766 (reprend l'état de la dernière fois)
    uv run python demo/lancer.py --neuf     # repart des données fictives d'origine
    uv run python demo/lancer.py --port 8770

Tout est fictif et local :
- clients : demo/clients/ (dont un jamais synchronisé), fiches : demo/deploiements/ (celles des tests, recopiées) ;
- DAGs : demo/dags/catalogue.json, lancés et suivis sur un faux Airflow HTTP local (demo/faux_airflow.py) par le vrai air.py ;
- agents et synchro Notion : faux binaire `claude` (demo/faux_claude.py), sans modèle ni Notion ;
- orchestrator : dépôt git jetable (worktrees des agents) ; le catalogue n'est jamais régénéré depuis lui.

Ni le .env, ni config/, ni data/ ne sont lus : l'état vit dans demo/.etat/ (ignoré par git). Aucun appel au vrai Airflow, à
Notion, à Claude ni à GitHub. Les tests (uv run pytest) n'utilisent pas ce lanceur : ils gardent leurs propres doublures.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEMO = Path(__file__).resolve().parent
ROOT = DEMO.parent
ETAT = DEMO / ".etat"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def preparer(neuf: bool) -> None:
    """Dossier d'état : données, configuration, orchestrator jetable, faux claude. Créé une fois, gardé ensuite."""
    if (ETAT / "pret").is_file() and not neuf:
        return
    if ETAT.exists():  # --neuf, ou préparation interrompue la dernière fois : on repart de zéro
        shutil.rmtree(ETAT)
    ETAT.mkdir(parents=True, exist_ok=True)
    data = ETAT / "data"
    shutil.copytree(DEMO / "deploiements", data / "deploiements", dirs_exist_ok=True)
    shutil.copytree(DEMO / "notion", data / "notion", dirs_exist_ok=True)
    shutil.copytree(DEMO / "dags", data / "dags", dirs_exist_ok=True)
    shutil.copytree(DEMO / "clients", ETAT / "config" / "clients", dirs_exist_ok=True)
    # Orchestrator jetable : un clone avec son origin, pour que les agents créent leurs worktrees ailleurs que dans un vrai clone.
    origin, clone = ETAT / "integration-orchestrator.git", ETAT / "orchestrator"  # nom attendu par ClaudeRunner
    _git(ETAT, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(ETAT, "clone", "-q", str(origin), str(clone))
    for key, value in (("user.email", "demo@exemple.test"), ("user.name", "demo")):
        _git(clone, "config", key, value)
    (clone / "README.md").write_text("Orchestrator fictif de l'environnement local de Jarvis.\n", encoding="utf-8")
    _git(clone, "add", "."), _git(clone, "commit", "-qm", "init"), _git(clone, "push", "-q", "origin", "HEAD:main")
    # Faux claude : petit exécutable qui appelle demo/faux_claude.py avec ce Python-ci.
    claude = ETAT / "claude"
    claude.write_text(f"#!/bin/sh\nexport FAUX_CLAUDE_ETAT={str(ETAT)!r}\nexec {sys.executable!r} {str(DEMO / 'faux_claude.py')!r} \"$@\"\n",
                      encoding="utf-8")
    claude.chmod(0o755)
    (ETAT / ".env").write_text("# Réglages de l'environnement local (fictifs). La page Réglages écrit ici, jamais dans le vrai .env.\n",
                               encoding="utf-8")
    (ETAT / ".env").chmod(0o600)
    (ETAT / "pret").write_text("", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis en local, avec seulement des faux clients, DAGs, agents et synchro.")
    parser.add_argument("--neuf", action="store_true", help="repartir des données fictives d'origine")
    parser.add_argument("--port", type=int, default=8766, help="port de Jarvis (défaut 8766 ; 8765 est la VM via VS Code)")
    args = parser.parse_args()
    preparer(args.neuf)

    sys.path.insert(0, str(DEMO))
    from faux_airflow import FauxAirflow
    airflow = FauxAirflow(ETAT / "data" / "dags" / "catalogue.json", ETAT / "data" / "deploiements").start()

    # Avant tout import de deploy_hub : il lit l'environnement au chargement.
    env = {
        "JARVIS_ENV_FILE": str(ETAT / ".env"),
        "JARVIS_DATA_DIR": str(ETAT / "data"),
        "JARVIS_CONFIG_DIR": str(ETAT / "config"),
        "DEPLOY_HUB_DEPLOYMENTS_DIR": str(ETAT / "data" / "deploiements"),
        "DEPLOY_HUB_NOTIFICATIONS": str(ETAT / "data" / "notifications.json"),
        "ORCHESTRATOR_DIR": str(ETAT / "orchestrator"),
        "AIRFLOW_BASE_URL": airflow.url,
        "AIRFLOW_USER": "demo",
        "AIRFLOW_PASSWORD": "demo",
        "JARVIS_DAG_INTERVAL_S": "5",
        "CLAUDE_BIN": str(ETAT / "claude"),
        "NOTION_TOKEN": "",
        "FLOW_DIGGER_DIR": str(ETAT / "pas-de-flow-digger"),
        "JARVIS_AGENT_ADD_DIRS": "",
    }
    os.environ.update(env)

    sys.path.insert(0, str(ROOT / "src"))
    from deploy_hub import main as app_main
    from deploy_hub.sources.dag_source import DagSource
    # Le catalogue fictif ne doit jamais être régénéré depuis l'orchestrator jetable : source des DAGs sans clone.
    app_main.dag_source = DagSource(ETAT / "pas-de-clone", app_main.dag_source.target)

    import uvicorn
    print(f"Jarvis (environnement local fictif) : http://localhost:{args.port}  ·  faux Airflow : {airflow.url}  ·  état : {ETAT}",
          flush=True)
    uvicorn.run(app_main.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
