"""Palette de commandes (⌘K) : une requête libre → les pages, clients, retours, cartes, règles et agents qui y répondent.

La requête mélange du texte et des mots que Jarvis comprend :
  presoa                                          un client (nom, ou début de nom) : on ne cherche que chez lui
  retours, actions, injections, règles, agents    un type d'élément
  retard, ko, ouvert, terminé, aujourd'hui, semaine, vigilance    un état
  @marie    un responsable        #bloqué    un statut        >thème    une commande
Le reste est du texte : chaque mot doit se retrouver dans l'élément, sans tenir compte des accents ni de la casse.
Les pages et les commandes, elles, se cherchent par leur nom tel qu'il est tapé (« recette » trouve la page Recette).
"""

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import cached_property

from deploy_hub.models import DONE, KO, Deployment

NAV = ("client", "page", "commande")  # cherchés par leur nom
DATA = ("retour", "action", "injection", "regle", "agent")  # cherchés par leur contenu et filtrés par les mots compris
KIND_LABELS = {"client": "Clients", "page": "Pages", "commande": "Commandes", "retour": "Retours de recette", "action": "Actions",
               "injection": "Injections", "regle": "Règles", "agent": "Agents"}
KIND_WORDS = {"retour": "retour", "retours": "retour", "recette": "retour", "action": "action", "actions": "action", "carte": "action",
              "cartes": "action", "injection": "injection", "injections": "injection", "regle": "regle", "regles": "regle",
              "agent": "agent", "agents": "agent", "client": "client", "clients": "client"}
STATE_WORDS = {"retard": "retard", "ko": "ko", "ouvert": "ouvert", "ouverts": "ouvert", "ouverte": "ouvert", "ouvertes": "ouvert",
               "termine": "termine", "termines": "termine", "terminee": "termine", "terminees": "termine", "fait": "termine",
               "aujourd'hui": "aujourdhui", "aujourdhui": "aujourdhui", "semaine": "semaine", "vigilance": "vigilance"}
STATE_LABELS = {"retard": "En retard", "ko": "KO", "ouvert": "Ouverts", "termine": "Terminés", "aujourdhui": "Aujourd'hui",
                "semaine": "Dans les 7 jours", "vigilance": "Point de vigilance"}
GROUP_SIZE, SOLO_SIZE = 6, 40  # résultats montrés par type ; quand un seul type répond
SNIPPET = 46  # caractères gardés de chaque côté du mot trouvé dans un texte long


def fold(text: str) -> str:
    """Minuscules sans accents, caractère pour caractère : la position d'un mot trouvé vaut aussi dans le texte d'origine."""
    out = []
    for ch in text:
        base = unicodedata.normalize("NFD", ch)[0].lower()
        out.append(base if len(base) == 1 else ch)
    return "".join(out)


def _words(folded: str) -> str:
    """Texte dont chaque mot est précédé d'une espace : « " " + mot in … » teste un début de mot."""
    return " " + re.sub(r"[^\w]+", " ", folded)


def segments(text: str, tokens: list[str]) -> list[tuple[str, bool]]:
    """Le texte découpé en morceaux (morceau, trouvé ?) pour surligner les mots de la requête."""
    folded, marks = fold(text), [False] * len(text)
    for token in tokens:
        start = folded.find(token)
        while token and start >= 0:
            marks[start:start + len(token)] = [True] * len(token)
            start = folded.find(token, start + len(token))
    out: list[tuple[str, bool]] = []
    for ch, mark in zip(text, marks):
        if out and out[-1][1] == mark:
            out[-1] = (out[-1][0] + ch, mark)
        else:
            out.append((ch, mark))
    return out


