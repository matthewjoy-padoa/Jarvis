"""Catalogue des DAGs depuis le clone orchestrator local, tel qu'il est sur le disque (comme l'Airflow local) :
vrai dépôt git temporaire, faux générateur."""

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
# Faux générateur : lit dags.txt du clone (un dag_id par ligne) et écrit un catalogue au format du vrai.
FAKE_GENERATOR = textwrap.dedent("""
    import argparse, json, os, pathlib
    p = argparse.ArgumentParser(); p.add_argument("repo"); p.add_argument("--sortie")
    a = p.parse_args()
    tree = pathlib.Path(a.repo)
    if (tree / "casse.txt").exists():
        raise SystemExit("config illisible")
    dags = {d: {"type": "etl", "ressources": ["firm"]} for d in (tree / "dags.txt").read_text().split()}
    out = {"genere_le": "2026-11-18 10:00", "commit_orchestrator": "", "clients": {"acme-uegar": {"logiciel": "uegar", "dags": dags}},
           "sans_pycache": os.environ.get("PYTHONDONTWRITEBYTECODE") == "1"}
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
    return DagSource(repo, tmp_path / "dags" / "catalogue.json", generator=[sys.executable, str(script)])


def wait(source, timeout=20):
    """Jusqu'à la fin des générations, enchaînées comprises : `running` ne se vide que quand tout est fait."""
    end = time.monotonic() + timeout
    while source.running and time.monotonic() < end:
        time.sleep(0.05)
    assert not source.running, f"génération toujours en cours : {source.running}"


def generated(source):
    source.refresh()
    wait(source)
    return sorted(json.loads(source.target.read_text())["clients"]["acme-uegar"]["dags"])


def status(source):
    return source.status(dag_catalogue.CATALOGUE)


def test_the_clone_is_read_as_on_disk(source, repo):
    assert generated(source) == ["acme-uegar-etl-firm"]
    data = json.loads(source.target.read_text())
    assert data["branche"] == "main" and data["commit_orchestrator"] == git(repo, "log", "-1", "--format=%h %cs")
    assert data["sans_pycache"]  # le clone n'est pas modifié par la génération
    (repo / "dags.txt").write_text("acme-uegar-etl-firm acme-uegar-etl-employee")  # modification non commitée : comptée
    assert generated(source) == ["acme-uegar-etl-employee", "acme-uegar-etl-firm"]
    assert "+ modifs" in json.loads(source.target.read_text())["commit_orchestrator"]
    assert git(repo, "branch", "--show-current") == "main" and git(repo, "status", "--porcelain") == "M dags.txt"


def test_switching_branch_in_the_clone_switches_the_dags(source, repo):
    assert generated(source) == ["acme-uegar-etl-firm"]
    git(repo, "switch", "-q", "ma-branche")
    assert generated(source) == ["acme-uegar-etl-firm", "acme-uegar-etl-vaccine"]
    git(repo, "checkout", "-q", "--detach", "main")  # l'Airflow local lit aussi un checkout détaché
    assert generated(source) == ["acme-uegar-etl-firm"] and status(source)["label"] == "HEAD détachée"


def test_editing_a_new_uncommitted_file_again_changes_the_version(source, repo):
    (repo / "nouveau.txt").write_text("a")
    first = source.current().version
    (repo / "nouveau.txt").write_text("ab")
    assert "+ modifs" in first and source.current().version != first


def test_a_failed_generation_keeps_the_previous_catalogue_and_says_why(source, repo):
    assert generated(source) == ["acme-uegar-etl-firm"]
    (repo / "casse.txt").write_text("x")
    commit(repo, "acme-uegar-etl-firm", "config cassée")
    assert generated(source) == ["acme-uegar-etl-firm"] and "config illisible" in status(source)["error"]
    source.refresh()  # même version en échec : pas de nouvel essai seul (sinon génération et rechargement en boucle)
    assert not source.running
    source.refresh(force=True)  # « Régénérer »
    assert source.running
    wait(source)


def test_without_a_clone_the_versioned_catalogue_serves(tmp_path):
    absent = DagSource(tmp_path / "absent", tmp_path / "dags" / "catalogue.json")
    assert not absent.status(dag_catalogue.CATALOGUE)["missing_clone"]  # pas encore relevé : rien d'affirmé
    absent.refresh()
    s = absent.status(dag_catalogue.CATALOGUE)
    assert s["missing_clone"] and not s["local"] and not absent.running


def test_the_page_shows_the_last_check_without_running_git(source, repo, monkeypatch):
    assert generated(source) == ["acme-uegar-etl-firm"]
    (repo / "dags.txt").write_text("acme-uegar-etl-firm acme-uegar-etl-employee")
    monkeypatch.setattr("deploy_hub.sources.dag_source._git", lambda *a: pytest.fail("git lancé pendant l'affichage"))
    assert status(source)["local"] and not status(source)["outdated"]  # relevé d'avant la modification


def test_unreadable_generation_date_does_not_break_the_page(source, tmp_path):
    versioned = tmp_path / "versionne.json"
    versioned.write_text(json.dumps({"genere_le": "2026-11-18T10:00:00", "commit_orchestrator": "abc 2026-11-18", "clients": {}}))
    assert source.status(versioned)["made"] is None


def test_dag_lists_follow_the_local_clone(source, repo, monkeypatch):
    monkeypatch.setattr(dag_catalogue, "LOCAL", source.target)
    git(repo, "switch", "-q", "ma-branche")
    generated(source)
    assert [c.dag_id for c in dag_catalogue.dags_for(["acme-uegar"], ["firm"])] == ["acme-uegar-etl-firm", "acme-uegar-etl-vaccine"]
    assert dag_catalogue.known_dag(["acme-uegar"], "acme-uegar-etl-vaccine")


def test_injections_tab_shows_the_local_clone_and_regenerates(source, monkeypatch):
    monkeypatch.setattr(main, "dag_source", source)
    monkeypatch.setattr(dag_catalogue, "LOCAL", source.target)
    app = TestClient(main.app)
    page = app.get("/injections").text  # l'affichage ne lance rien
    assert 'id="dag-source"' in page and "catalogue du dépôt Jarvis" in page and not source.running
    assert app.post("/dags/regenerer").status_code == 403  # pas depuis une page de Jarvis
    banner = app.post("/dags/regenerer", headers={"HX-Request": "true", **SAME}).text
    assert "depuis ton clone local (<code>main</code>)" in banner and 'hx-get="/dags/source"' in banner
    wait(source)
    done = app.get("/dags/source", headers={"HX-Request": "true"})
    assert done.headers.get("HX-Refresh") == "true" and "DAGs de ton clone local : <code>main</code>" in done.text
