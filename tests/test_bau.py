"""Demandes BAU : réglage, requêtes, plan et fusion dans le passage de synchro, page « BAU » et ticket d'une demande."""
import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import deploy_hub.sync_notion as sn
from deploy_hub import bau, main
from deploy_hub.config import data_dir
from deploy_hub.sources import ActionResult
from test_sync_passage import FINGERPRINT, SESSION, answer, run

PAGE = "5" * 32
LINK = f"https://app.notion.com/p/padoa/BAU-equipe-{PAGE}?pvs=204"
USER = "11111111-2222-3333-4444-555555555555"


def row(n: int, **over) -> dict:
    """Ligne de la base « Actions internes » telle que la requête bau.sql la renvoie (valeurs fictives)."""
    return {"url": f"https://app.notion.com/{n:032x}", "intitule": f"Demande {n}", "statut": "Pas commencé", "priorite": None,
            "echeance": None, "responsables": "[]", "referents": "[]", "cree_le": "2027-03-01 09:00:00Z",
            "modifie": "2027-03-02 10:00:00Z", "clients": None, "ressources": None, **over}


def results(rows: list[dict]) -> str:
    return json.dumps({"results": rows, "has_more": False})


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    """Dossier de Jarvis sans client, page BAU réglée ; les réponses de Notion sont lues dans un faux transcript."""
    (tmp_path / "config" / "clients").mkdir(parents=True)
    monkeypatch.setattr(sn, "CLAUDE_PROJECTS", tmp_path / "projects")
    monkeypatch.setenv(sn.SESSION_ENV, SESSION)
    monkeypatch.delenv(sn.CLIENT_ENV, raising=False)
    monkeypatch.setenv(bau.SETTING, LINK)
    return tmp_path


def passage(root: Path, fingerprint: str, answers: dict[str, list[dict]] | None = None, only: str = "") -> dict:
    """Un passage des seules demandes : empreinte, puis les requêtes du plan, auxquelles Notion répond « answers »
    ({nom du modèle de requête: lignes})."""
    asked = sn.first_queries(root, only)
    answer(asked["requetes"][0]["requete"], results([{"cle": bau.KEY, "demandes": fingerprint}]))
    out = sn.advance(root)
    known = {p.read_text(encoding="utf-8").rstrip(): p.stem for p in (bau.folder(root) / "requetes").glob("*.sql")}
    while not out.get("termine"):
        for q in out["requetes"]:
            name = known[q["requete"].split("\nLIMIT ")[0]]
            rows = (answers or {}).get(name, []) if "OFFSET" not in q["requete"] else []
            answer(q["requete"], results(rows), q.get("params", ()))
        out = sn.advance(root)
    return out


# ------------------------------------------------------------------------------------------------------------ réglage
def test_the_setting_takes_a_link_or_an_id_and_says_when_it_is_neither(monkeypatch):
    monkeypatch.delenv(bau.SETTING, raising=False)
    assert not bau.enabled() and bau.problem() == "" and bau.page_id() == ""
    monkeypatch.setenv(bau.SETTING, LINK)
    assert bau.enabled() and bau.page_id() == PAGE
    monkeypatch.setenv(bau.SETTING, "pas un lien")
    assert not bau.enabled() and bau.SETTING in bau.problem()


def test_queries_are_written_for_the_followed_page(root):
    folder = bau.write_queries(root)
    assert sorted(p.stem for p in folder.glob("*.sql")) == ["bau", "bau-delta", "bau-empreinte"]
    for name in bau.TEMPLATE_NAMES:
        sql = (folder / f"{name}.sql").read_text(encoding="utf-8")
        assert PAGE in sql and "{{" not in sql
    assert "?" in (folder / "bau-delta.sql").read_text(encoding="utf-8")
    assert f"'{bau.KEY}' AS cle" in (folder / "bau-empreinte.sql").read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------------------------------ synchro
