import os
import re
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from deploy_hub.config import PROJECT_ROOT
from deploy_hub.main import app, claude
from deploy_hub.models import Deployment, ResourceInjection
from deploy_hub.sources.claude_runner import retour_prompt
from deploy_hub.sources.fiches import FicheSource

client = TestClient(app)
DEMO_DIR = PROJECT_ROOT / "demo" / "deploiements"


def demo() -> Deployment:
    return FicheSource(DEMO_DIR).get_deployment("client-demo")


def test_fiches_parse_demo_deployment():
    d = demo()
    assert d.client == "Client Démo"
    assert d.go_live == date(2026, 12, 7)
    assert d.progress == 20  # 1 ressource sur 5 au statut terminé
    assert [r.id for r in d.open_retours] == ["R-02", "R-03", "R-04"]
    assert [a.title for a in d.vigilance][0].startswith("Mapping des codes AMT")


def test_unknown_dir_returns_no_deployment(tmp_path):
    assert FicheSource(tmp_path / "absent").list_deployments() == []


def _deployment(done: int, total: int, days_since_reception: int, days_to_freeze: int) -> Deployment:
    today = date.today()
    resources = [ResourceInjection(f"r{i}", "Validé" if i < done else "A faire") for i in range(total)]
    milestones = {
        "reception_donnees": today - timedelta(days=days_since_reception),
        "freeze": today + timedelta(days=days_to_freeze),
    }
    return Deployment("x", "X", "Kenora", milestones=milestones, resources=resources)


@pytest.mark.parametrize(
    ("done", "expected_health"),
    [(5, "🟢"), (4, "🟠"), (2, "🔴"), (10, "✅")],  # attendu 50 % ; écarts 0, -10, -30 pts ; 100 %
)
def test_health_follows_notion_rule(done, expected_health):
    d = _deployment(done, 10, days_since_reception=50, days_to_freeze=50)
    assert d.expected_progress == 50
    assert d.health == expected_health


def test_health_is_unknown_without_dates():
    assert Deployment("x", "X", "Kenora", resources=[ResourceInjection("r")]).health == "⚪"


def test_index_lists_deployments():
    r = client.get("/clients")
    assert r.status_code == 200
    assert "Client Démo" in r.text and "Client Exemple" in r.text
    assert "Mode démo" not in r.text  # badge retiré de la barre du haut (2026-11-12) : la simulation se lit dans État des systèmes


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/injections", "Vérification KO recette"),
        ("/actions", "Préparer la recette 2"),
        ("/recette", "Visites historiques"),
        ("/deadlines", "Freeze"),
        ("/agents", "Agents lancés"),
        ("/sources", "non connecté"),
    ],
)
def test_global_pages(path, expected):
    r = client.get(path)
    assert r.status_code == 200
    assert expected in r.text
    assert f'href="{path}"' in r.text  # joignable : barre du haut, page Clients ou pied de page


def _tag(html: str, marker: str) -> str:
    """La balise ouvrante (<article …> ou <tr …>) qui précède le texte donné."""
    before = html[:html.index(marker)]
    start = max(before.rfind("<article"), before.rfind("<tr"))
    return before[start:before.index(">", start)]


@pytest.mark.parametrize("vue", ["", "&vue=kanban"])
def test_recette_hides_closed_retours_unless_asked(vue):
    """Les retours terminés sont envoyés mais masqués (data-out) : la recherche de filters.js peut les proposer."""
    default, everything = client.get(f"/recette?x=1{vue}").text, client.get(f"/recette?tous=true{vue}").text
    assert " hidden data-out" in _tag(default, "<code>R-01</code>")  # R-01 : vérifié en intégration, donc terminé
    assert "data-out" not in _tag(default, "<code>R-02</code>")
    assert 'data-out-label="terminé(s)"' in default
    assert "<code>R-01</code>" in everything and " hidden data-out" not in everything


def test_demo_retours_have_owners_to_filter_on():
    html = client.get("/recette?tous=true").text
    assert 'data-responsable="Alex Martin|Sam Durand"' in html and 'data-responsable="Alex Martin"' in _tag(html, "<code>R-01</code>")


def test_deadlines_skip_done_actions_and_closed_retours():
    r = TestClient(app).get("/deadlines").text  # sans cookie : le client en cours est Client Démo, quel que soit l'ordre des tests
    assert "Recevoir l'extraction complète" not in r  # action terminée
    assert "R-02" in r and "R-01" not in r
    assert r.index("Kick-off") < r.index("Freeze")


@pytest.mark.parametrize(
    ("tab", "expected"),
    [
        ("synthese", "Points de vigilance"),
        ("injections", "Lancer sur"),
        ("actions", "Volume de documents CED"),
        ("recette", "Lancer un agent"),
        ("regles", "Ne pas reprendre les visites sans date"),
        ("accompagnement", "Question sur la reprise des pièces jointes"),
    ],
)
def test_deployment_tabs(tab, expected):
    r = client.get(f"/deploiements/client-demo/{tab}")
    assert r.status_code == 200 and expected in r.text
    assert f'href="/deploiements/client-demo/{tab}" class="active"' in r.text
    assert "Client Exemple" not in r.text.split("<main>", 1)[1]  # l'autre client n'est que dans la navigation


