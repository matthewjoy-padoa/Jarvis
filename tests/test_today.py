"""Journal des changements, page « Aujourd'hui », « Quoi de neuf » et injections regroupées par ressource."""

import json
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from deploy_hub import main
from deploy_hub.models import Action, Deployment, RetourRecette
from deploy_hub.sources.journal import changes_since
from deploy_hub.sync_notion import append_journal, diff_caches

R = "https://app.notion.com/" + "a" * 32
C = "https://app.notion.com/" + "c" * 32


def test_diff_caches_notes_status_and_text_changes():
    before = {R: {"url": R, "statut": "À faire", "commentaire_client": "Il manque des visites", "analyse": None}}
    after = {R: {"url": R, "statut": "OPS - Vérifié en SNAP", "commentaire_client": "Il manque des visites", "analyse": "Filtre trop strict"},
             C: {"url": C, "statut": "À faire"}}
    events = diff_caches(before, after, "recette", "2026-11-06T09:00:00")
    assert {(e["type"], e.get("champ")) for e in events} == {("modifie", "statut"), ("modifie", "analyse"), ("nouveau", None)}
    status = next(e for e in events if e.get("champ") == "statut")
    assert (status["avant"], status["apres"]) == ("À faire", "OPS - Vérifié en SNAP")
    assert diff_caches(None, after, "recette", "x") == []  # premier passage : pas d'historique
    assert [e["type"] for e in diff_caches(after, {}, "recette", "x")] == ["supprime", "supprime"]


def test_unchanged_or_equivalent_values_are_not_events():
    row = {"url": R, "statut": "À faire", "vigilance": "__NO__", "deadline": None}
    assert diff_caches({R: row}, {R: {**row, "vigilance": False, "deadline": ""}}, "actions", "x") == []


def test_journal_keeps_60_days(tmp_path):
    old = (datetime.now() - timedelta(days=61)).isoformat(timespec="seconds")
    (tmp_path / "journal.json").write_text(json.dumps([{"quand": old, "base": "actions", "url": C, "type": "nouveau"}]))
    append_journal(tmp_path, [{"quand": datetime.now().isoformat(timespec="seconds"), "base": "actions", "url": C, "type": "nouveau"}])
    assert len(json.loads((tmp_path / "journal.json").read_text())) == 1


