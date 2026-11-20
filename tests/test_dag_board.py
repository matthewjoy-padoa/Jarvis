"""Onglet DAG de la page Agents : une carte par run (sources/dag_board.py)."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from deploy_hub.sources.airflow import DagRun
from deploy_hub.sources.dag_board import board, dag_name
from deploy_hub.sources.dag_chain import DagChain, Step
from deploy_hub.sources.dag_watch import DagWatch, DagWatcher

NOW = datetime(2026, 11, 20, 12, 0)


def watch(i, dag_id, status, minutes_ago, summary=""):
    start = NOW - timedelta(minutes=minutes_ago)
    return DagWatch(i, "acme", "Acme", "acme", dag_id, f"manual__{i}", "snap", "Carte", start, status, None if status == "suivi en cours" else NOW, summary)


def test_a_dag_is_named_without_its_client_and_with_its_type():
    assert dag_name("acme-etl_group-administratif", "acme") == ("administratif", "ETL groupe")
    assert dag_name("acme-uegar-etl-firm", "acme-uegar") == ("firm", "ETL ressource")
    assert dag_name("acme-uegar-etl-firm") == ("firm", "ETL ressource")  # client à tiret, même inconnu
    assert dag_name("acme-transform_and_load-group-medical") == ("medical", "Transform & load groupe")
    assert dag_name("acme-ced", "acme") == ("ced", "") and dag_name("acme-ced") == ("acme-ced", "")  # hors convention


def test_each_run_has_one_card_newest_first(tmp_path):
    watcher = DagWatcher(tmp_path, None, {})
    watcher.watches = [watch(3, "acme-etl-firm", "suivi en cours", 5), watch(2, "acme-etl-visit_histo", "suivi en cours", 30, "Clear load (1/3)"),
                       watch(1, "acme-etl-amt_task", "vert", 90, "amt_task vert")]
    watcher.watches[1].clears = 2
    chain = DagChain(1, "acme", "Acme", "snap", [Step("acme-etl-amt_task", "vert", 1), Step("acme-etl-visit_histo", "en cours", 2),
                                                Step("acme-etl_group-medical")], NOW - timedelta(minutes=90))
    launches = [DagRun("acme-etl-firm", "snap", "Acme", "Carte", NOW - timedelta(minutes=5), "Lancé", False, "manual__3"),  # a son suivi #3
                DagRun("acme-etl-visit_histo", "snap", "Acme", "Enchaînement #1, étape 2", NOW - timedelta(minutes=30), "Lancé", False, "manual__2"),
                DagRun("acme-etl-note", "prod", "Acme", "Notes", NOW - timedelta(minutes=10), "Simulation : Airflow non configuré", True)]
    cards = board(watcher, [chain], launches)
    assert [c.key for c in cards] == ["suivi-3", "lancement-2", "chaine-1"]  # du plus récent au plus ancien ; ni doublon de suivi ni d'étape
    solo, simulated, chained = cards
    assert (solo.title, solo.kind, solo.watch_id, solo.clears) == ("firm", "ETL ressource", 3, 0)
    assert [(s.name, s.group) for s in chained.steps] == [("amt_task", "done"), ("visit_histo", "in_progress"), ("medical", "todo")]
    assert (chained.watch_id, chained.clears, chained.summary) == (2, 2, "Clear load (1/3)")  # l'étape en cours, ses relances
    assert (simulated.status, simulated.group, simulated.summary) == ("simulation", "other", "Simulation : Airflow non configuré")


def test_the_dag_tab_sorts_the_cards_by_status(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from deploy_hub import main
    watcher = DagWatcher(tmp_path, None, {})
    watcher.watches = [watch(3, "acme-etl-firm", "vert", 5), watch(2, "acme-etl-visit_histo", "suivi en cours", 30), watch(1, "acme-etl-amt_task", "arrêté", 90)]
    monkeypatch.setattr(main, "dag_watch", watcher)
    page = TestClient(main.app).get("/agents").text
    places = [page.index(x) for x in ("En cours <", 'id="suivi-2"', "En échec ou arrêtés <", 'id="suivi-1"', "Terminés <", 'id="suivi-3"')]
    assert places == sorted(places) and "Simulations <" not in page  # une section par état présent, dans cet ordre