def test_deployment_root_redirects_and_404():
    r = client.get("/deploiements/client-demo", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/deploiements/client-demo/synthese"
    assert client.get("/deploiements/inconnu/synthese").status_code == 404
    assert client.get("/deploiements/client-demo/inconnu").status_code == 404


def test_retour_prompt_carries_my_description():
    d = demo()
    prompt = retour_prompt(d, next(r for r in d.retours if r.id == "R-02"), "La conclusion manque sur les visites de reprise.")
    assert "Client : Client Démo (slug Jarvis : client-demo) · logiciel du retour : Preventiel" in prompt
    assert "Ressource orchestrator : visit_histo" in prompt
    assert "<precisions>\nLa conclusion manque sur les visites de reprise.\n</precisions>" in prompt
    assert "aucune donnée client" in prompt and "`fix-client-demo-visit_histo`" in prompt and "[FIX](client-demo)" in prompt


def test_agent_on_open_retour_uses_my_description_and_is_listed():
    claude.runs.clear()
    r = client.post("/actions/agent-retour/client-demo/R-02", data={"description": "Conclusion absente"})
    assert r.status_code == 200 and "Agent #" in r.text and "lancé sur" in r.text and "fix-client-demo-visit_histo" in r.text
    assert claude.runs[0].subject == "retour R-02" and "Conclusion absente" in claude.runs[0].prompt
    assert "fix-client-demo-visit_histo" in client.get("/agents").text


def test_agent_needs_a_description():
    claude.runs.clear()
    r = client.post("/actions/agent-retour/client-demo/R-02", data={"description": "   "})
    assert "écris le descriptif" in r.text and claude.runs == []


def test_agent_refused_on_closed_or_unknown_retour():
    claude.runs.clear()
    r = client.post("/actions/agent-retour/client-demo/R-01", data={"description": "x"})
    assert "déjà clos" in r.text and claude.runs == []
    assert client.post("/actions/agent-retour/client-demo/R-99", data={"description": "x"}).status_code == 404


def test_dag_and_resource_actions(fake_airflow):
    r = client.post("/actions/dag/demo_client_firm", data={"padoa_env": "snap"})  # la stack est toujours choisie
    assert r.status_code == 200 and "demo_client_firm lancé sur snap" in r.text and "Simulation" not in r.text
    r = client.post("/actions/claude/client-demo/visit_histo")
    assert r.status_code == 200 and "branche client-demo-visit_histo" in r.text


@pytest.mark.parametrize("path", ["/injections?vue=liste", "/actions", "/deadlines", "/deploiements/client-demo/regles", "/deploiements/client-demo/accompagnement", "/agents"])
def test_tables_are_filterable(path):
    r = client.get(path).text
    assert "<table data-filterable" in r and "<th data-filter" in r and "/static/filters.js" in r


def test_recette_cards_carry_filter_and_sort_attributes():
    r = client.get("/recette?tous=true").text
    assert 'data-items=".retour"' in r and "Client:client" in r
    assert 'data-statut="À faire"' in r and 'data-type="Visites historiques"' in r and 'data-deadline="2026-11-12"' in r


def test_dates_sort_on_iso_value():
    assert 'data-sort="2026-11-14"' in client.get("/actions").text


def test_actions_show_active_by_default_and_all_on_demand():
    default, everything = client.get("/actions").text, client.get("/actions?tous=true").text
    assert ' hidden data-out data-group="done"' in _tag(default, "extraction complète")  # terminée, hors fenêtre : masquée
    assert 'data-out-label="hors des actives"' in default
    assert "extraction complète" in everything and " hidden data-out" not in everything
    kanban = client.get("/actions?vue=kanban").text
    assert " hidden data-out" in _tag(kanban, "extraction complète") and "Responsable:responsable" in kanban


def test_filters_script_searches_without_accents_and_offers_hidden_matches():
    js = client.get("/static/filters.js").text
    assert 'normalize("NFD")' in js and "data-out" in js and "more-out" in js
    assert "filters-toggle" in js and "data-fold-filters" in js  # menus déroulants derrière « Filtres » sur la fiche
    assert "filters.js?v=11" in client.get("/actions").text


def test_filters_are_remembered_per_page_in_the_browser():
    """Recherche, filtres et tri gardés dans localStorage, par page ; pas le client (il suit le menu), ni face à une ancre."""
    js = client.get("/static/filters.js").text
    assert "`jarvis.filtres:${location.pathname}:" in js and "localStorage.setItem(entry.memory" in js
    assert "if (s.value && !isClient(k)) f[k] = s.value; else delete f[k];" in js
    assert "const saved = fromAnchor ? {} : recall(entry.memory);" in js
    assert "s.options.some((o) => o.value === v)" in js  # une valeur disparue des données n'est pas remise
    blocs = client.get("/deploiements/client-demo/injections").text  # vue « Par ressource » : la stack choisie est gardée, sauf la prod
    assert 'memory = "jarvis.injections.stack"' in blocs and "prod ? localStorage.removeItem(memory) : localStorage.setItem(memory" in blocs


def test_each_filter_accepts_several_values():
    """Un filtre = des cases à cocher : un élément passe s'il a l'une des valeurs cochées, « a|b » dans une ancre coche les deux."""
    js = client.get("/static/filters.js").text
    assert 'wrap.className = "filter multi"' in js and 'box.type = "checkbox"' in js
    assert "matches: (values) => !chosen.size || values.some((v) => chosen.has(v))" in js
    assert '(v || "").split("|").filter(Boolean).forEach((x) => { ctl.add(x); chosen.add(x); })' in js  # ancre et graphiques : « a|b »
    assert "details.filter.multi" in client.get("/static/style.css").text


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/deploiements/client-demo/synthese", ["Avancement dans le calendrier", "Injections (cartes)", "Retours de recette"]),
        ("/deploiements/client-demo/injections?vue=liste", ["Répartition des injections", 'class="segbar"']),
        ("/deploiements/client-demo/recette", ["Statut des retours", "Retours ouverts par type de données"]),
        ("/deploiements/client-demo/regles", ["Règles par type"]),
        ("/actions", ['class="counts"', "en retard", "point(s) de vigilance", "date(s) clé(s) à venir"]),
        ("/deadlines", ["Dans les 7 jours"]),
        ("/clients", ['class="segbar"']),
    ],
)
def test_summaries_are_rendered(path, expected):
    r = client.get(path)
    assert r.status_code == 200
    for text in expected:
        assert text in r.text, text


@pytest.mark.parametrize("tab", ["actions", "recette", "regles", "accompagnement", "injections?vue=liste"])
def test_fiche_lists_fold_their_filters_without_dropping_any(tab):
    """Listes de la fiche : les menus déroulants passent derrière « Filtres », la liste et ses colonnes restent entières."""
    html = client.get(f"/deploiements/client-demo/{tab}").text
    assert "data-fold-filters" in html


def test_secondary_blocks_are_folded_not_removed():
    """Rien n'est retiré : le détail est dans la page, replié."""
    syn = client.get("/deploiements/client-demo/synthese").text
    assert 'class="fold tl-fold"' in syn and 'class="tl-legend"' in syn  # liste des jalons repliée sous la frise
    assert 'class="panel fold-panel my-notes"' in syn and 'name="texte"' in syn  # notes repliées, toujours éditables
    rec = client.get("/deploiements/client-demo/recette?tous=true").text
    assert "Retours ouverts par type de données" in rec and 'class="hbars"' in rec
    assert "Voir le retour" in rec and "Lancer un agent" in rec and 'class="retour-more"' in rec
    assert "Contenu des retours gardé en local" in rec
    inj = client.get("/deploiements/client-demo/injections?vue=liste").text
    assert "Lancer le DAG" in inj and "Lancer Claude" in inj  # boutons de ligne toujours là (montrés au survol)


