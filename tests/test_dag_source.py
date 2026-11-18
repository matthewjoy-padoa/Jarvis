"""Catalogue des DAGs depuis une branche locale du clone orchestrator : vrai dépôt git temporaire, faux générateur."""

import json
import subprocess
import sys
import textwrap
import time

import pytest
from fastapi.testclient import TestClient

from deploy_hub import main
from deploy_hub.sources import dags as dag_catalogue
from deploy_hub.sources.dag_source import DagSource

SAME = {"origin": "http://testserver"}  # requête envoyée par une page de Jarvis

# Faux générateur : lit dags.txt de l'arbre extrait (un dag_id par ligne) et écrit un catalogue au format du vrai.
FAKE_GENERATOR = textwrap.dedent("""
    import argparse, json, pathlib
    p = argparse.ArgumentParser(); p.add_argument("repo"); p.add_argument("--sortie")
    a = p.parse_args()
    tree = pathlib.Path(a.repo)
    if (tree / "casse.txt").exists():
        raise SystemExit("config illisible")
    dags = {d: {"type": "etl", "ressources": ["firm"]} for d in (tree / "dags.txt").read_text().split()}
    out = {"genere_le": "2026-11-18 10:00", "commit_orchestrator": "", "clients": {"acme-uegar": {"logiciel": "uegar", "dags": dags}},
           "git_present": (tree / ".git").exists()}
    pathlib.Path(a.sortie).write_text(json.dumps(out))
""")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(repo, dags, message):
    (repo / "dags.txt").write_text(dags)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", message)


@pytest.fixture
def repo(tmp_path):
    """Clone orchestrator factice : main (un DAG) et ma-branche (deux DAGs), main extraite."""
    repo = tmp_path / "orchestrator"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "acme-uegar-etl-firm", "main")
    git(repo, "switch", "-qc", "ma-branche")
    commit(repo, "acme-uegar-etl-firm acme-uegar-etl-vaccine", "nouveau DAG")
    git(repo, "switch", "-q", "main")
    return repo


@pytest.fixture
def source(repo, tmp_path):
    script = tmp_path / "fake_generator.py"
    script.write_text(FAKE_GENERATOR)
    return DagSource(repo, tmp_path / "dags", generator=[sys.executable, str(script)])


def wait(source, timeout=20):
    """Jusqu'à la fin des générations, enchaînées comprises : `running` ne se vide que quand tout est fait."""
    end = time.monotonic() + timeout
    while source.running and time.monotonic() < end:
        time.sleep(0.05)
    assert not source.running, f"génération toujours en cours : {source.running}"


def error(source):
    return source.status(dag_catalogue.CATALOGUE)["error"]


def dag_ids(path):
    return sorted(json.loads(path.read_text())["clients"]["acme-uegar"]["dags"])


def test_branches_and_default_is_the_current_one(source):
    assert [(b.name, b.current) for b in source.branches()] == [("main", True), ("ma-branche", False)]
    assert source.chosen().name == "main"


def test_a_chosen_branch_is_read_as_committed_without_touching_the_clone(source, repo):
    (repo / "dags.txt").write_text("modif-non-commitee")  # la copie de travail ne compte pas
    head = git(repo, "rev-parse", "HEAD")
    assert source.choose("ma-branche")
    wait(source)
    path = source.active  # catalogue publié pour sources/dags.py
    assert dag_ids(path) == ["acme-uegar-etl-firm", "acme-uegar-etl-vaccine"] and not error(source)
    data = json.loads(path.read_text())
    assert data["branche"] == "ma-branche" and data["commit_orchestrator"] == source.chosen().commit and not data["git_present"]
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "branch", "--show-current") == "main"  # pas de checkout
    assert (repo / "dags.txt").read_text() == "modif-non-commitee"
    assert DagSource(repo, source.state_dir).chosen().name == "ma-branche"  # choix retenu


def test_unknown_branch_is_refused(source):
    assert not source.choose("n-existe-pas") and source.chosen().name == "main"


