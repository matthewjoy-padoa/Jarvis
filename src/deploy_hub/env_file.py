"""Réglages de Jarvis modifiables depuis la page Réglages : lecture et écriture du .env, sans jamais réafficher un secret.

- Seules les clés de FIELDS sont écrites ; les autres lignes du .env (commentaires compris) restent telles quelles.
- Un secret enregistré en clair n'est jamais renvoyé à la page : le champ arrive vide, et vide veut dire « inchangé ».
  Une référence 1Password (op://…) n'est pas un secret : elle s'affiche.
- Le fichier est réécrit d'un bloc (fichier temporaire puis remplacement), en 600.
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from deploy_hub.safe_write import write_text
from deploy_hub.config import ENV_FILE_KEYS, OP_PREFIX, PROCESS_KEYS, PROJECT_ROOT, parse_env_line


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str        # url, secret, path, bin (programme), int, choice ou text
    help: str
    live: bool = True  # False : pris en compte au prochain démarrage de Jarvis
    section: str = "Airflow"
    choices: tuple[str, ...] = ()  # kind « choice » : valeurs permises ("" = défaut)
    minimum: int = 1  # kind « int » : plus petite valeur permise
    group: str = ""  # sous-titre dans une partie longue (Agents Claude)


@dataclass(frozen=True)
class Section:
    """Une partie de la page Réglages : son ancre (sommaire), son icône (_icons.html) et ce qu'on y règle."""
    anchor: str
    icon: str
    about: str


AIRFLOW, NOTION, FOLDERS, CLAUDE, DAGS = "Airflow", "Notion", "Dossiers et liens", "Agents Claude", "Suivi des DAG"
SECTIONS = {
    AIRFLOW: Section("airflow", "play", "Où Jarvis lance et suit les DAG. Sans adresse, aucun DAG n'est lancé."),
    NOTION: Section("notion", "note", "Pour relire les tickets à la demande (bouton « Rafraîchir depuis Notion »)."),
    FOLDERS: Section("dossiers", "folder", "Où sont les dépôts sur cette machine, et l'adresse des environnements Padoa."),
    CLAUDE: Section("agents", "bot", "Avec quel modèle les agents travaillent, jusqu'où ils vont et ce qu'ils ont le droit de faire."),
    DAGS: Section("dags", "sync", "Comment Jarvis surveille un run lancé et relance les tâches en échec."),
}
ENGINE, LIMITS, RIGHTS = "Moteur", "Limites", "Permissions et dossiers"


