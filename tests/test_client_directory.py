"""Ajouter un client en le choisissant dans l'annuaire Notion (données fictives ; faux `claude`, aucun appel réseau)."""
import json
import time
from datetime import datetime, timedelta
from html import unescape

import pytest
from fastapi.testclient import TestClient

from deploy_hub import client_config, main
from deploy_hub.client_config import ClientConfig, ConfigError, parse_dashboards
from deploy_hub.sources import client_directory
from deploy_hub.sources.client_directory import (CLIENTS_DS, DASHBOARDS_PAGE, ClientDirectory, build, client_of, dashboards_of,
                                                 fiches_of, name_key, form_of, prompt, read_session)

from test_sync_routine import fake_claude

PAGE_TEXT = """<content>
<h1 color="purple_bg">
🚀 Dashboards Delivery
</h1>
<toggle heading="h3">Clients déployés
<page url="https://app.notion.com/p/aaa1">
Dashboard de déploiement - ACME 12-34
</page>
</toggle>
<page url="https://app.notion.com/p/zzz1">
Dashboard de déploiement — Bêta Santé (fusion Gamma)
</page>
<h1 color="blue_bg">
🎓 Dashboards Formation
</h1>
<page url="https://app.notion.com/p/ccc1">
Dashboard de formation - ACME 12/34
</page>
<page url="https://app.notion.com/p/ttt1">
Dashboard de formation - Nom du Client
</page>
<h1 color="green_bg">
:base-de-donnees: Dashboards Intégration
</h1>
<page url="https://app.notion.com/p/bbb1">
Dashboard intégration - ACME12-34
</page>
<page url="https://app.notion.com/p/bbb2">
Dashboard intégration - ACME 12-34 (lot 2)
</page>
<h1 color="brown_bg">
💰 Dashboards Gesfin
</h1>
<page url="https://app.notion.com/p/ddd1">
Dashboard de Gestion financière - ACME 12-34
</page>
<h1 color="red_bg">
🧪 Dashboards de test
</h1>
<page url="https://app.notion.com/p/eee1">
Dashboard de déploiement - ACME 12-34
</page>
</content>"""
FICHE = "https://app.notion.com/0123456789abcdef0123456789abcdef"
ROWS = [{"url": FICHE, "Nom": "Déploiement ACME 12-34", "élément parent": "[]"},
        {"url": "https://app.notion.com/1111111111111111aaaaaaaaaaaaaaaa", "Nom": "ACME 12-34 lot 2", "élément parent": '["x"]'},
        {"url": "https://app.notion.com/2222222222222222bbbbbbbbbbbbbbbb", "Nom": "Déploiement ACME 12-345", "élément parent": None},
        {"url": "https://app.notion.com/3333333333333333cccccccccccccccc", "Nom": "Bêta Santé", "élément parent": []}]


def test_the_dashboards_page_is_read_by_section_and_client():
    found = dashboards_of(PAGE_TEXT)
    assert [(d["famille"], d["client"], d["url"][-4:]) for d in found] == [
        ("deploiement", "ACME 12-34", "aaa1"), ("deploiement", "Bêta Santé (fusion Gamma)", "zzz1"), ("formation", "ACME 12/34", "ccc1"),
        ("integration", "ACME12-34", "bbb1"), ("integration", "ACME 12-34 (lot 2)", "bbb2"), ("gestion_financiere", "ACME 12-34", "ddd1")]
    # section de test et modèle « Nom du Client » ignorés
    assert name_key("ACME 12/34") == name_key("ACME12-34") == name_key("Acme 12-34 (lot 2)") == "acme1234"
    assert client_of("Deployment dashboard - Bêta") == "Bêta" and client_of("Sans séparateur") == ""
    from deploy_hub.sources.client_directory import fiche_key
    assert fiche_key("Déploiement ACME 12-34") == fiche_key("Deployment ACME 12-34") == fiche_key("ACME 12-34") == "acme1234"


