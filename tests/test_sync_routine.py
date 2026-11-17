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


def test_without_token_the_refresh_button_runs_the_routine_in_demo(monkeypatch):
    from deploy_hub import main
    monkeypatch.setattr(notion_api, "load_token", lambda root=None: None)
    for path in ("/actions/rafraichir", "/actions/rafraichir/client-demo"):
        r = TestClient(main.app).post(path)
        assert r.status_code == 200 and "Simulation : la routine de synchro Notion" in r.text and "HX-Refresh" not in r.headers
    page = TestClient(main.app).get("/deploiements/client-demo/synthese").text
    assert 'hx-post="/actions/rafraichir/client-demo"' in page and "Ni jeton Notion ni Claude Code" not in page  # bouton actif


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
    routine = SyncRoutine(True, folder=tmp_path / "notion", claude_bin=str(claude))
    r = routine.start()
    assert r.ok and not r.simulated
    state = wait(routine)
    assert state["ok"] and "inchangé" in state["summary"]
    args, cwd, prompt, config_dir = eval((tmp_path / "recu.txt").read_text())
    assert Path(cwd) == PROJECT_ROOT and prompt == routine_text() and config_dir == str(tmp_path / "ma-config")
    tools = args[args.index("--allowedTools") + 1:]
    assert "Bash(uv run python -m deploy_hub.sync_notion:*)" in tools and any(t.startswith("mcp__") for t in tools)
    assert any(t.startswith("Write(//") and t.endswith("/**)") for t in tools)  # écriture limitée au dossier de données


def test_live_routine_failure_is_reported_and_notified(tmp_path):
    class Notes:
        added = []
        def add(self, *args):
            self.added.append(args)
    notes = Notes()
    claude = fake_claude(tmp_path, "print('ÉCHEC tous : preparer : connecteur Notion absent'); sys.exit(0)")
    routine = SyncRoutine(True, notes, folder=tmp_path / "notion", claude_bin=str(claude))
    assert routine.start().ok
    state = wait(routine)
    assert not state["ok"] and "connecteur Notion absent" in state["summary"] and notes.added  # « ÉCHEC » dans le compte rendu


def test_one_routine_at_a_time(tmp_path):
    claude = fake_claude(tmp_path, "import time; time.sleep(1)")
    routine = SyncRoutine(True, folder=tmp_path / "notion", claude_bin=str(claude))
    assert routine.start().ok
    second = routine.start()
    assert not second.ok and "déjà en cours" in second.message
    wait(routine)


def test_live_routine_without_claude_cli_says_what_to_do(tmp_path):
    routine = SyncRoutine(True, folder=tmp_path / "notion", claude_bin=str(tmp_path / "absent"))
    r = routine.start()
    assert not r.ok and not r.simulated and "CLAUDE_BIN" in r.message


def test_status_reloads_the_page_only_when_a_live_sync_ends_well(monkeypatch, tmp_path):
    from deploy_hub import main
    routine = SyncRoutine(True, folder=tmp_path / "notion")
    monkeypatch.setattr(main, "sync_routine", routine)
    routine._save(running=False, finished="2026-11-17T16:00:00", ok=True, summary="client-demo : inchangé")
    r = TestClient(main.app).get("/actions/rafraichir/etat")
    assert "Synchro terminée" in r.text and r.headers.get("HX-Refresh") == "true"
    routine._save(running=False, finished="2026-11-17T16:00:00", ok=False, summary="ÉCHEC tous : plan : délai")
    r = TestClient(main.app).get("/actions/rafraichir/etat")
    assert "Synchro en échec" in r.text and "HX-Refresh" not in r.headers
    routine._save(running=True, started="2026-11-17T16:00:00")  # lancée avant un redémarrage : aucun processus ne la suit
    r = TestClient(main.app).get("/actions/rafraichir/etat")
    assert "Synchro interrompue" in r.text and "hx-trigger" not in r.text