def test_segbar_counts_match_the_retours():
    r = client.get("/deploiements/client-demo/synthese").text
    # Démo : 4 retours = 1 à faire, 2 en cours (R-04 clarification, R-03 est KO), 1 terminé
    assert "Retours de recette <span class=\"muted\">· 4 retours" in r
    assert "KO : 1 (25 %)" in r and "Terminé : 1 (25 %)" in r


def test_stacks_are_shown_on_every_tab_and_on_the_overview():
    for tab in ("synthese", "recette", "regles"):
        html = client.get(f"/deploiements/client-demo/{tab}").text
        assert 'href="https://example.com/recette"' in html and "Snap recette" in html
    assert 'href="https://example.com/prod"' in client.get("/clients").text


def test_stacks_fall_back_on_the_client_yaml(tmp_path):
    (tmp_path / "fiches").mkdir()
    (tmp_path / "fiches" / "acme.md").write_text("---\nclient: Acme\n---\n")
    (tmp_path / "clients").mkdir()
    (tmp_path / "clients" / "acme.yaml").write_text(
        'stacks:\n  - {env: prod, label: Prod, url: "https://example.com/acme"}\n  - {label: Vide, url: ""}\n'
    )
    d = FicheSource(tmp_path / "fiches", tmp_path / "clients").get_deployment("acme")
    assert [(s.env, s.label, s.url) for s in d.stacks] == [("prod", "Prod", "https://example.com/acme")]


def test_theme_switch_is_in_every_page():
    html = client.get("/").text
    assert 'id="theme-toggle"' in html and 'localStorage.getItem("theme")' in html


def test_christmas_theme_is_one_of_the_themes_and_has_its_palette():
    """« Noël » (nuit et clair) se choisit au bouton de thème et par la commande de la palette ; palettes dans la feuille de style."""
    html = client.get("/").text
    assert 'noel: "Noël nuit"' in html and '"noel-clair": "Noël clair"' in html
    css = client.get("/static/style.css").text
    assert ':root[data-theme="noel"] {' in css and ':root[data-theme="noel-clair"] {' in css
    assert ':root[data-theme^="noel"] body::before' in css  # le décor vaut pour les deux
    assert "Noël clair" in client.get("/palette", params={"q": ">thème"}).text


def test_christmas_is_forced_once_on_the_first_of_december_then_can_be_removed():
    """À partir du 1er décembre 2026, Noël s'impose une fois à chaque navigateur (« theme-noel-2026 » retient que c'est fait), quel que
    soit le thème retenu. Tout choix fait ensuite est retenu, Auto compris, donc Noël ne revient pas."""
    html = client.get("/").text
    head = html.split("</head>")[0]
    assert 'new Date("2026-12-01T00:00")' in head and 'localStorage.setItem("theme-noel-2026", "1")' in head
    assert '"noel-clair"' in head and "prefers-color-scheme: dark" in head
    assert 'localStorage.setItem("theme", next)' in html and 'removeItem("theme")' not in html  # Auto ("") est retenu, pas effacé


def test_navigation_shows_the_current_client_and_the_others_in_its_menu():
    """Un client à la fois : le client en cours dans la barre, les autres seulement dans le menu du sélecteur."""
    html = TestClient(app).get("/deploiements/client-demo/synthese").text
    nav = html.split("<main>", 1)[0]
    assert nav.count('class="client-chip') == 1 and "Client en cours : Client Démo" in nav
    assert 'href="/client/client-exemple?vers=/deploiements/client-demo/synthese"' in nav and "Tous les clients et réglages" in nav
    assert 'class="nav-item' in nav and '<span>Clients</span>' not in nav  # « Clients » quitte le menu principal
    chip = lambda path: re.search(r'class="(client-chip[^"]*)"', TestClient(app).get(path).text)[1]  # noqa: E731
    assert chip("/deploiements/client-demo/synthese") == chip("/deadlines")  # même aspect sur toutes les pages


def test_empty_columns_are_hidden():
    assert "Lancer un DAG" in client.get("/deploiements/client-demo/injections?vue=liste").text


def test_charts_set_filters():
    inj = client.get("/deploiements/client-demo/injections?vue=liste").text
    assert 'data-filter-set="groupe=todo"' in inj and 'data-group="' in inj and '<th data-filter="injection">' in inj
    rec = client.get("/deploiements/client-demo/recette").text
    assert 'data-filter-set="type=' in rec
    assert 'data-filter-set="groupe=done" data-filter-href="/deploiements/client-demo/recette?tous=true"' in rec  # retours clos masqués par défaut
    syn = client.get("/deploiements/client-demo/synthese").text
    assert 'data-filter-href="/deploiements/client-demo/injections?vue=liste"' in syn
    assert 'data-filter-set="groupe=todo;client=Client Démo"' in client.get("/injections").text


def test_retour_content_is_shown_and_given_to_the_agent():
    html = client.get("/deploiements/client-demo/recette?tous=true").text
    assert "Voir le retour" in html and "Texte fictif : filtre trop strict." in html
    claude.runs.clear()
    r = client.post("/actions/agent-retour/client-demo/R-03", data={"description": ""})  # descriptif facultatif
    assert "Agent #" in r.text and "lancé sur" in r.text
    prompt = claude.runs[0].prompt
    assert "<commentaire_client>\nTexte fictif : il manque des examens.\n</commentaire_client>" in prompt and "<analyse_padoa>" in prompt


def test_recette_content_is_read_from_the_local_file(tmp_path):
    (tmp_path / "deploiements").mkdir()
    (tmp_path / "deploiements" / "acme.md").write_text(
        '---\nclient: Acme\nretours:\n  - {id: ABC123, status: À faire, notion_url: "https://app.notion.com/abc"}\n---\n')
    (tmp_path / "notion" / "acme").mkdir(parents=True)
    (tmp_path / "notion" / "acme" / "contenu-recette.json").write_text('{"https://app.notion.com/abc": {"analyse": "Une analyse"}}')
    d = FicheSource(tmp_path / "deploiements").get_deployment("acme")
    assert d.retours[0].content == [("Analyse padoa", "Une analyse")]