def test_each_client_gets_its_dashboards_and_its_fiche():
    entries = {e["cle"]: e for e in build(dashboards_of(PAGE_TEXT), fiches_of(ROWS))}
    acme = entries["acme1234"]
    assert acme["client"] == "ACME 12-34" and len(acme["dashboards"]) == 5
    form = form_of(acme, {"acme1234", "acme1234-uegar", "autre"})
    assert form["slug"] == "acme1234"  # pas « acme-12-34 » : comme les clés de l'orchestrator
    assert form["orchestrator"] == "acme1234, acme1234-uegar"
    assert form["page_id"] == "https://app.notion.com/p/padoa/D-ploiement-ACME-12-34-0123456789abcdef0123456789abcdef"  # « Déploiement ACME 12-34 », pas « … 12-345 » ni le lot 2 ; lien complet
    assert form["notion_url"] == "https://app.notion.com/p/bbb1"
    assert form["dashboards"].splitlines() == ["Déploiement | https://app.notion.com/p/aaa1", "Formation | https://app.notion.com/p/ccc1",
                                               "Dashboard intégration - ACME 12-34 (lot 2) | https://app.notion.com/p/bbb2",
                                               "Gestion financière | https://app.notion.com/p/ddd1"]
    assert parse_dashboards(form["dashboards"])[0]["label"] == "Déploiement"
    beta = form_of(entries["betasante"])
    assert beta["page_id"] == "https://app.notion.com/p/padoa/B-ta-Sant-3333333333333333cccccccccccccccc" and beta["notion_url"] == ""


def test_two_main_fiches_with_the_name_leave_the_choice():
    rows = ROWS + [{"url": "https://app.notion.com/4444444444444444dddddddddddddddd", "Nom": "ACME 12-34 vague 2", "élément parent": ""}]
    entry = next(e for e in build(dashboards_of(PAGE_TEXT), fiches_of(rows)) if e["cle"] == "acme1234")
    assert form_of(entry)["page_id"] == "https://app.notion.com/p/padoa/D-ploiement-ACME-12-34-0123456789abcdef0123456789abcdef"  # le nom exact l'emporte
    rows = [r for r in rows if r["url"] != FICHE]
    entry = next(e for e in build(dashboards_of(PAGE_TEXT), fiches_of(rows)) if e["cle"] == "acme1234")
    assert form_of(entry)["page_id"] == ""  # « 12-345 » et « vague 2 » : à choisir dans la liste


def test_the_prompt_only_asks_to_read(monkeypatch):
    text = prompt()
    assert DASHBOARDS_PAGE in text and f"collection://{CLIENTS_DS}" in text and "Lecture seule" in text and "OFFSET" in text
    monkeypatch.setenv("JARVIS_NOTION_DASHBOARDS", "f" * 32)
    assert "f" * 32 in prompt()


def _save_session(projects, session, page=PAGE_TEXT, rows=ROWS, as_list=False):
    folder = projects / "-home-x-Jarvis" / session / "tool-results"
    folder.mkdir(parents=True)
    fetch = [{"type": "text", "text": json.dumps({"text": page})}] if as_list else {"metadata": {"type": "page"}, "text": page}
    (folder / "mcp-claude_ai_Notion-notion-fetch-1-a.txt").write_text(json.dumps(fetch))
    (folder / "mcp-claude_ai_Notion-notion-query-data-sources-2-b.txt").write_text(json.dumps({"results": rows[:2]}))
    (folder / "mcp-claude_ai_Notion-notion-query-data-sources-3-c.txt").write_text(json.dumps({"results": rows[2:]}))


@pytest.mark.parametrize("as_list", [False, True])
def test_the_saved_notion_answers_of_the_session_are_read(tmp_path, as_list):
    _save_session(tmp_path, "s1", as_list=as_list)
    dashboards, fiches = read_session("s1", tmp_path)
    assert len(dashboards) == 6 and len(fiches) == 4  # les deux pages de résultats
    with pytest.raises(ConfigError, match="pas lu la page"):
        read_session("autre-session", tmp_path)