def test_regenerated_when_the_branch_moves_on(source, repo):
    source.refresh()
    wait(source)
    assert dag_ids(source.active) == ["acme-uegar-etl-firm"]
    commit(repo, "acme-uegar-etl-firm acme-uegar-etl-employee", "nouveau DAG sur main")
    assert source.status(dag_catalogue.CATALOGUE)["outdated"]
    source.refresh()
    wait(source)
    assert dag_ids(source.active) == ["acme-uegar-etl-employee", "acme-uegar-etl-firm"]


def test_a_failed_generation_keeps_the_previous_catalogue_and_says_why(source, repo):
    source.refresh()
    wait(source)
    (repo / "casse.txt").write_text("x")
    commit(repo, "acme-uegar-etl-firm", "config cassée")
    source.refresh()
    wait(source)
    assert "config illisible" in error(source) and dag_ids(source.active) == ["acme-uegar-etl-firm"]
    source.refresh()  # même commit en échec : pas de nouvel essai seul (sinon génération et rechargement en boucle)
    assert not source.running
    source.refresh(force=True)  # « Régénérer »
    assert source.running
    wait(source)


def test_without_a_clone_the_versioned_catalogue_serves(tmp_path):
    source = DagSource(tmp_path / "absent", tmp_path / "dags")
    source.refresh()
    status = source.status(dag_catalogue.CATALOGUE)
    assert not source.active.exists() and not status["branches"] and not status["from_branch"] and not source.running


def test_dag_lists_follow_the_chosen_branch(source, monkeypatch):
    monkeypatch.setattr(dag_catalogue, "LOCAL", source.active)
    assert source.choose("ma-branche")
    wait(source)
    assert [c.dag_id for c in dag_catalogue.dags_for(["acme-uegar"], ["firm"])] == ["acme-uegar-etl-firm", "acme-uegar-etl-vaccine"]
    assert dag_catalogue.known_dag(["acme-uegar"], "acme-uegar-etl-vaccine")


def test_injections_tab_shows_and_switches_the_branch(source, monkeypatch):
    monkeypatch.setattr(main, "dag_source", source)
    monkeypatch.setattr(dag_catalogue, "LOCAL", source.active)
    app = TestClient(main.app)
    page = app.get("/injections").text
    assert 'id="dag-source"' in page and '<option value="ma-branche"' in page and "main (courante)" in page
    assert app.post("/dags/branche", data={"branche": "ma-branche"}).status_code == 403  # pas depuis une page de Jarvis
    banner = app.post("/dags/branche", data={"branche": "ma-branche"}, headers={"HX-Request": "true", **SAME}).text
    assert "Génération du catalogue depuis <code>ma-branche</code>" in banner and 'hx-get="/dags/source"' in banner
    wait(source)
    done = app.get("/dags/source", headers={"HX-Request": "true"})
    assert done.headers.get("HX-Refresh") == "true" and "<code>ma-branche</code> au commit" in done.text
    assert app.post("/dags/branche", data={"branche": "inconnue"}, headers=SAME).status_code == 404


def test_branches_never_share_a_catalogue_file(source):
    assert source.path("feat/x") != source.path("feat_x")


def test_detached_head_shows_no_branch_as_used(source, repo, monkeypatch):
    git(repo, "checkout", "-q", "--detach", "main")
    assert source.chosen() is None and not source.active.exists()
    monkeypatch.setattr(main, "dag_source", source)
    page = TestClient(main.app).get("/injections").text
    assert "HEAD détachée : choisis une branche" in page and "Catalogue du dépôt Jarvis" in page


def test_unreadable_generation_date_does_not_break_the_page(source, tmp_path):
    versioned = tmp_path / "versionne.json"
    versioned.write_text(json.dumps({"genere_le": "2026-11-18T10:00:00", "commit_orchestrator": "abc 2026-11-18", "clients": {}}))
    assert source.status(versioned)["made"] is None