def test_agent_brief_is_prefilled_and_sent_as_edited():
    html = client.get("/deploiements/client-demo/recette").text
    assert 'hx-get="/deploiements/client-demo/recette/R-02/brief"' in html  # chargé à l'ouverture
    form = client.get("/deploiements/client-demo/recette/R-03/brief").text
    assert 'name="prompt"' in form and "&lt;commentaire_client&gt;" in form and "&lt;contrat&gt;" in form
    claude.runs.clear()
    r = client.post("/actions/agent-retour/client-demo/R-03", data={"prompt": "Brief relu et modifié"})
    assert "Agent #" in r.text and "lancé sur" in r.text and claude.runs[0].prompt == "Brief relu et modifié"


def test_agent_brief_without_content_must_be_completed():
    form = client.get("/deploiements/client-demo/recette/R-02/brief").text
    assert "À COMPLÉTER" in form
    claude.runs.clear()
    brief = retour_prompt(demo(), next(r for r in demo().retours if r.id == "R-02"))
    r = client.post("/actions/agent-retour/client-demo/R-02", data={"prompt": brief})
    assert "complète-le avant de lancer" in r.text and claude.runs == []


@pytest.mark.parametrize(("stored", "shown"), [
    ("https://app.notion.com/fedcba9876543210fedcba9876543210", "https://app.notion.com/p/fedcba9876543210fedcba9876543210"),
    ("https://www.notion.so/fedcba98-7654-3210-fedc-ba9876543210", "https://app.notion.com/p/fedcba9876543210fedcba9876543210"),
    ("https://app.notion.com/p/padoa/Dashboard-Acme-abcdefabcdefabcdefabcdefabcdef12", "https://app.notion.com/p/padoa/Dashboard-Acme-abcdefabcdefabcdefabcdefabcdef12"),
    ("https://example.com/prod", "https://example.com/prod"),
])
def test_notion_links_are_openable(stored, shown):
    from deploy_hub.models import notion_link
    assert notion_link(stored) == shown


def test_fiche_links_get_the_p_segment(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "acme.md").write_text(
        '---\nclient: Acme\nactions:\n  - {title: Carte, notion_url: "https://app.notion.com/' + "a" * 32 + '"}\n'
        'retours:\n  - {id: ABC123, status: À faire, notion_url: "https://app.notion.com/' + "b" * 32 + '"}\n---\n')
    d = FicheSource(tmp_path / "d").get_deployment("acme")
    assert d.actions[0].notion_url == "https://app.notion.com/p/" + "a" * 32
    assert d.retours[0].notion_url == "https://app.notion.com/p/" + "b" * 32


