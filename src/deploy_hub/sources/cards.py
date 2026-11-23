"""Cartes à collectionner de l'équipe (page Cartes) : un jeu de cartes maison, à collectionner seulement (pas de partie).

Trois familles :
- équipe et logiciels : écrites à la main dans src/deploy_hub/cartes.yaml (versionné ; le dépôt est public : la carte
  d'une personne n'y entre qu'avec son accord). config/cartes.yaml, hors git, en ajoute sur sa machine (même format) ;
- clients : une carte par client suivi dans ce Jarvis, bâtie à l'affichage sur sa fiche (chiffres du jour). Rien n'en
  est versionné.

Chaque carte existe en trois finitions (standard, brillante, or). Un booster donne BOOSTER_SIZE cartes ; on en reçoit
WELCOME à la première visite, un par jour, et un par événement vu par Jarvis (DAG fini vert, agent terminé, retour de
recette terminé, PR fusionnée), DAILY_ACTIVITY_MAX par jour au plus. Chaque agent lancé incarne en plus une carte de
l'équipe, tirée au hasard, qui entre aussitôt dans la collection. La collection de chacun reste sur sa machine :
data/cartes.json (hors git).
"""

import copy
import json
import random
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from deploy_hub.models import DONE, RETOUR_STATUSES, Deployment, status_group
from deploy_hub.safe_write import write_json

CATALOGUE = Path(__file__).parent.parent / "cartes.yaml"
FAMILIES = {"equipe": "Équipe", "logiciel": "Logiciels", "client": "Clients"}  # dans l'ordre du classeur
RARITIES = {"commune": ("Commune", 60), "rare": ("Rare", 28), "epique": ("Épique", 10), "legendaire": ("Légendaire", 2)}  # libellé, poids
FINISHES = {"standard": ("Standard", 80), "brillante": ("Brillante", 16), "or": ("Or", 4)}
BOOSTER_SIZE = 5
WELCOME = 3  # boosters offerts à la première visite
DAILY_ACTIVITY_MAX = 5  # boosters gagnés par l'activité, par jour
EVENT_DAYS = 7  # un événement plus vieux ne rapporte plus rien : les clés gardées (SEEN_KEPT) couvrent largement cette fenêtre
SEEN_KEPT, LOG_KEPT = 2000, 30
CARD_ID = re.compile(r"[A-Za-z0-9-]+")  # l'id sert d'adresse : /cartes/<famille>:<id>
_lock = threading.Lock()


@dataclass(frozen=True)
class Stat:
    label: str
    value: str
    pct: int | None = None  # jauge de 0 à 100 ; None : la valeur seule


@dataclass(frozen=True)
class Card:
    id: str  # « equipe:matthew », « client:<slug> » : ne change jamais, la collection s'y réfère
    family: str
    name: str
    role: str = ""
    rarity: str = "commune"
    stats: tuple[Stat, ...] = ()
    power: str = ""
    effect: str = ""
    quote: str = ""
    photo: str = ""  # fichier de static/cartes/ ; vide : initiales
    emoji: str = ""
    icon: str = ""  # carte client : adresse de son icône (/icones/<slug>)

    @property
    def family_label(self) -> str:
        return FAMILIES[self.family]

    @property
    def rarity_label(self) -> str:
        return RARITIES[self.rarity][0]

    @property
    def initials(self) -> str:
        words = [w for w in self.name.replace("-", " ").split() if w[0].isalnum()]
        return "".join(w[0] for w in words[:2]).upper() or "?"


def _stat(label: str, value) -> Stat:
    if isinstance(value, bool) or not isinstance(value, int):
        return Stat(str(label), str(value))
    return Stat(str(label), str(value), max(0, min(100, value)))


def parse_catalogue(text: str, source: str = "cartes.yaml") -> list[Card]:
    """Cartes d'un fichier yaml : {famille: [{id, nom, role, rarete, stats, pouvoir, effet, citation, photo, emoji}]}.
    Une entrée mal écrite lève ValueError, avec le fichier et la carte en cause."""
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{source} : attendu « famille: [cartes] »")
    cards = []
    for family, entries in raw.items():
        if family not in FAMILIES or family == "client":
            raise ValueError(f"{source} : famille inconnue « {family} » (attendu : equipe, logiciel)")
        if not isinstance(entries or [], list):
            raise ValueError(f"{source} : « {family} » est une liste de cartes")
        for e in entries or []:
            if not isinstance(e, dict) or not e.get("id") or not e.get("nom"):
                raise ValueError(f"{source} : chaque carte de « {family} » a un id et un nom")
            if not CARD_ID.fullmatch(str(e["id"])):
                raise ValueError(f"{source} : id « {e['id']} » : lettres, chiffres et tirets seulement")
            if not isinstance(e.get("stats") or {}, dict):
                raise ValueError(f"{source} : les stats de {e['id']} s'écrivent « {{Nom: valeur}} »")
            rarity = e.get("rarete", "commune")
            if rarity not in RARITIES:
                raise ValueError(f"{source} : rareté inconnue « {rarity} » pour {e['id']} (attendu : {', '.join(RARITIES)})")
            cards.append(Card(
                id=f"{family}:{e['id']}", family=family, name=str(e["nom"]), role=str(e.get("role", "")), rarity=rarity,
                stats=tuple(_stat(k, v) for k, v in (e.get("stats") or {}).items())[:3], power=str(e.get("pouvoir", "")),
                effect=str(e.get("effet", "")), quote=str(e.get("citation", "")), photo=str(e.get("photo", "")),
                emoji=str(e.get("emoji", ""))))
    ids = [c.id for c in cards]
    if doubles := sorted({i for i in ids if ids.count(i) > 1}):
        raise ValueError(f"{source} : id en double : {', '.join(doubles)}")
    return cards


