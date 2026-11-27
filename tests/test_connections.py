"""Lignes de lancement : Airflow et Notion vraiment joignables, ou pourquoi pas (contre de faux serveurs, sans réseau)."""

from datetime import datetime, timezone
import logging

import httpx
import pytest
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
    assert connections.airflow_status(AirflowSource("", "", ""))[1].startswith("non configuré : AIRFLOW_BASE_URL")


def notion_answering(status: int, body=None):
    return httpx.MockTransport(lambda request: httpx.Response(status, json=body or {}))


def test_notion_connected_names_the_integration():
    assert connections.notion_status("ntn_x", transport=notion_answering(200, {"name": "Jarvis"})) == (True, "connecté (intégration « Jarvis »)")


def test_notion_refused_or_missing():
    assert connections.notion_status("ntn_x", transport=notion_answering(401)) == (False, "jeton refusé par Notion (401) : vérifie NOTION_TOKEN")
    assert "pas de NOTION_TOKEN" in connections.notion_status(None)[1]


def test_lines_are_short_and_marked():
    assert connections.lines((True, "connecté à http://localhost:8888"), (False, "pas de NOTION_TOKEN")) == [
        "Jarvis · Airflow : ✓ connecté à http://localhost:8888", "Jarvis · Notion : ✗ pas de NOTION_TOKEN"]


def test_the_lines_are_printed_when_jarvis_starts(caplog, monkeypatch):
    from deploy_hub import main
    monkeypatch.setattr(connections, "notion_status", lambda token: (False, "pas de NOTION_TOKEN"))
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with TestClient(main.app):  # démarrage (lifespan) puis arrêt
            pass
    assert any(m.startswith("Jarvis · Airflow : ") for m in caplog.messages)
    assert "Jarvis · Notion : ✗ pas de NOTION_TOKEN" in caplog.messages


# --- Claude Code : toujours connecté ?
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def fake_claude(tmp_path, answer: str, code: int = 0) -> str:
    """Faux `claude` : répond à « auth status --json » ce qu'on lui donne."""
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\ncat <<'JSON'\n{answer}\nJSON\nexit {code}\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def test_claude_code_logged_in_far_from_expiry_is_fine(tmp_path):
    login = connections.claude_status(fake_claude(tmp_path, '{"loggedIn": true, "loginExpiresAt": "2026-12-20T17:39:08.535Z", "email": "x@y.z"}'), NOW)
    assert login.ok and not login.alert and login.word == "connecté" and login.message == "connecté (jusqu'au 20/12/2026)"
    assert login.line == "Jarvis · Claude Code : ✓ connecté (jusqu'au 20/12/2026)" and "x@y.z" not in login.line  # aucune identité affichée
    assert connections.claude_status(fake_claude(tmp_path, '{"loggedIn": true}'), NOW).message == "connecté"  # clé d'API : pas d'expiration


def test_claude_code_logged_out_or_expired_is_an_alert_that_says_what_to_do(tmp_path):
    for answer in ('{"loggedIn": false}', '{"loggedIn": true, "loginExpiresAt": "2026-11-20T11:59:00Z"}'):
        login = connections.claude_status(fake_claude(tmp_path, answer, code=1), NOW)
        assert not login.ok and login.alert and login.word == "déconnecté"
        assert "claude auth login" in login.message and login.line.startswith("Jarvis · Claude Code : ✗ déconnecté")


def test_claude_code_about_to_expire_warns_before_it_stops(tmp_path):
    login = connections.claude_status(fake_claude(tmp_path, '{"loggedIn": true, "loginExpiresAt": "2026-11-22T17:39:00Z"}'), NOW)
    assert login.ok and login.alert and "expire le 22/11 à 18:39" in login.message and "claude auth login" in login.message  # heure de Paris
    assert login.line.startswith("Jarvis · Claude Code : ⚠ connecté, mais")
    later = connections.claude_status(fake_claude(tmp_path, '{"loggedIn": true, "loginExpiresAt": "2026-11-24T17:39:00Z"}'), NOW)
    assert later.ok and not later.alert  # plus de trois jours : rien à signaler
    bare = connections.claude_status(fake_claude(tmp_path, '{"loggedIn": true, "loginExpiresAt": "2026-11-22T17:39:00"}'), NOW)
    assert bare.alert and "expire le 22/11 à 18:39" in bare.message  # date sans fuseau : lue en UTC, pas d'erreur