@pytest.fixture
def acme(tmp_path):
    today = date.today()
    d = Deployment(
        slug="acme", client="Acme", software="Uegar",
        milestones={"deploiement": today + timedelta(days=10)},
        actions=[
            Action(title="Injection des firms", injection=True, injection_status="Vérification KO recette", resources=["firm"], notion_url=C),
            Action(title="Injection des firms (lot 2)", injection=True, injection_status="Validé", resources=["firm"]),
            Action(title="Injection des visites", injection=True, injection_status="A faire", resources=["visit_histo"]),
            Action(title="Préparer le COPIL", status="À faire", due=today - timedelta(days=2)),
        ],
        retours=[RetourRecette(id="0001AB", data_type="Adhérents", status="À faire", due=today, notion_url=R)],
    )
    deployments_dir = tmp_path / "deploiements"
    (tmp_path / "notion" / "acme").mkdir(parents=True)
    events = [
        {"quand": (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"), "base": "recette", "url": R,
         "type": "modifie", "champ": "statut", "avant": "Non vérifié", "apres": "À faire"},
        {"quand": (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds"), "base": "recette", "url": R,
         "type": "modifie", "champ": "commentaire_client", "avant": "Ancien texte", "apres": "Nouveau texte"},
        {"quand": (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds"), "base": "actions", "url": C,
         "type": "modifie", "champ": "statut_injection", "avant": "A faire", "apres": "Vérification KO recette"},
    ]
    (tmp_path / "notion" / "acme" / "journal.json").write_text(json.dumps(events))
    return d, deployments_dir


def test_changes_since_are_linked_to_retours_and_cards(acme):
    d, deployments_dir = acme
    changes = changes_since(d, deployments_dir, datetime.now() - timedelta(days=1))
    assert [c.verb for c in changes] == ["statut", "commentaire client modifié"]
    assert changes[0].label == "Retour 0001AB · Adhérents" and changes[0].link == "/deploiements/acme/ticket/retour/0001AB"
    assert len(changes_since(d, deployments_dir, datetime.now() - timedelta(days=7))) == 3


@pytest.fixture
def app(acme, monkeypatch):
    d, deployments_dir = acme
    monkeypatch.setattr(main.fiches, "get_deployment", lambda slug: d if slug == "acme" else None)
    monkeypatch.setattr(main.fiches, "list_deployments", lambda: [d])
    monkeypatch.setattr(main, "settings", type(main.settings)(**{**main.settings.__dict__, "deployments_dir": deployments_dir}))
    return TestClient(main.app)


def test_today_page(app):
    html = app.get("/").text
    assert "Accueil" in html and "Préparer le COPIL" in html and "Retour 0001AB" in html  # en retard, aujourd'hui
    assert "Injection des firms" in html and 'injections#acme-firm"' in html  # KO, lien vers le bloc
    assert "Déploiement" in html and "J-10" in html  # prochain jalon
    assert 'hx-get="/quoi-de-neuf"' in html


def test_whats_new_fragment(app):
    since = (datetime.now() - timedelta(days=1)).isoformat()
    html = app.get(f"/quoi-de-neuf?depuis={since}").text
    assert "<strong>1</strong> élément(s) modifié(s)" in html  # le retour : statut et commentaire sur une seule ligne
    assert html.count('class="wn-item"') == 1 and "Non vérifié" in html
    assert 'class="badge g-todo">À faire' in html and "Commentaire client" in html
    # seulement ce qui a été ajouté, précédé d'un « + » vert ; ni le texte retiré ni le contexte
    assert '<span class="wn-plus" aria-label="ajouté">+</span>Nouveau</p>' in html and "Ancien" not in html
    week = app.get(f"/quoi-de-neuf?depuis={(datetime.now() - timedelta(days=7)).isoformat()}").text
    assert "<strong>2</strong> élément(s) modifié(s)" in week and week.count('class="wn-day"') == 2


def test_injections_grouped_by_resource(acme):
    d, _ = acme
    blocks = d.injection_blocks
    assert [b["label"] for b in blocks] == ["firm", "visit_histo"]  # le bloc avec un KO d'abord
    assert blocks[0]["counts"]["ko"] == 1 and blocks[0]["counts"]["done"] == 1 and blocks[0]["id"] == "firm"


def test_grouped_view_is_the_default(app):
    html = app.get("/deploiements/acme/injections").text
    assert 'id="acme-firm"' in html and 'class="res-block worst-0"' in html and "open" in html
    assert 'id="acme-firm"' not in app.get("/deploiements/acme/injections?vue=liste").text


def test_actions_tab_has_a_multi_team_filter(app, acme):
    d, _ = acme
    d.actions.append(Action(title="Préparer le support", status="À faire", due=date.today(), teams=["INTE", "OPS"]))
    html = app.get("/deploiements/acme/actions?tous=true").text
    assert '<th data-filter="equipe" data-multi>Équipe</th>' in html
    assert 'data-values="INTE|OPS"' in html and '<span class="team team-ops">OPS</span>' in html
    assert 'data-values="Sans équipe"' in html



def test_changes_are_grouped_by_item_and_day(acme):
    from deploy_hub.sources.journal import group_changes
    d, deployments_dir = acme
    days = group_changes(changes_since(d, deployments_dir, datetime.now() - timedelta(days=7)))
    assert days[0][0] == "Aujourd'hui" and len(days) == 2
    retour = days[0][1][0]
    assert retour.label == "Retour 0001AB · Adhérents" and len(retour.changes) == 2
    assert [c.field for c in retour.status_changes] == ["statut"] and [c.field for c in retour.other_changes] == ["commentaire_client"]


def test_same_field_changed_twice_keeps_start_and_end(acme):
    from deploy_hub.sources.journal import Change, group_changes
    d, _ = acme
    t = datetime.now()
    first = Change(t - timedelta(hours=2), d, "retour", "Retour X", "", "u", "modifie", "statut", "À faire", "INTE - En cours")
    second = Change(t - timedelta(hours=1), d, "retour", "Retour X", "", "u", "modifie", "statut", "INTE - En cours", "Validé")
    item = group_changes([second, first])[0][1][0]
    assert [(c.before, c.after) for c in item.changes] == [("À faire", "Validé")]



def test_word_diff_like_git():
    from deploy_hub.sources.journal import word_diff
    before = "Le client voit des visites en double sur la fiche du salarié depuis la reprise de juin, il faut corriger le filtre du transform."
    after = before.replace("le filtre du transform.", "le filtre de date du transform et relancer le DAG.")
    diff = word_diff(before, after)
    assert ("+", "de date ") in diff and ("-", "transform.") in diff and ("+", "transform et relancer le DAG.") in diff
    assert diff[0] == ("=", "… de juin, il faut corriger le filtre ")  # contexte raccourci avant la modification
    assert word_diff("", "Nouvelle analyse.") == [("+", "Nouvelle analyse.")]
    assert word_diff("Texte retiré.", "") == [("-", "Texte retiré.")]


def test_ticket_of_a_retour_shows_details_content_and_history(app):
    html = app.get("/deploiements/acme/ticket/retour/0001ab", headers={"HX-Request": "true"}).text
    assert 'class="ticket"' in html and "<html" not in html  # fragment pour le panneau
    assert "Informations" in html and "Contenu du retour" in html and "Historique" in html
    assert "Non vérifié" in html and "Nouveau" in html  # changement de statut et texte ajouté, notés par la synchro
    assert "Lancer un agent" in html or "Aucun agent" in html
    page = app.get("/deploiements/acme/ticket/retour/0001AB").text
    assert "<html" in page and 'class="ticket"' in page  # page entière dans un nouvel onglet


def test_ticket_of_an_action_and_unknown_tickets(app, acme):
    d, _ = acme
    a = next(a for a in d.actions if a.ref)
    html = app.get(f"/deploiements/acme/ticket/action/{a.ref}", headers={"HX-Request": "true"}).text
    assert a.title in html and "Équipe(s)" in html and "Ouvrir dans Notion" in html
    assert app.get("/deploiements/acme/ticket/retour/ZZZZZZ").status_code == 404
    assert app.get("/deploiements/acme/ticket/autre/0001AB").status_code == 404


def test_lists_open_tickets_in_the_drawer(app):
    for path in ["/deploiements/acme/recette?tous=true", "/deploiements/acme/actions?tous=true", "/deploiements/acme/recette?vue=kanban&tous=true", "/"]:
        html = app.get(path).text
        assert 'data-ticket href="/deploiements/acme/ticket/' in html, path
        assert path == "/" or "data-ticket-card" in html, path  # toute la carte ou la ligne est cliquable
