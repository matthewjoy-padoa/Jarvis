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
    ("/agents", "/deploiements/client-exemple/synthese"),
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