def test_an_unreadable_state_is_said_but_is_not_an_alert(tmp_path):
    missing = connections.claude_status(str(tmp_path / "claude-absent"), NOW)
    assert not missing.ok and not missing.alert and missing.word == "absent" and "CLAUDE_BIN" in missing.message
    odd = connections.claude_status(fake_claude(tmp_path, "error: unknown command 'auth'"), NOW)
    assert not odd.ok and not odd.alert and odd.word == "inconnu" and "claude auth status" in odd.message


def test_jarvis_warns_at_startup_then_every_hour_and_notifies_once_a_day(tmp_path, monkeypatch, caplog):
    from deploy_hub import main
    from deploy_hub.sources.notifications import NotificationStore
    notes = NotificationStore(tmp_path / "notifications.json")
    monkeypatch.setattr(main, "notifications", notes)
    state = [connections.ClaudeLogin(False, "déconnecté : lance « claude auth login »", alert=True, word="déconnecté")]
    asked = []
    monkeypatch.setattr(main.connections, "claude_status", lambda claude_bin: asked.append(claude_bin) or state[0])
    clock = [5000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
    with caplog.at_level("INFO", logger="uvicorn.error"):
        main._check_claude_login(startup=True)
        assert [r.levelname for r in caplog.records] == ["WARNING"] and "Claude Code : ✗ déconnecté" in caplog.text
        assert [(n.title, n.level, n.link) for n in notes.all()] == [("Claude Code déconnecté", "urgent", "/sources")]
        main._check_claude_login()  # tour suivant de la boucle de fond, une minute après : rien
        clock[0] += main.CLAUDE_CHECK_S
        main._check_claude_login()  # une heure après : revérifié, la même notification n'est pas ajoutée deux fois le même jour
        assert len(asked) == 2 and len(notes.all()) == 1 and len(caplog.records) == 2
        state[0] = connections.ClaudeLogin(True, "connecté, mais la connexion expire le 22/11 à 18:39", alert=True)
        clock[0] += main.CLAUDE_CHECK_S
        main._check_claude_login()
        assert notes.all()[0].title == "Claude Code : connexion bientôt expirée" and notes.all()[0].level == "info"
        caplog.clear()
        state[0] = connections.ClaudeLogin(True, "connecté")
        main._check_claude_login(startup=True)
        assert [r.levelname for r in caplog.records] == ["INFO"] and "Claude Code : ✓ connecté" in caplog.text
        clock[0] += main.CLAUDE_CHECK_S
        main._check_claude_login()  # tout va bien : ni ligne ni notification
        assert len(caplog.records) == 1 and len(notes.all()) == 2
    assert "_check_claude_login" in __import__("inspect").getsource(main._push_round)
    assert "_check_claude_login, True" in __import__("inspect").getsource(main.lifespan)
    assert main._claude_login == state[0]  # la page État des systèmes montre ce dernier état, sans relancer claude


def test_the_sources_page_shows_the_claude_code_login(monkeypatch):
    from fastapi.testclient import TestClient
    from deploy_hub import main
    monkeypatch.setattr(main, "_claude_login", connections.ClaudeLogin(False, "déconnecté : lance « claude auth login »", alert=True, word="déconnecté"))
    monkeypatch.setattr(main.connections, "claude_status", lambda claude_bin: pytest.fail("claude ne doit pas être relancé à chaque affichage"))
    page = TestClient(main.app).get("/sources").text
    assert "<strong>Claude Code (connexion)</strong>" in page and '<span class="dot off"></span>déconnecté' in page and "claude auth login" in page
