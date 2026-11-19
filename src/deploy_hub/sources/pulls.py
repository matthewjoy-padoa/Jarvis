"""Suivi des PR de l'orchestrator, par client : `gh` dans le clone, réponses gardées cinq minutes.

En deux temps, parce que CI et relecture coûtent cher à GitHub sur toutes les PR du dépôt (18 s pour 165 PR, mesuré le
2026-11-13, contre 2 s sans) : la liste légère de toutes les PR ouvertes, puis CI et relecture des seules PR du client.

Rien n'est écrit : titres, auteurs, CI et relecteurs sont lus chez GitHub avec le `gh` de la personne qui fait tourner Jarvis.
Une PR est rattachée à un client par le scope de son titre (`[FIX](client) …`), par un agent lancé pour lui, ou par son logiciel.

Historique (PR fusionnées ou fermées) : une recherche GitHub par clé du client (`gh pr list --search`), recoupée par le scope
du titre ; en option, les PR fusionnées de son logiciel depuis la réception de ses données, qui l'impactent aussi.
"""

import json
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

from deploy_hub.config import child_environment
from deploy_hub.models import DONE, IN_PROGRESS, KO, TODO

FIELDS = "number,title,url,author,createdAt,updatedAt,isDraft,headRefName"
LIST = ("gh", "pr", "list", "--state", "open", "--limit", "300", "--json", FIELDS)
ENDED = FIELDS + ",state,mergedAt,closedAt"  # PR de l'historique
SEARCH = ("gh", "pr", "list", "--state", "closed", "--limit", "200", "--json", ENDED, "--search")  # closed : fusionnées comprises
DETAILS = "reviewDecision,reviewRequests,statusCheckRollup"  # gh pr view <numéro> --json …
TTL = 300  # secondes
OPEN = "PR ouvertes"  # clé de la liste des PR ouvertes parmi les lectures gardées
SCOPE = re.compile(r"\]\(([^)]+)\)")
SPLIT = re.compile(r"[,;/+&]")
COMMON = "all"  # scope des PR qui touchent tous les clients : `[FIX](all) …`
RED, PENDING, GREEN, NO_CI, UNKNOWN = "rouge", "en cours", "verte", "aucune", "inconnue"
CI_GROUPS = {RED: KO, PENDING: IN_PROGRESS, GREEN: DONE, NO_CI: TODO, UNKNOWN: TODO}
FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
REVIEWS = {"APPROVED": ("approuvée", DONE), "CHANGES_REQUESTED": ("changements demandés", KO)}
OUTCOMES = {"MERGED": ("fusionnée", DONE), "CLOSED": ("fermée sans fusion", KO)}


def names(text: str) -> set[str]:
    """Noms d'une liste libre, en minuscules : « cdg67, CDG50 » ou « Preventiel / Medtra » → {cdg67, cdg50} …"""
    return {s.strip().lower() for s in SPLIT.split(text or "") if s.strip()}


def ci_state(checks: list[dict]) -> str:
    """État de la CI d'une PR : rouge dès qu'un contrôle a échoué, en cours tant qu'un contrôle tourne, verte sinon."""
    ends = [(c.get("conclusion") or c.get("state") or "").upper() for c in checks or []]
    if any(e in FAILED for e in ends):
        return RED
    running = any((c.get("status") or "COMPLETED").upper() != "COMPLETED" or (c.get("state") or "").upper() in ("PENDING", "EXPECTED")
                  for c in checks or [])
    return PENDING if running else GREEN if ends else NO_CI


def client_keys(slug: str, customers: list[str]) -> set[str]:
    """Scopes de titre qui désignent un client : son slug et ses clés orchestrator."""
    return {slug.lower(), *(c.lower() for c in customers)}


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