def test_first_passage_reads_everything_then_only_what_changed(root):
    done = passage(root, "2|2027-03-02 10:00:00Z", {"bau": [row(1), row(2, responsables=json.dumps([f"user://{USER}"]))]})
    assert done["compte_rendu"] == ["BAU : demandes complet · 0 changement(s) · 2 demandes"]
    assert done["utilisateurs"] == [USER]  # responsable sans nom : traduit par l'étape 4 de la routine
    assert len(bau.load(root).demandes) == 2 and bau.load(root).checked_at is not None
    assert not list(bau.folder(root).glob("demandes*.json"))  # réponses fusionnées : retirées

    assert passage(root, "2|2027-03-02 10:00:00Z")["compte_rendu"] == ["BAU : inchangé"]

    changed = row(2, statut="En cours", modifie="2027-03-05 08:00:00Z")
    done = passage(root, "3|2027-03-05 08:00:00Z", {"bau-delta": [changed, row(3, modifie="2027-03-04 08:00:00Z")]})
    assert done["compte_rendu"] == ["BAU : demandes delta · 2 changement(s) · 3 demandes"]
    assert {x.title: x.status for x in bau.load(root).demandes} == {"Demande 1": "Pas commencé", "Demande 2": "En cours", "Demande 3": "Pas commencé"}


def test_the_delta_asks_for_what_changed_since_the_newest_known_request(root):
    passage(root, "1|2027-03-02 10:00:00Z", {"bau": [row(1)]})
    todo, why = bau.plan({"demandes": "1|2027-03-09 10:00:00Z"}, root)
    assert todo == [("bau-delta", "demandes_delta", ["2027-03-02 10:00:00Z"])] and why.startswith("delta")
    assert bau.plan({"demandes": "1|2027-03-02 10:00:00Z"}, root) == ([], "inchangé")


@pytest.mark.parametrize("change, reason", [
    ("moins", "disparues"),      # une demande retirée de la page : seule une lecture complète l'enlève du cache
    ("page", "page suivie"),     # réglage changé : le cache est celui d'une autre page
    ("colonnes", "colonnes"),    # une colonne ajoutée à la requête
    ("vieux", "périodique"),     # clients et ressources liés changent sans dater la demande
])
def test_everything_is_read_again_when_the_cache_cannot_be_trusted(root, monkeypatch, change, reason):
    passage(root, "2|2027-03-02 10:00:00Z", {"bau": [row(1), row(2)]})
    fingerprint = {"demandes": "2|2027-03-02 10:00:00Z"}
    if change == "moins":
        fingerprint = {"demandes": "1|2027-03-02 10:00:00Z"}
    elif change == "page":
        monkeypatch.setenv(bau.SETTING, "6" * 32)
    elif change == "colonnes":
        monkeypatch.setattr(bau, "version", lambda: "autre")
    else:
        state = json.loads((bau.folder(root) / bau.STATE).read_text(encoding="utf-8"))
        state["complet_le"] = (date.today() - timedelta(days=bau.FULL_SYNC_DAYS)).isoformat()
        (bau.folder(root) / bau.STATE).write_text(json.dumps(state), encoding="utf-8")
    todo, why = bau.plan(fingerprint, root)
    assert todo == [("bau", "demandes", [])] and reason in why


def test_a_full_read_asks_every_page_at_once(root):
    asked = sn.first_queries(root, "")
    answer(asked["requetes"][0]["requete"], results([{"cle": bau.KEY, "demandes": "250|2027-03-02 10:00:00Z"}]))
    queries = sn.advance(root)["requetes"]
    assert len(queries) == 3 and ["OFFSET" in q["requete"] for q in queries] == [False, True, True]


def test_an_unexpected_column_stops_the_merge_and_is_reported(root):
    done = passage(root, "1|2027-03-02 10:00:00Z", {"bau": [row(1, contenu="texte de la page")]})
    assert len(done["compte_rendu"]) == 1 and done["compte_rendu"][0].startswith("ÉCHEC BAU : fusion : Colonnes non autorisées")
    assert not (bau.folder(root) / bau.CACHE).exists()
    errors = json.loads((root / "data" / "notion" / sn.PLAN_ERRORS).read_text(encoding="utf-8"))
    assert list(errors) == ["BAU"]  # ce que Jarvis affiche sous le bouton