def wait(directory: ClientDirectory) -> dict:
    for _ in range(200):
        if not directory.running and not directory.state().get("running"):
            return directory.state()
        time.sleep(0.05)
    raise AssertionError("la lecture ne s'est pas terminée")


def claude_saving(tmp_path, projects) -> str:
    """Faux claude : range une page et une base comme le vrai (fichiers de sa session), puis répond « fini »."""
    body = f"""
import json, pathlib
session = sys.argv[sys.argv.index("--session-id") + 1]
folder = pathlib.Path({str(projects)!r}) / "-home-x-Jarvis" / session / "tool-results"
folder.mkdir(parents=True)
(folder / "mcp-claude_ai_Notion-notion-fetch-1-a.txt").write_text(json.dumps({{"text": {PAGE_TEXT!r}}}))
(folder / "mcp-claude_ai_Notion-notion-query-data-sources-2-b.txt").write_text(json.dumps({{"results": {ROWS!r}}}))
print("fini")
"""
    return str(fake_claude(tmp_path, body))


def test_the_directory_is_read_by_claude_with_notion_only(tmp_path):
    projects = tmp_path / "projects"
    directory = ClientDirectory(folder=tmp_path / "notion", claude_bin=claude_saving(tmp_path, projects), projects=projects)
    assert directory.should_refresh(directory.directory())
    assert directory.refresh().ok
    state = wait(directory)
    assert state["ok"] and state["resume"] == "2 clients, 4 fiches"
    assert not directory.should_refresh(directory.directory())
    assert directory.entry(directory.directory(), "acme1234")["client"] == "ACME 12-34"
    assert len(directory.directory()["fiches"]) == 3  # fiches principales seulement (pas le lot 2), pour la liste du champ
    args, _, stdin, _ = eval((tmp_path / "recu.txt").read_text())
    assert args[args.index("--allowedTools") + 1:] == ["mcp__claude_ai_Notion"] and "dontAsk" in args and "--session-id" in args
    assert DASHBOARDS_PAGE in stdin


def test_a_failed_reading_is_not_retried_at_each_visit(tmp_path):
    directory = ClientDirectory(folder=tmp_path / "notion", claude_bin=str(fake_claude(tmp_path, "print('ÉCHEC : Notion indisponible')")),
                                projects=tmp_path / "projects")
    directory.refresh()
    state = wait(directory)
    assert not state.get("ok") and "pas lu la page" in state["error"] and "Notion indisponible" in state["error"]
    assert not directory.should_refresh({})  # moins de 10 minutes : pas de nouvelle tentative à chaque ouverture
    assert "CLAUDE_BIN" in ClientDirectory(folder=tmp_path / "n2", claude_bin=str(tmp_path / "absent")).refresh().message


def test_a_reading_cut_by_a_restart_says_so(tmp_path):
    folder = tmp_path / "notion"
    folder.mkdir()
    (folder / "annuaire-etat.json").write_text(json.dumps({"running": True, "started": datetime.now().isoformat()}))
    state = ClientDirectory(folder=folder).state()
    assert not state["running"] and "interrompue" in state["error"]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(main.fiches, "clients_dir", tmp_path / "config" / "clients")
    directory = ClientDirectory(folder=tmp_path / "data" / "notion", claude_bin=str(tmp_path / "absent"))
    directory._write("annuaire.json", {"format": client_directory.FORMAT, "lu": datetime.now().isoformat(timespec="seconds"),
                                       "clients": build(dashboards_of(PAGE_TEXT), fiches_of(ROWS)),
                                       "fiches": [f for f in fiches_of(ROWS) if not f["parent"]]})
    monkeypatch.setattr(main, "client_directory", directory)
    monkeypatch.setattr(main.dag_catalogue, "catalogue", lambda: {"clients": {"acme1234": {}}})
    return TestClient(main.app)


