"""Vue simplifiée des fiches : onglets choisis (cookie par navigateur), bascule, onglet d'ouverture."""

import re

from fastapi.testclient import TestClient

from deploy_hub.main import TABS_COOKIE, app

FICHE = "/deploiements/client-demo"


def tabs_of(html: str) -> list[str]:
    nav = html.split('<nav class="tabs"', 1)[1].split("</nav>", 1)[0]
    return re.findall(rf'<a href="{FICHE}/([a-z]+)"', nav)


def post(c: TestClient, **form):
    return c.post("/fiche/vue", data=form, follow_redirects=False)


def test_full_view_by_default_with_the_simple_view_one_click_away():
    page = TestClient(app).get(f"{FICHE}/synthese").text
    assert tabs_of(page) == ["synthese", "injections", "actions", "recette", "pr", "regles", "accompagnement"]
    assert 'name="vue" value="simple" aria-pressed="false"' in page and "Ne garder que : Injections, Recette, PR" in page
    picker = page.split('id="tabs-pick"', 1)[1].split("</form>", 1)[0]
    assert re.findall(r'value="([a-z]+)" checked', picker) == ["injections", "recette", "pr"]  # ce que garde la vue simplifiée


def test_simple_view_keeps_three_tabs_and_leaves_the_tab_that_goes_away():
    c = TestClient(app)
    r = post(c, vue="simple", vers=f"{FICHE}/synthese")
    assert r.status_code == 303 and r.headers["location"] == f"{FICHE}/injections"  # la synthèse sort de la vue
    assert r.cookies.get(TABS_COOKIE) == "simple:injections.recette.pr"
    page = c.get(f"{FICHE}/recette").text
    assert tabs_of(page) == ["injections", "recette", "pr"] and 'name="vue" value="complete" aria-pressed="true"' in page
    assert post(c, vue="simple", vers=f"{FICHE}/recette?vue=kanban").headers["location"] == f"{FICHE}/recette?vue=kanban"  # onglet gardé : on reste


def test_simple_view_opens_fiches_on_its_first_tab():
    c = TestClient(app)
    post(c, vue="simple")
    assert c.get(FICHE, follow_redirects=False).headers["location"] == f"{FICHE}/injections"
    assert f'href="{FICHE}" class="nav-item nav-fiche' in c.get("/").text  # onglet « Fiche » du menu : il suit la vue
    r = c.get("/client/client-exemple", params={"vers": "/sources"}, follow_redirects=False)
    assert r.headers["location"] == "/deploiements/client-exemple/injections"
    post(c, vue="complete")
    assert c.get(FICHE, follow_redirects=False).headers["location"] == f"{FICHE}/synthese"


def test_a_hidden_tab_stays_reachable_and_shows_where_we_are():
    c = TestClient(app)
    post(c, vue="simple")
    page = c.get(f"{FICHE}/regles")
    assert page.status_code == 200 and tabs_of(page.text) == ["injections", "recette", "pr", "regles"]
    assert f'href="{FICHE}/regles" class="active off-view"' in page.text


def test_choosing_tabs_turns_the_simple_view_on_and_survives_the_full_view():
    c = TestClient(app)
    r = post(c, vue="simple", onglets=["pr", "synthese", "inconnu"], vers=f"{FICHE}/actions")
    assert r.headers["location"] == f"{FICHE}/synthese" and r.cookies.get(TABS_COOKIE) == "simple:synthese.pr"
    assert tabs_of(c.get(f"{FICHE}/pr").text) == ["synthese", "pr"]  # dans l'ordre de la fiche, onglet inconnu ignoré
    assert c.get(FICHE, follow_redirects=False).headers["location"] == f"{FICHE}/synthese"  # synthèse gardée : on y ouvre
    post(c, vue="complete")
    page = c.get(f"{FICHE}/pr").text
    assert len(tabs_of(page)) == 7 and "Ne garder que : Synthèse, PR" in page  # le choix est gardé pour la prochaine fois
    post(c, vue="simple", onglets=[])  # rien de coché : les onglets d'avant
    assert tabs_of(c.get(f"{FICHE}/pr").text) == ["synthese", "pr"]


def test_view_choice_never_redirects_outside_jarvis_and_ignores_a_bad_cookie():
    c = TestClient(app)
    assert post(c, vue="simple", vers="https://ailleurs.example/").headers["location"] == "/"
    assert post(c, vue="simple", vers="//ailleurs.example").headers["location"] == "/"
    c.cookies.set(TABS_COOKIE, "simple:nimporte.quoi")
    assert tabs_of(c.get(f"{FICHE}/recette").text) == ["injections", "recette", "pr"]
