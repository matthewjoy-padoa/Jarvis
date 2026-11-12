import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

import deploy_hub.sync_notion as module
from deploy_hub.sources.fiches import parse_deployment
from deploy_hub.sync_notion import SyncError, plan_all, sync

FIXTURES = Path(__file__).parent / "fixtures" / "notion" / "demo"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "config" / "clients").mkdir(parents=True)
    (tmp_path / "config" / "clients" / "demo.yaml").write_text("slug: demo\nclient: Démo\nsoftware: Pulse\n")
    shutil.copytree(FIXTURES, tmp_path / "data" / "notion" / "demo")
    return tmp_path


def raw(root: Path) -> Path:
    return root / "data" / "notion" / "demo"


def fiche(root: Path):
    return parse_deployment(root / "data" / "deploiements" / "demo.md")


def write(path: Path, rows: list[dict]) -> None:
    path.write_text(json.dumps({"results": rows, "has_more": False}))


def fingerprint(root: Path, actions=(4, "2027-04-01 10:00:00Z"), recette=(1, "2027-03-31 09:00:00Z"), regles="1|40", fiche="Démo|2027-06-01") -> dict:
    """Écrit la réponse de la requête d'empreinte (une ligne par client), puis renvoie le plan."""
    (root / "data" / "notion").mkdir(parents=True, exist_ok=True)
    write(root / "data" / "notion" / "empreintes.json", [{"cle": "demo", "actions": f"{actions[0]}|{actions[1]}",
                                                          "recette": f"{recette[0]}|{recette[1]}", "regles": regles, "fiche": fiche}])
    return plan_all(root)


def asked(p: dict) -> list[str]:
    """Sorties demandées par le plan, avec les paramètres des deltas."""
    return [Path(q["sortie"]).stem + (f"?{q['params'][0]}" if q["params"] else "") for q in p["requetes"]]


def deposit(root: Path, *names: str) -> None:
    for name in names:
        shutil.copy(FIXTURES / f"{name}.json", raw(root) / f"{name}.json")


def first_sync(root: Path) -> None:
    fingerprint(root)
    deposit(root, "client", "regles", "actions", "recette")
    sync("demo", root)


def test_full_sync_writes_a_fiche_with_every_card(root):
    summary = sync("demo", root)
    assert summary["mode"] == {"actions": "complet", "recette": "complet"}
    assert (summary["cartes"], summary["retours"], summary["regles"]) == (4, 1, 1)
    d = fiche(root)
    assert d.go_live == date(2027, 6, 1)
    assert d.injection_counts == {"Vérification KO recette": 1, "Ressource injectée en prod": 1}  # COPIL et extraction : pas des injections
    assert sorted(a.title for a in d.injection_cards) == ["Injection des vaccins", "Injection des visites"]
    assert d.progress == 50  # 1 injection en prod sur 2 cartes d'injection
    assert "COPIL #9" not in [a.title for a in d.active_actions]  # ni vigilance, ni date, ni en cours
    assert d.retours[0].id == "ABC123"
    assert not (raw(root) / "actions.json").exists()  # résultats bruts consommés, cache gardé
    assert (raw(root) / "cache-actions.json").exists()


def test_plan_asks_only_what_changed(root):
    assert asked(fingerprint(root)) == ["client", "regles", "actions", "recette"]  # jamais synchronisé : tout
    deposit(root, "client", "regles", "actions", "recette")
    sync("demo", root)
    assert fingerprint(root)["inchanges"] == ["demo"]  # rien n'a bougé : aucune requête
    assert asked(fingerprint(root, actions=(4, "2027-04-02 08:00:00Z"))) == ["client", "actions_delta?2027-04-01 10:00:00Z"]
    # une carte ajoutée : delta seulement (elle est datée après la dernière modification connue), plus de synchro complète
    assert asked(fingerprint(root, actions=(5, "2027-04-02 08:00:00Z"))) == ["client", "actions_delta?2027-04-01 10:00:00Z"]
    # une carte supprimée : la liste compacte des identifiants suffit
    assert asked(fingerprint(root, actions=(3, "2027-04-01 10:00:00Z"))) == ["client", "ids"]
    assert asked(fingerprint(root, fiche="Démo|2027-07-01")) == ["client"]
    assert asked(fingerprint(root, regles="2|80")) == ["client", "regles"]


def test_delta_updates_only_changed_rows(root):
    first_sync(root)
    fingerprint(root, actions=(4, "2027-04-02 08:00:00Z"))
    write(raw(root) / "actions_delta.json", [{
        "url": "https://app.notion.com/p/00000000000000000000000000000a04", "intitule": "COPIL #9", "statut": "En cours",
        "statut_injection": "Validé", "deadline": None, "vigilance": "__NO__", "evenement": "__NO__", "modifie": "2027-04-02 08:00:00Z"}])
    deposit(root, "client")
    summary = sync("demo", root)
    assert summary["mode"] == {"actions": "delta", "recette": "cache"} and summary["cartes"] == 4
    d = fiche(root)
    assert next(a for a in d.actions if a.title == "COPIL #9").injection_status == "Validé"
    assert d.progress == 50
    assert fingerprint(root, actions=(4, "2027-04-02 08:00:00Z"))["inchanges"] == ["demo"]
    assert not (raw(root) / "actions_delta.json").exists()