_catalogues: dict[tuple, tuple[list[Card], str]] = {}  # (fichier local, dates des deux fichiers) → dernier catalogue lu


def _mtime(path: Path | None) -> int:
    try:
        return path.stat().st_mtime_ns if path else 0
    except OSError:
        return 0


def catalogue(extra: Path | None = None) -> tuple[list[Card], str]:
    """(cartes écrites à la main, problème) : celles du dépôt, puis celles du fichier local s'il existe. Un fichier local
    illisible n'empêche rien : ses cartes manquent, et le problème est dit sur la page. Relu seulement quand un des deux
    fichiers a changé (pages Cartes et Agents, relevé de chaque minute)."""
    key = (extra, _mtime(CATALOGUE), _mtime(extra))
    if key not in _catalogues:
        _catalogues.clear()
        _catalogues[key] = _read_catalogue(extra)
    cards, problem = _catalogues[key]
    return list(cards), problem


def _read_catalogue(extra: Path | None) -> tuple[list[Card], str]:
    cards = parse_catalogue(CATALOGUE.read_text(encoding="utf-8"))
    if not extra or not extra.is_file():
        return cards, ""
    try:
        local = parse_catalogue(extra.read_text(encoding="utf-8"), extra.name)
    except (OSError, ValueError, yaml.YAMLError) as e:
        return cards, f"{extra} illisible : {e}"
    known = {c.id for c in cards}
    return cards + [c for c in local if c.id not in known], ""


def client_card(d: Deployment) -> Card:
    """Carte d'un client suivi, avec ses chiffres du jour (comptes seulement)."""
    stats = [Stat("Intégré", f"{d.progress} %", d.progress), Stat("Retours ouverts", str(len(d.open_retours)))]
    if d.go_live:
        stats.append(Stat("Déploiement", f"J{-d.days_left:+d}" if d.days_left else "aujourd'hui"))  # J-12, J+3, comme les fiches
    return Card(id=f"client:{d.slug}", family="client", name=d.client, role=d.software, rarity="epique" if d.health == "✅" else "rare",
                stats=tuple(stats), power=d.health_label, emoji=d.emoji, icon=f"/icones/{d.slug}" if d.icone else "")


def pool(written: list[Card], deployments: list[Deployment]) -> list[Card]:
    """Toutes les cartes qu'un booster peut donner ici : celles écrites à la main, puis une par client suivi."""
    return written + [client_card(d) for d in sorted(deployments, key=lambda d: d.client.lower()) if not d.pending]


def _weighted(rng: random.Random, table: dict[str, tuple[str, int]], keys) -> str:
    keys = [k for k in table if k in keys]
    return rng.choices(keys, weights=[table[k][1] for k in keys])[0]


def draw(cards: list[Card], rng: random.Random, size: int = BOOSTER_SIZE) -> list[tuple[Card, str]]:
    """Un booster : (carte, finition) × size. La rareté est tirée d'abord (parmi celles qui existent), puis une carte de
    cette rareté : ajouter des cartes communes ne rend pas les légendaires plus rares. Pas deux fois la même carte dans
    un booster tant qu'il en reste d'autres. La dernière carte n'est jamais commune s'il existe autre chose : elle est
    tirée la première, pour que les autres ne lui prennent pas les cartes rares."""
    out: list[tuple[Card, str]] = []
    for slot in range(size if cards else 0):
        drawn = {c.id for c, _ in out}
        left = [c for c in cards if c.id not in drawn] or cards
        rarities = {c.rarity for c in left}
        if slot == 0 and len(rarities) > 1:
            rarities.discard("commune")
        rarity = _weighted(rng, RARITIES, rarities)
        out.append((rng.choice([c for c in left if c.rarity == rarity]), _weighted(rng, FINISHES, FINISHES)))
    return out[1:] + out[:1]


