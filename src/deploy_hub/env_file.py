"""Réglages de Jarvis modifiables depuis la page Réglages : lecture et écriture du .env, sans jamais réafficher un secret.

- Seules les clés de FIELDS sont écrites ; les autres lignes du .env (commentaires compris) restent telles quelles.
- Un secret enregistré en clair n'est jamais renvoyé à la page : le champ arrive vide, et vide veut dire « inchangé ».
  Une référence 1Password (op://…) n'est pas un secret : elle s'affiche.
- Le fichier est réécrit d'un bloc (fichier temporaire puis remplacement), en 600.
"""

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from deploy_hub.config import ENV_FILE_KEYS, OP_PREFIX, PROCESS_KEYS, PROJECT_ROOT, parse_env_line


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str        # url, secret, path, bin (programme), int, choice ou text
    help: str
    live: bool = True  # False : pris en compte au prochain démarrage de Jarvis
    section: str = "Connexions et dossiers"
    choices: tuple[str, ...] = ()  # kind « choice » : valeurs permises ("" = défaut)
    minimum: int = 1  # kind « int » : plus petite valeur permise


CLAUDE = "Agents Claude"
DAGS = "Suivi des DAG"


FIELDS = (
    Field("AIRFLOW_BASE_URL", "Adresse d'Airflow", "url", "Vue depuis la machine de Jarvis : sur la VM, http://localhost:8888"),
    Field("AIRFLOW_USER", "Utilisateur Airflow", "secret", "Référence 1Password conseillée : op://<coffre>/<élément>/<champ>"),
    Field("AIRFLOW_PASSWORD", "Mot de passe Airflow", "secret", "Référence 1Password conseillée : op://<coffre>/<élément>/<champ>"),
    Field("NOTION_TOKEN", "Jeton Notion", "secret", "Facultatif : bouton « Rafraîchir depuis Notion ». Référence 1Password conseillée"),
    Field("ORCHESTRATOR_DIR", "Dossier de integration-orchestrator", "path", "Pour les agents Claude", live=False),
    Field("FLOW_DIGGER_DIR", "Dossier de Flow Digger", "path", "Vide : cherché aux emplacements habituels"),
    Field("JARVIS_MODE", "Mode", "choice", "demo : agents simulés ; live : vrais agents (claude -p)", live=False, section=CLAUDE,
          choices=("", "demo", "live")),
    Field("CLAUDE_BIN", "Programme claude", "bin", "Vide : « claude » dans le PATH", section=CLAUDE),
    Field("JARVIS_AGENT_MODEL", "Modèle", "text", "Vide : modèle par défaut de Claude Code (ex. opus, sonnet)", section=CLAUDE),
    Field("JARVIS_AGENT_PERMISSION_MODE", "Mode de permission", "choice",
          "acceptEdits : modifie les fichiers sans demander ; plan : lecture seule ; default : refuse ce qui n'est pas autorisé",
          section=CLAUDE, choices=("", "acceptEdits", "plan", "default")),
    Field("JARVIS_AGENT_MAX", "Agents simultanés au plus", "int", "Vide : 2", section=CLAUDE),
    Field("JARVIS_AGENT_TIMEOUT_MIN", "Durée maximale d'un agent (min)", "int", "Vide : 90 ; au-delà, l'agent est interrompu", section=CLAUDE),
    Field("JARVIS_AGENT_ADD_DIRS", "Dossiers lisibles en plus", "text", "Séparés par « : » (ex. ~/Documents/Claude_vault)", section=CLAUDE),
    Field("JARVIS_AGENT_ALLOWED_TOOLS", "Outils autorisés", "text", "Vide : liste par défaut (lire, modifier, uv run, git, gh pr create) ; sinon séparés par « ; »",
          section=CLAUDE),
    Field("JARVIS_WORKTREES_DIR", "Dossier des worktrees", "path", "Vide : <dossier de l'orchestrator>/.claude/worktrees/jarvis", section=CLAUDE),
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
    _write(file, "\n".join(lines) + "\n")
    for key, value in wanted.items():  # la suite de Jarvis voit tout de suite les nouvelles valeurs
        ENV_FILE_KEYS.add(key)
        PROCESS_KEYS.discard(key)  # appliquée tout de suite ; au redémarrage, l'environnement du processus regagnera
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    return list(wanted)


def _write(file: Path, text: str) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=".env.", suffix=".tmp")  # créé en 600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, file)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
