"""Bouton Flow Digger : démarre l'app sur la VM seulement si elle ne répond pas, puis y emmène."""

import socket
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from deploy_hub.main import app
from deploy_hub.sources import flow_digger

REAL_UPDATE = flow_digger.update


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Faux dépôt flow-digger (main.py présent), port libre, journal dans un dossier de test."""
    folder = tmp_path / "flow-digger"
    folder.mkdir()
    (folder / "main.py").write_text("")
    monkeypatch.setenv("FLOW_DIGGER_DIR", str(folder))
    monkeypatch.setenv("FLOW_DIGGER_PORT", str(free_port()))
    monkeypatch.delenv("FLOW_DIGGER_URL", raising=False)
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(flow_digger, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(flow_digger, "update", lambda repo: (True, ""))  # le pull a ses propres tests, sur de vrais dépôts
    return folder


def no_launch(*args, **kwargs):
    raise AssertionError("Flow Digger ne devait pas être lancé")


def test_the_command_is_the_one_of_the_readme(repo):
    assert flow_digger.command(repo) == ["uv", "run", "--frozen", "--project", str(repo), str(repo / "main.py"),
                                         "--start", str(flow_digger.port()), "--no-browser"]


def test_already_running_is_not_started_twice(repo, monkeypatch):
    monkeypatch.setattr(flow_digger.subprocess, "Popen", no_launch)
    with socket.socket() as server:
        server.bind(("127.0.0.1", flow_digger.port()))
        server.listen()
        assert flow_digger.start().url == f"http://localhost:{flow_digger.port()}"


def test_missing_repository_says_where_to_put_it(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("FLOW_DIGGER_DIR", str(tmp_path / "absent"))
    monkeypatch.setattr(flow_digger.subprocess, "Popen", no_launch)
    launch = flow_digger.start()
    assert not launch.url and "introuvable" in launch.error and "FLOW_DIGGER_DIR" in launch.error


def test_a_real_start_waits_for_the_port_and_hides_jarvis_secrets(repo, monkeypatch):
    """Un vrai processus détaché (http.server à la place de main.py --start) : Jarvis attend qu'il écoute."""
    monkeypatch.setenv("NOTION_TOKEN", "ne-doit-pas-sortir")
    monkeypatch.setattr(flow_digger, "command", lambda r: [sys.executable, "-m", "http.server", str(flow_digger.port()), "--bind", "127.0.0.1"])
    started = []
    real = subprocess.Popen

    def record(cmd, **kwargs):
        started.append(kwargs)
        proc = real(cmd, **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(flow_digger.subprocess, "Popen", record)
    try:
        launch = flow_digger.start(wait=10)
        assert launch.url == f"http://localhost:{flow_digger.port()}", launch.error + (repo.parent / "data" / "flow-digger.log").read_text()
        options = started[0]
        assert options["cwd"] == repo and options["start_new_session"] is True
        assert "NOTION_TOKEN" not in options["env"]
        assert (repo.parent / "data" / "flow-digger.log").is_file()
    finally:
        for p in started[1:]:
            p.terminate()
            p.wait(timeout=5)


def test_a_server_that_never_listens_is_reported(repo, monkeypatch):
    monkeypatch.setattr(flow_digger.subprocess, "Popen", lambda *a, **k: None)
    launch = flow_digger.start(wait=0.5)
    assert not launch.url and "ne répond pas" in launch.error and "flow-digger.log" in launch.error


def test_the_url_can_follow_another_forwarded_port(repo, monkeypatch):
    monkeypatch.setenv("FLOW_DIGGER_URL", "http://localhost:4005")
    assert flow_digger.url() == "http://localhost:4005"


def test_the_button_is_in_the_bar_and_opens_its_own_window():
    page = TestClient(app).get("/").text
    assert 'action="/outils/flow-digger" target="flow-digger"' in page and "data-own-window" in page
    assert 'class="tool-launch-ext" aria-hidden="true">↗<' in page


def test_another_site_cannot_start_it(monkeypatch):
    monkeypatch.setattr(flow_digger, "start", no_launch)
    client = TestClient(app)
    assert client.post("/outils/flow-digger").status_code == 403
    assert client.post("/outils/flow-digger", headers={"origin": "https://site-hostile.example"}).status_code == 403


def test_the_route_redirects_or_explains(monkeypatch):
    client = TestClient(app)
    same = {"origin": "http://testserver"}
    monkeypatch.setattr(flow_digger, "start", lambda force=False: flow_digger.Launch(url="http://localhost:4004"))
    r = client.post("/outils/flow-digger", headers=same, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "http://localhost:4004"
    monkeypatch.setattr(flow_digger, "start", lambda force=False: flow_digger.Launch(error="Flow Digger introuvable : clone le dépôt"))
    r = client.post("/outils/flow-digger", headers=same)
    assert r.status_code == 200 and "Flow Digger introuvable" in r.text


def test_reloading_the_tab_launches_nothing(monkeypatch):
    monkeypatch.setattr(flow_digger, "start", no_launch)
    r = TestClient(app).get("/outils/flow-digger", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


# --- Mise à jour (git pull --ff-only) avant le démarrage, sur de vrais dépôts git ----------------------------------

def git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


@pytest.fixture
def remote_and_clone(tmp_path):
    """Un dépôt « distant » (comme GitHub) et le clone de la VM, à jour au départ."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "main.py").write_text("v1\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "v1")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    return origin, clone


def test_new_commits_are_pulled_before_starting(remote_and_clone):
    origin, clone = remote_and_clone
    (origin / "main.py").write_text("v2\n")
    git(origin, "commit", "-qam", "v2")
    ok, detail = REAL_UPDATE(clone)
    assert ok and "→" in detail and (clone / "main.py").read_text() == "v2\n"


def test_nothing_new_is_fine(remote_and_clone):
    assert REAL_UPDATE(remote_and_clone[1]) == (True, "")


def test_a_diverged_clone_is_never_merged_nor_forced(remote_and_clone):
    origin, clone = remote_and_clone
    (origin / "main.py").write_text("distant\n")
    git(origin, "commit", "-qam", "distant")
    (clone / "main.py").write_text("local\n")
    git(clone, "commit", "-qam", "local")
    ok, detail = REAL_UPDATE(clone)
    assert not ok and "git pull a échoué" in detail
    assert (clone / "main.py").read_text() == "local\n"  # rien de fusionné ni d'écrasé


def test_a_branch_without_upstream_is_started_as_is(tmp_path):
    git(tmp_path, "init", "-q")
    assert REAL_UPDATE(tmp_path) == (True, "")


def test_a_failed_pull_launches_nothing_unless_forced(repo, monkeypatch):
    monkeypatch.setattr(flow_digger, "update", lambda r: (False, "git pull a échoué : Not possible to fast-forward"))
    monkeypatch.setattr(flow_digger.subprocess, "Popen", no_launch)
    launch = flow_digger.start(wait=0.2)
    assert not launch.url and launch.can_force and "fast-forward" in launch.error
    monkeypatch.setattr(flow_digger.subprocess, "Popen", lambda *a, **k: None)  # forcé : on lance sans pull
    assert "ne répond pas" in flow_digger.start(wait=0.2, force=True).error


def test_no_pull_when_already_running(repo, monkeypatch):
    monkeypatch.setattr(flow_digger, "update", lambda r: (_ for _ in ()).throw(AssertionError("pas de pull")))
    with socket.socket() as server:
        server.bind(("127.0.0.1", flow_digger.port()))
        server.listen()
        assert flow_digger.start().url


def test_the_failure_page_offers_to_launch_anyway(monkeypatch):
    calls = []
    def fake(force=False):
        calls.append(force)
        return flow_digger.Launch(error="Mise à jour de Flow Digger impossible", can_force=True) if not force else flow_digger.Launch(url="http://localhost:4004")
    monkeypatch.setattr(flow_digger, "start", fake)
    client = TestClient(app)
    page = client.post("/outils/flow-digger", headers={"origin": "http://testserver"})
    assert "Lancer quand même la version actuelle" in page.text and 'name="forcer" value="1"' in page.text
    r = client.post("/outils/flow-digger", data={"forcer": "1"}, headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303 and calls == [False, True]