FIELDS = (
    Field("AIRFLOW_BASE_URL", "Adresse d'Airflow", "url", "Vue depuis la machine de Jarvis : sur la VM, http://localhost:8888"),
    Field("AIRFLOW_USER", "Utilisateur Airflow", "secret", "Référence 1Password conseillée : op://<coffre>/<élément>/<champ>"),
    Field("AIRFLOW_PASSWORD", "Mot de passe Airflow", "secret", "Référence 1Password conseillée : op://<coffre>/<élément>/<champ>"),
    Field("NOTION_TOKEN", "Jeton Notion", "secret", "Facultatif. Référence 1Password conseillée : op://<coffre>/<élément>/<champ>",
          section=NOTION),
    Field("JARVIS_NOTION_DASHBOARDS", "Liste des dashboards clients", "text",
          "Id de la page Notion qui liste les dashboards des clients (annuaire de « Ajouter un client »). Vide : « Liste des dashboards "
          "pour le déploiement des nouveaux clients »", section=NOTION),
    Field("JARVIS_NOTION_BAU", "Page des demandes BAU", "text",
          "Lien (ou id) d'une page projet ou BAU de la base Notion « Equipes OPS - Projets et BAU », par exemple « BAU équipe "
          "intégration - TECHNIQUE » : ses actions deviennent la page « BAU » de Jarvis. Vide : pas de page BAU", section=NOTION),
    Field("JARVIS_BAU_SQUADS", "Vues de squad (BAU)", "text",
          "Vues Notion lues telles quelles pour le sélecteur de squad de la page BAU, « nom | lien de la vue » séparées par « ; ». "
          "Vide : SPSTA et Plateform, les vues « Action squad » de la page BAU de l'équipe intégration", section=NOTION),
    Field("ORCHESTRATOR_DIR", "Dossier de integration-orchestrator", "path", "Clone local de integration-orchestrator : DAGs proposés, onglet PR, agents Claude. Vide : ~/integration-orchestrator", live=False, section=FOLDERS),
    Field("FLOW_DIGGER_DIR", "Dossier de Flow Digger", "path", "Vide : cherché aux emplacements habituels", section=FOLDERS),
    Field("JARVIS_TZ", "Fuseau horaire", "text", "Vide : Europe/Paris, quelle que soit l'heure du serveur (la VM est en UTC). "
          "Nom IANA, ex. Europe/Paris", live=False, section=FOLDERS),
    Field("PADOA_ENV_URL", "Adresse des environnements Padoa", "text",
          "Bouton Environnements du dock. Modèle avec [stack] et [client] ; vide : https://[stack]-[client].aodap-staging.fr/support",
          section=FOLDERS),
    Field("CLAUDE_BIN", "Programme claude", "bin", "Vide : « claude » dans le PATH", section=CLAUDE, group=ENGINE),
    Field("JARVIS_AGENT_MODEL", "Modèle", "text", "Vide : modèle par défaut de Claude Code. Un alias (opus, sonnet, haiku) ou un nom complet",
          section=CLAUDE, group=ENGINE),
    Field("JARVIS_AGENT_FALLBACK_MODEL", "Modèle de secours", "text", "Pris à la place du modèle quand il est saturé. Vide : aucun",
          section=CLAUDE, group=ENGINE),
    Field("JARVIS_AGENT_EFFORT", "Effort de réflexion", "choice",
          "Du plus rapide (low) au plus poussé (max) : plus haut, l'agent réfléchit plus, coûte plus et dure plus. Vide : celui de Claude Code",
          section=CLAUDE, choices=("", "low", "medium", "high", "xhigh", "max"), group=ENGINE),
    Field("JARVIS_SYNC_EVERY_MIN", "Synchro Notion automatique (minutes)", "int",
          "Intervalle auquel Jarvis relit Notion tout seul, pour les clients dont le bouton « Synchro auto » de la fiche est activé. Vide : 60 ; 0 : jamais",
          section=CLAUDE, group=ENGINE, minimum=0),
    Field("JARVIS_SYNC_MODEL", "Modèle de la synchro Notion", "text",
          "Routine lancée par « Rafraîchir depuis Notion » sans jeton : elle ne fait qu'exécuter des requêtes, un modèle rapide suffit. "
          "Vide : haiku", section=CLAUDE, group=ENGINE),
    Field("JARVIS_SYNC_EFFORT", "Effort de la synchro Notion", "choice", "Vide : low (le plus rapide)",
          section=CLAUDE, choices=("", "low", "medium", "high"), group=ENGINE),
    Field("JARVIS_AGENT_MAX", "Agents simultanés au plus", "int", "Vide : 2", section=CLAUDE, group=LIMITS),
    Field("JARVIS_AGENT_TIMEOUT_MIN", "Durée maximale d'un agent (min)", "int", "Vide : 90 ; au-delà, l'agent est interrompu", section=CLAUDE,
          group=LIMITS),
    Field("JARVIS_AGENT_MAX_TURNS", "Tours au plus", "int", "L'agent s'arrête après ce nombre d'allers-retours avec ses outils. Vide : pas de limite",
          section=CLAUDE, group=LIMITS),
    Field("JARVIS_AGENT_MAX_BUDGET_USD", "Dépense maximale par agent ($)", "int", "L'agent s'arrête une fois ce montant atteint. Vide : pas de limite",
          section=CLAUDE, group=LIMITS),
    Field("JARVIS_AGENT_PERMISSION_MODE", "Mode de permission", "choice",
          "acceptEdits : modifie les fichiers sans demander ; plan : lecture seule ; default : refuse ce qui n'est pas autorisé",
          section=CLAUDE, choices=("", "acceptEdits", "plan", "default"), group=RIGHTS),
    Field("JARVIS_AGENT_ALLOWED_TOOLS", "Outils autorisés", "text", "Vide : liste par défaut (lire, modifier, uv run, git, gh pr create) ; sinon séparés par « ; »",
          section=CLAUDE, group=RIGHTS),
    Field("JARVIS_SYNC_ALLOWED_TOOLS", "Outils de la synchro Notion", "text",
          "Routine lancée par « Rafraîchir depuis Notion » sans jeton. Vide : commandes sync_notion, lecture et écriture dans les données, "
          "connecteur Notion (mcp__claude_ai_Notion) ; sinon séparés par « ; »", section=CLAUDE, group=RIGHTS),
    Field("JARVIS_AGENT_ADD_DIRS", "Dossiers lisibles en plus", "text", "Séparés par « : » (ex. ~/Documents/Claude_vault)", section=CLAUDE, group=RIGHTS),
    Field("JARVIS_WORKTREES_DIR", "Dossier des worktrees", "path", "Vide : <dossier de l'orchestrator>/.claude/worktrees/jarvis", section=CLAUDE,
          group=RIGHTS),
    Field("JARVIS_DAG_MAX_CLEARS", "Relances automatiques au plus", "int",
          "Vide : 3. Tâches en échec relancées (clear) par le suivi avant d'abandonner ; 0 : aucune relance", section=DAGS, minimum=0),
    Field("JARVIS_DAG_INTERVAL_S", "Intervalle entre deux vérifications (s)", "int", "Vide : 60", section=DAGS, minimum=10),
)
BY_KEY = {f.key: f for f in FIELDS}


