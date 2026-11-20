"""Lecture des fiches de déploiement : un fichier markdown par client, données dans le frontmatter YAML."""

import json
from pathlib import Path

import yaml

from deploy_hub.client_config import ConfigError, read_file
from deploy_hub.sync_notion import ICON_FILE
from deploy_hub.models import notion_link, Action, Deployment, Link, Regle, Remontee, ResourceInjection, RetourRecette, Stack


def _split_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    _, front, _ = text.split("---", 2)
    return yaml.safe_load(front) or {}


def _config_path(clients_dir: Path | None, slug: str) -> Path | None:
    return clients_dir / f"{slug}.yaml" if clients_dir else None


def _content_path(path: Path) -> Path:
    """Contenu des retours d'une fiche : data/notion/<slug>/contenu-recette.json, à côté du dossier des fiches."""
    return path.parent.parent / "notion" / path.stem / "contenu-recette.json"


def _recette_content(path: Path) -> dict[str, dict]:
    file = _content_path(path)
    return json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}


def _users_path(path: Path) -> Path:
    """Annuaire uuid Notion → nom (data/notion/utilisateurs.json, hors git), rempli par la synchro."""
    return path.parent.parent / "notion" / "utilisateurs.json"


def _users(path: Path) -> dict[str, str]:
    file = _users_path(path)
    return json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}


def _checks_path(path: Path) -> Path:
    """Heure de la dernière vérification de chaque client (data/notion/verifications.json), écrite par la synchro."""
    return path.parent.parent / "notion" / "verifications.json"


def _checked_at(path: Path) -> str:
    file = _checks_path(path)
    try:
        return str(json.loads(file.read_text(encoding="utf-8")).get(path.stem, "")) if file.is_file() else ""
    except (OSError, ValueError):
        return ""


def _names(ids: list[str], users: dict[str, str]) -> list[str]:
    """Noms des personnes ; une personne absente de l'annuaire reste visible sous un nom provisoire."""
    return [users.get(i) or f"Inconnu ({i[:8]})" for i in ids or []]


def _dashboards(cfg) -> list[Link]:
    """Autres dashboards Notion du client. Comme son dashboard (notion_link à chaque construction d'une fiche), un lien sans
    « /p/ », tel que la synchro en renvoie, est réparé : sinon Notion l'ouvre en erreur."""
    return [Link(l["label"], notion_link(l["url"])) for l in (cfg.dashboards if cfg else [])]


def _icon(fiche_dir: Path, slug: str, data: dict) -> dict:
    """Emoji et image du client : ceux de la fiche, sinon ceux que l'annuaire a lus (sans jeton Notion, la routine de synchro
    ne voit pas les icônes). L'image est dans data/notion/<slug>/, où la route /icones/<slug> la cherche."""
    from deploy_hub.sync_notion import icon_fields, saved_icon
    own = icon_fields(data)
    return own if any(own.values()) else saved_icon(_notion_dir(fiche_dir, slug))


def _notion_dir(fiche_dir: Path, slug: str) -> Path:
    """data/notion/<slug>/ à côté du dossier des fiches : icône du client (icone.json, image), où /icones/<slug> la cherche."""
    return fiche_dir.parent / "notion" / slug


def _from_config(cfg) -> dict:
    """Ce que la fiche tient de la seule configuration du client : son existence et son archivage."""
    return {"configured": True, "archived": cfg.archived, "archived_at": cfg.archived_at} if cfg else {}


def parse_deployment(path: Path, clients_dir: Path | None = None) -> Deployment:
    """Fiche synchronisée, avec par-dessus ce qui appartient à la configuration du client (nom, logiciel, statut,
    dashboard, stacks, clés orchestrator) : une modification dans l'app s'affiche tout de suite, sans attendre la synchro."""
    data = _split_frontmatter(path.read_text(encoding="utf-8"))
    content = _recette_content(path)
    users = _users(path)
    config_path = _config_path(clients_dir, path.stem)
    cfg = read_file(config_path) if config_path else None
    return Deployment(
        slug=path.stem,
        client=cfg.client if cfg else data.get("client", path.stem),
        software=(cfg.software if cfg else "") or data.get("software", "?"),
        status=cfg.status if cfg else data.get("status", "en cours"),
        **_icon(path.parent, path.stem, data),
        notion_url=notion_link((cfg.notion_url if cfg else "") or data.get("notion_url", "")),
        synced_at=str(data.get("synced_at", "")),
        checked_at=_checked_at(path),
        injection_counts=data.get("injection_counts") or {},
        milestones=data.get("milestones") or {},
        links=[Link(**l) for l in data.get("links", [])],
        stacks=[Stack(**s) for s in (cfg.stacks if cfg else data.get("stacks") or [])],
        drive=[Link(**l) for l in (cfg.drive if cfg else [])],
        dashboards=_dashboards(cfg),
        orchestrator=cfg.orchestrator if cfg else data.get("orchestrator") or [],
        resources=[ResourceInjection(**r) for r in data.get("resources", [])],
        actions=[Action(**{**a, "notion_url": notion_link(a.get("notion_url", ""))}) for a in data.get("actions", [])],
        retours=[RetourRecette(**{**r, **content.get(r.get("notion_url", ""), {}), "notion_url": notion_link(r.get("notion_url", "")),
                                    "owners": _names(r.get("owners"), users)})
                 for r in data.get("retours", [])],
        regles=[Regle(**r) for r in data.get("regles", [])],
        remontees=[Remontee(**r) for r in data.get("remontees", [])],
        **_from_config(cfg),
    )


