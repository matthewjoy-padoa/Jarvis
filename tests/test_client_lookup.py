"""Ajouter un client par son seul nom : Claude lit la liste des dashboards Notion (faux `claude` ici, aucun appel réseau)."""
import json
import time
from html import unescape

import pytest
from fastapi.testclient import TestClient

from deploy_hub import client_config, main
from deploy_hub.client_config import ConfigError, parse_dashboards
from deploy_hub.sources.client_lookup import CLIENTS_DS, DASHBOARDS_PAGE, ClientLookup, prompt, proposal, result_of

from test_sync_routine import fake_claude

FICHE = "https://app.notion.com/p/Deploiement-Acme-0123456789abcdef0123456789abcdef"
FOUND = {"nom": "Acme Santé", "fiche": FICHE, "candidats": [], "remarque": "",
         "dashboards": [{"famille": "deploiement", "titre": "Dashboard de déploiement - Acme", "url": "https://app.notion.com/p/aaa1"},
                        {"famille": "integration", "titre": "Dashboard intégration - Acme lot 1", "url": "https://app.notion.com/p/bbb1"},
                        {"famille": "integration", "titre": "Dashboard intégration - Acme lot 2", "url": "https://app.notion.com/p/bbb2"},
                        {"famille": "formation", "titre": "Dashboard de formation - Acme", "url": "https://app.notion.com/p/ccc1"},
                        {"famille": "gestion_financiere", "titre": "Dashboard de Gestion financière - Acme", "url": "https://app.notion.com/p/ddd1"},
                        {"famille": "formation", "titre": "Ailleurs", "url": "https://example.com/x"},  # pas Notion : ignoré
                        {"famille": "inconnue", "titre": "?", "url": "https://app.notion.com/p/eee1"}]}  # famille inconnue : ignoré


def answer(data) -> str:
    return f"Voici ce que j'ai trouvé.\n<client>\n{json.dumps(data, ensure_ascii=False)}\n</client>"


def test_the_answer_becomes_a_client_configuration_to_review():
    found = proposal(answer(FOUND))
    assert found["client"] == "Acme Santé" and found["slug"] == "acme-sante"
    assert found["page_id"] == "0123456789abcdef0123456789abcdef"  # la fiche dans « Clients en déploiement »
    assert found["notion_url"] == "https://app.notion.com/p/bbb1"  # premier dashboard intégration : le dashboard du client
    assert found["dashboards"].splitlines() == ["Déploiement | https://app.notion.com/p/aaa1",
                                                "Dashboard intégration - Acme lot 2 | https://app.notion.com/p/bbb2",  # autre lot : son titre
                                                "Formation | https://app.notion.com/p/ccc1",
                                                "Gestion financière | https://app.notion.com/p/ddd1"]
    assert parse_dashboards(found["dashboards"])[0] == {"label": "Déploiement", "url": "https://app.notion.com/p/aaa1"}


def test_an_answer_in_a_code_fence_is_read_and_a_missing_one_is_refused():
    fenced = answer(FOUND).replace("<client>\n", "<client>\n```json\n").replace("\n</client>", "\n```\n</client>")
    assert proposal(fenced)["client"] == "Acme Santé"
    for bad in ("pas de bloc", "<client>{pas du json</client>", "<client>[1, 2]</client>"):
        with pytest.raises(ConfigError, match="réponse lisible"):
            proposal(bad)
    lost = proposal(answer({"nom": "", "dashboards": [], "candidats": ["Acme Sud", "Acme Nord"], "fiche": "pas une adresse"}))
    assert lost["candidats"] == ["Acme Sud", "Acme Nord"] and lost["page_id"] == "" and lost["slug"] == ""


def test_the_prompt_names_the_client_the_dashboards_page_and_the_clients_base(monkeypatch):
    text = prompt("Acme 12-34")
    assert "« Acme 12-34 »" in text and DASHBOARDS_PAGE in text and f"collection://{CLIENTS_DS}" in text
    assert "Lecture seule" in text and "<client>" in text
    monkeypatch.setenv("JARVIS_NOTION_DASHBOARDS", "f" * 32)
    assert "f" * 32 in prompt("x")  # autre liste de dashboards (autre espace Notion) : réglage


def test_the_result_is_read_after_any_warning():
    out = "avertissement de Claude Code\n" + json.dumps({"type": "result", "result": answer(FOUND)})
    assert result_of(out) == answer(FOUND) and result_of("texte brut") == "texte brut"


def wait(lookup: ClientLookup) -> dict:
    for _ in range(200):
        if not lookup.running and not lookup.state().get("running"):
            return lookup.state()
        time.sleep(0.05)
    raise AssertionError("la recherche ne s'est pas terminée")


def claude_answering(tmp_path, data) -> str:
    out = json.dumps({"type": "result", "result": answer(data)})
    return str(fake_claude(tmp_path, f"print({out!r})"))


