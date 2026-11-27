"""Premier lancement : les dossiers que Jarvis peut trouver seul sont notés dans .env, une fois, et apparaissent donc déjà
remplis dans Réglages → Dossiers. Il n'y a pas d'emplacement convenu, chacun range ses dépôts où il veut : Jarvis reconnaît
un clone à l'adresse d'origine de son dépôt git. Le choix ne change plus tout seul d'un démarrage à l'autre (les worktrees
des agents vivent dans le clone de l'orchestrator). Un dossier déjà réglé, dans .env ou hors .env (vm.conf), n'est jamais
cherché ni remplacé."""
import os

from deploy_hub import env_file
from deploy_hub.config import ORCHESTRATOR_REPO, git_clones

FOLDERS = {"ORCHESTRATOR_DIR": ORCHESTRATOR_REPO, "FLOW_DIGGER_DIR": "flow-digger", "INTECG_DIR": "intecg"}  # réglage → nom du dépôt dans l'origine


def note_folders() -> list[str]:
    """Cherche chaque dossier pas encore réglé (clone git sous le dossier personnel, le plus récemment mis à jour s'il y en a
    plusieurs) et note ceux qu'il trouve ; renvoie les réglages remplis. Sans droit d'écrire .env, ce démarrage s'en sert
    quand même et la recherche sera refaite au suivant."""
    missing = {key: repo for key, repo in FOLDERS.items() if not os.getenv(key)}
    if not missing:
        return []
    clones = git_clones(tuple(missing.values()))
    found = {key: str(clones[repo][0]) for key, repo in missing.items() if clones[repo]}
    try:
        env_file.update(found)
    except (env_file.EnvError, OSError):
        os.environ.update(found)
    return list(found)