def test_choosing_a_client_fills_the_form_at_once(app, tmp_path):
    page = unescape(app.get("/clients/nouveau").text)
    assert '<select name="client"' in page and '<option value="acme1234">ACME 12-34</option>' in page
    assert "Ou remplir à la main" in page and "Chercher" not in page  # rien à chercher : on choisit
    form = unescape(app.get("/clients/nouveau?client=acme1234").text)
    assert 'value="acme1234" selected' in form and "ACME 12-34 : relis" in form
    for value in ('name="slug" value="acme1234"', 'value="https://app.notion.com/p/padoa/D-ploiement-ACME-12-34-0123456789abcdef0123456789abcdef"', 'value="https://app.notion.com/p/bbb1"',
                  'name="orchestrator" list="orchestrator-keys" value="acme1234"', "Formation | https://app.notion.com/p/ccc1",
                  '<option value="https://app.notion.com/p/padoa/D-ploiement-ACME-12-34-0123456789abcdef0123456789abcdef" label="Déploiement ACME 12-34">'):
        assert value in form, value
    app.post("/clients/enregistrer", data={"client": "ACME 12-34", "slug": "acme1234", "page_id": FICHE})
    assert client_config.load("acme1234").page_id == "0123456789abcdef0123456789abcdef"
    after = unescape(app.get("/clients/nouveau").text)
    assert '<option value="acme1234" disabled>ACME 12-34 · déjà suivi</option>' in after


