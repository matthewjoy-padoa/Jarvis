"""Onglet DAG de la page Agents : une carte par run lancé depuis Jarvis, au lieu de trois tableaux qui racontaient le même
run trois fois (suivis, enchaînements, lancements).

- un enchaînement est une seule carte, avec ses étapes ; les suivis de ses étapes n'ont pas de carte à eux ;
- un suivi hors enchaînement est une carte ;
- un lancement sans suivi (simulation, lancement refusé) est une carte courte : seul l'historique des lancements le connaît.

Ce que la carte sait d'un run vient du suivi (sources/dag_watch.py) : son état, sa dernière nouvelle, ses relances,
son rapport de fin. air.py ne dit pas quelle tâche tourne : la carte ne l'invente pas.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from deploy_hub.models import IN_PROGRESS, KO, OTHER
from deploy_hub.sources.dag_chain import STEP_CARD
from deploy_hub.sources.dags import TYPE_LABELS

TYPES = sorted(TYPE_LABELS, key=len, reverse=True)  # le plus long d'abord : « transform_and_load-group » avant « transform_and_load »


def dag_name(dag_id: str, customer: str = "") -> tuple[str, str]:
    """(nom lisible, type) d'un DAG, « <client>-<type>-<ressource ou groupe> » dans l'orchestrator :
    « acme-etl_group-administratif » → (« administratif », « ETL groupe »). Un DAG hors convention (« acme-ced ») garde ce
    qui suit son client, ou son nom entier si le client n'est pas connu (lancement sans suivi)."""
    for kind in TYPES:
        _, found, what = dag_id.partition(f"-{kind}-")
        if found and what:
            return what, TYPE_LABELS[kind]
    return (dag_id[len(customer) + 1:] if customer and dag_id.startswith(customer + "-") else dag_id), ""


@dataclass
class BoardStep:
    """Étape d'un enchaînement, telle que la carte la montre."""
    name: str
    dag_id: str
    status: str
    group: str
    watch_id: int | None = None
    message: str = ""


@dataclass
class RunCard:
    key: str  # ancre de la carte : suivi-7, chaine-2, lancement-0
    client: str
    slug: str
    padoa_env: str
    title: str
    kind: str  # type du DAG (« ETL groupe ») ; vide pour un enchaînement
    status: str
    group: str
    started_at: datetime
    ended_at: datetime | None = None
    dag_id: str = ""
    card: str = ""  # carte Notion ou ressource d'où le DAG a été lancé
    summary: str = ""  # dernière nouvelle du suivi, ou résultat du lancement
    clears: int = 0  # relances automatiques faites par le suivi
    report: bool = False  # rapport de fin disponible
    watch_id: int | None = None  # suivi à ouvrir (celui de l'étape en cours ou fautive pour un enchaînement)
    chain_id: int | None = None
    simulated: bool = False
    steps: list[BoardStep] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.group == IN_PROGRESS

    @property
    def duration(self) -> timedelta:
        return (self.ended_at or datetime.now()) - self.started_at


def board(watcher, chains, launches) -> list[RunCard]:
    """Cartes de l'onglet, du plus récent au plus ancien (le gabarit les range par état)."""
    by_id = {w.id: w for w in watcher.watches}
    in_chain = {s.watch_id for c in chains for s in c.steps if s.watch_id}
    cards = [_chain_card(c, by_id, watcher) for c in chains]
    cards += [_watch_card(w, watcher) for w in watcher.watches if w.id not in in_chain]
    # Lancements sans run créé (simulation, refus) : eux seuls n'ont pas de suivi. Ceux d'une chaîne sont sur sa carte.
    cards += [_launch_card(i, run) for i, run in enumerate(launches) if not run.run_id and not run.card.startswith(STEP_CARD)]
    return sorted(cards, key=lambda c: c.started_at, reverse=True)


def _launch_card(i: int, run) -> RunCard:
    title, kind = dag_name(run.dag_id)
    return RunCard(key=f"lancement-{i}", client=run.client, slug="", padoa_env=run.padoa_env, title=title, kind=kind,
                   status="simulation" if run.simulated else "non lancé", group=OTHER if run.simulated else KO,
                   started_at=run.started_at, dag_id=run.dag_id, card=run.card, summary=run.result, simulated=run.simulated)


def _watch_card(w, watcher) -> RunCard:
    title, kind = dag_name(w.dag_id, w.customer)
    return RunCard(key=f"suivi-{w.id}", client=w.client, slug=w.slug, padoa_env=w.padoa_env, title=title, kind=kind, status=w.status,
                   group=w.group, started_at=w.started_at, ended_at=w.ended_at, dag_id=w.dag_id, card=w.card, summary=w.summary,
                   clears=w.clears, report=watcher.report_path(w).is_file(), watch_id=w.id)


def _chain_card(chain, by_id, watcher) -> RunCard:
    steps = [BoardStep(dag_name(s.dag_id)[0], s.dag_id, s.status, s.group, s.watch_id, s.message) for s in chain.steps]
    watched = [by_id[s.watch_id] for s in chain.steps if s.watch_id in by_id]
    last = watched[-1] if watched else None  # l'étape en cours, ou celle où la chaîne s'est arrêtée
    return RunCard(key=f"chaine-{chain.id}", client=chain.client, slug=chain.slug, padoa_env=chain.padoa_env,
                   title=f"Enchaînement #{chain.id}", kind="", status=chain.status, group=chain.group, started_at=chain.started_at,
                   ended_at=chain.ended_at, summary=last.summary if last else "", clears=sum(w.clears for w in watched),
                   report=bool(last and watcher.report_path(last).is_file()), watch_id=last.id if last else None,
                   chain_id=chain.id, simulated=chain.simulated, steps=steps)
