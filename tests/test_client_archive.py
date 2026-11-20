"""Archiver un client (on ne travaille plus dessus) : hors menu, accueil, notifications et synchro ; fiche gardée, réversible."""

from datetime import date

import pytest
import yaml
from fastapi.testclient import TestClient

from deploy_hub import client_config, main, sync_notion
from deploy_hub.client_config import ClientConfig, ConfigError
from deploy_hub.sources import notion_api
from deploy_hub.sources.fiches import FicheSource

PAGE = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"


def _two_clients(root):
    client_config.save(ClientConfig(slug="acme", client="Acme", page_id=PAGE), root, new=True)
    client_config.save(ClientConfig(slug="bolt", client="Bolt", page_id=OTHER), root, new=True)


def test_archiving_is_written_in_the_yaml_and_undone_without_a_trace(tmp_path):
    _two_clients(tmp_path)
    path = tmp_path / "config" / "clients" / "acme.yaml"
    path.write_text(path.read_text() + "note: client multi-vagues\n")
    assert "archived" not in yaml.safe_load(path.read_text())  # un client suivi n'a aucune clé d'archivage

    assert client_config.set_archived("acme", True, tmp_path) and not client_config.set_archived("acme", True, tmp_path)
    data = yaml.safe_load(path.read_text())
    assert data["archived"] is True and data["archived_at"] == date.today() and data["note"] == "client multi-vagues"
    assert client_config.load("acme", tmp_path).archived_at == date.today()
    path.write_text(path.read_text().replace(date.today().isoformat(), "bientôt"))  # date retouchée à la main : archivé quand même
    reread = client_config.load("acme", tmp_path)
    assert reread.archived and reread.archived_at is None
    assert client_config.slugs(tmp_path) == ["acme", "bolt"] and client_config.active_slugs(tmp_path) == ["bolt"]

    client_config.set_archived("acme", False, tmp_path)
    data = yaml.safe_load(path.read_text())
    assert "archived" not in data and "archived_at" not in data and data["note"] == "client multi-vagues"
    assert client_config.active_slugs(tmp_path) == ["acme", "bolt"]


def test_archiving_an_unknown_client_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="Client inconnu"):
        client_config.set_archived("fantome", True, tmp_path)


def test_an_archived_client_keeps_its_notion_page(tmp_path):
    """Sa fiche Notion reste la sienne : on le désarchive, on ne l'ajoute pas une seconde fois."""
    _two_clients(tmp_path)
    client_config.set_archived("acme", True, tmp_path)
    with pytest.raises(ConfigError, match="déjà celle du client « Acme »"):
        client_config.save(ClientConfig(slug="acme2", client="Acme bis", page_id=PAGE), tmp_path, new=True)


def test_the_sync_skips_archived_clients(tmp_path):
    _two_clients(tmp_path)
    client_config.set_archived("acme", True, tmp_path)
    prepared = sync_notion.prepare(tmp_path)
    sql = (tmp_path / "data" / "notion" / "empreintes.sql").read_text()
    assert OTHER in sql and PAGE not in sql and prepared  # une seule empreinte : celle du client suivi
    assert notion_api.clients(tmp_path) == ["bolt"]  # « Tout rafraîchir » par l'API


def test_fiches_are_listed_apart(tmp_path):
    _two_clients(tmp_path)
    fiches = tmp_path / "data" / "deploiements"
    fiches.mkdir(parents=True)
    (fiches / "acme.md").write_text("---\nclient: Acme\n---\n")
    client_config.set_archived("acme", True, tmp_path)
    source = FicheSource(fiches, tmp_path / "config" / "clients")
    assert [d.slug for d in source.list_deployments()] == ["bolt"]
    assert [(d.slug, d.archived, d.pending) for d in source.archived_deployments()] == [("acme", True, False)]
    assert source.get_deployment("acme").archived_at is not None  # sa fiche reste lisible
    client_config.set_archived("bolt", True, tmp_path)  # jamais synchronisé : archivé quand même
    assert source.list_deployments() == [] and [d.slug for d in source.archived_deployments()] == ["acme", "bolt"]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(main.fiches, "clients_dir", tmp_path / "config" / "clients")  # lu au démarrage par l'app
    monkeypatch.setattr(main.fiches, "deployments_dir", tmp_path / "data" / "deploiements")
    synced = []
    monkeypatch.setattr(main, "_sync_new_client", lambda slug, background: synced.append(slug))
    client = TestClient(main.app)
    client.synced = synced
    client.post("/clients/enregistrer", data={"client": "Acme", "page_id": PAGE, "software": "Pulse"})
    client.post("/clients/enregistrer", data={"client": "Bolt", "page_id": OTHER})
    synced.clear()
    return client


def _bar(html: str) -> str:
    return html.split("<main>", 1)[0]