def test_an_old_directory_is_read_again_and_the_page_reloads_when_it_is_new(app, monkeypatch):
    directory = main.client_directory
    calls = []
    monkeypatch.setattr(directory, "refresh", lambda: calls.append(1))
    app.get("/clients/nouveau")
    assert not calls  # lu il y a moins d'un jour
    data = directory.directory()
    directory._write("annuaire.json", {**data, "lu": (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")})
    app.get("/clients/nouveau")
    assert calls == [1]
    first = app.get("/clients/annuaire/etat", params={"lu": ""})
    assert first.headers.get("HX-Refresh") == "true"  # aucune liste n'était affichée : la page se recharge pour la montrer
    newer = app.get("/clients/annuaire/etat", params={"lu": "ancienne"})
    assert "HX-Refresh" not in newer.headers and "Afficher la nouvelle liste" in newer.text  # jamais sous les doigts
    same = app.get("/clients/annuaire/etat", params={"lu": directory.directory()["lu"]})
    assert "HX-Refresh" not in same.headers and "Afficher la nouvelle liste" not in same.text


def test_jarvis_reads_the_directory_in_the_background_so_nobody_waits(app, monkeypatch):
    """Boucle de fond (chaque minute, dès le démarrage) : l'annuaire de plus d'un jour est relu sans qu'on ouvre la page."""
    directory = main.client_directory
    calls = []
    monkeypatch.setattr(directory, "refresh", lambda: calls.append(1))
    monkeypatch.setattr(main.dag_source, "refresh", lambda: 1 / 0)  # une tâche en échec n'empêche pas les suivantes
    monkeypatch.setattr(main.web_push, "pump", lambda _n: None)
    main._push_round()
    assert not calls  # lu il y a moins d'un jour
    data = directory.directory()
    directory._write("annuaire.json", {**data, "lu": (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")})
    main._push_round()
    assert calls == [1]


def test_without_claude_the_reading_is_not_retried_every_minute(tmp_path):
    directory = ClientDirectory(folder=tmp_path / "notion", claude_bin=str(tmp_path / "absent"))
    directory.refresh_if_stale()
    state = directory.state()
    assert "CLAUDE_BIN" in state["error"] and not directory.should_refresh({})  # noté : prochain essai dans 10 minutes


def test_a_refresh_with_a_list_already_shown_keeps_it_usable(app, tmp_path):
    directory = main.client_directory
    directory._write("annuaire-etat.json", {"running": True, "started": datetime.now().isoformat()})
    directory._proc = type("Proc", (), {"poll": lambda self: None})()  # lecture en cours dans ce Jarvis
    page = unescape(app.get("/clients/nouveau").text)
    assert '<option value="acme1234">ACME 12-34</option>' in page and "la liste ci-dessus reste utilisable" in page
    assert "Première lecture" not in page


def test_links_are_complete_like_notions_copy_link():
    """« Copier le lien » de Notion donne https://app.notion.com/p/<espace>/<Titre>-<id> : la lecture ne donne que l'id."""
    from deploy_hub.sources.client_directory import full_link
    assert full_link("Déploiement ACME 12-34", FICHE) == "https://app.notion.com/p/padoa/D-ploiement-ACME-12-34-0123456789abcdef0123456789abcdef"
    assert full_link("Dashboard intégration - ACME12-34 🇩🇪", "https://app.notion.com/p/" + "a" * 32) == (
        "https://app.notion.com/p/padoa/Dashboard-int-gration-ACME12-34-" + "a" * 32)
    assert full_link("", "https://app.notion.com/" + "b" * 32) == "https://app.notion.com/p/padoa/" + "b" * 32
    assert full_link("Sans id", "https://app.notion.com/p/aaa1") == "https://app.notion.com/p/aaa1"  # rien à compléter
    from deploy_hub.queries import page_id_of
    assert page_id_of("https://app.notion.com/p/padoa/D-ploiement-ACME-12-34-0123456789abcdef0123456789abcdef") == "0123456789abcdef0123456789abcdef"  # le formulaire n'en garde que l'id


def test_a_directory_of_an_older_format_is_read_again_at_once(tmp_path):
    """Annuaire lu avant les liens complets : relu tout de suite, sans attendre un jour."""
    directory = ClientDirectory(folder=tmp_path / "notion")
    now = datetime.now().isoformat(timespec="seconds")
    assert directory.should_refresh({"lu": now})  # format 1 (sans « format »)
    assert not directory.should_refresh({"format": client_directory.FORMAT, "lu": now})


def test_dashboard_links_without_p_are_repaired_on_display(app):
    """La synchro renvoie des adresses « https://app.notion.com/<id> » (sans /p/, en erreur) : le dashboard du client est réparé."""
    client_config.save(ClientConfig(slug="acme1234", client="ACME 12-34", page_id="0123456789abcdef0123456789abcdef",
                                    notion_url="https://app.notion.com/" + "c" * 32,
                                    dashboards=[{"label": "Formation", "url": "https://app.notion.com/" + "d" * 32}]))
    fiche = app.get("/deploiements/acme1234/synthese").text
    assert 'href="https://app.notion.com/p/' + "c" * 32 + '"' in fiche and 'href="https://app.notion.com/p/' + "d" * 32 + '"' in fiche


DASHBOARD = "e" * 32  # dashboard intégration fictif d'un client suivi


def test_the_directory_reading_does_not_read_icons():
    """L'annuaire garde sa durée : les icônes se lisent dans une tâche à part."""
    assert "ICONE" not in prompt() and "notion-fetch" in prompt()


def _follow(tmp_path, slug="acme1234"):
    folder = tmp_path / "config" / "clients"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{slug}.yaml").write_text(f"client: Acme\npage_id: {'0' * 32}\nnotion_url: https://app.notion.com/p/{DASHBOARD}\n")


def test_only_followed_clients_without_an_icon_are_read_in_a_separate_task(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CONFIG_DIR", str(tmp_path / "config"))
    _follow(tmp_path)
    out = f"ICONE https://app.notion.com/p/{DASHBOARD} 🦇"
    directory = ClientDirectory(folder=tmp_path / "notion", claude_bin=str(fake_claude(tmp_path, f"print({out!r})")))
    assert directory.missing_icons() == {DASHBOARD: ("acme1234", f"https://app.notion.com/p/{DASHBOARD}")}
    assert directory.refresh_icons()
    for _ in range(200):
        if not directory.icons_running:
            break
        time.sleep(0.05)
    from deploy_hub.sync_notion import saved_icon
    assert saved_icon(tmp_path / "notion" / "acme1234")["emoji"] == "🦇"
    args, _, stdin, _ = eval((tmp_path / "recu.txt").read_text())
    assert args[args.index("--allowedTools") + 1:] == ["mcp__claude_ai_Notion"] and DASHBOARD in stdin and "ICONE" in stdin
    assert directory.missing_icons() == {} and not directory.refresh_icons()  # lue une fois : plus relue


def test_a_failed_icon_reading_is_retried_later_but_not_every_minute(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CONFIG_DIR", str(tmp_path / "config"))
    _follow(tmp_path)
    directory = ClientDirectory(folder=tmp_path / "notion", claude_bin=str(fake_claude(tmp_path, "print('ÉCHEC : Notion indisponible')")))
    assert directory.refresh_icons()
    for _ in range(200):
        if not directory.icons_running:
            break
        time.sleep(0.05)
    assert directory.missing_icons() and not directory.refresh_icons()  # pas de ligne ICONE : pas notée ; pas avant 10 minutes


def test_a_page_without_icon_is_noted_and_not_read_again(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CONFIG_DIR", str(tmp_path / "config"))
    _follow(tmp_path)
    out = f"ICONE https://app.notion.com/p/{DASHBOARD} aucune"
    directory = ClientDirectory(folder=tmp_path / "notion", claude_bin=str(fake_claude(tmp_path, f"print({out!r})")))
    directory.refresh_icons()
    for _ in range(200):
        if not directory.icons_running:
            break
        time.sleep(0.05)
    from deploy_hub.sync_notion import saved_icon
    assert saved_icon(tmp_path / "notion" / "acme1234") == {"emoji": "", "icone": ""} and directory.missing_icons() == {}


def test_the_icon_is_kept_for_the_client_and_shown_on_its_fiche(tmp_path):
    from deploy_hub.sources.fiches import parse_deployment
    from deploy_hub.sync_notion import saved_icon
    directory = ClientDirectory(folder=tmp_path / "data" / "notion")
    followed = {DASHBOARD: "acme1234"}
    directory._save_icons({DASHBOARD: {"type": "emoji", "emoji": "🦇"}, "f" * 32: {"type": "emoji", "emoji": "🐙"}}, followed)
    assert saved_icon(tmp_path / "data" / "notion" / "acme1234") == {"emoji": "🦇", "icone": ""}
    assert not (tmp_path / "data" / "notion" / ("f" * 32)).exists()  # page d'un client non suivi : ignorée
    fiches_dir = tmp_path / "data" / "deploiements"
    fiches_dir.mkdir(parents=True)
    (fiches_dir / "acme1234.md").write_text("---\nclient: Acme\n---\n", encoding="utf-8")
    assert parse_deployment(fiches_dir / "acme1234.md").emoji == "🦇"  # fiche sans emoji : celui de l'annuaire
    (fiches_dir / "acme1234.md").write_text("---\nclient: Acme\nemoji: 🦖\n---\n", encoding="utf-8")
    assert parse_deployment(fiches_dir / "acme1234.md").emoji == "🦖"  # celui de la fiche (synchro avec jeton) l'emporte


def test_icons_written_in_the_answer_cover_small_pages():
    """Une petite page revient directement à Claude (pas de fichier rangé) : son icône vient de la ligne ICONE de sa réponse."""
    from deploy_hub.sources.client_directory import answered_icons
    answer = (f"fini\nICONE https://app.notion.com/p/{DASHBOARD} 🦖\nICONE https://app.notion.com/p/{'f' * 32} https://img.test/a.png\n"
              f"ICONE https://app.notion.com/p/{'1' * 32} aucune\nICONE https://app.notion.com/p/{'2' * 32} mot\nICONE pas-une-page 🐙")
    assert answered_icons(answer) == {DASHBOARD: {"type": "emoji", "emoji": "🦖"},
                                      "f" * 32: {"type": "external", "external": {"url": "https://img.test/a.png"}},
                                      "1" * 32: {}, "2" * 32: {}}  # « aucune » ou un mot : lue, sans icône
    from deploy_hub.sources.client_directory import icons_prompt
    assert "ICONE <adresse de la page> <icône>" in icons_prompt([f"https://app.notion.com/p/{DASHBOARD}"])
