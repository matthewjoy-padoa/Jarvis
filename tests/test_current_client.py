"""Un client à la fois : client en cours (cookie), sélecteur, accueil et Deadlines recentrés, changement de client."""

import pytest
from fastapi.testclient import TestClient

from deploy_hub.main import CLIENT_COOKIE, app


def fresh() -> TestClient:
    return TestClient(app)  # chaque test part sans cookie


def test_default_client_is_the_closest_go_live():
    nav = fresh().get("/").text.split("<main>", 1)[0]
    assert "Client en cours : Client Démo" in nav  # déploiement dans 25 jours, contre 67 pour Client Exemple


def test_opening_a_fiche_makes_it_the_current_client():
    c = fresh()
    page = c.get("/deploiements/client-exemple/actions")
    assert page.cookies.get(CLIENT_COOKIE) == "client-exemple"
    assert "Client en cours : Client Exemple" in c.get("/").text.split("<main>", 1)[0]


@pytest.mark.parametrize(("vers", "target"), [
    ("/deploiements/client-demo/recette", "/deploiements/client-exemple/recette"),  # même onglet, autre client
    ("/deploiements/client-demo", "/deploiements/client-exemple/synthese"),
    ("/", "/"),
    ("/deadlines", "/deadlines"),
    ("https://ailleurs.example/", "/deploiements/client-exemple/synthese"),  # jamais de redirection hors de Jarvis
    ("/agents", "/agents"),  # page globale : ses filtres suivent le client du menu
    ("/sources", "/deploiements/client-exemple/synthese"),
])
def test_switching_client_keeps_the_page(vers, target):
    r = fresh().get("/client/client-exemple", params={"vers": vers}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == target and r.cookies.get(CLIENT_COOKIE) == "client-exemple"


def test_switching_to_an_unknown_client_is_refused():
    assert fresh().get("/client/inconnu", follow_redirects=False).status_code == 404


def test_home_and_deadlines_show_only_the_current_client():
    c = fresh()
    c.get("/client/client-exemple")
    home, deadlines = c.get("/").text, c.get("/deadlines").text
    assert "Client Démo" not in home.split("<main>", 1)[1]  # rien de l'autre client dans le contenu de l'accueil
    assert "Client Démo" not in deadlines.split("<main>", 1)[1].split("Tous les clients")[0]
    assert "Client Démo" in c.get("/deadlines", params={"tous": "true"}).text.split("<main>", 1)[1]


def test_whats_new_on_home_is_scoped_to_the_current_client():
    c = fresh()
    c.get("/client/client-exemple")
    assert "client=client-exemple" in c.get("/").text



def test_switching_client_keeps_the_options_of_a_global_page():
    def target(vers):
        return fresh().get("/client/client-exemple", params={"vers": vers}, follow_redirects=False).headers["location"]
    assert target("/deadlines?client=client-demo&vue=calendrier&mois=2026-12") == "/deadlines?vue=calendrier&mois=2026-12"  # le menu gagne
    assert target("/actions?tous=true&vue=kanban") == "/actions?tous=true&vue=kanban"  # ici tous=true : actions terminées comprises


def test_the_menu_client_is_given_to_the_filters():
    c = fresh()
    c.get("/deploiements/client-exemple/actions")
    html = c.get("/recette?tous=true").text
    assert '<body data-current-client="Client Exemple">' in html
    assert "/client/client-demo?vers=/recette%3Ftous%3Dtrue" in html  # le menu garde la page et ses options


def test_the_current_client_fiche_is_one_click_away():
    c = fresh()
    c.get("/deploiements/client-exemple/actions")
    bar = c.get("/deadlines").text.split("<main>", 1)[0]
    assert 'href="/deploiements/client-exemple/synthese" class="nav-item nav-fiche' in bar  # onglet « Fiche »
    fiche_bar = c.get("/deploiements/client-exemple/synthese").text.split("<main>", 1)[0]
    assert 'nav-fiche active' in fiche_bar
    # chaque autre client du menu : un raccourci direct vers sa fiche (et il devient le client en cours)
    assert 'href="/client/client-demo?vers=/deploiements/client-demo/synthese"' in bar
    r = c.get("/client/client-demo?vers=/deploiements/client-demo/synthese", follow_redirects=False)
    assert r.headers["location"] == "/deploiements/client-demo/synthese" and r.cookies.get(CLIENT_COOKIE) == "client-demo"


def test_the_side_shortcuts_follow_the_current_client():
    c = fresh()
    c.get("/deploiements/client-exemple/actions")
    html = c.get("/deadlines").text
    rail = html.split('<aside class="quick-rail"', 1)[1].split("</aside>", 1)[0]
    assert 'aria-label="Raccourcis de Client Exemple"' in rail and "Google Drive" in rail
    assert 'href="/clients/client-exemple/configuration">+ Ajouter les liens Drive' in rail  # pas encore de lien Drive