def test_archive_from_the_clients_page_and_back(app):
    page = app.get("/clients").text
    assert 'action="/clients/acme/archiver"' in page and 'id="archives"' not in page  # pas de section vide
    app.cookies.set(main.CLIENT_COOKIE, "acme")
    assert "Acme" in _bar(app.get("/").text)

    r = app.post("/clients/acme/archiver", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/clients?archive=acme"
    page = app.get("/clients?archive=acme").text
    cards, archives = page.split('id="archives"', 1)
    assert "Acme est archivé" in cards and 'href="/deploiements/acme"' not in cards.split("<main>", 1)[1]
    assert 'href="/deploiements/acme"' in archives and 'action="/clients/acme/desarchiver"' in archives
    assert "Acme" not in _bar(app.get("/").text)  # il était le client en cours : le menu passe à un client suivi
    assert "Bolt" in _bar(app.get("/").text)
    assert "Acme" not in app.get("/palette?q=acme").text  # ni dans la recherche

    r = app.post("/clients/acme/desarchiver", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/clients?desarchive=acme"
    assert app.synced == ["acme"]  # ses données, figées depuis l'archivage, sont resynchronisées
    page = app.get("/clients?desarchive=acme").text
    assert "Acme est de retour dans le suivi" in page and 'id="archives"' not in page
    app.post("/clients/acme/desarchiver")
    assert app.synced == ["acme"]  # déjà suivi : pas de seconde synchro


def test_an_archived_fiche_is_read_without_becoming_the_current_client(app):
    app.post("/clients/acme/archiver")
    r = app.get("/deploiements/acme/synthese")
    assert r.status_code == 200 and "<strong>Archivé le" in r.text and 'action="/clients/acme/desarchiver"' in r.text
    assert 'hx-post="/actions/rafraichir/acme"' not in r.text  # plus de bouton « Rafraîchir »
    assert "set-cookie" not in r.headers  # consultée, elle ne devient pas le client en cours
    assert "Passer à Bolt" in _bar(r.text)  # le menu propose toujours le client suivi
    refused = app.post("/actions/rafraichir/acme")
    assert "Acme est archivé" in refused.text
    assert "set-cookie" in app.get("/deploiements/bolt/synthese").headers


def test_editing_an_archived_client_keeps_it_archived(app):
    app.post("/clients/acme/archiver")
    form = app.get("/clients/acme/configuration").text
    assert "Client archivé" in form and 'action="/clients/acme/desarchiver"' in form and 'action="/clients/acme/archiver"' not in form
    app.post("/clients/enregistrer", data={"existing_slug": "acme", "client": "Acme SST", "page_id": PAGE})
    cfg = client_config.load("acme")
    assert cfg.client == "Acme SST" and cfg.archived and cfg.archived_at
    assert 'action="/clients/bolt/archiver"' in app.get("/clients/bolt/configuration").text


def test_archived_clients_raise_no_notification(app, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(main.watchers, "sync_late_notes", lambda deployments, now: seen.append([d.slug for d in deployments]) or [])
    app.post("/clients/acme/archiver")
    main._deadline_notes()
    assert seen == [["bolt"]]


def test_archive_confirmation_survives_an_apostrophe(app):
    app.post("/clients/enregistrer", data={"client": "L'AIST 22", "slug": "aist22", "page_id": "a" * 32})
    assert 'confirm(&#34;Archiver L\\u0027AIST 22 ? Il sort du menu, de l\\u0027accueil,' in app.get("/clients").text


def test_archiving_an_unknown_client_is_a_404(app):
    assert app.post("/clients/fantome/archiver", follow_redirects=False).status_code == 404
    assert app.post("/clients/fantome/desarchiver", follow_redirects=False).status_code == 404


def test_a_client_with_a_broken_notion_page_can_still_be_archived(tmp_path):
    path = tmp_path / "config" / "clients" / "demo.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("client: Démo\nsoftware: Pulse\n")  # yaml écrit à la main, sans page_id
    assert client_config.set_archived("demo", True, tmp_path) and client_config.active_slugs(tmp_path) == []


def test_a_fiche_without_configuration_offers_no_archiving(app, tmp_path):
    fiches = tmp_path / "data" / "deploiements"
    fiches.mkdir(parents=True, exist_ok=True)
    (fiches / "vieux.md").write_text("---\nclient: Vieux\n---\n")
    assert 'action="/clients/vieux/archiver"' not in app.get("/clients").text
    assert 'action="/clients/vieux/archiver"' not in app.get("/clients/vieux/configuration").text


def test_reminders_of_an_archived_client_stay_silent(app, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace
    due = [SimpleNamespace(id=n, due=date.today() - timedelta(days=1), label=label, slug=slug, kind="", ref="")
           for n, (label, slug) in enumerate([("relancer Acme", "acme"), ("relancer Bolt", "bolt"), ("libre", "")])]
    monkeypatch.setattr(main.personal, "due", lambda today: due)
    app.post("/clients/acme/archiver")
    notes = main._reminder_notes(main.fiches.list_deployments(), date.today())
    assert [n["title"] for n in notes] == ["Rappel : relancer Bolt", "Rappel : libre"]