@dataclass(frozen=True)
class Pull:
    """Une carte sortie d'un booster."""
    card: Card
    finish: str
    new: bool  # première fois dans cette finition


@dataclass(frozen=True)
class Event:
    """Ce que Jarvis a vu et qui vaut un booster. key : une même clé n'est récompensée qu'une fois."""
    key: str
    label: str
    when: datetime


def note_events(notes) -> list[Event]:
    """Notifications qui valent un booster : suivi de DAG fini vert (un par suivi), agent terminé sans échec."""
    out = []
    for n in notes:
        if n.kind == "dag" and n.key.startswith("suivi:") and n.detail.startswith("✅"):
            out.append(Event(f"dag:{n.link}", f"DAG vert · {n.title}", n.when))
        elif n.kind == "agent" and n.level == "info":
            out.append(Event(n.key, n.title.split(" (")[0], n.when))
    return out


def retour_events(changes) -> list[Event]:
    """Retours de recette passés à un statut terminé (journal des synchros) : un booster par retour, même rouvert puis
    refermé. Le libellé ne porte que le nom du client."""
    def group(value) -> str:
        return status_group(str(value), RETOUR_STATUSES) if value else ""
    return [Event(f"retour:{c.deployment.slug}:{c.notion_url}", f"Retour de recette terminé · {c.deployment.client}", c.when)
            for c in changes
            if c.kind == "retour" and c.type == "modifie" and c.field == "statut" and group(c.after) == DONE and group(c.before) != DONE]


def pull_events(prs) -> list[Event]:
    """PR fusionnées (PullRequests.merged : historique gardé par l'onglet PR) : un booster par PR."""
    return [Event(f"pr:{p.url}", f"PR fusionnée · #{p.number}", p.ended) for p in prs if p.ended]