@dataclass
class PullRequest:
    number: int
    title: str
    url: str
    author: str
    branch: str
    created: datetime
    updated: datetime
    draft: bool = False
    decision: str | None = None  # APPROVED, CHANGES_REQUESTED, REVIEW_REQUIRED ou vide ; None : pas (encore) lue
    reviewers: list[str] = field(default_factory=list)  # relectures demandées, pas encore faites
    ci: str = UNKNOWN
    why: str = ""  # pourquoi elle est sur la fiche du client : client, agent ou logiciel
    state: str = "OPEN"  # OPEN, MERGED ou CLOSED
    ended: datetime | None = None  # fusionnée ou fermée le

    @classmethod
    def from_gh(cls, raw: dict) -> "PullRequest":
        ended = raw.get("mergedAt") or raw.get("closedAt")
        return cls(raw["number"], raw.get("title", ""), raw.get("url", ""), (raw.get("author") or {}).get("login", ""),
                   raw.get("headRefName", ""), _when(raw["createdAt"]), _when(raw.get("updatedAt") or raw["createdAt"]),
                   bool(raw.get("isDraft")), state=raw.get("state") or "OPEN", ended=_when(ended) if ended else None)

    @property
    def outcome(self) -> tuple[str, str]:
        """(libellé, groupe d'affichage) d'une PR de l'historique (fusionnée ou fermée)."""
        return OUTCOMES.get(self.state, (self.state.lower(), TODO))

    def with_details(self, raw: dict, why: str) -> "PullRequest":
        """CI et relecture (gh pr view) ; raw vide : elles n'ont pas pu être lues."""
        if not raw:
            return replace(self, why=why)
        asked = [r.get("login") or r.get("slug") or r.get("name") or "?" for r in raw.get("reviewRequests") or []]
        return replace(self, why=why, decision=raw.get("reviewDecision") or "", reviewers=asked, ci=ci_state(raw.get("statusCheckRollup")))

    @property
    def scopes(self) -> set[str]:
        """Scopes du titre, en minuscules : `[FIX](cdg67, cdg50) …` → {cdg67, cdg50}."""
        found = SCOPE.search(self.title)
        return names(found[1]) if found else set()

    @property
    def ci_group(self) -> str:
        return CI_GROUPS[self.ci]

    @property
    def review(self) -> tuple[str, str]:
        """(libellé, groupe d'affichage) de la relecture."""
        if self.draft:
            return "brouillon", TODO
        if self.decision is None:
            return UNKNOWN, TODO
        if self.decision in REVIEWS:
            return REVIEWS[self.decision]
        return ("relecture demandée", IN_PROGRESS) if self.reviewers else ("sans relecteur", KO)

    def age(self, today: date) -> int:
        return (today - self.created.date()).days

    def idle(self, today: date) -> int:
        """Jours sans mouvement (commit, commentaire, relecture)."""
        return (today - self.updated.date()).days


def _gh(directory: Path, command: tuple) -> str:
    return subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=30, check=True, env=child_environment()).stdout


