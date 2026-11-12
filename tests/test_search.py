"""Palette de commandes (⌘K) : compréhension de la requête, classement, surlignage, et la route /palette."""
from datetime import date

from fastapi.testclient import TestClient

from deploy_hub.config import PROJECT_ROOT
from deploy_hub.main import CROSS_PAGES, DEPLOYMENT_TABS, PAGES, app, ticket_url
from deploy_hub.search import Index, fold, parse, segments
from deploy_hub.sources.fiches import FicheSource

client = TestClient(app)
TODAY = date(2026, 11, 14)  # « Préparer la recette 2 » tombe ce jour ; R-02, le mapping AMT et le cadrage sont en retard


def index(runs=()) -> Index:
    deployments = FicheSource(PROJECT_ROOT / "demo" / "deploiements").list_deployments()
    return Index(deployments, runs, TODAY, PAGES + CROSS_PAGES, DEPLOYMENT_TABS, ticket_url)


def titles(results, group: str | None = None) -> list[str]:
    return [h.item.title for label, _, hits in results.groups if group in (None, label) for h in hits]


def test_fold_removes_accents_and_keeps_positions():
    assert fold("Démo ÉTÉ Œuvre") == "demo ete œuvre"
    assert len(fold("Règles à statuer")) == len("Règles à statuer")


def test_segments_highlight_the_original_text():
    assert segments("Examens complémentaires", ["complem"]) == [("Examens ", False), ("complém", True), ("entaires", False)]
    assert segments("Vaccins", []) == [("Vaccins", False)]


def test_empty_query_suggests_what_is_urgent_then_where_to_go():
    r = index().search("  ")
    assert [label for label, _, _ in r.groups] == ["À traiter", "Pages", "Commandes"]
    assert titles(r, "À traiter")[0] == "Préparer la recette 2"  # l'échéance du jour avant les retards
    assert "Retour R-02 · Visites historiques" in titles(r, "À traiter")
    assert "Recevoir l'extraction complète" not in titles(r)  # en retard sur le papier, mais terminée
    assert titles(r, "Pages")[:2] == ["Accueil", "Clients"]


def test_a_client_name_restricts_the_search_and_shows_its_card():
    r = index().search("démo")
    assert r.card.slug == "client-demo" and r.chips == ["Client Démo"]
    assert r.card_late == 2  # R-02 et le mapping AMT
    hits = [h.item for _, _, hits in r.groups for h in hits]
    assert hits and all(i.slug == "client-demo" for i in hits)
    assert not any(i.kind == "client" for i in hits)  # la carte remplace la ligne « Clients »
    assert "Client Démo · Recette" in titles(r, "Pages")
    assert set(titles(r, "Actions") + titles(r, "Retours de recette")) == {
        "Mapping des codes AMT à valider par le client", "Retour R-02 · Visites historiques"}  # un client seul : ses retards


def test_state_and_kind_words_filter():
    ix = index()
    late = ix.search("retard")
    assert late.chips == ["En retard"]
    assert set(titles(late)) == {"Retour R-02 · Visites historiques", "Mapping des codes AMT à valider par le client",
                                 "Cadrer le périmètre de reprise"}
    assert titles(ix.search("retours ko")) == ["Retour R-03 · Examens complémentaires repris"]
    assert titles(ix.search("exemple actions ouvertes")) == ["Cadrer le périmètre de reprise"]
    assert titles(ix.search("aujourd'hui")) == ["Préparer la recette 2"]
    assert set(titles(ix.search("vigilance"))) == {"Mapping des codes AMT à valider par le client", "Volume de documents CED à confirmer"}
    assert "Recevoir l'extraction complète" in titles(ix.search("actions terminées"))


