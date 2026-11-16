"""Suivi des PR ouvertes de l'orchestrator, par client : `gh` dans le clone, réponses gardées cinq minutes.

En deux temps, parce que CI et relecture coûtent cher à GitHub sur toutes les PR du dépôt (18 s pour 165 PR, mesuré le
2026-11-13, contre 2 s sans) : la liste légère de toutes les PR ouvertes, puis CI et relecture des seules PR du client.

Rien n'est écrit : titres, auteurs, CI et relecteurs sont lus chez GitHub avec le `gh` de la personne qui fait tourner Jarvis.
Une PR est rattachée à un client par le scope de son titre (`[FIX](client) …`), par un agent lancé pour lui, ou par son logiciel.
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

LIST = ("gh", "pr", "list", "--state", "open", "--limit", "300", "--json", "number,title,url,author,createdAt,updatedAt,isDraft,headRefName")
DETAILS = "reviewDecision,reviewRequests,statusCheckRollup"  # gh pr view <numéro> --json …
TTL = 300  # secondes
SCOPE = re.compile(r"\]\(([^)]+)\)")
SPLIT = re.compile(r"[,;/+&]")
RED, PENDING, GREEN, NO_CI, UNKNOWN = "rouge", "en cours", "verte", "aucune", "inconnue"
CI_GROUPS = {RED: KO, PENDING: IN_PROGRESS, GREEN: DONE, NO_CI: TODO, UNKNOWN: TODO}
FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
REVIEWS = {"APPROVED": ("approuvée", DONE), "CHANGES_REQUESTED": ("changements demandés", KO)}


def ci_state(checks: list[dict]) -> str:
    """État de la CI d'une PR : rouge dès qu'un contrôle a échoué, en cours tant qu'un contrôle tourne, verte sinon."""
    ends = [(c.get("conclusion") or c.get("state") or "").upper() for c in checks or []]
    if any(e in FAILED for e in ends):
        return RED
    running = any((c.get("status") or "COMPLETED").upper() != "COMPLETED" or (c.get("state") or "").upper() in ("PENDING", "EXPECTED")
                  for c in checks or [])
    return PENDING if running else GREEN if ends else NO_CI


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

    @classmethod
    def from_gh(cls, raw: dict) -> "PullRequest":
        return cls(raw["number"], raw.get("title", ""), raw.get("url", ""), (raw.get("author") or {}).get("login", ""),
                   raw.get("headRefName", ""), _when(raw["createdAt"]), _when(raw.get("updatedAt") or raw["createdAt"]),
                   bool(raw.get("isDraft")))

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
        return {s.strip().lower() for s in SPLIT.split(found[1]) if s.strip()} if found else set()

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
        self._cache: tuple[float, list[PullRequest], str] | None = None
        self._details: dict[int, tuple[float, dict]] = {}  # par numéro de PR : (lu à, CI et relecture)
        self.read_at: datetime | None = None

    def _stale(self, read: float) -> bool:
        return time.monotonic() - read > self.ttl

    def all(self, fresh: bool = False) -> tuple[list[PullRequest], str]:
        """(PR ouvertes, les plus anciennes d'abord, sans CI ni relecture ; message si GitHub n'a pas pu être lu)."""
        with self._lock:
            if fresh or not self._cache or self._stale(self._cache[0]):
                prs, error = [], ""
                try:
                    prs = sorted((PullRequest.from_gh(raw) for raw in json.loads(self.run(self.directory, LIST))), key=lambda p: p.created)
                except FileNotFoundError:
                    error = f"`gh` ou le dépôt {self.directory} est introuvable sur cette machine."
                except subprocess.CalledProcessError as e:
                    error = f"`gh pr list` a échoué : {(e.stderr or '').strip()[-300:] or f'code {e.returncode}'}"
                except subprocess.TimeoutExpired:
                    error = "GitHub n'a pas répondu en 30 s."
                except (ValueError, KeyError, TypeError, AttributeError):
                    error = "Réponse de `gh pr list` illisible."
                self._cache, self.read_at = (time.monotonic(), prs, error), datetime.now()
            return self._cache[1], self._cache[2]

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

    def for_client(self, slug: str, customers: list[str], software: str, agent_urls: set[str], fresh: bool = False):
        """PR qui concernent un client, avec CI et relecture : scope du titre = sa clé orchestrator ou son slug, PR d'un de
        ses agents, ou scope = son logiciel."""
        prs, error = self.all(fresh)
        keys = {slug.lower(), *(c.lower() for c in customers)}
        softwares = {s.strip().lower() for s in SPLIT.split(software or "") if s.strip()}
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
