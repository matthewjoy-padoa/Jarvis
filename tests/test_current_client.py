"""Un client à la fois : client en cours (cookie), sélecteur, accueil et Deadlines recentrés, changement de client."""

import re

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
    assert 'href="/deploiements/client-exemple" class="nav-item nav-fiche' in bar  # onglet « Fiche »
    fiche_bar = c.get("/deploiements/client-exemple/synthese").text.split("<main>", 1)[0]
    assert 'nav-fiche active' in fiche_bar
    # chaque autre client du menu : un raccourci direct vers sa fiche (et il devient le client en cours)
    assert 'href="/client/client-demo?vers=/deploiements/client-demo"' in bar
    r = c.get("/client/client-demo?vers=/deploiements/client-demo", follow_redirects=False)
    assert r.headers["location"] == "/deploiements/client-demo/synthese" and r.cookies.get(CLIENT_COOKIE) == "client-demo"


def test_the_side_shortcuts_follow_the_current_client():
    c = fresh()
    c.get("/deploiements/client-exemple/actions")
    html = c.get("/deadlines").text
    dock = html.split('<nav class="dock"', 1)[1].split("</nav>", 1)[0]
    assert 'aria-label="Raccourcis de Client Exemple"' in dock
    assert 'href="/clients/client-exemple/liens?vers=/deadlines"' in dock and 'data-tip="Ajouter un lien Drive"' in dock  # pas encore de lien


def test_the_dock_gives_the_padoa_environments_of_the_current_client(monkeypatch):
    c = fresh()
    c.get("/deploiements/client-exemple/actions")
    dock = lambda: c.get("/deadlines").text.split('<div class="dock-envs"', 1)[1].split("<nav", 1)[0]  # noqa: E731
    monkeypatch.delenv("PADOA_ENV_URL", raising=False)
    assert 'aria-label="Environnements Padoa de Client Exemple"' in dock()
    assert 'src="/static/padoa.png"' in dock() and c.get("/static/padoa.png").status_code == 200  # à part du dock, logo Padoa
    assert dock().count(".aodap-staging.fr/support") == 3 and 'href="/reglages"' not in dock()  # réglage vide : modèle par défaut
    assert [f'style="--i: {i}; --n: 3"' in dock() for i in range(3)] == [True] * 3  # éventail : rang et nombre de pastilles
    monkeypatch.setenv("PADOA_ENV_URL", "https://[stack]-[client].exemple.test/support")
    hosts = re.findall(r'href="https://([^."]+)\.exemple\.test/support"', dock())
    assert hosts == ["snap-client-exemple", "snap-recette-client-exemple", "integration-client-exemple"]  # le slug Jarvis
    monkeypatch.setenv("PADOA_ENV_URL", "javascript:alert(1)//[stack]")
    assert "javascript:" not in dock() and 'href="/reglages"' in dock()  # modèle refusé : renvoi vers les Réglages


def test_without_any_client_every_page_offers_to_add_one(monkeypatch):
    """Sans client, pas de sélecteur (il portait le seul lien vers Clients) : la barre et l'accueil proposent d'en ajouter un."""
    from deploy_hub import main
    monkeypatch.setattr(main.fiches, "list_deployments", lambda: [])
    c = TestClient(app)
    for path in ("/", "/deadlines", "/agents"):
        bar = c.get(path).text.split("<main>", 1)[0]
        assert 'class="client-switch"' not in bar and 'class="client-chip client-add" href="/clients/nouveau"' in bar, path
    home = c.get("/").text.split("<main>", 1)[1]
    assert "Ajoute ton premier client" in home and 'href="/clients/nouveau"' in home
    assert c.get("/clients/nouveau").status_code == 200
