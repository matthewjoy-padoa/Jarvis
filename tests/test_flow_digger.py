"""Bouton Flow Digger : démarre l'app sur la VM seulement si elle ne répond pas, puis y emmène."""

import socket
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from deploy_hub.main import app
from deploy_hub.sources import flow_digger


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
    monkeypatch.setattr(flow_digger, "start", lambda: flow_digger.Launch(url="http://localhost:4004"))
    r = client.post("/outils/flow-digger", headers=same, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "http://localhost:4004"
    monkeypatch.setattr(flow_digger, "start", lambda: flow_digger.Launch(error="Flow Digger introuvable : clone le dépôt"))
    r = client.post("/outils/flow-digger", headers=same)
    assert r.status_code == 200 and "Flow Digger introuvable" in r.text


def test_reloading_the_tab_launches_nothing(monkeypatch):
    monkeypatch.setattr(flow_digger, "start", no_launch)
    r = TestClient(app).get("/outils/flow-digger", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
