"""« Quoi de neuf » : événements du journal des changements (data/notion/<client>/journal.json, écrit à chaque synchro
par sync_notion.publish), rattachés aux retours et aux cartes de la fiche."""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from deploy_hub.models import Deployment, notion_link

FIELD_LABELS = {
    "statut": "statut",
    "statut_injection": "statut d'injection",
    "deadline": "deadline",
    "vigilance": "point de vigilance",
    "commentaire_client": "commentaire client",
    "exemple": "exemple salarié / adhérent",
    "analyse": "analyse padoa",
    "solutions": "solutions possibles",
    "discussion": "discussion INTE/OPS",
    "echanges": "échanges / suivi",
}
SHORT_FIELDS = {"statut", "statut_injection", "deadline", "vigilance"}  # affichés « avant → après » ; les textes, en dépliant


@dataclass
class Change:
    when: datetime
    deployment: Deployment
    kind: str  # retour | carte
    label: str  # « Retour 01DB10 · SIR/SIA » ou intitulé de la carte
    link: str  # page de l'app
    notion_url: str
    type: str  # nouveau | modifie | supprime
    field: str = ""
    before: object = None
    after: object = None

    @property
    def field_label(self) -> str:
        return FIELD_LABELS.get(self.field, self.field)

    @property
    def is_text(self) -> bool:
        return bool(self.field) and self.field not in SHORT_FIELDS

    @property
    def verb(self) -> str:
        if self.type == "nouveau":
            return "nouveau"
        if self.type == "supprime":
            return "supprimé de Notion"
        if self.is_text:
            return f"{self.field_label} " + ("ajouté" if self.before is None else "retiré" if self.after is None else "modifié")
        return self.field_label


def journal_path(deployments_dir: Path, slug: str) -> Path:
    return deployments_dir.parent / "notion" / slug / "journal.json"


def changes_since(d: Deployment, deployments_dir: Path, since: datetime) -> list[Change]:
    path = journal_path(deployments_dir, d.slug)
    if not path.is_file():
        return []
    retours = {notion_link(r.notion_url): r for r in d.retours}
    actions = {notion_link(a.notion_url): a for a in d.actions}
    out = []
    for e in json.loads(path.read_text(encoding="utf-8")):
        when = datetime.fromisoformat(e["quand"])
        if when <= since:
            continue
        url = notion_link(e["url"])
        if e["base"] == "recette":
            r = retours.get(url)
            label = r.label if r else "Retour supprimé"
            link = f"/deploiements/{d.slug}/recette?tous=true#{r.id}" if r else ""
            kind = "retour"
        else:
            a = actions.get(url)
            label = a.title if a else "Carte supprimée"
            link = f"/deploiements/{d.slug}/{'injections' if a and a.injection else 'actions?tous=true'}" if a else ""
            kind = "carte"
        out.append(Change(when, d, kind, label, link, url, e["type"], e.get("champ", ""), e.get("avant"), e.get("apres")))
    return sorted(out, key=lambda c: c.when, reverse=True)
