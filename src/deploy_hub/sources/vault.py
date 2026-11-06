"""Lecture des fiches de déploiement : un fichier markdown par client, données dans le frontmatter YAML."""

import json
from pathlib import Path

import yaml

from deploy_hub.models import notion_link, Action, Deployment, Link, Regle, Remontee, ResourceInjection, RetourRecette, Stack


def _split_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    _, front, _ = text.split("---", 2)
    return yaml.safe_load(front) or {}


def _client_config(clients_dir: Path | None, slug: str) -> dict:
    """sync/clients/<slug>.yaml (stacks, clés orchestrator) : lu à chaque page, sans attendre la synchro Notion."""
    path = clients_dir / f"{slug}.yaml" if clients_dir else None
    if not path or not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _client_stacks(clients_dir: Path | None, slug: str) -> list[dict]:
    return [s for s in _client_config(clients_dir, slug).get("stacks") or [] if s.get("url")]


def _recette_content(path: Path) -> dict[str, dict]:
    """Contenu des retours (data/notion/<slug>/contenu-recette.json, à côté du dossier des fiches), par adresse Notion."""
    file = path.parent.parent / "notion" / path.stem / "contenu-recette.json"
    return json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}


def parse_deployment(path: Path, clients_dir: Path | None = None) -> Deployment:
    data = _split_frontmatter(path.read_text(encoding="utf-8"))
    content = _recette_content(path)
    return Deployment(
        slug=path.stem,
        client=data.get("client", path.stem),
        software=data.get("software", "?"),
        status=data.get("status", "en cours"),
        notion_url=data.get("notion_url", ""),
        synced_at=str(data.get("synced_at", "")),
        injection_counts=data.get("injection_counts") or {},
        milestones=data.get("milestones") or {},
        links=[Link(**l) for l in data.get("links", [])],
        stacks=[Stack(**s) for s in data.get("stacks") or _client_stacks(clients_dir, path.stem)],
        orchestrator=data.get("orchestrator") or _client_config(clients_dir, path.stem).get("orchestrator") or [],
        resources=[ResourceInjection(**r) for r in data.get("resources", [])],
        actions=[Action(**{**a, "notion_url": notion_link(a.get("notion_url", ""))}) for a in data.get("actions", [])],
        retours=[RetourRecette(**{**r, **content.get(r.get("notion_url", ""), {}), "notion_url": notion_link(r.get("notion_url", ""))})
                 for r in data.get("retours", [])],
        regles=[Regle(**r) for r in data.get("regles", [])],
        remontees=[Remontee(**r) for r in data.get("remontees", [])],
    )


class VaultSource:
    def __init__(self, deployments_dir: Path, clients_dir: Path | None = None):
        self.deployments_dir = deployments_dir
        self.clients_dir = clients_dir

    def list_deployments(self) -> list[Deployment]:
        if not self.deployments_dir.is_dir():
            return []
        return [parse_deployment(p, self.clients_dir) for p in sorted(self.deployments_dir.glob("*.md"))]

    def get_deployment(self, slug: str) -> Deployment | None:
        path = self.deployments_dir / f"{slug}.md"
        return parse_deployment(path, self.clients_dir) if path.is_file() else None