def test_deleted_rows_leave_the_cache_through_the_id_list(root):
    first_sync(root)
    fingerprint(root, actions=(3, "2027-04-01 10:00:00Z"))
    write(raw(root) / "ids.json", [{"actions": "0000000000000a01,0000000000000a02,0000000000000a03", "recette": "0000000000abc123"}])
    deposit(root, "client")
    summary = sync("demo", root)
    assert summary["cartes"] == 3 and summary["retirees"] == {"actions": 1, "recette": 0}
    assert fingerprint(root, actions=(3, "2027-04-01 10:00:00Z"))["inchanges"] == ["demo"]


def test_missing_result_is_refused(root):
    first_sync(root)
    fingerprint(root, actions=(4, "2027-04-02 08:00:00Z"))
    with pytest.raises(SyncError, match="actions_delta.json"):
        sync("demo", root)


def test_new_recette_column_rereads_metadata_but_keeps_texts(root, monkeypatch):
    first_sync(root)
    texts = json.loads((raw(root) / "contenu-recette.json").read_text())
    versions = module.versions()
    monkeypatch.setattr(module, "versions", lambda: {**versions, "recette": "autre"})  # ex. colonne « Responsable »
    assert asked(fingerprint(root)) == ["client", "recette_meta"]
    meta = {k: v for k, v in json.loads((FIXTURES / "recette.json").read_text())["results"][0].items() if k in module.RECETTE_META}
    write(raw(root) / "recette_meta.json", [{**meta, "responsables": '["user://u-1"]'}])
    deposit(root, "client")
    assert sync("demo", root)["mode"]["recette"] == "meta"
    assert json.loads((raw(root) / "contenu-recette.json").read_text()) == texts  # textes gardés sans les relire
    assert "u-1" in json.loads((raw(root) / "cache-recette.json").read_text())[meta["url"]]["responsables"]
    assert fingerprint(root)["inchanges"] == ["demo"]


def test_new_action_column_rereads_only_actions(root, monkeypatch):
    first_sync(root)
    versions = module.versions()
    monkeypatch.setattr(module, "versions", lambda: {**versions, "actions": "autre"})
    assert asked(fingerprint(root)) == ["client", "actions"]


def test_actions_are_read_in_full_once_a_week_and_recette_never(root, monkeypatch):
    first_sync(root)

    class NextWeek(date):
        @classmethod
        def today(cls):
            return date.fromordinal(date.today().toordinal() + 7)

    monkeypatch.setattr(module, "date", NextWeek)
    assert asked(fingerprint(root)) == ["client", "actions"]


def test_state_from_before_the_per_base_versions_does_not_reread_everything(root):
    first_sync(root)
    state = json.loads((raw(root) / "etat-synchro.json").read_text())
    for old in ("ancienne-version-globale", None):
        state["colonnes"] = old
        (raw(root) / "etat-synchro.json").write_text(json.dumps({k: v for k, v in state.items() if v is not None}))
        assert asked(fingerprint(root)) == ["client", "recette_meta"]  # seule la recette a changé de colonnes


def test_fusionner_reports_each_client(root):
    first_sync(root)
    fingerprint(root, actions=(4, "2027-04-02 08:00:00Z"))
    results = module.merge_all(root)
    assert "Résultat manquant" in results["demo"]["erreur"]


def test_delta_without_cache_is_refused(root):
    for base in ("actions", "recette"):
        (raw(root) / f"{base}.json").rename(raw(root) / f"{base}_delta.json")
    with pytest.raises(SyncError, match="ni résultat complet ni cache"):
        sync("demo", root)


def test_sync_adds_recette_pages_to_the_hook_blocklist_without_removing_any(root):
    blocklist = root / "data" / "recette-pages.txt"
    blocklist.write_text("ffffffffffffffffffffffffffffffff\n")
    sync("demo", root)
    assert blocklist.read_text().split() == ["00000000000000000000000000abc123", "ffffffffffffffffffffffffffffffff"]


@pytest.mark.parametrize("name", ["recette.json", "recette_delta.json"])
def test_sync_refuses_recette_columns_outside_the_allowlist(root, name):
    if name == "recette_delta.json":
        sync("demo", root)
    rows = json.loads((FIXTURES / "recette.json").read_text())["results"]
    rows[0]["responsable"] = "colonne non prévue"
    write(raw(root) / name, rows)
    before = (root / "data" / "deploiements" / "demo.md").read_text() if name == "recette_delta.json" else None
    with pytest.raises(SyncError, match="non autorisées"):
        sync("demo", root)
    if before is None:
        assert not (root / "data" / "deploiements" / "demo.md").exists()
    else:
        assert (root / "data" / "deploiements" / "demo.md").read_text() == before


def test_sync_fails_when_a_result_is_missing(root):
    (raw(root) / "regles.json").unlink()
    with pytest.raises(SyncError, match="manquant"):
        sync("demo", root)