def test_the_lookup_only_reads_notion_and_keeps_its_result(tmp_path):
    lookup = ClientLookup(folder=tmp_path / "notion", claude_bin=claude_answering(tmp_path, FOUND))
    assert lookup.start("Acme").ok
    state = wait(lookup)
    assert state["ok"] and state["name"] == "Acme" and state["proposal"]["client"] == "Acme Santé"
    args, _, stdin, _ = eval((tmp_path / "recu.txt").read_text())
    assert args[args.index("--allowedTools") + 1:] == ["mcp__claude_ai_Notion"]  # rien d'autre que le connecteur Notion
    assert "dontAsk" in args and "« Acme »" in stdin
    assert not lookup.start("  ").ok
    assert "CLAUDE_BIN" in ClientLookup(folder=tmp_path / "n2", claude_bin=str(tmp_path / "absent")).start("Acme").message


def test_nothing_found_is_not_a_success(tmp_path):
    lookup = ClientLookup(folder=tmp_path / "notion",
                          claude_bin=claude_answering(tmp_path, {"nom": "", "dashboards": [], "candidats": ["Acme Sud"], "fiche": ""}))
    lookup.start("Acme")
    state = wait(lookup)
    assert not state["ok"] and state["proposal"]["candidats"] == ["Acme Sud"]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(main.fiches, "clients_dir", tmp_path / "config" / "clients")
    return TestClient(main.app)


def test_add_a_client_by_its_name_only(app, tmp_path, monkeypatch):
    lookup = ClientLookup(folder=tmp_path / "data" / "notion", claude_bin=claude_answering(tmp_path, FOUND))
    monkeypatch.setattr(main, "client_lookup", lookup)
    page = unescape(app.get("/clients/nouveau").text)
    assert "Chercher dans Notion" in page and "Ou remplir à la main" in page  # le nom d'abord, le formulaire replié
    started = app.post("/clients/recherche", data={"nom": "Acme"})
    assert 'id="client-lookup-out"' in started.text
    wait(lookup)
    done = app.get("/clients/recherche/etat")
    assert done.headers["HX-Redirect"] == "/clients/nouveau?depuis=notion"
    form = unescape(app.get("/clients/nouveau?depuis=notion").text)
    assert "Trouvé dans Notion : Acme Santé" in form and 'value="acme-sante"' in form and "Ou remplir à la main" not in form
    assert 'value="0123456789abcdef0123456789abcdef"' in form and "Formation | https://app.notion.com/p/ccc1" in form
    saved = app.post("/clients/enregistrer", data={"client": "Acme Santé", "slug": "acme-sante", "page_id": "0123456789abcdef0123456789abcdef",
                                                    "notion_url": "https://app.notion.com/p/bbb1",
                                                    "dashboards": "Formation | https://app.notion.com/p/ccc1"}, follow_redirects=False)
    assert saved.status_code == 303
    cfg = client_config.load("acme-sante")
    assert cfg.dashboards == [{"label": "Formation", "url": "https://app.notion.com/p/ccc1"}]
    fiche = app.get("/deploiements/acme-sante/synthese").text
    assert 'href="https://app.notion.com/p/ccc1"' in fiche and "Formation ↗" in fiche  # dans les raccourcis du client


def test_a_lookup_that_finds_nothing_offers_close_names(app, tmp_path, monkeypatch):
    lookup = ClientLookup(folder=tmp_path / "data" / "notion",
                          claude_bin=claude_answering(tmp_path, {"nom": "", "dashboards": [], "candidats": ["Acme Sud"], "fiche": ""}))
    monkeypatch.setattr(main, "client_lookup", lookup)
    app.post("/clients/recherche", data={"nom": "Acm"})
    wait(lookup)
    status = unescape(app.get("/clients/recherche/etat").text)
    assert "Aucun client « Acm »" in status and '<input type="hidden" name="nom" value="Acme Sud">' in status
    assert "HX-Redirect" not in app.get("/clients/recherche/etat").headers
    assert "Trouvé dans Notion" not in app.get("/clients/nouveau?depuis=notion").text  # rien à pré-remplir


@pytest.mark.parametrize("line", ["Déploiement | https://drive.google.com/x", "https://example.com/p/1"])
def test_only_notion_addresses_are_accepted_as_dashboards(line):
    with pytest.raises(ConfigError, match="app.notion.com"):
        parse_dashboards(line)


def test_a_lookup_cut_by_a_restart_says_so_instead_of_waiting_forever(tmp_path):
    folder = tmp_path / "notion"
    folder.mkdir()
    (folder / "recherche-client.json").write_text(json.dumps({"running": True, "name": "Acme", "started": "2026-11-19T10:00:00"}))
    state = ClientLookup(folder=folder).state()  # nouveau Jarvis : il n'a lancé aucune recherche
    assert not state["running"] and not state["ok"] and "interrompue" in state["error"]
