"""Synchro directe par l'API Notion, contre un faux Notion (httpx.MockTransport) : aucun appel réseau, aucun jeton réel."""

import json
import re
from pathlib import Path

import httpx
import pytest

from deploy_hub.sources import notion_api
from deploy_hub.sources.notion_api import NotionClient, refresh
from deploy_hub.sources.fiches import parse_deployment
from deploy_hub.sync_notion import SyncError, plan_client

ROOT = Path(__file__).resolve().parents[1]
PAGE = "0123456789abcdef0123456789abcdef"


def prop(pid, kind, v):
    return {"id": pid, "type": kind, kind: v}


SCHEMAS = {
    notion_api.CLIENTS_DS: {"Nom": ("t", "title"), "Date de Kick-off": ("k", "date"), "Date réception des données": ("r", "date"),
                            "Date recette 1": ("r1", "date"), "Date recette 2": ("r2", "date"), "Date recette 3": ("r3", "date"),
                            "Date du Freeze": ("f", "date"), "Date formation": ("fo", "date"), "Date déploiement": ("d", "date"),
                            "Date déploiement Lot 2": ("d2", "date")},
    notion_api.ACTIONS_DS: {"Intitulé": ("ti", "title"), "Statut": ("st", "status"), "Statut intégration - Injections": ("si", "select"),
                            "Deadline": ("dl", "date"), "Point de vigilance": ("pv", "checkbox"), "Événement": ("ev", "checkbox"),
                            "Client": ("cl", "relation"), "Actions à mener (manuel)": ("am", "multi_select"),
                            "Actions à mener - Bible": ("ab", "rollup"), "Ressources liées - Manuel": ("rm", "relation"),
                            "Ressources liées - Bible": ("rb", "rollup")},
    notion_api.RESOURCES_DS: {"Integration name": ("rn", "title")},
    notion_api.RECETTE_DS: {"Statut": ("rs", "status"), "Type de données": ("rt", "rich_text"), "Logiciel": ("rl", "select"),
                            "Deadline": ("rd", "date"), "🏕️ Clients en déploiement": ("rc", "relation"), "Client": ("rcl", "select"),
                            "Commentaire client (NE PAS MODIFIER)": ("cc", "rich_text"), "Ligne du cahier de recette": ("rtitle", "title"),
                            "Type de retour": ("tr", "select"), "Exemple Salarié / adhérents (NE PAS MODIFIER)": ("ex", "rich_text"),
                            "Analyse padoa": ("an", "rich_text"), "Solutions possibles": ("so", "rich_text"),
                            "Discussion INTE/OPS": ("di", "rich_text"), "Échanges / Suivi Déploiement-Inté": ("ec", "rich_text"),
                            "Raison d'abandon": ("ra", "rich_text"), "Responsable": ("re", "people")},
    notion_api.REGLES_DS: {"Intitulé de la règle": ("gi", "title"), "Type de règle": ("gt", "select"),
                           "Application de la règle": ("ga", "select"), "Statut de la règle": ("gs", "status"),
                           "Clients padoa ayant la règle": ("gc", "relation")},
}


def actions_pages(n):
    return [{"id": f"{i:032x}", "url": f"https://www.notion.so/Carte-{i}-{i:032x}", "created_time": "2026-10-01T08:00:00.000Z",
             "last_edited_time": "2026-11-05T10:00:00.000Z",
             "properties": {"Intitulé": prop("ti", "title", [{"plain_text": f"Carte {i}"}]), "Statut": prop("st", "status", {"name": "À faire"}),
                            "Statut intégration - Injections": prop("si", "select", {"name": "KO" if i == 0 else "A faire"}),
                            "Deadline": prop("dl", "date", {"start": "2026-11-20"}), "Point de vigilance": prop("pv", "checkbox", i == 1),
                            "Événement": prop("ev", "checkbox", False),
                            # une carte sur trois est une injection par le champ manuel, une sur trois par la carte Bible
                            "Actions à mener (manuel)": prop("am", "multi_select", [{"name": "INTE Injection"}] if i % 3 == 0 else [{"name": "OPS Paramétrage"}]),
                            "Actions à mener - Bible": prop("ab", "rollup", {"type": "array", "array": [prop("x", "multi_select", [{"name": "INTE Injection" if i % 3 == 1 else "INTE Requête"}])]}),
                            # ressource liée sur la carte (i pair), sinon via la carte Bible
                            "Ressources liées - Manuel": prop("rm", "relation", [{"id": "f" * 32}] if i % 2 == 0 else []),
                            "Ressources liées - Bible": prop("rb", "rollup", {"type": "array", "array": [prop("y", "relation", [{"id": "e" * 32}])]})}}
            for i in range(n)]


