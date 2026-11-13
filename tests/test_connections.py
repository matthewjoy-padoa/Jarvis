"""Lignes de lancement : Airflow et Notion vraiment joignables, ou pourquoi pas (contre de faux serveurs, sans réseau)."""

import logging

import httpx
from fastapi.testclient import TestClient

from deploy_hub.sources import connections
from deploy_hub.sources.airflow import AirflowSource


def airflow_answering(status: int) -> AirflowSource:
    seen = []

    def fake(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"dags": [], "total_entries": 0})

    airflow = AirflowSource("http://localhost:8888", "jarvis", "mdp", transport=httpx.MockTransport(fake))
    airflow.seen = seen
    return airflow


def test_airflow_connected_with_a_read_only_request():
    airflow = airflow_answering(200)
    assert connections.airflow_status(airflow) == (True, "connecté à http://localhost:8888")
    request = airflow.seen[0]
    assert request.method == "GET" and request.url.path == "/api/v1/dags" and request.headers["authorization"].startswith("Basic ")


def test_airflow_refused_credentials():
    ok, message = connections.airflow_status(airflow_answering(401))
    assert not ok and "identifiants refusés" in message and "mdp" not in message


def test_airflow_unreachable():
    def down(request):
        raise httpx.ConnectError("refusé")
    airflow = AirflowSource("http://localhost:8081", "jarvis", "mdp", transport=httpx.MockTransport(down))
    assert connections.airflow_status(airflow) == (False, "injoignable sur http://localhost:8081 (ConnectError)")


def test_airflow_simulated_says_why():
    assert connections.airflow_status(AirflowSource("", "", ""))[1].startswith("simulé : AIRFLOW_BASE_URL")
    ok, message = connections.airflow_status(AirflowSource("http://localhost:8888", "", ""), "AIRFLOW_USER : does not have a field 'user'")
    assert not ok and "référence 1Password illisible" in message and "does not have a field" in message


def notion_answering(status: int, body=None):
    return httpx.MockTransport(lambda request: httpx.Response(status, json=body or {}))


def test_notion_connected_names_the_integration():
    assert connections.notion_status("ntn_x", transport=notion_answering(200, {"name": "Jarvis"})) == (True, "connecté (intégration « Jarvis »)")


def test_notion_refused_or_missing():
    assert connections.notion_status("ntn_x", transport=notion_answering(401)) == (False, "jeton refusé par Notion (401) : vérifie NOTION_TOKEN")
    assert "pas de NOTION_TOKEN" in connections.notion_status(None)[1]
    assert "jeton illisible" in connections.notion_status(None, "not signed in")[1]


def test_lines_are_short_and_marked():
    assert connections.lines((True, "connecté à http://localhost:8888"), (False, "pas de NOTION_TOKEN")) == [
        "Jarvis · Airflow : ✓ connecté à http://localhost:8888", "Jarvis · Notion : ✗ pas de NOTION_TOKEN"]


def test_the_lines_are_printed_when_jarvis_starts(caplog, monkeypatch):
    from deploy_hub import main
    monkeypatch.setattr(connections, "notion_status", lambda token, problem="": (False, "pas de NOTION_TOKEN"))
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with TestClient(main.app):  # démarrage (lifespan) puis arrêt
            pass
    assert any(m.startswith("Jarvis · Airflow : ") for m in caplog.messages)
    assert "Jarvis · Notion : ✗ pas de NOTION_TOKEN" in caplog.messages
