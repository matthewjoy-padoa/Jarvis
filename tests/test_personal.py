"""Perso : brief du matin, tickets épinglés, notes par client, rappels (données fictives de demo/)."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from deploy_hub import brief, main
from deploy_hub.models import Action, Deployment, RetourRecette
from deploy_hub.sources.personal import PersonalStore, remind_date

SLUG, REF = "client-demo", "R-01"
TICKET = f"/deploiements/{SLUG}/ticket/retour/{REF}"


@pytest.fixture(autouse=True)
def clean():
    """Chaque test part sans épingle, note, rappel ni notification."""
    for path in (main.personal.path, main.notifications.path):
        path.unlink(missing_ok=True)
    yield


@pytest.fixture
def client():
    c = TestClient(main.app)
    c.cookies.set(main.CLIENT_COOKIE, SLUG)
    return c


# ------------------------------------------------------------ Stockage
def test_store_keeps_pins_notes_and_reminders_across_instances(tmp_path):
    store = PersonalStore(tmp_path / "perso.json")
    assert store.toggle_pin("acme", "retour", "0001AB") is True
    store.set_note("acme", "  Contact : la DSI  ")
    r = store.add_reminder(date(2026, 11, 16), "Relancer la DSI", "acme")
    again = PersonalStore(tmp_path / "perso.json")
    assert again.is_pinned("acme", "retour", "0001AB") and again.note("acme") == "Contact : la DSI"
    assert [(x.id, x.due, x.label) for x in again.reminders("acme")] == [(r.id, date(2026, 11, 16), "Relancer la DSI")]
    assert again.toggle_pin("acme", "retour", "0001AB") is False and again.pins() == []
    again.set_note("acme", "")
    again.remove_reminder(r.id)
    assert again.note("acme") == "" and again.reminders() == []


def test_reminders_of_a_client_include_the_free_ones_only(tmp_path):
    store = PersonalStore(tmp_path / "perso.json")
    store.add_reminder(date(2026, 11, 20), "Autre client", "autre")
    store.add_reminder(date(2026, 11, 18), "Libre")
    store.add_reminder(date(2026, 11, 17), "Acme", "acme")
    assert [r.label for r in store.reminders("acme")] == ["Acme", "Libre"]
    assert len(store.reminders()) == 3


@pytest.mark.parametrize(("when", "day", "expected"), [
    ("demain", "", date(2026, 11, 14)),
    ("lundi", "", date(2026, 11, 16)),      # vendredi 13 → lundi 16
    ("semaine", "", date(2026, 11, 20)),
    ("date", "2026-12-01", date(2026, 12, 1)),
    ("date", "2026-11-12", None),            # passée
    ("date", "pas une date", None),
    ("", "", None),
])
def test_remind_date(when, day, expected):
    assert remind_date(when, day, date(2026, 11, 13)) == expected


def test_monday_reminder_on_a_monday_is_next_week():
    assert remind_date("lundi", "", date(2026, 11, 16)) == date(2026, 11, 23)


# ------------------------------------------------------------ Brief
def test_brief_lists_late_today_week_ko_milestones_and_reminders(tmp_path):
    today = date(2026, 11, 13)
    d = Deployment(
        slug="acme", client="Acme", software="Uegar",
        milestones={"deploiement": today + timedelta(days=5), "freeze": today + timedelta(days=30)},
        actions=[Action(title="Injection des firms", injection=True, injection_status="Vérification KO recette", resources=["firm"]),
                 Action(title="Préparer le COPIL", status="À faire", due=today - timedelta(days=2)),
                 Action(title="Envoyer le planning", status="À faire", due=today + timedelta(days=3))],
        retours=[RetourRecette(id="0001AB", data_type="Adhérents", status="À faire", due=today)],
    )
    store = PersonalStore(tmp_path / "perso.json")
    reminders = [store.add_reminder(today, "Relancer la DSI", "acme"), store.add_reminder(today, "Poser mes congés")]
    text = brief.build([d], main._deadlines([d]), reminders, today, main._in_period)
    assert text.splitlines()[0] == "*Brief du vendredi 13 novembre*"
    for expected in ("*Acme* · déploiement le 18/11/2026 (J-5)", "🔴 En retard (1)", "• 11/11 · Action · Préparer le COPIL", "🟠 Aujourd'hui (1)",
                     "🟡 Dans les 7 jours (1)", "• 16/11 · Action · Envoyer le planning", "⚠️ Injections KO (1)", "• Injection des firms (firm)",
                     "🏁 Jalons dans les 7 jours (1)", "• Relancer la DSI", "• Poser mes congés"):
        assert expected in text, expected
    assert "13/12" not in text  # jalon à 30 jours : hors brief


def test_brief_of_a_quiet_client_says_so_and_long_sections_are_cut():
    today = date(2026, 11, 13)
    quiet = Deployment(slug="calme", client="Calme", software="Uegar")
    busy = Deployment(slug="acme", client="Acme", software="Uegar",
                      actions=[Action(title=f"Action {n}", status="À faire", due=today - timedelta(days=1)) for n in range(11)])
    text = brief.build([quiet, busy], main._deadlines([quiet, busy]), [], today, main._in_period)
    assert "Rien en retard ni à échéance cette semaine." in text
    assert "🔴 En retard (11)" in text and "• … et 3 autre(s)" in text and "Action 8" not in text


def test_home_has_the_brief_button_and_the_page_can_show_every_client(client):
    assert 'href="/brief"' in client.get("/").text
    one = client.get("/brief").text
    assert 'id="brief-copy"' in one and "*Client Démo*" in one and "*Client Exemple*" not in one
    assert "*Client Exemple*" in client.get("/brief?tous=true").text


# ------------------------------------------------------------ Épingles et notes
def test_pinning_a_ticket_puts_it_on_the_home_page(client):
    assert "Épingler" in client.get(TICKET).text and "Épinglés" not in client.get("/").text
    r = client.post(f"/perso/epingle/{SLUG}/retour/{REF.lower()}")
    assert r.status_code == 200 and "Épinglé" in r.text and 'aria-pressed="true"' in r.text
    home = client.get("/").text
    assert "Épinglés" in home and f'href="{TICKET}"' in home
    assert 'aria-pressed="false"' in client.post(f"/perso/epingle/{SLUG}/retour/{REF}").text
    assert "Épinglés" not in client.get("/").text


def test_pins_stay_with_their_client(client):
    client.post(f"/perso/epingle/{SLUG}/retour/{REF}")
    client.cookies.set(main.CLIENT_COOKIE, "client-exemple")
    assert "Épinglés" not in client.get("/").text


@pytest.mark.parametrize("url", [f"/perso/epingle/{SLUG}/retour/INCONNU", f"/perso/epingle/{SLUG}/autre/{REF}", f"/perso/epingle/inconnu/retour/{REF}"])
def test_only_known_tickets_can_be_pinned(client, url):
    assert client.post(url).status_code == 404 and main.personal.pins() == []


def test_a_client_note_is_saved_and_shown_on_its_synthese_only(client):
    r = client.post(f"/perso/note/{SLUG}", data={"texte": "Contact : la DSI <le lundi>"})
    assert r.status_code == 200 and "Enregistré à" in r.text
    assert "Contact : la DSI &lt;le lundi&gt;" in client.get(f"/deploiements/{SLUG}/synthese").text
    assert "Contact : la DSI" not in client.get("/deploiements/client-exemple/synthese").text
    assert client.post("/perso/note/inconnu", data={"texte": "x"}).status_code == 404


# ------------------------------------------------------------ Rappels
def test_a_ticket_reminder_takes_the_ticket_label_and_shows_on_the_ticket_and_home(client):
    r = client.post("/perso/rappel", data={"slug": SLUG, "kind": "retour", "ref": REF, "quand": "lundi", "label": "ignoré"})
    assert r.status_code == 200 and "Rappel le" in r.text
    [reminder] = main.personal.reminders()
    assert (reminder.slug, reminder.kind, reminder.ref) == (SLUG, "retour", REF) and reminder.label != "ignoré"
    assert reminder.due > date.today() and reminder.due.weekday() == 0
    assert "Rappel le" in client.get(TICKET).text and f'href="{TICKET}"' in client.get("/").text.split('id="reminders"')[1]


def test_a_free_reminder_needs_a_label_and_a_coming_date(client):
    assert "Écris ce qu" in client.post("/perso/rappel", data={"slug": SLUG, "quand": "demain", "label": " "}).text
    assert "Choisis une date à venir" in client.post("/perso/rappel", data={"slug": SLUG, "quand": "date", "jour": "2020-01-01", "label": "x"}).text
    assert main.personal.reminders() == []
    ok = client.post("/perso/rappel", data={"slug": SLUG, "quand": "demain", "label": "Relancer la DSI"})
    assert 'id="reminders"' in ok.text and "Relancer la DSI" in ok.text


def test_a_due_reminder_becomes_one_notification_until_done(client):
    due = main.personal.add_reminder(date.today(), "Relancer la DSI", SLUG, "retour", REF)
    main.personal.add_reminder(date.today() + timedelta(days=1), "Pas encore")
    for _ in range(2):  # deux passages de la veille : une seule notification
        client.get("/notifications/compte")
    notes = [n for n in main.notifications.all() if n.kind == "rappel"]
    assert [(n.title, n.link, n.client, n.icon) for n in notes] == [("Rappel : Relancer la DSI", TICKET, "Client Démo", "bell")]
    assert client.post(f"/perso/rappel/{due.id}/fait").text == ""
    assert [r.label for r in main.personal.reminders()] == ["Pas encore"]
