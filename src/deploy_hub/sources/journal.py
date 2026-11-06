"""« Quoi de neuf » : événements du journal des changements (data/notion/<client>/journal.json, écrit à chaque synchro
par sync_notion.publish), rattachés aux retours et aux cartes de la fiche."""

import difflib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from deploy_hub.models import ACTION_STATUSES, INJECTION_STATUSES, RETOUR_STATUSES, Deployment, notion_link, status_group

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
    def diff(self) -> list[tuple[str, str]]:
        """Diff mot à mot d'un texte modifié, façon git : [(« = » | « + » | « - », texte)].
        Le contexte inchangé est raccourci autour des modifications ; un texte ajouté de zéro est tout en « + »."""
        return word_diff(str(self.before or ""), str(self.after or ""))

    @property
    def is_status(self) -> bool:
        return self.field in ("statut", "statut_injection")

    def group_of(self, value) -> str:
        """Groupe de statut (todo, in_progress, ko, done) d'une valeur avant/après, pour colorer son étiquette."""
        if not self.is_status or not value:
            return ""
        table = RETOUR_STATUSES if self.kind == "retour" else INJECTION_STATUSES if self.field == "statut_injection" else ACTION_STATUSES
        return status_group(str(value), table)

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


@dataclass
class ChangedItem:
    """Un retour ou une carte et tous ses changements de la période : une ligne dans « Quoi de neuf »."""
    kind: str
    label: str
    link: str
    notion_url: str
    deployment: Deployment
    changes: list[Change]

    @property
    def when(self) -> datetime:
        return max(c.when for c in self.changes)

    @property
    def status_changes(self) -> list[Change]:
        return [c for c in self.changes if c.type == "modifie" and c.is_status]

    @property
    def other_changes(self) -> list[Change]:
        return [c for c in self.changes if not (c.type == "modifie" and c.is_status)]


def group_changes(changes: list[Change]) -> list[tuple[str, list[ChangedItem]]]:
    """Changements regroupés par élément (un retour ou une carte = une ligne), puis par jour du dernier changement.
    Renvoie [(« Aujourd'hui » | « Hier » | « lundi 3 novembre », [éléments du plus récent au plus ancien]), …]."""
    items: dict[tuple, ChangedItem] = {}
    for c in sorted(changes, key=lambda c: c.when):
        key = (c.deployment.slug, c.notion_url or c.label)
        if key not in items:
            items[key] = ChangedItem(c.kind, c.label, c.link, c.notion_url, c.deployment, [])
        # un même champ modifié plusieurs fois : garder l'état de départ et l'état d'arrivée
        same = next((x for x in items[key].changes if x.type == c.type == "modifie" and x.field == c.field), None)
        if same:
            same.after, same.when = c.after, c.when
        else:
            items[key].changes.append(Change(c.when, c.deployment, c.kind, c.label, c.link, c.notion_url, c.type, c.field, c.before, c.after))
    days: dict[str, list[ChangedItem]] = {}
    for item in sorted(items.values(), key=lambda i: i.when, reverse=True):
        days.setdefault(_day_label(item.when), []).append(item)
    return list(days.items())


DAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre")


def _day_label(when: datetime) -> str:
    delta = (datetime.now().date() - when.date()).days
    if delta == 0:
        return "Aujourd'hui"
    if delta == 1:
        return "Hier"
    return f"{DAYS[when.weekday()]} {when.day} {MONTHS[when.month - 1]}"


CONTEXT_WORDS = 8  # mots inchangés gardés de part et d'autre d'une modification
TOKEN = re.compile(r"\s+|[^\s]+")


def _shorten(text: str, keep_start: bool, keep_end: bool) -> str:
    """Contexte inchangé : seulement quelques mots au bord des modifications, « … » au milieu."""
    words = text.split(" ")
    if len(words) <= 2 * CONTEXT_WORDS + 1:
        return text
    head = " ".join(words[:CONTEXT_WORDS]) if keep_start else ""
    tail = " ".join(words[-CONTEXT_WORDS:]) if keep_end else ""
    return " … ".join(p for p in (head, tail) if p) if head and tail else (f"{head} …" if head else f"… {tail}")


def word_diff(before: str, after: str) -> list[tuple[str, str]]:
    a, b = TOKEN.findall(before), TOKEN.findall(after)
    out: list[tuple[str, str]] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op in ("delete", "replace"):
            out.append(("-", "".join(a[i1:i2])))
        if op in ("insert", "replace"):
            out.append(("+", "".join(b[j1:j2])))
        if op == "equal":
            out.append(("=", "".join(a[i1:i2])))
    # contexte raccourci : au début, on garde la fin ; à la fin, le début ; au milieu, les deux bords
    for k, (op, text) in enumerate(out):
        if op == "=":
            out[k] = ("=", _shorten(text, keep_start=k > 0, keep_end=k < len(out) - 1))
    return [(op, t) for op, t in out if t.strip() or op == "="]