def test_requests_ride_along_with_clients_but_not_with_one_client(root):
    clients = root / "config" / "clients"
    (clients / "demo.yaml").write_text(f"client: Demo\nsoftware: Pulse\nstatus: en cours\npage_id: {'d' * 32}\n", encoding="utf-8")
    asked = sn.first_queries(root, "")  # tout : l'empreinte des clients et celle des demandes, dans le même message
    assert len(asked["requetes"]) == 2
    answer(asked["requetes"][0]["requete"], results([FINGERPRINT]))
    answer(asked["requetes"][1]["requete"], results([{"cle": bau.KEY, "demandes": "1|2027-03-02 10:00:00Z"}]))
    out = sn.advance(root)
    mine = [q for q in out["requetes"] if "Projet ou équipe liée" in q["requete"]]
    assert len(mine) == 1 and len(out["requetes"]) == 5  # client, règles, actions, recette, demandes
    answer(mine[0]["requete"], results([row(1)]))
    run(root, {"requetes": [q for q in out["requetes"] if q not in mine]})
    done = sn.advance(root)
    assert [line.split(" : ")[0] for line in done["compte_rendu"]] == ["demo", "BAU"]

    assert len(sn.first_queries(root, "demo")["requetes"]) == 1  # la fiche d'un client ne relit que lui
    state = json.loads((root / "data" / "notion" / sn.PASSAGE).read_text(encoding="utf-8"))
    assert state["clients"] == ["demo"] and state["bau"] is False


def test_asking_for_requests_without_a_page_says_so(root, monkeypatch):
    monkeypatch.delenv(bau.SETTING)
    with pytest.raises(sn.SyncError, match=bau.SETTING):
        sn.prepare(root, bau.KEY)
    with pytest.raises(sn.SyncError, match="Aucun client suivi"):
        sn.prepare(root, "")


# --------------------------------------------------------------------------------------------------------------- page
client = TestClient(main.app)


@pytest.fixture
def board(monkeypatch):
    """Demandes fictives dans le dossier de données des tests, page BAU réglée ; tout est retiré ensuite."""
    monkeypatch.setenv(bau.SETTING, LINK)
    folder = bau.folder()
    folder.mkdir(parents=True, exist_ok=True)
    today = date.today()
    rows = [row(1, intitule="Corriger le mapping des contrats", statut="En cours", priorite="P0🔥",
                echeance=(today - timedelta(days=2)).isoformat(), responsables=json.dumps([f"user://{USER}"]),
                clients="client-demo", ressources="firm,visit_histo"),
            row(2, intitule="Ajouter une colonne au reporting", priorite="P2🧊"),
            row(3, intitule="Ancienne demande réglée", statut="Terminé")]
    (folder / bau.CACHE).write_text(json.dumps({r["url"]: r for r in rows}), encoding="utf-8")
    (folder / bau.STATE).write_text(json.dumps({"page": PAGE, "colonnes": bau.version()}), encoding="utf-8")
    users = sn.users_path()
    users.parent.mkdir(parents=True, exist_ok=True)
    before = users.read_text(encoding="utf-8") if users.is_file() else None
    users.write_text(json.dumps({USER: "Camille Test"}), encoding="utf-8")
    yield bau.load()
    shutil.rmtree(folder)
    users.write_text(before, encoding="utf-8") if before is not None else users.unlink()


def test_without_a_page_the_menu_hides_bau_and_the_page_says_how_to_set_it(monkeypatch):
    monkeypatch.delenv(bau.SETTING, raising=False)
    assert 'href="/bau"' not in client.get("/").text
    page = client.get("/bau")
    assert page.status_code == 200 and "Aucune page BAU suivie" in page.text and f"/reglages#reglage-{bau.SETTING}" in page.text


def test_a_wrong_setting_keeps_the_menu_entry_and_says_what_to_fix(monkeypatch):
    monkeypatch.setenv(bau.SETTING, "pas un lien")
    assert 'href="/bau"' in client.get("/").text
    assert "Page BAU à corriger" in client.get("/bau").text


