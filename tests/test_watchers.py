"""Veilles : synchro en retard, santé qui se dégrade, jalon proche, échec du rafraîchissement Notion."""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from deploy_hub import main
from deploy_hub.models import Action, Deployment, RetourRecette
from deploy_hub.sources import watchers
from deploy_hub.sources.notifications import NotificationStore

MONDAY_14H = datetime(2026, 11, 9, 14, 0)


def dep(**kw) -> Deployment:
    return Deployment(slug="acme", client="Acme", software="Uegar", **kw)


def test_sync_late_only_on_working_hours():
    d = dep(synced_at="09/11/2026 08:05")
    notes = watchers.sync_late_notes([d], MONDAY_14H)
    assert [n["title"] for n in notes] == ["Acme · synchro en retard"] and "09/11 à 08:05" in notes[0]["detail"]
    assert watchers.sync_late_notes([dep(synced_at="09/11/2026 12:00")], MONDAY_14H) == []  # 2 h : normal
    assert watchers.sync_late_notes([d], MONDAY_14H.replace(hour=21)) == []  # le soir
    assert watchers.sync_late_notes([d], datetime(2026, 11, 8, 14, 0)) == []  # dimanche


def test_health_degradation_is_notified_once_seen():
    today = date.today()
    late = dep(milestones={"reception_donnees": today - timedelta(days=80), "freeze": today + timedelta(days=20)},
               injection_counts={"A faire": 10})  # 0 % intégré, attendu 80 % → 🔴
    notes, state = watchers.health_notes([late], {}, MONDAY_14H)
    assert notes == [] and state == {"acme": "🔴"}  # première vue : on mémorise sans notifier
    notes, state = watchers.health_notes([late], {"acme": "🟢"}, MONDAY_14H)
    assert notes[0]["title"] == "Acme · avancement : dans les temps → très en retard" and notes[0]["level"] == "urgent" and "attendu" in notes[0]["detail"]
    assert watchers.health_notes([late], {"acme": "🔴"}, MONDAY_14H)[0] == []  # pas de dégradation


def test_key_milestone_at_j7_and_j1():
    today = date(2026, 11, 9)
    d = dep(milestones={"freeze": today + timedelta(days=1), "deploiement": today + timedelta(days=7), "formation": today + timedelta(days=1)},
            actions=[Action(title="Injection firm", injection=True, injection_status="Vérification KO recette"),
                     Action(title="Injection visites", injection=True, injection_status="Validé")],
            retours=[RetourRecette(id="0001AB", status="À faire")])
    notes = {n["title"]: n for n in watchers.milestone_notes([d], today)}
    assert set(notes) == {"Acme · Freeze demain (10/11)", "Acme · Déploiement dans 7 jours (16/11)"}  # formation : pas un jalon suivi
    assert notes["Acme · Freeze demain (10/11)"]["level"] == "urgent"
    assert notes["Acme · Freeze demain (10/11)"]["detail"] == "Reste 1 injection(s) non terminée(s) dont 1 KO, et 1 retour(s) de recette ouvert(s)."
    assert notes["Acme · Déploiement dans 7 jours (16/11)"]["level"] == "info"
    assert watchers.milestone_notes([d], today + timedelta(days=2)) == []


@pytest.fixture
def store(tmp_path):
    return NotificationStore(tmp_path / "notifications.json")


def test_watchers_run_with_the_bell_and_keep_health_state(store, monkeypatch):
    d = dep(synced_at="01/01/2020 08:00")
    monkeypatch.setattr(main.vault, "list_deployments", lambda: [d])
    monkeypatch.setattr(main, "notifications", store)
    TestClient(main.app).get("/notifications/compte")
    assert store.load_state() == {"sante": {"acme": d.health}}


def test_refresh_failure_is_notified(store, monkeypatch):
    class Boom:
        def __init__(self, token): pass
    monkeypatch.setattr(main, "notifications", store)
    monkeypatch.setattr(main.notion_api, "load_token", lambda root=None: "jeton-de-test")
    monkeypatch.setattr(main.notion_api, "NotionClient", Boom)
    monkeypatch.setattr(main.notion_api, "refresh", lambda slug, notion: (_ for _ in ()).throw(RuntimeError("Notion indisponible")))
    ok, message = main._refresh(["acme"])
    assert not ok and "Notion indisponible" in message
    note = store.all()[0]
    assert note.title == "acme · rafraîchissement Notion en échec" and note.level == "urgent"
