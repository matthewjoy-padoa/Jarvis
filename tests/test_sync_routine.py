"""Routine de synchro Notion embarquée : consigne unique dans le dépôt, commande /jarvis-sync, bouton sans jeton."""
import os
import stat
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deploy_hub import sync_notion
from deploy_hub.config import PROJECT_ROOT
from deploy_hub.sources import notion_api, sync_routine
from deploy_hub.sources.sync_routine import SyncRoutine, routine_text


def test_the_routine_is_in_the_repository_without_anything_to_fill():
    text = routine_text()
    assert "<DOSSIER_JARVIS>" not in text and "<CANAL_SLACK>" not in text and "~/Documents" not in text
    for step in ("preparer", "plan", "fusionner", "utilisateurs"):
        assert f"uv run python -m deploy_hub.sync_notion {step}" in text
    command = (PROJECT_ROOT / ".claude" / "commands" / "jarvis-sync.md").read_text(encoding="utf-8")
    assert "sync/routine.md" in command and "$ARGUMENTS" in command  # /jarvis-sync [canal Slack]
    assert "sync/routine.md" in (PROJECT_ROOT / "docs" / "routine-synchro.md").read_text(encoding="utf-8")


def test_the_routine_queries_exactly_the_clients_configured_in_the_app(tmp_path):
    """« preparer » lit config/clients/ de l'app : l'empreinte porte ces clients-là, ni plus ni moins."""
    folder = tmp_path / "config" / "clients"
    folder.mkdir(parents=True)
    for slug, page in (("alpha", "a" * 32), ("beta", "b" * 32)):
        (folder / f"{slug}.yaml").write_text(f"client: {slug.title()}\nsoftware: Preventiel\nstatus: en cours\npage_id: {page}\n",
                                             encoding="utf-8")
    sync_notion.prepare(tmp_path)
    sql = (tmp_path / "data" / "notion" / "empreintes.sql").read_text(encoding="utf-8")
    assert "a" * 32 in sql and "b" * 32 in sql and sql.count("UNION ALL") == 1  # deux clients, une requête


def test_without_token_the_refresh_button_runs_the_routine(monkeypatch, tmp_path):
    """Pas de mode démo : sans jeton, le bouton lance vraiment la routine (ici, un faux claude)."""
    from deploy_hub import main
    monkeypatch.setattr(notion_api, "load_token", lambda root=None: None)
    claude = fake_claude(tmp_path, "print('client-demo : inchangé')")
    routine = SyncRoutine(folder=tmp_path / "notion", claude_bin=str(claude))
    monkeypatch.setattr(main, "sync_routine", routine)
    r = TestClient(main.app).post("/actions/rafraichir/client-demo")
    assert 'data-sync-state="running"' in r.text and 'data-sync-pct="' in r.text and 'hx-get="/actions/rafraichir/etat"' in r.text  # le bouton se remplit
    assert wait(routine)["ok"]
    page = TestClient(main.app).get("/deploiements/client-demo/synthese").text
    assert 'hx-post="/actions/rafraichir/client-demo"' in page and "Ni jeton Notion ni Claude Code" not in page  # bouton actif


def test_without_token_nor_claude_the_button_says_what_to_do(monkeypatch, tmp_path):
    from deploy_hub import main
    monkeypatch.setattr(notion_api, "load_token", lambda root=None: None)
    monkeypatch.setattr(main, "sync_routine", SyncRoutine(folder=tmp_path / "notion", claude_bin=str(tmp_path / "absent")))
    page = TestClient(main.app).get("/deploiements/client-demo/synthese").text
    assert "Ni jeton Notion ni Claude Code" in page  # bouton grisé, l'infobulle dit pourquoi
    r = TestClient(main.app).post("/actions/rafraichir")
    assert "CLAUDE_BIN" in r.text and "Simulation" not in r.text


