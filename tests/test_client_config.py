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
    monkeypatch.setattr(main.fiches, "clients_dir", tmp_path / "config" / "clients")  # lu au démarrage par l'app
    return TestClient(main.app)


def test_add_edit_and_retire_a_client_from_the_app(app, tmp_path):
    assert "Ajouter un client" in app.get("/clients/nouveau").text
    r = app.post("/clients/enregistrer", data={"client": "Acme", "page_id": PAGE, "software": "Pulse"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/deploiements/acme/synthese"  # sa fiche, synchro lancée
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


def test_drive_links_are_shown_as_links_and_never_given_to_agents(app):
    from deploy_hub.models import Deployment, Link, RetourRecette
    from deploy_hub.sources.claude_runner import retour_prompt
    app.post("/clients/enregistrer", data={"client": "Acme", "page_id": PAGE, "drive": f"Dossier client | {DRIVE}"})
    fiche = app.get("/deploiements/acme/synthese").text
    assert f'href="{DRIVE}" target="_blank" rel="noopener noreferrer" data-tip="Drive · Dossier client ↗"' in fiche  # dans le dock
    d = Deployment(slug="acme", client="Acme", software="Pulse", drive=[Link("Dossier client", DRIVE)])
    assert DRIVE not in retour_prompt(d, RetourRecette(id="0001AB", data_type="Adhérents", status="À faire"))


def test_the_form_has_a_drive_field(app):
    page = app.get("/clients/nouveau").text
    assert 'name="drive"' in page and "ne lit jamais leur contenu" in page


def test_without_drive_links_the_fiche_offers_to_add_them(app):
    app.post("/clients/enregistrer", data={"client": "Acme", "page_id": PAGE})
    fiche = app.get("/deploiements/acme/synthese").text
    assert 'data-tip="Ajouter un lien Drive"' in fiche and 'class="stacks"' not in fiche  # le dock, plus de rangée en doublon


def test_the_links_page_only_changes_the_links(app):
    app.post("/clients/enregistrer", data={"client": "Acme", "page_id": PAGE, "software": "Pulse", "orchestrator": "acme-pulse"})
    page = app.get("/clients/acme/liens?vers=/deadlines").text
    assert "Liens de Acme" in page and 'name="drive"' in page and 'name="stacks"' in page
    assert 'name="page_id"' not in page and 'name="orchestrator"' not in page  # rien d'autre que les liens
    r = app.post("/clients/acme/liens", data={"drive": f"Dossier | {DRIVE}", "stacks": "prod | Production | https://acme.padoa.fr",
                                              "notion_url": "", "vers": "/deadlines"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/deadlines"  # retour à la page d'où l'on vient
    cfg = client_config.load("acme")
    assert cfg.drive == [{"label": "Dossier", "url": DRIVE}] and cfg.stacks[0]["label"] == "Production"
    assert (cfg.software, cfg.orchestrator, cfg.page_id) == ("Pulse", ["acme-pulse"], PAGE)  # le reste est intact


def test_the_links_page_refuses_bad_links_and_foreign_returns(app):
    app.post("/clients/enregistrer", data={"client": "Acme", "page_id": PAGE})
    bad = app.post("/clients/acme/liens", data={"drive": "https://ailleurs.example/x"})
    assert "drive.google.com" in bad.text and client_config.load("acme").drive == []
    r = app.post("/clients/acme/liens", data={"drive": "", "vers": "//site-hostile.example/"}, follow_redirects=False)
    assert r.headers["location"] == "/deploiements/acme/synthese"  # jamais de redirection hors de Jarvis
    assert app.get("/clients/inconnu/liens", follow_redirects=False).headers["location"] == "/clients/inconnu/configuration"


def test_a_client_just_added_is_in_the_selector_with_an_empty_fiche(app, tmp_path):
    """Un client ajouté apparaît tout de suite (sélecteur, fiche vide, bouton Rafraîchir), sans attendre la première synchro."""
    app.post("/clients/enregistrer", data={"client": "Acme", "page_id": PAGE, "software": "Pulse"}, follow_redirects=False)
    home = app.get("/").text
    bar = home.split("<main>", 1)[0]
    assert "Acme" in bar and 'class="client-switch"' in bar  # dans le sélecteur
    fiche = app.get("/deploiements/acme/synthese")
    assert fiche.status_code == 200 and "Pas encore synchronisé" in fiche.text and 'hx-post="/actions/rafraichir/acme"' in fiche.text
    clients_page = app.get("/clients").text
    assert "En attente de première synchro" in clients_page  # carte « en attente », pas une carte vide de client synchronisé
    fiche_file = tmp_path / "data" / "deploiements" / "acme.md"  # après la synchro : la vraie fiche prend le relais
    fiche_file.parent.mkdir(parents=True)
    fiche_file.write_text("---\nclient: Acme\nsynced_at: 17/11 16:00\n---\n")
    monkeypatch_dir = main.fiches.deployments_dir
    try:
        main.fiches.deployments_dir = fiche_file.parent
        assert "Pas encore synchronisé" not in app.get("/deploiements/acme/synthese").text
    finally:
        main.fiches.deployments_dir = monkeypatch_dir


def test_a_new_client_is_synced_at_once_without_a_token_by_the_routine(app, monkeypatch):
    started = []
    monkeypatch.setattr(main.notion_api, "load_token", lambda root=None: None)
    monkeypatch.setattr(main.sync_routine, "start", lambda slug="", client="": started.append(f"routine {slug}".strip()))
    monkeypatch.setattr(main.client_directory, "refresh_icons", lambda: started.append("icones"))  # son emoji, à part
    r = app.post("/clients/enregistrer", data={"client": "Acme", "page_id": PAGE}, follow_redirects=False)
    assert r.headers["location"] == "/deploiements/acme/synthese" and started == ["routine acme", "icones"]
    app.post("/clients/enregistrer", data={"existing_slug": "acme", "client": "Acme SST", "page_id": PAGE})
    assert started == ["routine acme", "icones"]  # une modification ne relance pas de synchro


def test_a_new_client_is_synced_at_once_with_a_token_by_the_api(app, monkeypatch):
    import threading
    done = threading.Event()
    calls = []
    monkeypatch.setattr(main.notion_api, "load_token", lambda root=None: "jeton-fictif")
    monkeypatch.setattr(main, "_refresh", lambda slugs: (calls.append(slugs), done.set()))
    app.post("/clients/enregistrer", data={"client": "Acme", "page_id": PAGE}, follow_redirects=False)
    assert done.wait(5) and calls == [["acme"]]  # en fond : la page ne l'attend pas


def test_a_client_not_synced_yet_can_be_opened_from_the_clients_page(app, monkeypatch):
    monkeypatch.setattr(main, "_sync_new_client", lambda slug, background: None)
    app.post("/clients/enregistrer", data={"client": "Acme", "page_id": PAGE})
    page = app.get("/clients").text
    assert "En attente de première synchro" in page and '<a class="card-link" href="/deploiements/acme"><strong>Acme</strong></a>' in page
    assert app.get("/deploiements/acme/synthese").status_code == 200


def test_automatic_sync_is_off_by_default_and_toggled_from_the_fiche(app, tmp_path, monkeypatch):
    """Bouton « Synchro auto » de la fiche : non par défaut ; un clic l'active pour ce client seulement, noté dans son yaml."""
    from deploy_hub import client_config
    monkeypatch.setattr(main, "_sync_new_client", lambda slug, background: None)
    app.post("/clients/enregistrer", data={"client": "Acme", "page_id": PAGE})
    yaml_file = tmp_path / "config" / "clients" / "acme.yaml"
    assert client_config.load("acme", tmp_path).auto_sync is False and "auto_sync" not in yaml_file.read_text(encoding="utf-8")
    fiche = app.get("/deploiements/acme/synthese").text
    assert 'class="secondary auto-sync" aria-pressed="false"' in fiche and "Synchro auto : non" in fiche
    assert """hx-post="/clients/acme/synchro-auto" hx-vals='{"actif": "1"}'""" in fiche
    r = app.post("/clients/acme/synchro-auto", data={"actif": "1"})
    assert r.status_code == 200 and 'aria-pressed="true"' in r.text and "Synchro auto : oui" in r.text and '"actif": "0"' in r.text
    assert 'class="auto-sync-box"' in r.text and "· prochaine " in r.text  # l'heure prévue s'affiche dès l'activation
    assert client_config.load("acme", tmp_path).auto_sync is True and "auto_sync: true" in yaml_file.read_text(encoding="utf-8")
    app.post("/clients/enregistrer", data={"existing_slug": "acme", "client": "Acme SST", "page_id": PAGE})  # modifier sa configuration ne la coupe pas
    assert client_config.load("acme", tmp_path).auto_sync is True
    off = app.post("/clients/acme/synchro-auto", data={"actif": "0"}).text
    assert "Synchro auto : non" in off and "· prochaine" not in off
    assert client_config.load("acme", tmp_path).auto_sync is False
    assert app.post("/clients/inconnu/synchro-auto", data={"actif": "1"}).status_code == 404


def test_the_other_notion_dashboards_of_a_client_are_in_the_dock(app):
    """Ils n'étaient que dans la rangée de liens du haut de la fiche, retirée : le dock les reprend."""
    app.post("/clients/enregistrer", data={"client": "Acme", "page_id": PAGE, "dashboards": f"Formation | https://app.notion.com/p/{PAGE}"})
    fiche = app.get("/deploiements/acme/synthese").text
    assert 'data-tip="Notion · Formation ↗"' in fiche