class CardStore:
    """Collection, boosters en réserve et gains déjà comptés : data/cartes.json.
    {"cree", "boosters", "jour", "gains_du_jour", "cartes": {id: {finition: nombre}}, "noms": {id: nom}, "vus": [clés],
     "journal": [...], "agents": {numéro d'agent: id de carte}}"""

    def __init__(self, path: Path):
        self.path = path
        self._cache: tuple[int, dict] | None = None  # (mtime du fichier, contenu) : relu seulement s'il a changé

    def _load(self) -> dict:
        """Contenu du fichier, gardé en mémoire : à ne pas modifier (voir _read)."""
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            return {}
        if not self._cache or self._cache[0] != mtime:
            self._cache = (mtime, json.loads(self.path.read_text(encoding="utf-8")))
        return self._cache[1]

    def _read(self) -> dict:
        return copy.deepcopy(self._load())  # copie : l'appelant la modifie avant d'écrire

    def _write(self, data: dict) -> None:
        data["vus"] = data["vus"][-SEEN_KEPT:]
        data["journal"] = data["journal"][-LOG_KEPT:]
        write_json(self.path, data)

    @staticmethod
    def _gain(data: dict, label: str, now: datetime, count: int = 1) -> None:
        data["boosters"] += count
        data["journal"].append({"quand": now.isoformat(timespec="seconds"), "quoi": label, "boosters": count})

    def _opened(self, now: datetime) -> tuple[dict, bool]:
        """(contenu, changé) : collection créée à la première visite (boosters de bienvenue), booster du jour ajouté
        la première fois que ce jour est vu."""
        data, changed = self._read(), False
        if not data:
            data = {"cree": now.isoformat(timespec="seconds"), "boosters": 0, "jour": "", "gains_du_jour": 0,
                    "cartes": {}, "noms": {}, "vus": [], "journal": []}
            self._gain(data, "Bienvenue", now, WELCOME)
        if data["jour"] != now.date().isoformat():
            data["jour"], data["gains_du_jour"], changed = now.date().isoformat(), 0, True
            self._gain(data, "Booster du jour", now)
        return data, changed

    def visit(self, now: datetime | None = None) -> dict:
        """État du moment, booster du jour compris."""
        now = now or datetime.now()
        with _lock:
            data, changed = self._opened(now)
            if changed:
                self._write(data)
            return data

    @property
    def boosters(self) -> int:
        """Boosters en réserve, sans rien créer ni gagner : pastille de la barre, sur toutes les pages. Un fichier abîmé
        ne doit pas les casser toutes : 0, et c'est la page Cartes qui dira l'erreur."""
        try:
            return self._load().get("boosters", 0)
        except (OSError, ValueError):
            return 0

    def earn(self, events: list[Event], now: datetime | None = None) -> int:
        """Un booster par événement nouveau, postérieur à la création de la collection (le passé ne compte pas) et vieux
        de EVENT_DAYS jours au plus (les sources rejouent tout leur historique à chaque relevé, les clés vues ne sont pas
        gardées sans fin), dans la limite du jour ; au-delà, l'événement est noté vu et ne rapportera rien. Renvoie le
        nombre de boosters gagnés."""
        now = now or datetime.now()
        with _lock:
            data, changed = self._opened(now)
            seen, earned = set(data["vus"]), 0
            since = max(datetime.fromisoformat(data["cree"]), now - timedelta(days=EVENT_DAYS))
            for e in sorted(events, key=lambda e: e.when):
                if e.key in seen or e.when < since:
                    continue
                seen.add(e.key)
                data["vus"].append(e.key)
                if data["gains_du_jour"] < DAILY_ACTIVITY_MAX:
                    data["gains_du_jour"] += 1
                    earned += 1
                    self._gain(data, e.label, now)
                changed = True
            if changed:
                self._write(data)
            return earned

    def agent_cards(self, runs: list[tuple[int, datetime]], team: list[Card], rng: random.Random | None = None,
                    now: datetime | None = None) -> dict[int, str]:
        """{numéro d'agent: id de sa carte}. Chaque agent (numéro, heure de lancement) lancé depuis la création de la
        collection incarne une carte de l'équipe, tirée au hasard une fois pour toutes ; elle entre dans la collection
        en finition standard (un exemplaire par agent)."""
        now, rng = now or datetime.now(), rng or random.Random()
        known = self._load().get("agents", {})
        if not team or all(str(run_id) in known for run_id, _ in runs):  # rien à tirer : ni copie ni écriture (page d'un agent, relue toutes les 5 s)
            return {int(k): v for k, v in known.items()}
        with _lock:
            data, changed = self._opened(now)
            linked, since = data.setdefault("agents", {}), datetime.fromisoformat(data["cree"])
            for run_id, started in runs:
                if str(run_id) in linked:
                    continue
                if started < since:  # agent d'avant la collection : pas de carte, et on ne le regarde plus
                    linked[str(run_id)] = ""
                    changed = True
                    continue
                card = rng.choice(team)
                linked[str(run_id)] = card.id
                owned = data["cartes"].setdefault(card.id, {})
                owned["standard"] = owned.get("standard", 0) + 1
                data["noms"][card.id] = card.name
                changed = True
            if changed:
                self._write(data)
            return {int(k): v for k, v in linked.items()}

    def open_booster(self, cards: list[Card], rng: random.Random | None = None, now: datetime | None = None) -> list[Pull]:
        """Ouvre un booster de la réserve et range ses cartes ; liste vide s'il n'y en a pas (ou aucune carte à tirer)."""
        now = now or datetime.now()
        with _lock:
            data, _ = self._opened(now)
            pulled = draw(cards, rng or random.Random()) if data["boosters"] > 0 else []
            if not pulled:
                return []
            data["boosters"] -= 1
            out = []
            for card, finish in pulled:
                owned = data["cartes"].setdefault(card.id, {})
                out.append(Pull(card, finish, finish not in owned))
                owned[finish] = owned.get(finish, 0) + 1
                data["noms"][card.id] = card.name  # un client retiré plus tard garde sa carte, à son nom
            self._write(data)
            return out


@dataclass(frozen=True)
class Slot:
    """Une case du classeur : la carte, et ce qu'on en a par finition."""
    number: int
    card: Card
    owned: dict[str, int]

    @property
    def best(self) -> str:
        """Finition montrée dans le classeur : la plus rare qu'on possède."""
        return next((f for f in reversed(FINISHES) if f in self.owned), "")

    @property
    def copies(self) -> int:
        return sum(self.owned.values())


def binder(cards: list[Card], data: dict) -> list[tuple[str, list[Slot]]]:
    """Classeur : [(famille, cases numérotées)], cartes possédées ou non. Une carte possédée qui n'est plus dans le jeu
    (client retiré, carte locale enlevée) garde sa case, à son nom."""
    known = {c.id for c in cards}
    gone = [Card(id=i, family=i.split(":")[0] if i.split(":")[0] in FAMILIES else "client", name=data.get("noms", {}).get(i, i),
                 role="Carte retirée du jeu") for i in data.get("cartes", {}) if i not in known]
    slots = [Slot(n, c, data.get("cartes", {}).get(c.id, {})) for n, c in enumerate(cards + gone, 1)]
    return [(family, found) for family in FAMILIES if (found := [s for s in slots if s.card.family == family])]


def progress(groups: list[tuple[str, list[Slot]]]) -> tuple[int, int]:
    """(finitions possédées, finitions à trouver) sur tout le classeur."""
    slots = [s for _, found in groups for s in found]
    return sum(len(s.owned) for s in slots), len(slots) * len(FINISHES)