class PullRequests:
    def __init__(self, directory: Path, run=_gh, ttl: float = TTL):
        """run(dossier, commande gh) → sortie JSON ; remplaçable dans les tests."""
        self.directory = directory
        self.run = run
        self.ttl = ttl
        self._lock = threading.Lock()
        # Lectures GitHub gardées cinq minutes, par clé (OPEN : les PR ouvertes ; sinon une recherche de l'historique ou
        # le lien d'une PR d'agent) : (lu à, PR, message d'erreur, heure de lecture).
        self._reads: dict[str, tuple[float, list[PullRequest], str, datetime]] = {}
        self._details: dict[int, tuple[float, dict]] = {}  # par numéro de PR : (lu à, CI et relecture)

    @property
    def read_at(self) -> datetime | None:
        """Heure de la dernière lecture des PR ouvertes chez GitHub."""
        known = self._reads.get(OPEN)
        return known[3] if known else None

    def _stale(self, read: float) -> bool:
        return time.monotonic() - read > self.ttl

    def _list(self, command: tuple) -> tuple[list[PullRequest], str]:
        """`gh pr list …` lu : (PR, les plus anciennes d'abord ; message si GitHub n'a pas pu être lu)."""
        try:
            raw = json.loads(self.run(self.directory, command))
            return sorted((PullRequest.from_gh(r) for r in (raw if isinstance(raw, list) else [raw])), key=lambda p: p.created), ""
        except FileNotFoundError:  # deux causes possibles : le dossier de travail, ou le programme gh
            if not Path(self.directory).is_dir():
                return [], (f"Clone de integration-orchestrator introuvable ({self.directory}) : renseigne ORCHESTRATOR_DIR "
                            "dans Réglages, puis redémarre Jarvis.")
            return [], "`gh` (GitHub CLI) introuvable sur cette machine : installe-le, puis `gh auth login`."
        except subprocess.CalledProcessError as e:
            return [], f"`gh pr list` a échoué : {(e.stderr or '').strip()[-300:] or f'code {e.returncode}'}"
        except subprocess.TimeoutExpired:
            return [], "GitHub n'a pas répondu en 30 s."
        except (ValueError, KeyError, TypeError, AttributeError):
            return [], "Réponse de `gh pr list` illisible."

    def all(self, fresh: bool = False) -> tuple[list[PullRequest], str]:
        """(PR ouvertes, les plus anciennes d'abord, sans CI ni relecture ; message si GitHub n'a pas pu être lu)."""
        return self._read(OPEN, LIST, fresh)

    def _read(self, key: str, command: tuple, fresh: bool) -> tuple[list[PullRequest], str]:
        """Lecture `gh` gardée cinq minutes. `gh` tourne hors du verrou, pour que les PR ouvertes et l'historique, chargés
        ensemble, ne s'attendent pas : deux pages qui trouvent la même clé périmée au même moment lisent donc chacune
        GitHub (la dernière lecture gagne), un coût accepté plutôt que de bloquer toutes les lectures derrière une seule."""
        with self._lock:
            known = self._reads.get(key)
        if fresh or not known or self._stale(known[0]):
            known = (time.monotonic(), *self._list(command), datetime.now())
            with self._lock:
                self._reads = {k: v for k, v in self._reads.items() if not self._stale(v[0])} | {key: known}
        return known[1], known[2]

    def search(self, query: str, fresh: bool = False) -> tuple[list[PullRequest], str]:
        """PR fusionnées ou fermées trouvées par une recherche GitHub."""
        return self._read(query, (*SEARCH, query), fresh)

    def _agent_pr(self, url: str, fresh: bool) -> tuple[list[PullRequest], str]:
        """PR d'un agent, lue par son lien : son titre peut ne pas porter la clé du client."""
        return self._read(url, ("gh", "pr", "view", url, "--json", ENDED), fresh)

    def history(self, slug: str, customers: list[str], agent_urls: set[str], since: date | None = None,
                softwares: set[str] = frozenset(), common: bool = False, fresh: bool = False) -> tuple[list[PullRequest], str]:
        """PR fusionnées ou fermées du client (scope du titre = sa clé ou son slug, ou PR d'un de ses agents), les plus
        récentes d'abord. Avec since, en option, les PR fusionnées depuis cette date qui l'impactent aussi : scope = un de ses
        logiciels (softwares), ou scope `all`, communes à tous les clients (common)."""
        keys = client_keys(slug, customers)
        impacts = ([(n, "logiciel") for n in sorted(softwares)] + ([(COMMON, "commune")] if common else [])) if since else []
        reads = ([(lambda k=k: self.search(f"{k} in:title", fresh), lambda p: p.scopes & keys, "client") for k in sorted(keys)]
                 + [(lambda u=u: self._agent_pr(u, fresh), lambda p: True, "agent") for u in sorted(agent_urls)]
                 + [(lambda n=n: self.search(f"{n} in:title is:merged merged:>={since.isoformat()}", fresh),
                     lambda p, n=n: p.state == "MERGED" and n in p.scopes, why)  # seulement les PR de ce scope
                    for n, why in impacts])
        with ThreadPoolExecutor(max_workers=8) as pool:  # recherches indépendantes, en parallèle
            results = list(pool.map(lambda read: read[0](), reads))
        found: dict[int, PullRequest] = {}
        error = ""
        for (_, keep, why), (prs, failed) in zip(reads, results):  # dans l'ordre : client, agent, logiciel, commune
            error = error or failed
            for p in prs:
                if p.state != "OPEN" and p.number not in found and keep(p):
                    found[p.number] = replace(p, why=why)
        return sorted(found.values(), key=lambda p: p.ended or p.updated, reverse=True), error

    def _detail(self, number: int, fresh: bool) -> dict:
        known = self._details.get(number)
        if known and not fresh and not self._stale(known[0]):
            return known[1]
        try:
            raw = json.loads(self.run(self.directory, ("gh", "pr", "view", str(number), "--json", DETAILS)))
            raw = raw if isinstance(raw, dict) else {}
        except (OSError, subprocess.SubprocessError, ValueError):
            raw = {}
        if raw:  # un échec n'est pas gardé : la prochaine ouverture de l'onglet réessaie
            self._details[number] = (time.monotonic(), raw)
        return raw

    def for_client(self, slug: str, customers: list[str], softwares: set[str], agent_urls: set[str], fresh: bool = False):
        """PR qui concernent un client, avec CI et relecture : scope du titre = sa clé orchestrator ou son slug, PR d'un de
        ses agents, ou scope = son logiciel."""
        prs, error = self.all(fresh)
        keys = client_keys(slug, customers)
        found = []
        for pr in prs:
            why = "client" if pr.scopes & keys else "agent" if pr.url in agent_urls else "logiciel" if pr.scopes & softwares else ""
            if why:
                found.append((pr, why))
        with ThreadPoolExecutor(max_workers=8) as pool:  # une lecture par PR, en parallèle
            details = list(pool.map(lambda item: self._detail(item[0].number, fresh), found))
        missed = [f"#{pr.number}" for (pr, _), raw in zip(found, details) if not raw]
        if missed and not error:
            error = f"CI et relecture illisibles pour {', '.join(missed)}."
        return [pr.with_details(raw, why) for (pr, why), raw in zip(found, details)], error