class EnvError(ValueError):
    pass


def path() -> Path:
    return Path(os.getenv("JARVIS_ENV_FILE") or PROJECT_ROOT / ".env")


def read(file: Path | None = None) -> dict[str, str]:
    file = file or path()
    if not file.is_file():
        return {}
    return dict(p for line in file.read_text(encoding="utf-8").splitlines() if (p := parse_env_line(line)))


def is_reference(value: str) -> bool:
    return value.startswith(OP_PREFIX)


def view(file: Path | None = None) -> list[dict]:
    """Ce que la page affiche, champ par champ : la valeur (jamais celle d'un secret en clair) et son état."""
    values = read(file)
    out = []
    for f in FIELDS:
        value = values.get(f.key, "")
        clear_secret = f.kind == "secret" and value and not is_reference(value)
        out.append({"field": f, "value": "" if clear_secret else value, "stored_clear": bool(clear_secret),
                    "reference": is_reference(value), "empty": not value,
                    "overridden": f.key in PROCESS_KEYS,  # fixée hors .env (vm.conf…) : elle gagnera au redémarrage
                    "path_ok": _found(f, value)})
    return out


def _found(field: Field, value: str) -> bool | None:
    """Dossier ou programme : trouvé ou non (None : rien à vérifier)."""
    if field.kind == "path" and value:
        return Path(value).expanduser().is_dir()
    if field.kind == "bin":
        return shutil.which(str(Path(value).expanduser()) if value else "claude") is not None
    return None


def _clean(field: Field, value: str) -> str:
    value = (value or "").strip()
    if any(c in value for c in "\r\n\0"):
        raise EnvError(f"{field.label} : une seule ligne")
    if " #" in value:
        raise EnvError(f"{field.label} : « #» précédé d'une espace serait lu comme un commentaire")
    if field.kind == "url" and value and not value.startswith(("http://", "https://")):
        raise EnvError(f"{field.label} : commence par http:// ou https://")
    if field.kind == "int" and value and not (value.isdigit() and int(value) >= field.minimum):
        raise EnvError(f"{field.label} : un nombre entier, {field.minimum} au moins")
    if field.kind == "choice" and value not in field.choices + ("",):
        raise EnvError(f"{field.label} : {' ou '.join(c for c in field.choices if c)}")
    if field.kind == "secret" and value.startswith("op:") and not is_reference(value):
        raise EnvError(f"{field.label} : une référence 1Password commence par op://")
    return value


def update(form: dict[str, str], clear: set[str] = frozenset(), file: Path | None = None) -> list[str]:
    """Écrit les champs du formulaire (un secret laissé vide reste inchangé, sauf s'il est dans « clear »), met à jour
    l'environnement de Jarvis ; renvoie les clés modifiées. Rien n'est écrit si un champ est invalide."""
    file = file or path()
    current = read(file)
    wanted: dict[str, str] = {}
    for f in FIELDS:
        if f.key not in form and f.key not in clear:
            continue
        value = "" if f.key in clear else _clean(f, form.get(f.key, ""))
        if f.kind == "secret" and not value and f.key not in clear:
            continue  # secret non retapé : on garde l'actuel
        if value != current.get(f.key, ""):
            wanted[f.key] = value
    if not wanted:
        return []
    lines = file.read_text(encoding="utf-8").splitlines() if file.is_file() else []
    done = set()
    for i, line in enumerate(lines):
        parsed = parse_env_line(line)
        if parsed and parsed[0] in wanted:
            lines[i] = f"{parsed[0]}={wanted[parsed[0]]}"
            done.add(parsed[0])
    lines += [f"{k}={v}" for k, v in wanted.items() if k not in done]
    write_text(file, "\n".join(lines) + "\n")
    for key, value in wanted.items():  # la suite de Jarvis voit tout de suite les nouvelles valeurs
        ENV_FILE_KEYS.add(key)
        PROCESS_KEYS.discard(key)  # appliquée tout de suite ; au redémarrage, l'environnement du processus regagnera
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    return list(wanted)


