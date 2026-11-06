"""Notifications : stockage dédoublonné, urgences après une synchro, agent terminé, échéance du jour, API et panneau."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from deploy_hub import main
from deploy_hub.models import Action, Deployment, RetourRecette
from deploy_hub.sources.claude_runner import ClaudeRunner
from deploy_hub.sources.notifications import NotificationStore
from deploy_hub.sync_notion import sync_notes

R = "https://app.notion.com/" + "a" * 26 + "01db10"
C = "https://app.notion.com/" + "c" * 32
WHEN = "2026-11-06T10:00:00"


@pytest.fixture
def store(tmp_path):
    return NotificationStore(tmp_path / "notifications.json")


def test_same_key_is_notified_once(store):
    assert store.add("k1", "sync", "urgent", "Titre") is not None
    assert store.add("k1", "sync", "urgent", "Titre") is None
    store.add("k2", "agent", "info", "Autre")
    assert [n.key for n in store.all()] == ["k2", "k1"] and len(store.unread()) == 2
    store.mark_read([store.all()[0].id])
    assert [n.key for n in store.unread()] == ["k1"]
    store.mark_read()
    assert store.unread() == [] and [n.key for n in store.after(1)] == ["k2"]


def test_sync_notes_flag_urgent_changes():
    soon = (date.today() + timedelta(days=1)).isoformat()
    caches = {
        "recette": {R: {"url": R, "statut": "OPS - Vérifié et pas OK", "type_donnees": "AMT"}},
        "actions": {C: {"url": C, "intitule": "Injection des firms", "statut": "En cours", "injection": 1}},
    }
    events = [
        {"base": "recette", "url": R, "type": "modifie", "champ": "statut", "avant": "À faire", "apres": "OPS - Vérifié et pas OK"},
        {"base": "actions", "url": C, "type": "modifie", "champ": "statut_injection", "avant": "A faire", "apres": "Vérification KO recette"},
        {"base": "actions", "url": C, "type": "modifie", "champ": "vigilance", "avant": None, "apres": "__YES__"},
        {"base": "actions", "url": C, "type": "modifie", "champ": "deadline", "avant": None, "apres": soon},
        {"base": "actions", "url": C, "type": "modifie", "champ": "statut", "avant": "À faire", "apres": "En cours"},  # pas urgent
    ]
    notes = sync_notes("acme", "Acme", events, caches, WHEN)
    urgent = [n for n in notes if n["level"] == "urgent"]
    assert [n["detail"] for n in urgent] == ["retour passé en OPS - Vérifié et pas OK", "injection passée en Vérification KO recette",
                                             "point de vigilance levé", f"deadline amenée au {date.fromisoformat(soon).strftime('%d/%m')}"]
    assert urgent[0]["title"] == "Acme · Retour 01DB10 · AMT" and urgent[0]["link"] == "/deploiements/acme/recette?tous=true#01DB10"
    assert [n["detail"] for n in notes if n["level"] == "info"] == ["1 autre(s) changement(s) depuis la synchro précédente"]


def test_new_open_retour_is_urgent_and_far_deadline_is_not():
    caches = {"recette": {R: {"url": R, "statut": "À faire"}}, "actions": {C: {"url": C, "statut": "À faire"}}}
    far = (date.today() + timedelta(days=30)).isoformat()
    notes = sync_notes("acme", "Acme", [{"base": "recette", "url": R, "type": "nouveau"},
                                        {"base": "actions", "url": C, "type": "modifie", "champ": "deadline", "apres": far}], caches, WHEN)
    assert [(n["level"], n["detail"]) for n in notes] == [("urgent", "nouveau retour de recette"),
                                                         ("info", "1 autre(s) changement(s) depuis la synchro précédente")]


def test_agent_end_is_notified(store, tmp_path):
    runner = ClaudeRunner(tmp_path, notifications=store)
    d = Deployment(slug="acme", client="Acme", software="Uegar", retours=[RetourRecette(id="01DB10", status="À faire")])
    runner.launch_retour(d, d.retours[0], prompt="brief")
    note = store.all()[0]
    assert note.kind == "agent" and note.title == "Agent #1 terminé (simulé) · Acme" and runner.runs[0].status == "terminé (simulé)"


@pytest.fixture
def app(store, monkeypatch):
    today = date.today()
    d = Deployment(slug="acme", client="Acme", software="Uegar", milestones={"freeze": today},
                   actions=[Action(title="Envoyer le planning", due=today), Action(title="Plus tard", due=today + timedelta(days=3))])
    monkeypatch.setattr(main.fiches, "list_deployments", lambda: [d])
    monkeypatch.setattr(main, "notifications", store)
    return TestClient(main.app)


def test_deadlines_of_the_day_once(app, store):
    app.get("/notifications/compte")
    app.get("/notifications/compte")
    titles = sorted(n.detail for n in store.all() if n.kind == "deadline")
    assert titles == ["Action : Envoyer le planning", "Jalon : Freeze"]


def test_bell_panel_and_mark_read(app, store):
    assert 'class="bell-count urgent"' in app.get("/notifications/compte").text
    panel = app.get("/notifications/panneau").text
    assert "Acme · échéance aujourd&#39;hui" in panel and "Tout marquer comme lu" in panel
    r = app.post("/notifications/lu")
    assert r.headers["HX-Trigger"] == "notifs-changed" and store.unread() == []
    assert app.get("/notifications/compte").text.strip() == ""
    assert "Notifications" in app.get("/notifications").text


def test_new_notifications_api(app, store):
    items = app.get("/notifications/nouvelles?apres=0").json()
    assert len(items) == 2 and {i["level"] for i in items} == {"urgent"}
    assert app.get(f"/notifications/nouvelles?apres={max(i['id'] for i in items)}").json() == []


def test_notification_links_navigate_and_mark_read_separately(app, store):
    panel = app.get("/notifications/panneau").text
    assert "hx-post" not in panel.split('<ul class="notif-list">', 1)[1].split("</ul>", 1)[0]  # htmx bloquerait la navigation
    assert "data-read-id=" in panel
    first = store.all()[0]
    app.post("/notifications/lu", data={"id": first.id})
    assert next(n for n in store.all() if n.id == first.id).read


def test_old_notion_links_in_notifications_are_fixed(store):
    store.add("old", "deadline", "urgent", "Acme · échéance aujourd'hui", link="https://app.notion.com/" + "a" * 32)
    assert store.all()[0].link == "https://app.notion.com/p/" + "a" * 32