class FicheSource:
    def __init__(self, deployments_dir: Path, clients_dir: Path | None = None):
        self.deployments_dir = deployments_dir
        self.clients_dir = clients_dir
        self._cache: dict[Path, tuple[tuple, Deployment]] = {}

    @staticmethod
    def _stamp(path: Path | None) -> int:
        try:
            return path.stat().st_mtime_ns if path else 0
        except FileNotFoundError:
            return 0

    def _parse(self, path: Path) -> Deployment:
        """Fiche analysée, gardée en mémoire tant que ni elle, ni son contenu de recette, ni la config du client, ni l'annuaire n'ont changé
        (le menu et chaque page relisent toutes les fiches)."""
        stamps = (self._stamp(path), self._stamp(_content_path(path)), self._stamp(_config_path(self.clients_dir, path.stem)),
                  self._stamp(_users_path(path)), self._stamp(_checks_path(path)),
                  self._stamp(_notion_dir(path.parent, path.stem) / ICON_FILE))  # icône lue à part (sans jeton)
        cached = self._cache.get(path)
        if cached and cached[0] == stamps:
            return cached[1]
        deployment = parse_deployment(path, self.clients_dir)
        self._cache[path] = (stamps, deployment)
        return deployment

    def _configured(self) -> list[str]:
        """Clients configurés dans l'app (config/clients/<slug>.yaml)."""
        return sorted(p.stem for p in self.clients_dir.glob("*.yaml")) if self.clients_dir and self.clients_dir.is_dir() else []

    def _pending(self, slug: str) -> Deployment | None:
        """Client ajouté dans l'app mais pas encore synchronisé : fiche vide tirée de sa configuration, pour qu'il apparaisse
        tout de suite (sélecteur, fiche, bouton Rafraîchir) au lieu d'attendre la première synchro."""
        try:
            cfg = read_file(_config_path(self.clients_dir, slug)) if self.clients_dir else None
        except (ConfigError, ValueError, OSError):  # configuration illisible : la page Clients le signale, le reste s'affiche
            return None
        if not cfg:
            return None
        return Deployment(slug=slug, client=cfg.client, software=cfg.software or "?", status=cfg.status, notion_url=notion_link(cfg.notion_url),
                          stacks=[Stack(**s) for s in cfg.stacks], drive=[Link(**l) for l in cfg.drive],
                          dashboards=_dashboards(cfg), orchestrator=cfg.orchestrator,
                          pending=True, **_icon(self.deployments_dir, slug, {}), **_from_config(cfg))

    def _all(self) -> list[Deployment]:
        """Fiches synchronisées, et les clients configurés qui n'en ont pas encore (fiche vide, « pending »)."""
        paths = sorted(self.deployments_dir.glob("*.md")) if self.deployments_dir.is_dir() else []
        self._cache = {p: v for p, v in self._cache.items() if p in paths}  # fiches retirées ou supprimées : oubliées
        synced = [self._parse(p) for p in paths]
        have = {d.slug for d in synced}
        waiting = [d for slug in self._configured() if slug not in have and (d := self._pending(slug))]
        return sorted(synced + waiting, key=lambda d: d.slug)

    def list_deployments(self) -> list[Deployment]:
        """Clients suivis : tout ce que l'app affiche et surveille. Les archivés sont à part (archived_deployments)."""
        return [d for d in self._all() if not d.archived]

    def archived_deployments(self) -> list[Deployment]:
        return [d for d in self._all() if d.archived]

    def get_deployment(self, slug: str) -> Deployment | None:
        path = self.deployments_dir / f"{slug}.md"
        return self._parse(path) if path.is_file() else self._pending(slug)
