"""Configuration des clients par l'utilisateur : formulaire, fichier yaml local, requêtes générées, retrait sans perte."""

import pytest
import yaml
from fastapi.testclient import TestClient

from deploy_hub import client_config, main
from deploy_hub.client_config import ClientConfig, ConfigError, from_form, parse_stacks, slugify

PAGE = "0123456789abcdef0123456789abcdef"


def test_slugify():
    assert slugify("Acme Santé au Travail") == "acme-sante-au-travail"


def test_from_form_reads_ids_lists_and_stacks():
    cfg = from_form({"client": "Acme", "page_id": f"https://app.notion.com/p/padoa/Deploiement-Acme-{PAGE}",
                     "recette_clients": "acme_pulse, acme-uegar", "orchestrator": "acme-uegar\nacme-pulse",
                     "stacks": "prod | Production | https://acme.padoa.fr\nhttps://acme-recette.padoa.fr"})
    assert (cfg.slug, cfg.page_id) == ("acme", PAGE)
    assert cfg.recette_clients == ["acme_pulse", "acme-uegar"] and cfg.orchestrator == ["acme-uegar", "acme-pulse"]
    assert cfg.stacks == [{"env": "prod", "label": "Production", "url": "https://acme.padoa.fr"},
                          {"env": "", "label": "acme-recette.padoa.fr", "url": "https://acme-recette.padoa.fr"}]


@pytest.mark.parametrize(("form", "message"), [
    ({"page_id": PAGE}, "nom du client"),
    ({"client": "Acme", "page_id": "rien"}, "32 caractères"),
    ({"client": "Acme", "page_id": PAGE, "slug": "Pas Valide"}, "Identifiant invalide"),
    ({"client": "Acme", "page_id": PAGE, "stacks": "prod | Prod | acme.fr"}, "https://"),
])
def test_form_errors(form, message):
    with pytest.raises(ConfigError, match=message):
        from_form(form)


def test_save_writes_yaml_and_queries_and_refuses_duplicates(tmp_path):
    cfg = ClientConfig(slug="acme", client="Acme", page_id=PAGE, recette_clients=["acme_pulse"])
    path = client_config.save(cfg, tmp_path, new=True)
    data = yaml.safe_load(path.read_text())
    assert path == tmp_path / "config" / "clients" / "acme.yaml" and data["page_id"] == PAGE
    assert "acme_pulse" in (tmp_path / "data" / "notion" / "acme" / "requetes" / "recette.sql").read_text()
    with pytest.raises(ConfigError, match="existe déjà"):
        client_config.save(cfg, tmp_path, new=True)
    assert client_config.load("acme", tmp_path).stacks_text == ""


def test_retire_keeps_the_file(tmp_path):
    client_config.save(ClientConfig(slug="acme", client="Acme", page_id=PAGE), tmp_path, new=True)
    moved = client_config.retire("acme", tmp_path)
    assert moved.parent == tmp_path / "config" / "clients" / "retires" and moved.is_file()
    assert client_config.all_configs(tmp_path) == []


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    return TestClient(main.app)


