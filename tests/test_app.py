import os
from datetime import date, timedelta

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
    assert "Mode démo" in r.text


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


def test_recette_hides_closed_retours_unless_asked():
    assert "R-01" not in client.get("/recette").text
    assert "R-01" in client.get("/recette?tous=true").text


def test_deadlines_skip_done_actions_and_closed_retours():
    r = client.get("/deadlines").text
    assert "Recevoir l'extraction complète" not in r  # action terminée
    assert "R-02" in r and "R-01" not in r
    assert r.index("Kick-off") < r.index("Freeze")


@pytest.mark.parametrize(
    ("tab", "expected"),
    [
        ("synthese", "Points de vigilance"),
        ("injections", "Lancer le DAG"),
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
    assert "aucune donnée client" in prompt and "`fix-client-demo-r-02`" in prompt and "[FIX](client-demo)" in prompt


def test_agent_on_open_retour_uses_my_description_and_is_listed():
    claude.runs.clear()
    r = client.post("/actions/agent-retour/client-demo/R-02", data={"description": "Conclusion absente"})
    assert r.status_code == 200 and "Simulation : agent #" in r.text and "fix-client-demo-r-02" in r.text
    assert claude.runs[0].subject == "retour R-02" and "Conclusion absente" in claude.runs[0].prompt
    assert "fix-client-demo-r-02" in client.get("/agents").text


def test_agent_needs_a_description():
    claude.runs.clear()
    r = client.post("/actions/agent-retour/client-demo/R-02", data={"description": "   "})
    assert "écris le descriptif" in r.text and claude.runs == []


def test_agent_refused_on_closed_or_unknown_retour():
    claude.runs.clear()
    r = client.post("/actions/agent-retour/client-demo/R-01", data={"description": "x"})
    assert "déjà clos" in r.text and claude.runs == []
    assert client.post("/actions/agent-retour/client-demo/R-99", data={"description": "x"}).status_code == 404


def test_dag_and_resource_actions_are_simulated():
    r = client.post("/actions/dag/demo_client_firm")
    assert r.status_code == 200 and "Simulation" in r.text and "demo_client_firm sur integration" in r.text
    r = client.post("/actions/claude/client-demo/visit_histo")
    assert r.status_code == 200 and "branche client-demo-visit_histo" in r.text


@pytest.mark.parametrize("path", ["/injections?vue=liste", "/actions", "/deadlines", "/deploiements/client-demo/regles", "/deploiements/client-demo/accompagnement", "/agents"])
def test_tables_are_filterable(path):
    r = client.get(path).text
    assert "<table data-filterable>" in r and "<th data-filter" in r and "/static/filters.js" in r


def test_recette_cards_carry_filter_and_sort_attributes():
    r = client.get("/recette?tous=true").text
    assert 'data-items=".retour"' in r and "Client:client" in r
    assert 'data-statut="À faire"' in r and 'data-type="Visites historiques"' in r and 'data-deadline="2026-11-12"' in r


def test_dates_sort_on_iso_value():
    assert 'data-sort="2026-11-14"' in client.get("/actions").text


def test_actions_show_active_by_default_and_all_on_demand():
    assert "extraction complète" not in client.get("/actions").text  # terminée, hors fenêtre
    assert "extraction complète" in client.get("/actions?tous=true").text


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/deploiements/client-demo/synthese", ["Avancement dans le calendrier", "Injections (cartes)", "Retours de recette"]),
        ("/deploiements/client-demo/injections", ["Répartition des injections", 'class="segbar"']),
        ("/deploiements/client-demo/recette", ["Statut des retours", "Retours ouverts par type de données"]),
        ("/deploiements/client-demo/regles", ["Règles par type"]),
        ("/actions", ["En retard", "Points de vigilance"]),
        ("/deadlines", ["Dans les 7 jours"]),
        ("/clients", ['class="segbar"']),
    ],
)
def test_summaries_are_rendered(path, expected):
    r = client.get(path)
    assert r.status_code == 200
    for text in expected:
        assert text in r.text, text


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


def test_navigation_lists_clients_and_marks_the_current_one():
    html = client.get("/deploiements/client-demo/synthese").text
    nav = html.split("<main>", 1)[0]
    assert 'href="/deploiements/client-demo" class="client-chip' in nav and 'h-' in nav and "Client Exemple" in nav
    assert nav.count('client-chip') >= 2 and 'class="client-chip h-' in nav and ' active"' in nav.split('href="/deploiements/client-demo"', 1)[1].split('>', 1)[0]


def test_empty_columns_are_hidden():
    assert "Lancer un DAG" in client.get("/deploiements/client-demo/injections?vue=liste").text


def test_charts_set_filters():
    inj = client.get("/deploiements/client-demo/injections?vue=liste").text
    assert 'data-filter-set="groupe=todo"' in inj and 'data-group="' in inj and '<th data-filter="injection">' in inj
    rec = client.get("/deploiements/client-demo/recette").text
    assert 'data-filter-set="type=' in rec
    assert 'data-filter-set="groupe=done" data-filter-href="/deploiements/client-demo/recette?tous=true"' in rec  # retours clos masqués par défaut
    syn = client.get("/deploiements/client-demo/synthese").text
    assert 'data-filter-href="/deploiements/client-demo/injections"' in syn
    assert 'data-filter-set="groupe=todo;client=Client Démo"' in client.get("/injections").text


def test_retour_content_is_shown_and_given_to_the_agent():
    html = client.get("/deploiements/client-demo/recette?tous=true").text
    assert "Voir le retour" in html and "Texte fictif : filtre trop strict." in html
    claude.runs.clear()
    r = client.post("/actions/agent-retour/client-demo/R-03", data={"description": ""})  # descriptif facultatif
    assert "Simulation : agent #" in r.text
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
    assert "Simulation : agent #" in r.text and claude.runs[0].prompt == "Brief relu et modifié"


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
