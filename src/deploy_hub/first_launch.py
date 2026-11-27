"""Premier lancement : les dossiers que Jarvis peut trouver seul sont notés dans .env, une fois, et apparaissent donc déjà
remplis dans Réglages → Dossiers. Le choix ne change plus tout seul d'un démarrage à l'autre (les worktrees des agents vivent
dans le clone de l'orchestrator). Un dossier déjà réglé, dans .env ou hors .env (vm.conf), n'est jamais cherché ni remplacé."""
import os

from deploy_hub import env_file
from deploy_hub.config import ORCHESTRATOR_REPO, git_clones
from deploy_hub.sources import flow_digger, intecg

# Réglage → (nom du dépôt dans l'adresse d'origine, emplacements habituels à essayer d'abord)
FOLDERS = {"ORCHESTRATOR_DIR": (ORCHESTRATOR_REPO, None),
           "FLOW_DIGGER_DIR": ("flow-digger", flow_digger.folder),
           "INTECG_DIR": ("intecg", intecg.folder)}


def note_folders() -> list[str]:
    """Cherche chaque dossier pas encore réglé (emplacements habituels, sinon clone git sous le dossier personnel, le plus
    récemment mis à jour) et note ceux qu'il trouve ; renvoie les réglages remplis. Sans droit d'écrire .env, ce démarrage
    s'en sert quand même et la recherche sera refaite au suivant."""
    missing = {key: todo for key, todo in FOLDERS.items() if not os.getenv(key)}
    if not missing:
        return []
    clones = git_clones(tuple(repo for repo, _ in missing.values()))
    found = {key: str(folder) for key, (repo, usual) in missing.items()
             if (folder := (usual and usual()) or next(iter(clones[repo]), None))}
    try:
        env_file.update(found)
    except (env_file.EnvError, OSError):
        os.environ.update(found)
    return list(found)