@dataclass
class Item:
    kind: str
    title: str
    url: str
    sub: str = ""  # ligne secondaire : client, type, responsable…
    badge: str = ""  # statut affiché
    group: str = ""  # groupe du statut (couleur du badge ; filtres ko, ouvert, terminé)
    ref: str = ""  # identifiant court : retour, carte, agent
    slug: str = ""  # client de l'élément
    owners: tuple[str, ...] = ()
    due: date | None = None
    late: bool = False
    vigilance: bool = False
    ticket: bool = False  # s'ouvre dans la fenêtre de détail
    external: bool = False  # lien Notion : nouvel onglet
    action: str = ""  # commande exécutée dans le navigateur (thème)
    content: tuple[tuple[str, str], ...] = ()  # (libellé, texte) : cherchés en dernier, montrés en extrait

    def __post_init__(self):
        self.f_title = fold(self.title)
        self.w_title = _words(self.f_title)
        self.f_meta = fold(" ".join([self.sub, self.badge]))
        self.w_meta = _words(self.f_meta)
        self.f_ref = fold(self.ref)
        self.f_owners = fold(" ".join(self.owners))
        self.f_badge = fold(self.badge)
        self.f_content = [(label, text, fold(text)) for label, text in self.content]

    @property
    def done(self) -> bool:
        return self.group == DONE

    def score(self, token: str) -> int:
        """Poids d'un mot dans l'élément : identifiant > titre > ligne secondaire > textes longs. 0 : absent."""
        if self.f_ref and token == self.f_ref:
            return 100
        if self.f_ref and len(token) >= 2 and self.f_ref.startswith(token):
            return 60
        if self.f_title.startswith(token):
            return 50
        if " " + token in self.w_title:
            return 40
        if token in self.f_title:
            return 25
        if " " + token in self.w_meta:
            return 15
        if token in self.f_meta:
            return 10
        return 4 if any(token in f for _, _, f in self.f_content) else 0

    def snippet(self, tokens: list[str]) -> tuple[str, list[tuple[str, bool]]] | None:
        """Extrait du premier texte long qui contient un mot absent du titre et de la ligne secondaire."""
        for token in tokens:
            if token in self.f_title or token in self.f_meta:
                continue
            for label, text, folded in self.f_content:
                at = folded.find(token)
                if at < 0:
                    continue
                start, end = max(0, at - SNIPPET), min(len(text), at + len(token) + SNIPPET)
                cut = ("…" if start else "") + text[start:end].replace("\n", " ") + ("…" if end < len(text) else "")
                return label, segments(cut, tokens)
        return None


@dataclass
class Hit:
    item: Item
    score: int
    title: list[tuple[str, bool]]
    sub: list[tuple[str, bool]]
    snippet: tuple[str, list[tuple[str, bool]]] | None = None


@dataclass
class Query:
    words: list[str] = field(default_factory=list)  # tous les mots tapés, hors client : nom des pages et des commandes
    text: list[str] = field(default_factory=list)  # les mots qui ne sont ni un type, ni un état
    clients: list[Deployment] = field(default_factory=list)
    kinds: set[str] = field(default_factory=set)
    states: set[str] = field(default_factory=set)
    owner: str = ""
    status: str = ""
    commands: bool = False
    chips: list[str] = field(default_factory=list)  # ce que Jarvis a compris, dans l'ordre de la requête

    @property
    def filtered(self) -> bool:
        return bool(self.kinds or self.states or self.owner or self.status)


@dataclass
class Results:
    query: str
    chips: list[str] = field(default_factory=list)
    groups: list[tuple[str, int, list[Hit]]] = field(default_factory=list)  # (libellé, total, premiers résultats)
    card: Deployment | None = None  # un seul client reconnu : sa carte en tête
    card_late: int = 0
    corrected: str = ""  # requête corrigée quand la requête tapée ne trouvait rien (« visti » → « visit »)

    @property
    def total(self) -> int:
        return sum(n for _, n, _ in self.groups)


def _client_matches(token: str, deployments: list[Deployment]) -> list[Deployment]:
    """Clients désignés par un mot : nom ou identifiant exact, sinon début (4 lettres au moins) du nom ou d'un de ses mots."""
    names = [(d, fold(d.slug), fold(d.client)) for d in deployments]
    exact = [d for d, slug, name in names if token in (slug, name)]
    if exact or len(token) < 4:
        return exact
    return [d for d, slug, name in names if slug.startswith(token) or " " + token in _words(name)]


def parse(query: str, deployments: list[Deployment]) -> Query:
    q = Query()
    raw = query.strip()
    if raw.startswith(">"):
        q.commands, raw = True, raw[1:]
        q.chips.append("Commandes")
    for token in fold(raw).split():
        if token.startswith("@") and len(token) > 1:
            q.owner = token[1:]
            q.chips.append(token)
        elif token.startswith("#") and len(token) > 1:
            q.status = token[1:]
            q.chips.append(token)
        elif not q.commands and (found := _client_matches(token, deployments)):
            q.clients += [d for d in found if d not in q.clients]
            q.chips += [d.client for d in found]
        else:
            q.words.append(token)
            if not q.commands and token in KIND_WORDS:
                q.kinds.add(KIND_WORDS[token])
                q.chips.append(KIND_LABELS[KIND_WORDS[token]])
            elif not q.commands and token in STATE_WORDS:
                q.states.add(STATE_WORDS[token])
                q.chips.append(STATE_LABELS[STATE_WORDS[token]])
            else:
                q.text.append(token)
    q.chips = list(dict.fromkeys(q.chips))
    return q