def test_owner_status_and_command_prefixes():
    ix = index()
    assert titles(ix.search("@cdp")) == ["Mapping des codes AMT à valider par le client"]
    assert titles(ix.search("#valider")) == ["Mapping des codes AMT à valider par le client"]
    assert "Préparer la recette 2" in titles(ix.search("@Inté ouvert"))
    theme = ix.search(">thème")
    assert theme.chips == ["Commandes"] and [h.item.action for _, _, hits in theme.groups for h in hits] == ["theme"]
    assert len(titles(ix.search(">"))) == 6
    assert parse("@cdp #valider retard", ix.deployments).chips == ["@cdp", "#valider", "En retard"]


def test_exact_identifier_ranks_first_and_opens_the_ticket():
    r = index().search("R-03")
    first = r.groups[0][2][0]
    assert first.item.ref == "R-03" and first.score >= 100
    assert first.item.ticket and first.item.url == ticket_url("client-demo", "retour", "R-03")
    assert len(titles(index().search("r-0"), "Retours de recette")) == 4  # début d'identifiant


def test_text_is_searched_without_accents_down_to_the_long_texts():
    ix = index()
    assert titles(ix.search("complementaires")) == ["Retour R-03 · Examens complémentaires repris"]
    hit = ix.search("filtre strict").groups[0][2][0]
    assert hit.item.ref == "R-03"
    label, parts = hit.snippet
    assert label == "Analyse padoa" and ("filtre", True) in parts and ("strict", True) in parts
    assert titles(ix.search("radiés"), "Règles") == ["Clôturer les adhérents radiés avant 2020"]
    assert ix.search("vaccins visites").total == 0  # tous les mots doivent être présents


def test_pages_are_found_by_their_name_even_when_it_is_a_keyword():
    r = index().search("recette")
    assert "Recette" in titles(r, "Pages") and "Kanban des retours de recette" in titles(r, "Commandes")
    assert r.groups[0][0] == "Pages"  # la page d'abord, les retours ensuite
    assert set(titles(index().search("clients"), "Clients")) == {"Client Démo", "Client Exemple"}


def test_a_typo_is_corrected_only_when_nothing_matches():
    ix = index()
    r = ix.search("vacins")
    assert r.corrected == "vaccins" and titles(r) == ["Retour R-04 · Vaccins"]
    assert ix.search("vaccins").corrected == ""
    nothing = ix.search("zzzzqq")
    assert nothing.total == 0 and nothing.corrected == "" and nothing.card is None


def test_large_groups_are_capped_and_counted():
    r = index().search("démo actions")
    assert [(label, total) for label, total, _ in r.groups] == [("Pages", 1), ("Actions", 5)]  # l'onglet du client, puis ses actions
    assert titles(r, "Pages") == ["Client Démo · Actions"]
    solo = index().search("r-0")
    assert len(solo.groups) == 1 and len(solo.groups[0][2]) == solo.groups[0][1] == 4  # un seul type : tout est montré
    many = index().search("e")
    assert len(many.groups) > 1 and all(len(hits) <= 6 for _, _, hits in many.groups)
    assert any(total > len(hits) for _, total, hits in many.groups)


def test_palette_route_renders_results():
    page = client.get("/palette", params={"q": "R-03 filtre"})
    assert page.status_code == 200
    assert 'data-ticket' in page.text and "<mark>R-03</mark>" in page.text and "<mark>filtre</mark>" in page.text
    assert "Analyse padoa" in page.text
    empty = client.get("/palette")
    assert "Pages" in empty.text and "Changer de thème" in empty.text and 'data-pal-action="theme"' in empty.text
    card = client.get("/palette", params={"q": "client-demo"})
    assert 'class="pal-card"' in card.text and "Jarvis a compris" in card.text


def test_palette_route_escapes_the_query():
    page = client.get("/palette", params={"q": "<script>alert(1)</script>"})
    assert page.status_code == 200 and "<script>" not in page.text and "&lt;script&gt;" in page.text


def test_every_page_carries_the_palette():
    home = client.get("/").text
    assert 'id="palette"' in home and "/static/palette.js" in home and "data-palette-open" in home