RECETTE_PAGES = [
    {"id": "a" * 32, "url": "https://www.notion.so/Titre-avec-une-donnee-de-sante-" + "a" * 32, "created_time": "2026-10-02T08:00:00.000Z",
     "last_edited_time": "2026-11-05T09:00:00.000Z", "icon": {"emoji": "🩺"},
     "properties": {"Statut": prop("rs", "status", {"name": "OPS - Vérifié en SNAP"}), "Type de données": prop("rt", "rich_text", [{"plain_text": "AMT"}]),
                    "Logiciel": prop("rl", "select", {"name": "Pulse"}), "Deadline": prop("rd", "date", None),
                    "Commentaire client (NE PAS MODIFIER)": prop("cc", "rich_text", [{"plain_text": "Le commentaire du client"}]),
                    # renvoyé par erreur malgré filter_properties : doit être ignoré
                    "Raison d'abandon": prop("ra", "rich_text", [{"plain_text": "colonne non demandée"}])}},
    {"id": "b" * 32, "url": "https://www.notion.so/" + "b" * 32, "created_time": "2026-10-03T08:00:00.000Z", "last_edited_time": "2026-11-05T09:30:00.000Z",
     "properties": {"Statut": prop("rs", "status", {"name": "texte libre saisi à la main"}),
                    "Type de données": prop("rt", "rich_text", [{"plain_text": "x" * 41}]), "Logiciel": prop("rl", "select", None),
                    "Deadline": prop("rd", "date", {"start": "2026-11-10"})}},
]


class FakeNotion:
    def __init__(self, n_actions=150):
        self.requests: list[httpx.Request] = []
        self.actions = actions_pages(n_actions)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/v1")
        if m := re.fullmatch(r"/data_sources/([0-9a-f-]+)", path):
            props = {name: {"id": i, "type": t, "name": name} for name, (i, t) in SCHEMAS[m.group(1)].items()}
            return httpx.Response(200, json={"properties": props})
        if path.startswith("/pages/"):
            return httpx.Response(200, json={"id": PAGE, "properties": {
                "Nom": prop("t", "title", [{"plain_text": "Acme"}]), "Date de Kick-off": prop("k", "date", {"start": "2026-04-16"}),
                "Date déploiement": prop("d", "date", {"start": "2026-11-17"}), "Date du Freeze": prop("f", "date", None)}})
        if m := re.fullmatch(r"/data_sources/([0-9a-f-]+)/query", path):
            ds, body = m.group(1), json.loads(request.content)
            resources = [{"id": "f" * 32, "properties": {"Integration name": prop("rn", "title", [{"plain_text": "firm"}])}},
                         {"id": "e" * 32, "properties": {"Integration name": prop("rn", "title", [{"plain_text": "visit_histo"}])}}]
            pages = {notion_api.ACTIONS_DS: self.actions, notion_api.RECETTE_DS: RECETTE_PAGES, notion_api.RESOURCES_DS: resources}.get(ds, [])
            start = int(body.get("start_cursor") or 0)
            chunk = pages[start:start + 100]
            more = start + 100 < len(pages)
            return httpx.Response(200, json={"results": chunk, "has_more": more, "next_cursor": str(start + 100) if more else None})
        return httpx.Response(404, json={})


@pytest.fixture
def root(tmp_path):
    (tmp_path / "config" / "clients").mkdir(parents=True)
    (tmp_path / "config" / "clients" / "acme.yaml").write_text(
        f"slug: acme\nclient: Acme\nsoftware: Pulse\npage_id: {PAGE}\nrecette_clients: [acme_pulse, acme-uegar]\n")
    return tmp_path


def client_for(fake):
    return NotionClient("jeton-de-test", transport=httpx.MockTransport(fake), pause=0)


def test_refresh_writes_the_fiche_from_notion(root):
    fake = FakeNotion()
    result = refresh("acme", client_for(fake), root)
    assert result["cartes"] == 150 and result["retours"] == 2 and result["mode"] == "api"  # 2 pages d'actions lues
    d = parse_deployment(root / "data" / "deploiements" / "acme.md")
    assert sum(a.injection for a in d.actions) == 100  # i % 3 in (0, 1) sur 150 cartes
    assert d.injection_counts == {"KO": 1, "A faire": 99}
    assert {a.title: a.resources for a in d.actions}["Carte 0"] == ["firm"] and {a.title: a.resources for a in d.actions}["Carte 1"] == ["visit_histo"]
    assert len(d.vigilance) == 1 and str(d.go_live) == "2026-11-17"


