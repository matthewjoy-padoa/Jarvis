import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

import deploy_hub.sync_notion as module
from deploy_hub.sources.fiches import parse_deployment
from deploy_hub.sync_notion import SyncError, plan, sync

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


def fingerprint(root: Path, actions=(4, "2027-04-01 10:00:00Z"), recette=(1, "2027-03-31 09:00:00Z")) -> None:
    write(raw(root) / "empreinte_actions.json", [{"n": actions[0], "modifie": actions[1]}])
    write(raw(root) / "empreinte_recette.json", [{"n": recette[0], "modifie": recette[1]}])


def test_full_sync_writes_a_fiche_with_every_card(root):
    fingerprint(root)
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


def test_plan_after_a_full_sync(root):
    fingerprint(root)
    assert plan("demo", root) == {"mode": "complet"}  # jamais synchronisé
    sync("demo", root)
    assert plan("demo", root) == {"mode": "inchangé"}
    fingerprint(root, actions=(4, "2027-04-02 08:00:00Z"))
    assert plan("demo", root) == {"mode": "delta", "actions_depuis": "2027-04-01 10:00:00Z"}
    fingerprint(root, actions=(5, "2027-04-02 08:00:00Z"))
    assert plan("demo", root)["mode"] == "complet"  # une carte ajoutée ou supprimée


def test_new_query_column_forces_a_full_sync(root, monkeypatch):
    fingerprint(root)
    sync("demo", root)
    assert plan("demo", root) == {"mode": "inchangé"}
    monkeypatch.setattr("deploy_hub.sync_notion.columns_version", lambda: "autre")  # ex. colonne « Responsable » ajoutée
    assert plan("demo", root) == {"mode": "complet", "raison": "colonnes des requêtes modifiées"}


def test_delta_updates_only_changed_rows(root):
    fingerprint(root)
    sync("demo", root)
    write(raw(root) / "actions_delta.json", [{
        "url": "https://app.notion.com/p/00000000000000000000000000000a04", "intitule": "COPIL #9", "statut": "En cours",
        "statut_injection": "Validé", "deadline": None, "vigilance": "__NO__", "evenement": "__NO__", "modifie": "2027-04-02 08:00:00Z"}])
    fingerprint(root, actions=(4, "2027-04-02 08:00:00Z"))
    summary = sync("demo", root)
    assert summary["mode"] == {"actions": "delta", "recette": "cache"} and summary["cartes"] == 4
    d = fiche(root)
    assert next(a for a in d.actions if a.title == "COPIL #9").injection_status == "Validé"
    assert d.progress == 50
    assert plan("demo", root) == {"mode": "inchangé"}
    assert not (raw(root) / "actions_delta.json").exists()


def test_delta_without_cache_is_refused(root):
    for base in ("actions", "recette"):
        (raw(root) / f"{base}.json").rename(raw(root) / f"{base}_delta.json")
    with pytest.raises(SyncError, match="ni résultat complet ni cache"):
        sync("demo", root)


def test_full_sync_again_after_a_week(root, monkeypatch):
    fingerprint(root)
    sync("demo", root)

    class NextWeek(date):
        @classmethod
        def today(cls):
            return date.fromordinal(date.today().toordinal() + 7)

    monkeypatch.setattr(module, "date", NextWeek)
    assert plan("demo", root)["mode"] == "complet"


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