def test_sync_merges_result_pages(root):
    write(raw(root) / "recette-2.json", [{"url": "https://app.notion.com/p/00000000000000000000000000def456", "statut": "Validé",
                                         "type_donnees": "AMT", "logiciel": None, "deadline": None, "cree_le": "2027-04-01T09:00:00Z",
                                         "modifie": "2027-04-01 09:00:00Z"}])
    assert sync("demo", root)["retours"] == 2
    assert [r.id for r in fiche(root).retours] == ["ABC123", "DEF456"]


def test_sync_refuses_a_full_last_page(root):
    row = json.loads((FIXTURES / "actions.json").read_text())["results"][0]
    write(raw(root) / "actions-2.json", [dict(row, url=f"https://app.notion.com/p/{i:032x}") for i in range(100)])
    with pytest.raises(SyncError, match="pleine"):
        sync("demo", root)


def test_sync_refuses_a_full_first_page_alone(root):
    row = json.loads((FIXTURES / "actions.json").read_text())["results"][0]
    write(raw(root) / "actions.json", [dict(row, url=f"https://app.notion.com/p/{i:032x}") for i in range(100)])
    with pytest.raises(SyncError, match="pleine"):
        sync("demo", root)


def test_sync_accepts_a_full_page_followed_by_an_empty_one(root):
    row = json.loads((FIXTURES / "actions.json").read_text())["results"][0]
    write(raw(root) / "actions.json", [dict(row, url=f"https://app.notion.com/p/{i:032x}") for i in range(100)])
    write(raw(root) / "actions-2.json", [])
    assert sync("demo", root)["cartes"] == 100


def test_recette_content_is_kept_apart_from_versioned_files(root):
    rows = json.loads((FIXTURES / "recette.json").read_text())["results"]
    rows[0].update(commentaire_client="Commentaire du client", analyse="Analyse padoa")
    write(raw(root) / "recette.json", rows)
    sync("demo", root)
    url = rows[0]["url"]
    content = json.loads((raw(root) / "contenu-recette.json").read_text())
    assert content[url] == {"commentaire_client": "Commentaire du client", "analyse": "Analyse padoa"}
    assert "Commentaire du client" not in (raw(root) / "cache-recette.json").read_text()
    assert "Commentaire du client" not in (root / "data" / "deploiements" / "demo.md").read_text()
    # un delta sans changement de contenu ne l'efface pas
    write(raw(root) / "recette_delta.json", [{**{k: v for k, v in rows[1].items()}, "statut": "Validé"}] if len(rows) > 1 else [])
    sync("demo", root)
    assert json.loads((raw(root) / "contenu-recette.json").read_text())[url]["analyse"] == "Analyse padoa"


@pytest.mark.parametrize(("types", "teams"), [
    ("INTE Injection", ["INTE"]),
    ("OPS vérification INTE Aide/Conseil", ["INTE", "OPS"]),
    ('["OPS Récup/Envoi infos","RM Gesfin - Aide/Conseil"] GESFIN Paramétrage', ["OPS", "Gesfin"]),
    ("FORMATION Préparation Support/Réunion", ["Formation"]),
    ("", []),
    (None, []),
])
def test_teams_come_from_action_type_prefixes(types, teams):
    from deploy_hub.sync_notion import teams_of
    assert teams_of(types) == teams


def test_responsables_are_named_from_the_directory(tmp_path):
    from deploy_hub.sources.fiches import _names
    from deploy_hub.sync_notion import add_users, unknown_users, user_ids
    assert user_ids('["user://a-1", "user://b-2"]') == ["a-1", "b-2"] and user_ids(None) == []
    cache = tmp_path / "data" / "notion" / "acme" / "cache-recette.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"u": {"url": "u", "responsables": '["user://a-1", "user://b-2"]'}}))
    assert unknown_users(tmp_path) == ["a-1", "b-2"]
    add_users({"user://a-1": "Alice"}, tmp_path)
    assert unknown_users(tmp_path) == ["b-2"]
    assert _names(["a-1", "b-2"], {"a-1": "Alice"}) == ["Alice", "Inconnu (b-2)"]


def test_a_copied_tool_result_file_is_read_as_is(root):
    rows = json.loads((FIXTURES / "regles.json").read_text())
    (raw(root) / "regles.json").write_text(json.dumps([{"type": "text", "text": json.dumps(rows)}]))
    assert module._rows(raw(root) / "regles.json") == rows["results"]


def test_unknown_notion_page_is_reported_before_any_query(root):
    p = fingerprint(root, fiche=None)
    assert p["requetes"] == [] and "page_id" in p["erreurs"]["demo"]


def test_the_routine_keeps_the_emoji_of_the_direct_refresh(root):
    """La routine SQL ne voit pas les icônes : l'emoji posé par la synchro directe survit à son passage."""
    path = root / "data" / "deploiements" / "demo.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nclient: Demo\nemoji: 🦖\n---\n", encoding="utf-8")
    sync("demo", root)
    assert fiche(root).emoji == "🦖"