def test_fiches_are_cached_until_the_file_changes(tmp_path):
    (tmp_path / "d").mkdir()
    fiche = tmp_path / "d" / "acme.md"
    fiche.write_text("---\nclient: Acme\n---\n")
    source = FicheSource(tmp_path / "d")
    first = source.get_deployment("acme")
    assert source.get_deployment("acme") is first  # pas de nouvelle analyse
    import os, time
    fiche.write_text("---\nclient: Acme SST\n---\n")
    os.utime(fiche, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    assert source.get_deployment("acme").client == "Acme SST"


def test_env_file_settings_are_read(tmp_path, monkeypatch):
    from deploy_hub.config import load_env_file
    monkeypatch.delenv("JARVIS_DATA_DIR", raising=False)
    (tmp_path / ".env").write_text("JARVIS_DATA_DIR=/tmp/ailleurs  # commentaire\n")
    load_env_file(tmp_path / ".env")
    assert os.environ["JARVIS_DATA_DIR"] == "/tmp/ailleurs"
    monkeypatch.delenv("JARVIS_DATA_DIR")


def test_agent_page_shows_status_brief_and_log():
    claude.runs.clear()
    client.post("/actions/agent-retour/client-demo/R-03", data={"prompt": "Brief de la page agent"})
    run = claude.runs[0]
    assert f'href="/agents/{run.id}"' in client.get("/agents").text
    page = client.get(f"/agents/{run.id}")
    assert page.status_code == 200 and "Brief de la page agent" in page.text and "terminé" in page.text and "(simulé)" not in page.text
    assert "every 5s" not in page.text  # fini : plus de rafraîchissement
    assert client.get(f"/agents/{run.id}/journal").status_code == 200
    assert client.get("/agents/9999").status_code == 404


def test_an_agent_with_questions_can_be_answered_from_its_page():
    from deploy_hub.sources.claude_runner import WAITING
    claude.runs.clear()
    client.post("/actions/agent-retour/client-demo/R-03", data={"prompt": "Brief initial"})
    run = claude.runs[0]
    run.status, run.summary = WAITING, ("Rien modifié.\nQuestions :\n1. Client ou logiciel ?\n   - client : apst2607 seul\n"
                                        "2. Faut-il un test de non-régression ?")
    from html import unescape
    page = unescape(client.get(f"/agents/{run.id}").text)
    assert "L'agent a 2 questions" in page and f'hx-post="/agents/{run.id}/repondre"' in page and 'badge g-todo' in page
    assert 'name="reponse_1"' in page and 'name="reponse_2"' in page and "Client ou logiciel ?</p>" in page
    assert "<li>client : apst2607 seul</li>" in page  # détail de la question, sous elle, mis en forme
    assert "Ce que l'agent a constaté" in page and "<p>Rien modifié.</p>" in page  # le contexte écrit avant les questions
    schema = client.get("/agents").text  # dans le schéma : parmi les « en cours », marqué « en attente » (« ? », autre couleur)
    assert 'class="flow-lane flow-in_progress"' in schema and schema.count('<a class="flow-node flow-bot is-waiting"') == 1
    assert schema.index("flow-lane flow-in_progress") < schema.index("flow-bot is-waiting") < schema.index("flow-lane flow-done")
    empty = client.post(f"/agents/{run.id}/repondre", data={"reponse": " "})
    assert "Écris ta réponse" in empty.text and "HX-Redirect" not in empty.headers
    count = len(claude.runs)
    answered = client.post(f"/agents/{run.id}/repondre", data={"reponse_1": "Au niveau client.", "reponse_2": "", "reponse": "Merci"})
    assert answered.headers["HX-Redirect"] == f"/agents/{run.id}" and len(claude.runs) == count  # la même fiche repart
    assert "<reponse>\nQuestion 1 : Client ou logiciel ?\n- client : apst2607 seul\nRéponse : Au niveau client." in run.stdin
    assert "Question 2 : Faut-il un test de non-régression ?\nRéponse : (pas de réponse)" in run.stdin
    assert "Précision : Merci" in run.stdin and run.prompt == "Brief initial"  # le brief reste celui de la fiche
    assert run.status != WAITING and run.passages[-1]["status"] == WAITING and run.passages[-1]["next"] == "réponse"
    page = client.get(f"/agents/{run.id}").text
    assert "2 passages sur cette fiche" in page and "puis ta réponse" in page


def test_each_agent_draws_a_head_and_keeps_it():
    from deploy_hub.main import pick
    heads = ["a", "b"]
    drawn = [pick(heads, agent_id) for agent_id in range(1, 40)]
    assert set(drawn) == {"a", "b"} and drawn == [pick(heads, agent_id) for agent_id in range(1, 40)]  # au hasard, mais sans retirage
    claude.runs.clear()
    client.post("/actions/agent-retour/client-demo/R-03", data={"prompt": "Brief"})
    assert client.get("/agents").text.count('<image class="im-face" href="/static/agent-face') == 1  # l'armure de l'agent porte une photo
    assert all(client.get(f"/static/{name}").status_code == 200 for name in ("agent-face.webp", "agent-face-2.webp"))


@pytest.mark.parametrize("path", ["/actions?vue=kanban&tous=true", "/recette?vue=kanban&tous=true",
                                  "/deploiements/client-demo/actions?vue=kanban", "/deploiements/client-demo/recette?vue=kanban&tous=true"])
def test_kanban_views(path):
    r = client.get(path)
    assert r.status_code == 200 and 'class="kanban"' in r.text and 'class="kb-card"' in r.text
    assert "Kanban</a>" in r.text and "<table" not in r.text.split('class="kanban"')[1]


def test_kanban_columns_follow_notion_order():
    from types import SimpleNamespace as S
    from deploy_hub.models import RETOUR_STATUSES, kanban_columns, status_group
    def r(status):
        return S(status=status, group=status_group(status, RETOUR_STATUSES))
    cols = kanban_columns([(None, r("Validé")), (None, r("INTE - En cours")), (None, r("À faire")), (None, r("À faire"))], RETOUR_STATUSES)
    assert [c["status"] for c in cols] == ["À faire", "INTE - En cours", "Validé"] and len(cols[0]["cards"]) == 2


def test_charts_are_readable_and_lead_to_the_tickets_they_count():
    """Accueil, page Clients et synthèse : jauge d'avancement, barres fines Injections et Recette, tout cliquable."""
    home = client.get("/").text
    glance = home.split('class="panel glance"', 1)[1].split("</section>", 1)[0]
    slug = re.search(r'class="gauge-link" href="/deploiements/([^/"]+)/synthese"', glance)[1]  # la jauge mène à la fiche
    assert f'data-filter-href="/deploiements/{slug}/injections?vue=liste"' in glance
    assert f'data-filter-href="/deploiements/{slug}/recette"' in glance and 'data-filter-set="groupe=' in glance
    assert home.index('class="panel glance"') < home.index('id="whats-new"')  # « Quoi de neuf » passe en bas
    cards = client.get("/clients").text
    assert cards.count('class="card-charts"') == 2 and cards.count('class="gauge') >= 2 and "legend-slim" in cards
    assert 'class="gauge-link"' not in cards  # la carte est déjà un lien vers la fiche
    synthese = client.get("/deploiements/client-demo/synthese").text
    assert 'class="gauge' in synthese
    marks = re.findall(r'<a class="tl-mark[^"]*" href="([^"]+)"', synthese)  # jalons de la frise → calendrier des deadlines
    assert marks and all(m.startswith("/deadlines?client=client-demo&vue=calendrier&mois=") for m in marks)
    assert "Prochain :" in synthese and synthese.count("tl-mark next") == 1  # prochain jalon mis en avant






def test_agent_tokens_are_read_from_the_result_event_and_shown(tmp_path):
    from deploy_hub.sources.claude_runner import AgentRun, read_tokens, tokens_text, usage

    run = AgentRun(1, "client-demo", "Client Démo", "retour R-01", "fix-r-01", "brief", datetime(2026, 11, 17, 9), "terminé",
                   ended_at=datetime(2026, 11, 17, 9, 10), category="Adhérents")
    assert run.tokens is None
    read_tokens(run, {"usage": {"input_tokens": 900, "output_tokens": 8_100, "cache_read_input_tokens": 1_200_000,
                                "cache_creation_input_tokens": 70_000, "service_tier": "standard"}})
    assert (run.input_tokens, run.output_tokens, run.cache_read_tokens, run.cache_write_tokens) == (900, 8_100, 1_200_000, 70_000)
    assert run.tokens == 1_279_000 and tokens_text(run.tokens) == "1,3 M"
    assert [tokens_text(n) for n in (0, 950, 412_300, 999_600)] == ["0", "950", "412 k", "1,0 M"]
    total = usage([run])["total"]
    assert total["tokens"] == 1_279_000 and total["token_kinds"][2] == ("Cache relu", 1_200_000)
    read_tokens(run, {})  # agent arrêté : Claude Code n'annonce rien
    assert run.tokens is None and usage([run])["total"]["tokens"] is None


def test_agents_page_shows_tokens_chart(monkeypatch):
    """Page Agents : bilan des tokens (un seul anneau), schéma des agents par état, onglet DAG ; ni coût en argent ni exemples."""
    import deploy_hub.main as main
    from deploy_hub.sources.claude_runner import AgentRun, read_tokens

    def agent(n, status, tokens, pr=""):
        run = AgentRun(n, "client-demo", "Client Démo", f"retour R-0{n}", f"fix-r-0{n}", "brief", datetime(2026, 11, 17, 9), status,
                       ended_at=None if status == "en cours" else datetime(2026, 11, 17, 9, 20), category="Adhérents", pr_url=pr)
        read_tokens(run, {"usage": {"input_tokens": 1_000, "output_tokens": 20_000, "cache_read_input_tokens": tokens,
                                    "cache_creation_input_tokens": 0}})
        return run

    monkeypatch.setattr(main.claude, "runs", [agent(3, "en cours", 100_000), agent(2, "terminé", 3_400_000, "https://github.com/a/b/pull/2"),
                                              agent(1, "terminé", 1_200_000)])
    page = TestClient(main.app).get("/agents").text
    assert "Coût" not in page and "badge example" not in page  # ni coût en argent, ni agents fictifs
    assert page.count('<svg class="ring') <= 1 and "ring-cell" not in page  # au plus un anneau : le bilan
    assert "Tokens et durée" in page and "Cache relu" in page and "4,6 M" in page  # 2 agents finis : 3 421 000 + 1 221 000
    assert 'href="#dags" data-pane="pane-dags"' in page and page.index('id="pane-dags"') < page.index("Aucun DAG lancé depuis Jarvis")  # onglet DAG
    assert 'class="flow-lane flow-in_progress"' in page and page.count('<a class="flow-node') == 3 and '<tr data-agent="2" data-ticket-card' in page



def test_context_is_read_from_the_journal_even_while_the_agent_runs():
    import json

    from deploy_hub.sources.claude_runner import AgentRun, read_context

    def turn(tokens, parent=None):
        return json.dumps({"type": "assistant", "parent_tool_use_id": parent, "message": {"content": [], "usage": {
            "input_tokens": 10, "cache_read_input_tokens": tokens, "cache_creation_input_tokens": 990, "output_tokens": 500}}})

    run = AgentRun(1, "client-demo", "Client Démo", "retour R-01", "fix-r-01", "brief", datetime(2026, 11, 17, 9), "en cours")
    read_context(run, "")
    assert run.context_pct is None
    read_context(run, "\n".join([turn(20_000), turn(49_000), turn(5_000, parent="sous-agent")]))
    assert (run.context_tokens, run.context_window, run.context_pct) == (50_000, 200_000, 25)  # dernier tour de l'agent lui-même
    end = json.dumps({"type": "result", "modelUsage": {"modele": {"contextWindow": 1_000_000}}})
    read_context(run, "\n".join([turn(499_000), end]))
    assert (run.context_window, run.context_pct) == (1_000_000, 50)


def test_footer_shows_version_from_pyproject():
    from deploy_hub.sources import running_code
    assert f"v{running_code.version()}</a></footer>" in client.get("/").text


def test_an_agent_in_error_has_a_relaunch_button():
    claude.runs.clear()
    client.post("/actions/agent-retour/client-demo/R-03", data={"prompt": "Brief initial"})
    run = claude.runs[0]
    assert 'hx-post="/agents/%d/relancer"' % run.id not in client.get(f"/agents/{run.id}").text  # terminé : pas de bouton
    run.status = "échec"
    assert 'hx-post="/agents/%d/relancer"' % run.id in client.get(f"/agents/{run.id}").text
    count = len(claude.runs)
    r = client.post(f"/agents/{run.id}/relancer")
    assert r.headers["HX-Redirect"] == f"/agents/{run.id}" and len(claude.runs) == count  # la même fiche repart
    assert run.prompt == "Brief initial" and not run.stdin and run.passages[-1]["status"] == "échec"
    page = client.get(f"/agents/{run.id}").text
    assert 'hx-post="/agents/%d/relancer"' % run.id not in page and "puis relancé" in page


def test_the_agent_form_says_before_the_click_why_no_agent_would_start(monkeypatch):
    """Vu sur la VM : ORCHESTRATOR_DIR vide, le clic sur « Lancer l'agent » ne lançait rien et le refus passait inaperçu."""
    from html import unescape
    claude.runs.clear()
    monkeypatch.setattr(claude, "problem", lambda: "Clone de integration-orchestrator introuvable (/x) : renseigne ORCHESTRATOR_DIR dans Réglages.")
    form = unescape(client.get("/deploiements/client-demo/recette/R-03/brief").text)
    assert "L'agent ne peut pas partir : Clone de integration-orchestrator introuvable" in form and 'href="/reglages#reglage-ORCHESTRATOR_DIR"' in form
    assert "Lancer l'agent" in form and " disabled " in form
    assert form.index('id="agent-client-demo-R-03"') < form.index("</form>")  # résultat sous le bouton, dans le formulaire


def test_the_agent_form_points_to_the_agent_already_on_this_retour():
    from html import unescape
    from deploy_hub.sources.claude_runner import WAITING
    claude.runs.clear()
    assert "Un agent existe déjà" not in client.get("/deploiements/client-demo/recette/R-03/brief").text
    client.post("/actions/agent-retour/client-demo/R-03", data={"prompt": "Brief"})
    run = claude.runs[0]
    run.status = WAITING
    form = unescape(client.get("/deploiements/client-demo/recette/R-03/brief").text)
    assert f'href="/agents/{run.id}">agent #{run.id}</a>, question en attente' in form and "réponds-lui sur sa page" in form
    assert " disabled " not in form  # relancer reste possible
    run.status = "en cours"
    assert " disabled " in client.get("/deploiements/client-demo/recette/R-03/brief").text


def test_the_agent_summary_is_formatted_and_its_html_is_never_interpreted():
    from deploy_hub.sources.claude_runner import WAITING
    claude.runs.clear()
    client.post("/actions/agent-retour/client-demo/R-03", data={"prompt": "Brief"})
    run = claude.runs[0]
    run.summary = "Cause : **mapping.py:12**, voir `extract.sql`.\n\n- point 1\n- point 2\n\n<script>alert(1)</script> [x](javascript:alert(1))"
    page = client.get(f"/agents/{run.id}").text
    assert "<strong>mapping.py:12</strong>" in page and "<code>extract.sql</code>" in page and "<li>point 1</li>" in page
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page and 'href="javascript:' not in page
    run.status, run.summary = WAITING, "J'ai lu **a.py**.\n\nQuestions :\n1. Corriger côté `client` ?\n   - **Contexte** : a.py:3\n"
    page = client.get(f"/agents/{run.id}").text
    assert "Corriger côté <code>client</code> ?</p>" in page and "<strong>Contexte</strong> : a.py:3" in page
    assert "J'ai lu <strong>a.py</strong>." in page.replace("&#39;", "'")


def test_structured_questions_offer_their_options_with_the_recommendation_checked():
    import json
    from html import unescape
    from deploy_hub.sources.claude_runner import WAITING
    claude.runs.clear()
    client.post("/actions/agent-retour/client-demo/R-03", data={"prompt": "Brief"})
    run = claude.runs[0]
    block = {"questions": [{"question": "Corriger côté client ou logiciel ?", "contexte": "Copie client en `a.sql:40`.",
                            "options": [{"libelle": "client", "consequence": "seul ce client"},
                                        {"libelle": "logiciel", "consequence": "4 clients à revérifier"}],
                            "recommandation": "client", "pourquoi": "la copie a une autre raison d'être"}]}
    run.status, run.summary = WAITING, f"J'ai lu **a.sql**.\n\n<questions>\n{json.dumps(block, ensure_ascii=False)}\n</questions>"
    page = unescape(client.get(f"/agents/{run.id}").text)
    assert "L'agent a une question" in page and "<questions>" not in page  # le bloc JSON n'est jamais affiché
    assert 'name="choix_1" value="1" checked' in page and 'name="choix_1" value="2">' in page and "recommandé" in page
    assert "4 clients à revérifier" in page and "la copie a une autre raison d'être" in page and "<code>a.sql:40</code>" in page
    answered = client.post(f"/agents/{run.id}/repondre", data={"choix_1": "1", "reponse_1": ""})  # un clic : la recommandation
    assert answered.headers["HX-Redirect"] == f"/agents/{run.id}"
    assert "Question 1 : Corriger côté client ou logiciel ?\nCopie client en `a.sql:40`.\nRéponse : option « client »" in run.stdin


def test_retour_statuses_put_the_ball_where_it_is():
    """« À faire » = ce que l'Inté doit faire ; un statut inconnu (« Autre ») va dans « À classer », plus dans « À faire »."""
    from deploy_hub.models import DONE, IN_PROGRESS, OTHER, TODO, RetourRecette, group_counts
    group = lambda s: RetourRecette(id="X", status=s).group
    assert [group(s) for s in ("À faire", "INTE - En cours", "INTE - Fait > à faire en prod")] == [TODO] * 3
    assert [group(s) for s in ("INTE - En attente donnée client", "OPS - Validation à faire en SNAP", "Non vérifié par le client")] == [IN_PROGRESS] * 3
    assert [group(s) for s in ("INTE - Ancien retour - OK", "OPS - Vérifié en SNAP", "terminé")] == [DONE] * 3
    assert group("Autre") == group("Action identifiée") == OTHER
    assert OTHER not in group_counts([TODO, DONE]) and group_counts([OTHER])[OTHER] == 1


def test_agents_are_archived_from_their_page_or_by_drag_and_drop_and_can_be_reordered():
    claude.runs.clear()
    for _ in range(2):
        client.post("/actions/agent-retour/client-demo/R-03", data={"prompt": "Brief"})
    first, second = sorted(claude.runs, key=lambda r: r.id)
    for run in (first, second):
        run.status = "terminé"
    page = client.get("/agents").text
    assert "data-archive-zone" in page and "data-fly" in page and 'href="/agents/archives">Archivés (' in page
    assert f'hx-post="/agents/{first.id}/archiver"' in client.get(f"/agents/{first.id}").text
    assert client.post("/agents/ordre", data={"ids": f"{first.id},{second.id}"}).status_code == 204
    page = client.get("/agents").text
    assert page.index(f'data-agent="{first.id}" data-fly') < page.index(f'data-agent="{second.id}" data-fly')  # ordre choisi
    done = client.post(f"/agents/{first.id}/archiver")
    assert done.status_code == 200 and done.headers["HX-Redirect"] == "/agents" and claude.get(first.id) is None
    assert f'href="/agents/{first.id}"' in client.get("/agents/archives").text
    assert "Ressortir des archives" in client.get(f"/agents/{first.id}").text  # la fiche reste lisible
    second.status = "en cours"
    refused = client.post(f"/agents/{second.id}/archiver")
    assert refused.status_code == 409 and "travaille ou attend" in refused.text
    second.status = "terminé"
    back = client.post(f"/agents/{first.id}/ressortir")
    assert back.headers["HX-Redirect"] == f"/agents/{first.id}" and claude.get(first.id)


def test_an_agent_is_renamed_and_pinned_from_its_page_and_pinned_ones_lead_the_schema():
    claude.runs.clear()
    for _ in range(2):
        client.post("/actions/agent-retour/client-demo/R-03", data={"prompt": "Brief"})
    first, second = sorted(claude.runs, key=lambda r: r.id)
    for run in (first, second):
        run.status = "terminé"
    page = client.get(f"/agents/{first.id}").text
    assert f'action="/agents/{first.id}/renommer"' in page and "Épingler" in page
    r = client.post(f"/agents/{first.id}/renommer", data={"titre": "Champ de vision"}, follow_redirects=False)
    assert r.status_code == 303 and first.title == "Champ de vision"
    client.post(f"/agents/{first.id}/epingler", follow_redirects=False)
    assert first.pinned and "Désépingler" in client.get(f"/agents/{first.id}").text
    listing = client.get("/agents").text
    assert "Champ de vision" in listing and 'class="bot-pin"' in listing
    assert listing.index(f'data-agent="{first.id}" data-fly') < listing.index(f'data-agent="{second.id}" data-fly')  # épinglé d'abord


def test_dragging_an_agent_is_animated_and_calm_when_motion_is_reduced():
    claude.runs.clear()
    client.post("/actions/agent-retour/client-demo/R-03", data={"prompt": "Brief"})
    claude.runs[0].status = "terminé"
    page = client.get("/agents").text
    assert "const flip = " in page and "is-dragging-agent" in page and "is-swallowing" in page  # voisins, zone éveillée, archive
    assert "takeOff" in page and "im-jets" in page and "bot-puff" in page  # il s'envole, réacteurs allumés, fumée
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in page
    css = (__import__("pathlib").Path(__file__).parents[1] / "src" / "deploy_hub" / "static" / "style.css").read_text()
    assert "@keyframes archive-ready" in css and "@keyframes archive-swallow" in css


def test_the_whole_agent_card_opens_the_agent():
    claude.runs.clear()
    client.post("/actions/agent-retour/client-demo/R-03", data={"prompt": "Brief"})
    run = claude.runs[0]
    run.status = "terminé"
    page = client.get("/agents").text
    assert f'<tr data-agent="{run.id}" data-ticket-card' in page and f'<a href="/agents/{run.id}" data-row-link>' in page
    assert f'class="ticket-run" data-ticket-card' in client.get("/deploiements/client-demo/ticket/retour/R-03").text
    claude.archive(run.id)
    assert f'<a href="/agents/{run.id}" data-row-link>' in client.get("/agents/archives").text
    css = (__import__("pathlib").Path(__file__).parents[1] / "src" / "deploy_hub" / "static" / "style.css").read_text()
    assert ".flow-tip::before" in css  # la bulle du schéma reste ouverte quand la souris la rejoint


SOBER = {"jarvis_affichage": "sobre"}  # cookie posé par POST /affichage


def test_sober_display_is_chosen_per_browser_and_has_no_christmas():
    """Affichage sobre : choisi dans Réglages ou en pied de page, gardé dans un cookie ; le serveur marque alors <html>, charge
    sobre.css, n'offre plus Noël au bouton de thème et ne l'impose pas en décembre. Sans le cookie, rien ne change."""
    browser = TestClient(app)
    plain = browser.get("/")
    assert "<html lang=\"fr\">" in plain.text and 'value="sobre"' in plain.text and "Affichage sobre" in plain.text
    assert "sobre.css" not in plain.text and '<header class="topbar">' in plain.text
    chosen = browser.post("/affichage", data={"affichage": "sobre", "vers": "/agents"}, follow_redirects=False)
    assert chosen.status_code == 303 and chosen.headers["location"] == "/agents"
    html = browser.get("/").text
    assert '<html lang="fr" data-style="sobre">' in html and "Affichage animé" in html and "/static/sobre.css?v=" in html
    assert "Just A Rather Very Intelligent System</em>" not in html and "outil de l'équipe Integration" in html
    head = html.split("</head>")[0]
    assert '!sober && new Date() >= new Date("2026-12-01T00:00")' in head  # Noël jamais imposé en sobre
    assert '{ noel: "dark", "noel-clair": "light" }' in head  # un thème Noël retenu s'affiche en Nuit ou en Clair
    assert 'root.dataset.style === "sobre" ? ["", "dark", "light"]' in html  # bouton de thème sans Noël
    settings = browser.get("/reglages").text
    assert 'id="affichage"' in settings and '<span class="badge">sobre</span>' in settings
    back = browser.post("/affichage", data={"affichage": "jarvis"}, follow_redirects=False)
    assert back.headers["location"] == "/reglages#affichage"
    assert "<html lang=\"fr\">" in browser.get("/").text
    assert ':root[data-style="sobre"] {' in client.get("/static/sobre.css").text


def test_display_choice_only_returns_to_a_page_of_jarvis():
    for outside in ("https://example.com/", "//example.com/x"):
        r = TestClient(app).post("/affichage", data={"affichage": "sobre", "vers": outside}, follow_redirects=False)
        assert r.headers["location"] == "/reglages#affichage"


def test_sober_display_has_its_menu_in_a_side_column():
    """En sobre, la barre du haut, le dock et les boutons flottants laissent la place à une colonne : client en cours, recherche,
    pages, liens du client écrits en toutes lettres, outils (Flow Digger, Notes, Console), notifications, thème, réglages."""
    html = TestClient(app, cookies=SOBER).get("/deploiements/client-demo/synthese").text
    side = html[html.index('<aside class="side"'):html.index("</aside>")]
    for missing in ('<header class="topbar">', 'class="dock-wrap"', "console-fab", 'class="brand-mark"'):
        assert missing not in html, missing
    assert "data-client-switch" in side and "data-palette-open" in side and 'id="theme-toggle"' in side and 'id="notif-panel"' in side
    for label in ("Accueil", "Fiche du client", "Deadlines", "Agents", "BAU", "Clients", "Flow Digger", "Notes", "Console", "Réglages"):
        assert f"<span>{label}</span>" in side, label
    assert 'class="side-item active" title="Fiche de Client Démo"' in side  # la page en cours est marquée
    assert "data-notes-open" in side and "data-console-open" in side and 'action="/outils/flow-digger"' in side
    assert "Liens de Client Démo" in side and 'href="/clients/client-demo/liens?vers=' in side
    assert 'id="notes"' in html and 'id="term"' in html and 'id="palette"' in html  # les fenêtres, elles, sont toujours là


def test_sober_home_is_a_dashboard_without_greeting():
    sober = TestClient(app, cookies=SOBER).get("/").text
    assert "<h1>Tableau de bord " in sober and "Brief du jour" in sober
    for chatty in ("Bonjour.", "Bonsoir.", "Par quoi commence-t-on", "Jarvis a relevé", "Brief du matin"):
        assert chatty not in sober, chatty
    animated = TestClient(app).get("/").text
    assert ("Bonjour." in animated or "Bonsoir." in animated) and "Brief du matin" in animated and "Tableau de bord" not in animated


def test_sober_agents_page_is_counters_and_a_table_without_suits():
    """En sobre, la page Agents donne les étapes en compteurs puis le tableau : ni schéma, ni armure, ni photo, ni
    glisser-déposer (l'agent s'archive depuis sa page). L'affichage animé garde son schéma et ses armures."""
    claude.runs.clear()
    client.post("/actions/agent-retour/client-demo/R-03", data={"prompt": "Brief"})
    run = claude.runs[0]
    run.status = "terminé"
    sober = TestClient(app, cookies=SOBER)
    page = sober.get("/agents").text
    assert "<strong>1</strong><span>Terminés, sans PR</span>" in page and "<span>En attente de ta réponse</span>" in page
    assert f'<tr data-agent="{run.id}"' in page and 'href="/agents/archives">Archivés (' in page
    for decor in ('class="bot"', "agent-face", "data-fly", "data-archive-zone", "il s'envole", "takeOff", "agent-flow"):
        assert decor not in page, decor
    assert f'hx-post="/agents/{run.id}/archiver"' in sober.get(f"/agents/{run.id}").text
    animated = TestClient(app).get("/agents").text
    assert 'class="bot"' in animated and "data-archive-zone" in animated and 'class="panel agent-flow"' in animated