def _in_state(item: Item, state: str, today: date) -> bool:
    if state == "retard":
        return item.late
    if state == "ko":
        return item.group == KO
    if state == "ouvert":
        return bool(item.group) and item.group != DONE
    if state == "termine":
        return item.group == DONE
    if state == "aujourdhui":
        return item.due == today
    if state == "semaine":
        return item.due is not None and item.group != DONE and today <= item.due <= today + timedelta(days=7)
    return item.vigilance


def _retour_items(d: Deployment, today: date, ticket_url) -> list[Item]:
    return [Item("retour", r.label, ticket_url(d.slug, "retour", r.id),
                 sub=" · ".join(p for p in (d.client, r.data_type if r.ligne else "", r.software, r.resource or "", ", ".join(r.owners)) if p),
                 badge=r.status, group=r.group, ref=r.id, slug=d.slug, owners=tuple(r.owners), due=r.due,
                 late=bool(r.due and r.due < today and r.is_open), ticket=True,
                 content=tuple(c for c in r.content if c[0] != "Ligne du cahier de recette"))
            for r in d.retours]


def _action_items(d: Deployment, today: date, ticket_url) -> list[Item]:
    out = []
    for a in d.actions:
        injection = a.injection and bool(a.injection_status)
        out.append(Item(
            "injection" if a.injection else "action", a.title,
            ticket_url(d.slug, "action", a.ref) if a.ref else a.notion_url or f"/deploiements/{d.slug}/actions",
            sub=" · ".join(p for p in (d.client, ", ".join(a.teams), a.owner, " ".join(a.resources)) if p),
            badge=a.injection_status if injection else a.status, group=a.injection_group if injection else a.group,
            ref=a.ref, slug=d.slug, owners=(a.owner,) if a.owner else (), due=a.due,
            late=bool(a.due and a.due < today and a.group != DONE), vigilance=a.vigilance,
            ticket=bool(a.ref), external=not a.ref and bool(a.notion_url),
            content=(("Commentaire", a.comment),) if a.comment else ()))
    return out


def _client_items(d: Deployment, tabs) -> list[Item]:
    left = f"J-{d.days_left}" if d.go_live and d.days_left >= 0 else ""
    card = Item("client", d.client, f"/deploiements/{d.slug}", slug=d.slug, group=d.health_level,
                sub=" · ".join(p for p in (d.software, f"{d.progress} % intégré", left) if p))
    return [card] + [Item("page", f"{d.client} · {label}", f"/deploiements/{d.slug}/{tab}", slug=d.slug) for tab, label in tabs]


COMMANDS = [
    ("Changer de thème : auto, nuit, clair", "#", "theme"),
    ("Ajouter un client", "/clients/nouveau", ""),
    ("Kanban des retours de recette", "/recette?vue=kanban", ""),
    ("Kanban des actions", "/actions?vue=kanban", ""),
    ("Notifications", "/notifications", ""),
    ("État des systèmes", "/sources", ""),
]


