"""Synthèse · « Où ça coince » : blocs d'injection et types de retours avec un KO, classés, chacun menant à sa liste."""

from fastapi.testclient import TestClient

from deploy_hub import main
from deploy_hub.models import Action, Deployment, RetourRecette

KO_INJ, RUN_INJ, TODO_INJ, DONE_INJ = "Vérification KO inté/snap", "En cours de dev", "A faire", "Ressource injectée en prod"
KO_RET, DONE_RET = "OPS - Vérifié et pas OK", "Validé"


def card(resources, status):
    return Action(title=f"Injection {'+'.join(resources)} {status}", injection=True, injection_status=status, resources=resources)


def deployment():
    return Deployment(slug="acme", client="Acme", software="Uegar", actions=[
        card(["firm_contact"], KO_INJ), card(["firm_contact"], KO_INJ),                     # 2 KO
        card(["firm"], KO_INJ), card(["firm"], RUN_INJ), card(["firm"], DONE_INJ),           # 1 KO, 2 ouvertes / 3
        card(["amt", "amt_task"], KO_INJ), card(["amt", "amt_task"], RUN_INJ),               # un seul bloc pour deux ressources
        card(["vaccine"], TODO_INJ),                                                         # aucun KO : pas un point bloquant
    ], retours=[RetourRecette(id=f"R-{i}", data_type="AMT", status=s) for i, s in enumerate([KO_RET, "À faire", "À faire", DONE_RET])]
       + [RetourRecette(id="R-9", data_type="Vaccins", status="À faire")])


def test_points_are_blocks_and_types_with_a_ko_most_ko_first_then_most_open():
    points = deployment().blocking_points
    assert [(p["kind"], p["label"], p["counts"]["ko"], p["open"], p["total"]) for p in points] == [
        ("injection", "firm_contact", 2, 2, 2),
        ("retour", "AMT", 1, 3, 4),  # 3 ouverts : passe devant les blocs à 1 KO moins chargés
        ("injection", "amt + amt_task", 1, 2, 2),
        ("injection", "firm", 1, 2, 3)]
    amt_block = points[2]
    assert amt_block["resources"] == ["amt", "amt_task"] and amt_block["href"] == "/deploiements/acme/injections#acme-amt-amt_task"
    assert points[1]["href"] == "/deploiements/acme/recette#type=AMT"


def test_a_type_with_spaces_is_url_encoded_in_the_filter_link():
    d = Deployment(slug="acme", client="Acme", software="Uegar", retours=[RetourRecette(id="R-1", data_type="Visites historiques", status=KO_RET)])
    assert d.blocking_points[0]["href"] == "/deploiements/acme/recette#type=Visites%20historiques"


def test_a_retour_without_type_links_to_the_unfiltered_list():
    d = Deployment(slug="acme", client="Acme", software="Uegar", retours=[RetourRecette(id="R-1", data_type="", status=KO_RET)])
    point = d.blocking_points[0]
    assert point["label"] == "Type non précisé" and point["href"] == "/deploiements/acme/recette"


def test_no_ko_means_no_point():
    assert Deployment(slug="x", client="X", software="Uegar", actions=[card(["firm"], TODO_INJ)]).blocking_points == []


def test_synthesis_shows_the_top_five_and_the_rest(monkeypatch):
    d = deployment()
    d.actions += [card([f"res_{i}"], KO_INJ) for i in range(3)]  # 7 points avec un KO
    monkeypatch.setattr(main.fiches, "get_deployment", lambda slug: d if slug == "acme" else None)
    monkeypatch.setattr(main.fiches, "list_deployments", lambda: [d])
    html = TestClient(main.app).get("/deploiements/acme/synthese").text
    assert "Où ça coince" in html and html.count('class="blocking-row"') == 5
    assert html.index("injections#acme-firm_contact") < html.index("recette#type=AMT")  # le plus de KO d'abord
    assert 'href="/deploiements/acme/recette#type=AMT"' in html and "+ 2 autre(s) point(s) avec un KO" in html


def test_synthesis_says_when_nothing_is_blocking(monkeypatch):
    d = Deployment(slug="calme", client="Calme", software="Uegar", actions=[card(["firm"], TODO_INJ)])
    monkeypatch.setattr(main.fiches, "get_deployment", lambda slug: d if slug == "calme" else None)
    monkeypatch.setattr(main.fiches, "list_deployments", lambda: [d])
    html = TestClient(main.app).get("/deploiements/calme/synthese").text
    assert "Aucun point bloquant" in html and 'class="blocking-row"' not in html