def test_add_edit_and_retire_a_client_from_the_app(app, tmp_path):
    assert "Ajouter un client" in app.get("/clients/nouveau").text
    r = app.post("/clients/enregistrer", data={"client": "Acme", "page_id": PAGE, "software": "Pulse"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/clients?enregistre=acme"
    assert (tmp_path / "config" / "clients" / "acme.yaml").is_file()
    clients_page = app.get("/clients").text
    assert "En attente de première synchro" in clients_page and "Acme" in clients_page
    edit = app.get("/clients/acme/configuration").text
    assert f'value="{PAGE}"' in edit and "readonly" in edit
    r = app.post("/clients/enregistrer", data={"existing_slug": "acme", "client": "Acme SST", "page_id": PAGE}, follow_redirects=False)
    assert r.status_code == 303 and client_config.load("acme").client == "Acme SST"
    assert app.post("/clients/acme/retirer", follow_redirects=False).status_code == 303
    assert app.get("/clients/acme/configuration").status_code == 404


def test_form_shows_errors_and_keeps_values(app):
    r = app.post("/clients/enregistrer", data={"client": "Acme", "page_id": "pas un id"})
    assert "32 caractères" in r.text and 'value="Acme"' in r.text


def test_retire_also_sets_the_fiche_aside(tmp_path):
    client_config.save(ClientConfig(slug="acme", client="Acme", page_id=PAGE), tmp_path, new=True)
    fiche = tmp_path / "data" / "deploiements" / "acme.md"
    fiche.parent.mkdir(parents=True)
    fiche.write_text("---\nclient: Acme\n---\n")
    client_config.retire("acme", tmp_path)
    assert not fiche.exists() and len(list((fiche.parent / "retires").glob("acme-*.md"))) == 1


def test_save_keeps_keys_added_by_hand(tmp_path):
    path = client_config.save(ClientConfig(slug="acme", client="Acme", page_id=PAGE), tmp_path, new=True)
    path.write_text(path.read_text() + "note: client multi-vagues\n")
    client_config.save(ClientConfig(slug="acme", client="Acme SST", page_id=PAGE), tmp_path)
    data = yaml.safe_load(path.read_text())
    assert data["client"] == "Acme SST" and data["note"] == "client multi-vagues"


def test_retire_confirmation_survives_an_apostrophe(app):
    app.post("/clients/enregistrer", data={"client": "L'AIST 22", "slug": "aist22", "page_id": PAGE})
    html = app.get("/clients/aist22/configuration").text
    assert 'confirm(&#34;Retirer L\\u0027AIST 22 du suivi' in html  # chaîne JSON : apostrophe encodée, JS valide


def test_page_id_of_a_titled_notion_url():
    from deploy_hub.sync_notion import _page_id
    assert _page_id("https://www.notion.so/Retour-Acme-" + PAGE) == PAGE and _page_id("https://example.com") == ""


def test_two_clients_cannot_share_a_notion_page(tmp_path):
    from deploy_hub.client_config import ClientConfig, save
    save(ClientConfig(slug="acme", client="Acme", page_id="a" * 32), tmp_path, new=True)
    with pytest.raises(ConfigError, match="déjà celle du client « Acme »"):
        save(ClientConfig(slug="acme2", client="Acme bis", page_id="a" * 32), tmp_path, new=True)


DRIVE = "https://drive.google.com/drive/folders/ID-FACTICE-DE-TEST"  # adresse inventée : aucun vrai lien Drive dans les tests
DOC = "https://docs.google.com/spreadsheets/d/ID-FACTICE-DE-TEST/edit"


def test_drive_links_are_read_from_the_form_and_kept_in_the_yaml(tmp_path):
    cfg = from_form({"client": "Acme", "page_id": PAGE, "drive": f"Dossier client | {DRIVE}\n{DOC}"})
    assert cfg.drive == [{"label": "Dossier client", "url": DRIVE}, {"label": "Drive", "url": DOC}]
    client_config.save(cfg, tmp_path, new=True)
    again = client_config.load("acme", tmp_path)
    assert again.drive == cfg.drive and again.drive_text == f"Dossier client | {DRIVE}\nDrive | {DOC}"


@pytest.mark.parametrize("line", ["Dossier | http://drive.google.com/x", "Dossier | https://evil.example/drive.google.com/x",
                                  "https://drive.google.com.evil.example/x"])
def test_only_google_drive_addresses_are_accepted(line):
    with pytest.raises(ConfigError, match="drive.google.com"):
        from_form({"client": "Acme", "page_id": PAGE, "drive": line})


def test_drive_links_are_shown_as_links_and_never_given_to_agents():
    from deploy_hub.models import Deployment, Link, RetourRecette
    from deploy_hub.sources.claude_runner import retour_prompt
    d = Deployment(slug="acme", client="Acme", software="Pulse", drive=[Link("Dossier client", DRIVE)])
    html = main.templates.get_template("_stacks.html").render(d=d)
    assert f'href="{DRIVE}" target="_blank" rel="noopener noreferrer"' in html and "Dossier client ↗" in html
    assert DRIVE not in retour_prompt(d, RetourRecette(id="0001AB", data_type="Adhérents", status="À faire"))


def test_the_form_has_a_drive_field(app):
    page = app.get("/clients/nouveau").text
    assert 'name="drive"' in page and "ne lit jamais leur contenu" in page


def test_without_drive_links_the_fiche_offers_to_add_them():
    from deploy_hub.models import Deployment
    html = main.templates.get_template("_stacks.html").render(d=Deployment(slug="acme", client="Acme", software="Pulse"))
    assert '<a class="muted" href="/clients/acme/configuration" title="Ajouter les liens Google Drive du client">+ Drive</a>' in html