def test_a_set_page_waits_for_its_first_sync(monkeypatch):
    monkeypatch.setenv(bau.SETTING, "7" * 32)
    assert "pas encore synchronisées" in client.get("/bau").text


def test_requests_are_sorted_by_priority_and_the_done_ones_are_out_of_the_default_view(board):
    assert [x.title for x in board.demandes] == ["Corriger le mapping des contrats", "Ajouter une colonne au reporting", "Ancienne demande réglée"]
    assert [x.title for x in board.open] == ["Corriger le mapping des contrats", "Ajouter une colonne au reporting"]
    page = client.get("/bau").text
    assert 'href="/bau"' in page and "Camille Test" in page and "P0🔥" in page
    done = page.split("Ancienne demande réglée")[0].rsplit("<article", 1)[1]
    assert "hidden data-out" in done  # masquée, proposée par la recherche
    assert "hidden data-out" not in client.get("/bau?tous=true").text
    late = page.split("Corriger le mapping des contrats")[1].split("</article>")[0]
    assert 'class="late"' in late


def test_kanban_has_one_column_per_status_in_notion_order(board):
    page = client.get("/bau?vue=kanban").text
    columns = [c.split('"')[0] for c in page.split('role="listitem" aria-label="')[1:]]
    assert columns == ["Pas commencé", "En cours", "Terminé"]
    assert page.count('class="kb-card"') == 3


def test_a_request_opens_as_a_panel_or_a_page_and_links_its_followed_client(board):
    ref = board.demandes[0].ref
    panel = client.get(f"/bau/demande/{ref.lower()}", headers={"HX-Request": "true"}).text
    assert "<html" not in panel  # le panneau seul, sans la page autour
    assert "Corriger le mapping des contrats" in panel and "Camille Test" in panel and "<code>firm</code>" in panel
    assert '<a href="/deploiements/client-demo">client-demo</a>' in panel  # client suivi dans Jarvis : sa fiche
    assert board.demandes[0].notion_url == f"https://app.notion.com/p/{1:032x}" and board.demandes[0].notion_url in panel
    page = client.get(f"/bau/demande/{ref}")
    assert page.status_code == 200 and "<html" in page.text and "← BAU" in page.text
    assert client.get("/bau/demande/INCONNUE").status_code == 404


def test_home_shows_where_the_team_stands(board):
    home = client.get("/").text
    assert "2 demande(s) ouverte(s)" in home and "1 en retard" in home


def test_refresh_starts_the_routine_for_the_requests_only(board, monkeypatch):
    started = []
    monkeypatch.setattr(main.sync_routine, "start", lambda *args, **kwargs: started.append(args) or ActionResult(False, "Claude Code introuvable", False))
    answer_ = client.post("/bau/rafraichir")
    assert started == [(bau.KEY, "les demandes BAU")] and "Claude Code introuvable" in answer_.text


def test_the_page_setting_is_in_the_settings_page():
    from deploy_hub import env_file
    assert env_file.BY_KEY[bau.SETTING].section == env_file.NOTION


def test_the_cache_of_another_page_is_not_shown(board, monkeypatch):
    monkeypatch.setenv(bau.SETTING, "8" * 32)
    assert not bau.load().synced and bau.load().demandes == []


def test_data_stays_in_the_private_data_folder():
    assert bau.folder() == data_dir() / "notion" / bau.KEY


def test_changing_the_followed_page_is_a_first_passage_not_hundreds_of_changes(root, monkeypatch):
    passage(root, "2|2027-03-02 10:00:00Z", {"bau": [row(1), row(2)]})
    monkeypatch.setenv(bau.SETTING, "6" * 32)
    done = passage(root, "1|2027-03-02 10:00:00Z", {"bau": [row(3)]})
    assert done["compte_rendu"] == ["BAU : demandes complet · 0 changement(s) · 1 demandes"]


def test_home_without_any_client_talks_about_the_requests_instead_of_asking_for_a_client(board, monkeypatch):
    monkeypatch.setattr(main.fiches, "list_deployments", lambda: [])
    home = client.get("/").text
    assert "demande(s) BAU ouverte(s)" in home and "Ajoute ton premier client" not in home
