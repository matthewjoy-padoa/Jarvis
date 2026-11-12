"""Lecture des fiches de déploiement : un fichier markdown par client, données dans le frontmatter YAML."""

import json
from pathlib import Path

import yaml

from deploy_hub.client_config import read_file
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


def _names(ids: list[str], users: dict[str, str]) -> list[str]:
    """Noms des personnes ; une personne absente de l'annuaire reste visible sous un nom provisoire."""
    return [users.get(i) or f"Inconnu ({i[:8]})" for i in ids or []]


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
        emoji=str(data.get("emoji") or ""),
        notion_url=(cfg.notion_url if cfg else "") or data.get("notion_url", ""),
        synced_at=str(data.get("synced_at", "")),
        injection_counts=data.get("injection_counts") or {},
        milestones=data.get("milestones") or {},
        links=[Link(**l) for l in data.get("links", [])],
        stacks=[Stack(**s) for s in (cfg.stacks if cfg else data.get("stacks") or [])],
        orchestrator=cfg.orchestrator if cfg else data.get("orchestrator") or [],
        resources=[ResourceInjection(**r) for r in data.get("resources", [])],
        actions=[Action(**{**a, "notion_url": notion_link(a.get("notion_url", ""))}) for a in data.get("actions", [])],
        retours=[RetourRecette(**{**r, **content.get(r.get("notion_url", ""), {}), "notion_url": notion_link(r.get("notion_url", "")),
                                    "owners": _names(r.get("owners"), users)})
                 for r in data.get("retours", [])],
        regles=[Regle(**r) for r in data.get("regles", [])],
        remontees=[Remontee(**r) for r in data.get("remontees", [])],
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
                  self._stamp(_users_path(path)))
        cached = self._cache.get(path)
        if cached and cached[0] == stamps:
            return cached[1]
        deployment = parse_deployment(path, self.clients_dir)
        self._cache[path] = (stamps, deployment)
        return deployment

    def list_deployments(self) -> list[Deployment]:
        if not self.deployments_dir.is_dir():
            return []
        paths = sorted(self.deployments_dir.glob("*.md"))
        self._cache = {p: v for p, v in self._cache.items() if p in paths}  # fiches retirées ou supprimées : oubliées
        return [self._parse(p) for p in paths]

    def get_deployment(self, slug: str) -> Deployment | None:
        path = self.deployments_dir / f"{slug}.md"
        return self._parse(path) if path.is_file() else None
