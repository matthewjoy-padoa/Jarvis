"""Choix de vue gardés (cookie jarvis_vues, posé par filters.js au clic sur une pastille) : ils servent quand l'adresse
ne porte aucun choix de vue ; dès qu'elle en porte un (pastille, lien direct), elle seule compte."""

from fastapi.testclient import TestClient

from deploy_hub.main import VIEWS_COOKIE, app

FICHE = "/deploiements/client-demo"


def browser(kept: str = "") -> TestClient:
    c = TestClient(app)
    if kept:
        c.cookies.set(VIEWS_COOKIE, kept)
    return c


def active(html: str, label: str) -> bool:
    return f'class="active" data-view="{label}"' in html


def test_pills_say_what_to_keep_and_nothing_is_kept_by_default():
    page = browser().get(f"{FICHE}/recette")
    assert active(page.text, "recette.vue:liste") and active(page.text, "recette.tous:false")
    assert 'data-view="recette.vue:kanban"' in page.text and 'data-view="recette.tous:true"' in page.text
    assert VIEWS_COOKIE not in page.headers.get("set-cookie", "")  # le serveur ne pose rien : le clic sur une pastille le fait
    js = browser().get("/static/filters.js").text
    assert 'e.target.closest("a[data-view]")' in js and "jarvis_vues=" in js and 'bar.querySelectorAll("a.active[data-view]")' in js and 'addEventListener("auxclick", keepClickedView)' in js


def test_kept_choices_apply_when_the_address_says_nothing():
    c = browser("recette.vue:kanban|recette.tous:true|actions.tous:true|injections.vue:liste")
    recette = c.get(f"{FICHE}/recette").text
    assert active(recette, "recette.vue:kanban") and active(recette, "recette.tous:true") and 'class="kanban"' in recette
    assert active(c.get("/recette").text, "recette.vue:kanban")  # même choix sur la page tous clients
    actions = c.get(f"{FICHE}/actions").text
    assert active(actions, "actions.tous:true") and active(actions, "actions.vue:tableau")  # un choix par liste
    assert active(c.get(f"{FICHE}/injections").text, "injections.vue:liste")


def test_the_address_alone_counts_as_soon_as_it_carries_a_view_choice():
    c = browser("recette.vue:kanban|recette.tous:true|injections.vue:liste")
    page = c.get(f"{FICHE}/recette?vue=liste&tous=false").text
    assert active(page, "recette.vue:liste") and active(page, "recette.tous:false")
    direct = c.get(f"{FICHE}/recette?tous=true").text  # lien d'une notification vers un retour : la liste, pas le kanban gardé
    assert active(direct, "recette.vue:liste") and active(direct, "recette.tous:true")
    assert VIEWS_COOKIE not in c.get(f"{FICHE}/recette?vue=liste").headers.get("set-cookie", "")  # et ce qui est gardé ne change pas
    assert active(c.get(f"{FICHE}/recette").text, "recette.vue:kanban")
    assert active(c.get(f"{FICHE}/injections?vue=ressources").text, "injections.vue:ressources")  # lien vers un bloc de ressource


def test_deadlines_keep_their_period_and_view():
    c = browser("deadlines.periode:retard|deadlines.vue:calendrier")
    page = c.get("/deadlines").text
    assert active(page, "deadlines.periode:retard") and active(page, "deadlines.vue:calendrier")
    back = browser("deadlines.periode:toutes|deadlines.vue:liste").get("/deadlines").text
    assert active(back, "deadlines.periode:toutes") and active(back, "deadlines.vue:liste")
    tile = c.get("/deadlines?periode=semaine").text  # compteur de l'accueil : sa période, en liste
    assert active(tile, "deadlines.periode:semaine") and active(tile, "deadlines.vue:liste")
    mark = c.get("/deadlines?vue=calendrier&mois=2026-12").text  # jalon de la frise : son mois, toutes les échéances
    assert active(mark, "deadlines.periode:toutes") and active(mark, "deadlines.vue:calendrier")


def test_a_broken_cookie_is_ignored():
    page = browser("n'importe|quoi::|recette.vue").get(f"{FICHE}/recette")
    assert page.status_code == 200 and active(page.text, "recette.vue:liste")