class Index:
    """Tout ce que la palette sait trouver, préparé une fois (textes sans accents) tant que les fiches ne changent pas."""

    def __init__(self, deployments: list[Deployment], runs, today: date, pages, tabs, ticket_url):
        self.deployments, self.today = deployments, today
        self.items = [Item("page", label, path) for path, label in pages]
        self.items += [Item("commande", title, url, action=action) for title, url, action in COMMANDS]
        for d in deployments:
            self.items += _client_items(d, tabs) + _retour_items(d, today, ticket_url) + _action_items(d, today, ticket_url)
            self.items += [Item("regle", g.title, f"/deploiements/{d.slug}/regles", slug=d.slug, badge=g.status,
                                sub=" · ".join(p for p in (d.client, g.resource_table, g.type) if p)) for g in d.regles]
        self.items += [Item("agent", f"Agent #{r.id} · {r.subject}", f"/agents/{r.id}", sub=f"{r.client} · {r.branch}",
                            badge=r.status, group=r.group, ref=str(r.id), slug=r.slug) for r in runs]

    @cached_property
    def vocabulary(self) -> list[str]:
        """Mots (4 lettres au moins) des titres et lignes secondaires : de quoi proposer une correction de frappe."""
        return sorted({w for i in self.items for w in (i.w_title + i.w_meta).split() if len(w) >= 4})

    def _match(self, item: Item, q: Query, slugs: set[str]) -> int | None:
        """Score de l'élément pour la requête, ou None s'il n'y répond pas."""
        if slugs and item.slug not in slugs:
            return None
        if item.kind in NAV:
            if q.commands and item.kind != "commande":
                return None
            if q.owner or q.status:
                return None
            if item.kind in q.kinds and not q.states:  # « clients » : tous les clients
                tokens = q.text
            elif q.words or slugs or q.commands:
                tokens = q.words
            else:
                return None
            scores = [item.score(t) for t in tokens]
            return None if 0 in scores else sum(scores) + 5
        if q.commands or (q.kinds and item.kind not in q.kinds):
            return None
        if not (q.text or q.filtered) and not item.late:  # un client seul : ce qui est en retard chez lui
            return None
        if not all(_in_state(item, s, self.today) for s in q.states):
            return None
        if q.owner and q.owner not in item.f_owners:
            return None
        if q.status and q.status not in item.f_badge:
            return None
        scores = [item.score(t) for t in q.text]
        if 0 in scores:
            return None
        return sum(scores) + (3 if item.group != DONE else 0) + (2 if item.late else 0)

    def _find(self, q: Query) -> list[tuple[int, Item]]:
        slugs = {d.slug for d in q.clients}
        found = [(score, i) for i in self.items if (score := self._match(i, q, slugs)) is not None]
        return sorted(found, key=lambda f: (-f[0], not f[1].late, f[1].due or date.max, f[1].title))

    def _correct(self, q: Query) -> list[str] | None:
        """Les mots de la requête, ceux qui ne se trouvent nulle part remplacés par le mot connu le plus proche."""
        fixed, changed = [], False
        for token in q.text:
            if any(i.score(token) for i in self.items):
                fixed.append(token)
                continue
            close = difflib.get_close_matches(token, self.vocabulary, n=1, cutoff=0.75)
            fixed.append(close[0] if close else token)
            changed = changed or bool(close)
        return fixed if changed else None

    def suggestions(self) -> Results:
        """Requête vide : ce qui presse (échéances du jour, puis retards les plus récents), puis où aller."""
        urgent = sorted((i for i in self.items if i.late or (i.due == self.today and i.group != DONE)), key=lambda i: i.due, reverse=True)
        groups = [("À traiter", len(urgent), [self._hit(i, 0, []) for i in urgent[:5]])] if urgent else []
        for kind in ("page", "commande"):
            nav = [self._hit(i, 0, []) for i in self.items if i.kind == kind and not i.slug]
            groups.append((KIND_LABELS[kind], len(nav), nav))
        return Results("", groups=groups)

    @staticmethod
    def _hit(item: Item, score: int, tokens: list[str]) -> Hit:
        return Hit(item, score, segments(item.title, tokens), segments(item.sub, tokens), item.snippet(tokens))

    def search(self, query: str) -> Results:
        q = parse(query, self.deployments)
        if not (q.words or q.clients or q.owner or q.status or q.commands):
            return self.suggestions()
        results = Results(query.strip(), chips=q.chips)
        found = self._find(q)
        if not found and q.text and (fixed := self._correct(q)):
            q.words = [fixed[q.text.index(w)] if w in q.text else w for w in q.words]
            q.text = fixed
            found = self._find(q)
            results.corrected = " ".join(fixed) if found else ""
        if len(q.clients) == 1:  # un seul client reconnu : sa carte en tête, à la place de sa ligne dans « Clients »
            results.card = q.clients[0]
            results.card_late = sum(i.late for i in self.items if i.slug == results.card.slug)
            found = [(s, i) for s, i in found if not (i.kind == "client" and i.slug == results.card.slug)]
        by_kind: dict[str, list[tuple[int, Item]]] = {}
        for score, item in found:
            by_kind.setdefault(item.kind, []).append((score, item))
        order = sorted(by_kind, key=lambda k: (-by_kind[k][0][0], (NAV + DATA).index(k)))
        size = SOLO_SIZE if len(order) == 1 else GROUP_SIZE
        tokens = q.words if q.words else q.text
        results.groups = [(KIND_LABELS[k], len(by_kind[k]), [self._hit(i, s, tokens) for s, i in by_kind[k][:size]]) for k in order]
        return results
