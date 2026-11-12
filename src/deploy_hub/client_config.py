"""Configuration des clients suivis : un yaml par client dans config/clients/ (hors git, propre à chaque utilisateur).

Écrite par la page « Ajouter un client » de l'app ou à la main (modèle : config.example/clients/exemple.yaml).
Enregistrer un client écrit aussi ses requêtes SQL (queries.write) ; la synchro charge ensuite ses données.
"""

import re
import unicodedata
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

import yaml

from deploy_hub import queries
from deploy_hub.config import PROJECT_ROOT, clients_dir, deployments_dir
from deploy_hub.queries import ConfigError, page_id_of

HEADER = ("# Client suivi par Jarvis : configuration locale (hors git), modifiable depuis l'app (Clients → Configurer).\n"
          "# L'app réécrit ce fichier : les commentaires ne sont pas conservés, les clés ajoutées à la main si.\n")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40]


def _list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in re.split(r"[,\n;]", value or "") if v.strip()]


@dataclass
class ClientConfig:
    slug: str
    client: str
    page_id: str
    software: str = ""
    status: str = "en cours"
    notion_url: str = ""
    recette_clients: list[str] = field(default_factory=list)
    orchestrator: list[str] = field(default_factory=list)
    stacks: list[dict] = field(default_factory=list)

    @property
    def stacks_text(self) -> str:
        """Stacks au format du formulaire : une par ligne, « env | libellé | adresse »."""
        return "\n".join(" | ".join(filter(None, [s.get("env", ""), s.get("label", ""), s.get("url", "")])) for s in self.stacks)

    def to_yaml(self) -> str:
        return HEADER + yaml.safe_dump(asdict(self), allow_unicode=True, sort_keys=False)

    @classmethod
    def from_deployment(cls, d) -> "ClientConfig":
        """Point de départ pour configurer une fiche qui n'a pas encore de yaml (sa fiche Notion reste à renseigner)."""
        return cls(slug=d.slug, client=d.client, page_id="", software=d.software, status=d.status, notion_url=d.notion_url,
                   orchestrator=list(d.orchestrator), stacks=[asdict(s) for s in d.stacks])


FIELDS = tuple(f.name for f in fields(ClientConfig))


def _normalize(slug: str, data: dict) -> ClientConfig:
    """Seule lecture d'un yaml client (app, synchro, routine) : listes et valeurs par défaut harmonisées."""
    return ClientConfig(
        slug=slug, client=str(data.get("client") or slug), page_id=str(data.get("page_id") or ""),
        software=str(data.get("software") or ""), status=str(data.get("status") or "en cours"),
        notion_url=str(data.get("notion_url") or ""), recette_clients=_list(data.get("recette_clients")),
        orchestrator=_list(data.get("orchestrator")),
        stacks=[dict(s) for s in data.get("stacks") or [] if isinstance(s, dict) and s.get("url")],
    )


def read_file(path: Path) -> ClientConfig | None:
    if not path.is_file():
        return None
    return _normalize(path.stem, yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def parse_stacks(text: str) -> list[dict]:
    """Une stack par ligne : « env | libellé | adresse », « libellé | adresse » ou une adresse seule."""
    stacks = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if not parts:
            continue
        url = parts[-1]
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"Stack « {line.strip()} » : l'adresse doit commencer par https://")
        label = parts[-2] if len(parts) >= 2 else url.split("//", 1)[1].split("/", 1)[0]
        env = parts[0] if len(parts) >= 3 else ""
        stacks.append({"env": env, "label": label, "url": url})
    return stacks


def from_form(form: dict, existing_slug: str | None = None) -> ClientConfig:
    name = (form.get("client") or "").strip()
    if not name:
        raise ConfigError("Le nom du client est obligatoire.")
    slug = existing_slug or (form.get("slug") or "").strip().lower() or slugify(name)
    if not SLUG.match(slug):
        raise ConfigError("Identifiant invalide : lettres minuscules, chiffres et tirets.")
    dashboard = (form.get("notion_url") or "").strip()
    if dashboard and not dashboard.startswith(("http://", "https://")):
        raise ConfigError("Le lien du dashboard Notion doit être une adresse https://")
    return ClientConfig(
        slug=slug,
        client=name,
        page_id=page_id_of(form.get("page_id") or ""),
        software=(form.get("software") or "").strip(),
        status=(form.get("status") or "en cours").strip() or "en cours",
        notion_url=dashboard,
        recette_clients=_list(form.get("recette_clients")),
        orchestrator=_list(form.get("orchestrator")),
        stacks=parse_stacks(form.get("stacks") or ""),
    )


def load(slug: str, root: Path = PROJECT_ROOT) -> ClientConfig | None:
    return read_file(clients_dir(root) / f"{slug}.yaml")


def slugs(root: Path = PROJECT_ROOT) -> list[str]:
    """Clients configurés : liste unique pour l'app, la synchro, la routine et « Tout rafraîchir »."""
    return sorted(p.stem for p in clients_dir(root).glob("*.yaml"))


def all_configs(root: Path = PROJECT_ROOT) -> list[ClientConfig]:
    return [c for slug in slugs(root) if (c := load(slug, root))]


def save(config: ClientConfig, root: Path = PROJECT_ROOT, new: bool = False) -> Path:
    """Écrit config/clients/<slug>.yaml et les requêtes du client. Refuse d'écraser un client existant à la création."""
    folder = clients_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{config.slug}.yaml"
    if new and path.exists():
        raise ConfigError(f"Un client « {config.slug} » existe déjà : choisis un autre identifiant ou modifie-le.")
    twin = next((c for c in all_configs(root) if c.slug != config.slug and c.page_id and c.page_id == config.page_id), None)
    if twin:  # deux clients sur la même fiche Notion : l'un des deux affiche les données de l'autre
        raise ConfigError(f"Cette fiche Notion est déjà celle du client « {twin.client} » ({twin.slug}).")
    extra = {}
    if path.is_file():  # clés ajoutées à la main : conservées (les commentaires, eux, ne le sont pas)
        extra = {k: v for k, v in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).items() if k not in FIELDS}
    text = config.to_yaml()
    if extra:
        text += yaml.safe_dump(extra, allow_unicode=True, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    queries.write(config.slug, asdict(config), root)
    return path


def retire(slug: str, root: Path = PROJECT_ROOT) -> Path:
    """Retire un client du suivi : son yaml part dans config/clients/retires/ et sa fiche dans data/deploiements/retires/.
    Rien n'est supprimé ; caches et contenu restent dans data/notion/<slug>/."""
    path = clients_dir(root) / f"{slug}.yaml"
    if not path.is_file():
        raise ConfigError(f"Client inconnu : {slug}")
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    target = clients_dir(root) / "retires" / f"{slug}-{stamp}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    fiche = deployments_dir(root) / f"{slug}.md"
    if fiche.is_file():  # sinon le client resterait affiché (menu, accueil, notifications) sans plus être synchronisé
        (fiche.parent / "retires").mkdir(exist_ok=True)
        fiche.rename(fiche.parent / "retires" / f"{slug}-{stamp}.md")
    return path.rename(target)
