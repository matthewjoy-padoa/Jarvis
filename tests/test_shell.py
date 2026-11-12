"""Shell de la VM dans la console : refusé par défaut et hors garde-fous ; une vraie commande passe par le pseudo-terminal."""

import json
import socket
import threading
import time

import pytest
import uvicorn
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from deploy_hub import shell
from deploy_hub.main import app

ORIGIN = {"origin": "http://testserver"}  # la page de Jarvis elle-même (hôte du TestClient)


def url(token=None):
    return f"/terminal/ws?token={shell.TOKEN if token is None else token}"


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_SHELL", "1")
    monkeypatch.setenv("SHELL", "/bin/sh")  # shell court et prévisible pour les tests
    monkeypatch.setattr(shell, "LOCAL_HOSTS", {"testclient"})  # l'adresse que donne le TestClient
    monkeypatch.setattr(shell, "LOCAL_NAMES", shell.LOCAL_NAMES | {"testserver"})  # le nom d'hôte du TestClient


def refused(path, headers=None) -> str:
    with pytest.raises(WebSocketDisconnect) as e:
        with TestClient(app).websocket_connect(path, headers=headers or {}) as ws:
            ws.receive_bytes()
    assert e.value.code == shell.REFUSED
    return e.value.reason


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_SHELL", raising=False)
    assert "désactivé" in refused(url(), ORIGIN)


@pytest.mark.parametrize(("path", "headers", "why"), [
    (url(""), ORIGIN, "jeton"),                                    # pas de jeton
    (url("faux"), ORIGIN, "jeton"),                                # mauvais jeton
    (url(), {}, "ne vient pas de Jarvis"),                         # pas d'origine
    (url(), {"origin": "https://site-hostile.example"}, "ne vient pas de Jarvis"),  # page d'un autre site
])
def test_guards_refuse(enabled, path, headers, why):
    assert why in refused(path, headers)


def test_dns_rebinding_is_refused(enabled):
    """Un site qui fait pointer son nom vers 127.0.0.1 : même origine que l'hôte appelé, mais nom d'hôte non local."""
    hostile = {"host": "jarvis.site-hostile.example:8765", "origin": "http://jarvis.site-hostile.example:8765"}
    assert "localhost" in refused(url(), hostile)


def test_only_local_connections(enabled, monkeypatch):
    monkeypatch.setattr(shell, "LOCAL_HOSTS", {"127.0.0.1"})  # le TestClient n'est pas « local »
    assert "connexions locales" in refused(url(), ORIGIN)


@pytest.fixture
def server(enabled):
    """Un vrai serveur sur un port libre : le client de test de Starlette ne sait pas relayer un pseudo-terminal lu par un thread."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    end = time.time() + 10
    while not srv.started and time.time() < end:
        time.sleep(0.05)
    shell.LOCAL_HOSTS = {"127.0.0.1"}
    yield port
    srv.should_exit = True


def read_until(ws, needle: str, seconds: float = 8) -> str:
    seen, end = "", time.time() + seconds
    while needle not in seen and time.time() < end:
        try:
            seen += ws.recv(timeout=0.5).decode(errors="replace")
        except TimeoutError:
            pass
    assert needle in seen, f"{needle!r} jamais reçu : {seen[-300:]!r}"
    return seen


def test_a_real_command_runs_in_the_pty_without_jarvis_secrets(server, monkeypatch):
    monkeypatch.setenv("AIRFLOW_PASSWORD", "ne-doit-pas-sortir")
    with connect(f"ws://127.0.0.1:{server}/terminal/ws?token={shell.TOKEN}", origin=f"http://127.0.0.1:{server}", open_timeout=5) as ws:
        ws.send(json.dumps({"type": "resize", "cols": 120, "rows": 40}))  # taille envoyée par le navigateur
        ws.send(json.dumps({"type": "input", "data": "echo jarvis-$((40+2)) [${AIRFLOW_PASSWORD}] $(tput cols)\r"}))
        out = read_until(ws, "jarvis-42 [] 120")
        assert "ne-doit-pas-sortir" not in out
        ws.send(json.dumps({"type": "input", "data": "exit\r"}))
        with pytest.raises(ConnectionClosed):  # le shell sorti, la session se ferme
            for _ in range(40):
                ws.recv(timeout=0.5)


def test_the_page_carries_the_token_only_when_enabled(monkeypatch):
    monkeypatch.delenv("JARVIS_SHELL", raising=False)
    assert "data-shell-token" not in TestClient(app).get("/").text
    monkeypatch.setenv("JARVIS_SHELL", "1")
    assert f'data-shell-token="{shell.TOKEN}"' in TestClient(app).get("/").text