def fake_claude(tmp_path: Path, body: str) -> Path:
    """Faux binaire `claude` : écrit ce qu'il reçoit (arguments, dossier, consigne, CONFIG) puis exécute body."""
    script = tmp_path / "claude"
    script.write_text(f"""#!{sys.executable}
import os, sys
data = sys.stdin.read()
open({str(tmp_path / 'recu.txt')!r}, 'w').write(repr((sys.argv[1:], os.getcwd(), data, os.environ.get('JARVIS_CONFIG_DIR'))))
{body}
""", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def wait(routine: SyncRoutine) -> dict:
    for _ in range(100):
        if not routine.running and "finished" in routine.state():
            return routine.state()
        time.sleep(0.05)
    raise AssertionError("la routine ne s'est pas terminée")


def test_live_routine_runs_claude_in_the_app_folder_with_the_app_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CONFIG_DIR", str(tmp_path / "ma-config"))  # dossier de clients de l'app : transmis tel quel
    claude = fake_claude(tmp_path, "print('client-demo : inchangé')")
    routine = SyncRoutine(folder=tmp_path / "notion", claude_bin=str(claude))
    r = routine.start()
    assert r.ok and not r.simulated
    state = wait(routine)
    assert state["ok"] and "inchangé" in state["summary"]
    args, cwd, prompt, config_dir = eval((tmp_path / "recu.txt").read_text())
    assert Path(cwd) == PROJECT_ROOT and prompt == routine_text() and config_dir == str(tmp_path / "ma-config")
    tools = args[args.index("--allowedTools") + 1:]
    assert "Bash(uv run python -m deploy_hub.sync_notion:*)" in tools and any(t.startswith("mcp__") for t in tools)
    assert any(t.startswith("Edit(//") and t.endswith("/**)") for t in tools)  # écriture limitée au dossier de données


def test_live_routine_failure_is_reported_and_notified(tmp_path):
    class Notes:
        added = []
        def add(self, *args):
            self.added.append(args)
    notes = Notes()
    claude = fake_claude(tmp_path, "print('ÉCHEC tous : preparer : connecteur Notion absent'); sys.exit(0)")
    routine = SyncRoutine(notes, folder=tmp_path / "notion", claude_bin=str(claude))
    assert routine.start().ok
    state = wait(routine)
    assert not state["ok"] and "connecteur Notion absent" in state["summary"] and notes.added  # « ÉCHEC » dans le compte rendu


def test_one_routine_at_a_time(tmp_path):
    claude = fake_claude(tmp_path, "import time; time.sleep(1)")
    routine = SyncRoutine(folder=tmp_path / "notion", claude_bin=str(claude))
    assert routine.start().ok
    second = routine.start()
    assert not second.ok and "déjà en cours" in second.message
    wait(routine)


def test_live_routine_without_claude_cli_says_what_to_do(tmp_path):
    routine = SyncRoutine(folder=tmp_path / "notion", claude_bin=str(tmp_path / "absent"))
    r = routine.start()
    assert not r.ok and not r.simulated and "CLAUDE_BIN" in r.message


def test_status_reloads_the_page_only_when_a_live_sync_ends_well(monkeypatch, tmp_path):
    from deploy_hub import main
    routine = SyncRoutine(folder=tmp_path / "notion")
    monkeypatch.setattr(main, "sync_routine", routine)
    routine._save(running=False, finished="2026-11-17T16:00:00", ok=True, summary="client-demo : inchangé")
    r = TestClient(main.app).get("/actions/rafraichir/etat")
    assert "Synchro terminée" in r.text and 'data-reload-after="3"' in r.text and "HX-Refresh" not in r.headers  # message, puis rechargement
    routine._save(running=False, finished="2026-11-17T16:00:00", ok=False, summary="ÉCHEC tous : plan : délai")
    r = TestClient(main.app).get("/actions/rafraichir/etat")
    assert "Synchro en échec" in r.text and "data-reload-after" not in r.text  # l'échec reste affiché
    routine._save(running=True, started="2026-11-17T16:00:00")  # lancée avant un redémarrage : aucun processus ne la suit
    r = TestClient(main.app).get("/actions/rafraichir/etat")
    assert "Synchro interrompue" in r.text and "hx-trigger" not in r.text


def test_progress_follows_the_files_the_routine_drops(tmp_path):
    """Cercle d'avancement : empreinte, plan, requêtes déposées, fusion, lus dans data/notion depuis le lancement."""
    import json
    folder = tmp_path / "notion"
    routine = SyncRoutine(folder=folder)
    routine._save(running=True, started="2000-01-01T00:00:00")
    assert routine.progress() == {"pct": 5, "label": "Empreinte de tous les clients…"}
    (folder / "empreintes.json").write_text("{}")
    assert routine.progress()["label"] == "Plan des requêtes…"
    raw = folder / "acme"
    raw.mkdir()
    (raw / "plan.json").write_text(json.dumps({"sorties": ["actions", "recette", "client", "regles"]}))
    (raw / "actions.json").write_text("{}")
    assert routine.progress() == {"pct": 20 + 65 * 1 // 4, "label": "Requêtes : 1 / 4"}
    for name in ("recette", "client", "regles"):
        (raw / f"{name}.json").write_text("{}")
    assert routine.progress()["label"] == "Fusion…"
    (raw / "plan.json").unlink()
    (raw / "etat-synchro.json").write_text("{}")  # fusionné : le plan est effacé
    assert routine.progress() == {"pct": 92, "label": "Fusion : 1 client(s)…"}


def test_refresh_button_does_not_move_when_clicked():
    """Attente, avancement et résultat vont dans un encart posé sous le bouton (position absolue), jamais à côté de lui."""
    css = (PROJECT_ROOT / "src" / "deploy_hub" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".refresh .refresh-out { position: absolute; top: calc(100% + 6px)" in css


def test_a_failure_buried_in_a_long_report_is_still_a_failure(tmp_path):
    """Claude a écrit « ÉCHEC » en haut d'un long compte rendu (vu le 2026-11-17 sur la VM) : c'est un échec, pas un succès."""
    report = "\n".join(["La synchro a échoué pour acme.", "", "**ÉCHEC acme : plan : fiche Notion introuvable.**"] + [f"ligne {i}" for i in range(15)])
    claude = fake_claude(tmp_path, f"print({report!r})")
    routine = SyncRoutine(folder=tmp_path / "notion", claude_bin=str(claude))
    assert routine.start().ok
    state = wait(routine)
    assert not state["ok"] and "ÉCHEC acme : plan : fiche Notion introuvable." in state["summary"] and "**" not in state["summary"]


def test_plan_errors_are_reported_per_client_whatever_claude_writes(tmp_path):
    """Les erreurs par client du « plan » (fichier écrit par Python) suffisent à signaler l'échec, même si Claude ne dit rien."""
    import json
    folder = tmp_path / "notion"
    script = f"""import json, pathlib
pathlib.Path({str(folder / "plan-erreurs.json")!r}).write_text(json.dumps({{"apst2607": "fiche Notion introuvable : vérifie le lien"}}))
print("Tout s'est bien passé.")"""
    routine = SyncRoutine(folder=folder, claude_bin=str(fake_claude(tmp_path, script)))
    assert routine.start().ok
    state = wait(routine)
    assert not state["ok"] and state["summary"] == "apst2607 : fiche Notion introuvable : vérifie le lien"


def test_plan_writes_its_errors_for_jarvis(tmp_path):
    """« plan » note ses erreurs par client dans data/notion/plan-erreurs.json (ici : client absent de l'empreinte)."""
    import json
    folder = tmp_path / "config" / "clients"
    folder.mkdir(parents=True)
    (folder / "acme.yaml").write_text(f"client: Acme\nsoftware: Pulse\nstatus: en cours\npage_id: {'a' * 32}\n", encoding="utf-8")
    (tmp_path / "data" / "notion").mkdir(parents=True)
    (tmp_path / "data" / "notion" / "empreintes.json").write_text(json.dumps({"results": []}), encoding="utf-8")
    out = sync_notion.plan_all(tmp_path)
    written = json.loads((tmp_path / "data" / "notion" / "plan-erreurs.json").read_text(encoding="utf-8"))
    assert written == out["erreurs"] and "acme" in written


def test_the_refresh_button_fills_up_instead_of_a_box():
    """Le bouton porte l'avancement : deux libellés superposés (largeur fixe), plus d'encart d'attente à côté."""
    from deploy_hub import main as app_module
    client = TestClient(app_module.app)
    for path in ("/deploiements/client-demo/synthese", "/clients"):
        html = client.get(path).text
        assert 'class="secondary sync-btn"' in html and 'class="btn-idle"' in html and 'class="btn-busy"' in html
        assert "Jarvis interroge Notion" not in html  # plus d'encart d'attente
    page = client.get("/").text
    assert "is-syncing" in page and "✓ Synchro terminée" in page  # script du bouton dans base.html


def test_a_running_sync_is_picked_up_again_when_the_page_loads(monkeypatch, tmp_path):
    """Rechargement ou changement de page pendant une synchro : le bouton reprend son avancement au lieu de l'oublier."""
    from deploy_hub import main as app_module
    routine = SyncRoutine(folder=tmp_path / "notion")
    monkeypatch.setattr(app_module, "sync_routine", routine)
    client = TestClient(app_module.app)
    resume = 'hx-get="/actions/rafraichir/etat" hx-trigger="load"'
    assert resume not in client.get("/deploiements/client-demo/synthese").text  # rien en cours : rien à reprendre
    monkeypatch.setattr(SyncRoutine, "running", property(lambda self: True))
    for path in ("/deploiements/client-demo/synthese", "/clients"):
        assert resume in client.get(path).text