def test_recette_asks_only_the_listed_properties(root):
    fake = FakeNotion()
    refresh("acme", client_for(fake), root)
    recette_queries = [r for r in fake.requests if r.url.path.endswith(f"{notion_api.RECETTE_DS}/query")]
    asked = set(recette_queries[0].url.params.get_list("filter_properties[]"))
    assert asked == {"rs", "rt", "rl", "re", "rd", "rtitle", "tr", "cc", "ex", "an", "so", "di", "ec"}  # responsable oui, raison d'abandon non
    assert not any("/blocks" in r.url.path for r in fake.requests)  # corps des pages jamais lu
    body = json.loads(recette_queries[0].content)
    assert {"property": "Client", "select": {"equals": "acme-uegar"}} in body["filter"]["or"]


def test_recette_content_goes_to_the_local_file_only(root):
    refresh("acme", client_for(FakeNotion()), root)
    raw_dir = root / "data" / "notion" / "acme"
    assert json.loads((raw_dir / "contenu-recette.json").read_text())["https://app.notion.com/" + "a" * 32] == {"commentaire_client": "Le commentaire du client"}
    versioned = "".join(p.read_text() for p in root.rglob("*") if p.is_file() and p.name != "contenu-recette.json")
    assert "Le commentaire du client" not in versioned and "colonne non demandée" not in versioned
    assert "donnee-de-sante" not in versioned and "🩺" not in versioned
    cache = json.loads((root / "data" / "notion" / "acme" / "cache-recette.json").read_text())
    rows = sorted(cache.values(), key=lambda r: r["cree_le"])
    assert rows[0]["url"] == "https://app.notion.com/" + "a" * 32  # adresse reconstruite sans le titre
    assert rows[1]["statut"] == "Autre" and rows[1]["type_donnees"] == "Autre"
    assert set(rows[0]) <= {"url", "statut", "type_donnees", "logiciel", "responsables", "deadline", "cree_le", "modifie"}
    assert (root / "data" / "recette-pages.txt").read_text().split() == ["a" * 32, "b" * 32]


def test_routine_after_a_direct_refresh_reads_only_what_changed(root):
    refresh("acme", client_for(FakeNotion()), root)
    raw = root / "data" / "notion" / "acme"
    cache = json.loads((raw / "cache-actions.json").read_text())
    n = len(cache)
    last = max(r["modifie"] for r in cache.values() if r.get("modifie"))
    p = plan_client("acme", {"actions": f"{n}|{last}", "recette": "0|"}, root)
    assert ("actions", "actions", []) not in p["requetes"]  # colonnes et date de relecture notées par le rafraîchissement


def test_missing_property_or_access_stops_before_writing(root):
    def no_access(request):
        return httpx.Response(404, json={}) if "/query" in request.url.path else FakeNotion()(request)
    with pytest.raises(SyncError, match="partagée avec l'intégration"):
        refresh("acme", client_for(no_access), root)
    assert not (root / "data" / "deploiements" / "acme.md").exists()


def test_recette_statuses_match_the_sql_allowlist():
    sql = (ROOT / "sync" / "requetes" / "recette.sql").read_text()
    listed = re.search(r'"Statut" IN \((.*?)\) THEN', sql, re.S).group(1)
    statuts = tuple(v.replace("''", "'") for v in re.findall(r"'((?:[^']|'')*)'", listed))
    assert statuts == notion_api.RECETTE_STATUTS


def test_token_comes_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    assert notion_api.load_token(tmp_path) is None
    (tmp_path / ".env").write_text('NOTION_TOKEN="ntn_test"\n')
    assert notion_api.load_token(tmp_path) == "ntn_test"


def test_refresh_button_without_token(monkeypatch):
    from fastapi.testclient import TestClient
    from deploy_hub import main
    monkeypatch.setattr(notion_api, "load_token", lambda root=None: None)
    r = TestClient(main.app).post("/actions/rafraichir")
    assert "Jeton Notion absent" in r.text and "HX-Refresh" not in r.headers


def test_people_column_keeps_notion_ids():
    assert notion_api.value({"type": "people", "people": [{"id": "u-1", "name": "X"}, {"id": "u-2"}]}) == '["user://u-1", "user://u-2"]'
    assert notion_api.value({"type": "people", "people": []}) is None
