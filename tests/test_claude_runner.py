"""Lanceur réel des agents, avec un faux `claude` et un dépôt git jetable (aucun appel réseau, aucun vrai Claude)."""

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from deploy_hub.sources.claude_runner import INSTRUCTION, ClaudeRunner, agent_branch, find_pr, read_log
from deploy_hub.sources.notifications import NotificationStore

FAKE_CLAUDE = r'''
import json, os, sys, time
brief = sys.stdin.read()
mode = os.environ.get("FAKE_MODE", "ok")
open("argv.json", "w").write(json.dumps({"argv": sys.argv[1:], "brief": brief, "cwd": os.getcwd(),
                                         "secret": os.environ.get("AIRFLOW_PASSWORD")}))
def say(e): print(json.dumps(e), flush=True)
say({"type": "system", "subtype": "init"})
say({"type": "assistant", "message": {"content": [{"type": "text", "text": "Je lis le brief."},
     {"type": "tool_use", "name": "Bash", "input": {"command": "uv run pytest tests"}}]}})
say({"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "Permission refusée"}]}})
if mode == "slow":
    time.sleep(30)
if mode == "fail":
    say({"type": "result", "subtype": "error_during_execution", "result": "", "num_turns": 2})
    sys.exit(1)
say({"type": "result", "subtype": "success", "num_turns": 3, "total_cost_usd": 0.42,
     "result": "Cause : mapping.py:12. PR : https://github.com/padoa/integration-orchestrator/pull/4242"})
'''


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Clone de travail d'un « orchestrator » avec son origin, sur une autre branche avec une modif non commitée."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "orchestrator"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "t@t"), _git(work, "config", "user.name", "t")
    (work / "a.txt").write_text("v1\n")
    _git(work, "add", "."), _git(work, "commit", "-qm", "init"), _git(work, "push", "-q", "origin", "HEAD:main")
    _git(work, "checkout", "-qb", "travail-en-cours")
    (work / "a.txt").write_text("modif locale\n")
    return work


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE)
    launcher = tmp_path / "claude"
    launcher.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    launcher.chmod(0o755)
    return launcher


class D:  # fiche minimale
    slug, client, software = "acme", "Acme", "Preventiel"


class R:
    id, software, status, data_type, type_retour, resource, due, notion_url, content = "R-7", "", "Ouvert", "", "", "", None, "", []


def _runner(repo: Path, claude: Path, tmp_path: Path, **kw) -> ClaudeRunner:
    return ClaudeRunner(repo, tmp_path / "agents", live=True, notifications=NotificationStore(tmp_path / "notifs.json"),
                        claude_bin=str(claude), add_dirs=(tmp_path / "vault",), **kw)


def _wait(runner: ClaudeRunner, run_id: int, timeout: float = 20) -> None:
    end = time.time() + timeout
    while runner.get(run_id).running and time.time() < end:
        time.sleep(0.05)
    assert not runner.get(run_id).running, "l'agent ne s'est pas terminé"


def test_real_agent_runs_in_its_own_worktree_and_finds_the_pr(repo, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("AIRFLOW_PASSWORD", "ne-doit-pas-fuiter")
    runner = _runner(repo, fake_claude, tmp_path)
    result = runner.launch_retour(D, R, prompt="Brief relu")
    assert result.ok and not result.simulated and "Agent #1 lancé" in result.message
    _wait(runner, 1)
    run = runner.get(1)
    assert run.status == "terminé" and run.pr_url.endswith("/pull/4242") and run.cost_usd == 0.42
    assert "mapping.py:12" in run.summary

    worktree = Path(run.worktree)
    assert worktree == repo / ".claude" / "worktrees" / "jarvis" / "fix-acme-r-7"
    seen = json.loads((worktree / "argv.json").read_text())
    assert seen["brief"] == "Brief relu" and seen["argv"][:2] == ["-p", INSTRUCTION] and seen["secret"] is None
    assert seen["argv"][seen["argv"].index("--add-dir") + 1] == str(tmp_path / "vault")
    assert seen["argv"].index("--allowedTools") > seen["argv"].index("--add-dir")  # options variadiques en dernier
    assert _git(worktree, "branch", "--show-current").strip() == "fix-acme-r-7"
    assert (worktree / "a.txt").read_text() == "v1\n"  # depuis origin/main
    # le checkout de travail n'a pas bougé
    assert _git(repo, "branch", "--show-current").strip() == "travail-en-cours"
    assert (repo / "a.txt").read_text() == "modif locale\n"

    kinds = [k for k, _ in read_log(runner.log_path(run))]
    assert kinds == ["texte", "outil", "refus", "fin"]
    notif = runner.notifications.all()[0]
    assert "terminé" in notif.title and notif.link == "/agents/1"


def test_failed_agent_is_reported(repo, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "fail")
    runner = _runner(repo, fake_claude, tmp_path)
    runner.launch_retour(D, R, prompt="x")
    _wait(runner, 1)
    run = runner.get(1)
    assert run.status == "échec" and "Code de sortie 1" in run.summary and not run.pr_url


def test_agent_can_be_stopped_and_relaunch_reuses_worktree(repo, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "slow")
    runner = _runner(repo, fake_claude, tmp_path)
    runner.launch_retour(D, R, prompt="x")
    end = time.time() + 10
    while not runner._procs.get(1) and time.time() < end:
        time.sleep(0.05)
    again = runner.launch_retour(D, R, prompt="y")
    assert not again.ok and "déjà sur la branche" in again.message
    assert runner.stop(1)
    _wait(runner, 1)
    assert runner.get(1).status == "arrêté"

    monkeypatch.setenv("FAKE_MODE", "ok")
    runner.launch_retour(D, R, prompt="z")
    _wait(runner, 2)
    assert runner.get(2).status == "terminé" and runner.get(2).worktree == runner.get(1).worktree


def test_worktree_failure_ends_in_failure(tmp_path, fake_claude):
    runner = _runner(tmp_path / "pas-un-depot", fake_claude, tmp_path)
    runner.launch_retour(D, R, prompt="x")
    _wait(runner, 1)
    assert runner.get(1).status == "échec" and "Worktree impossible" in runner.get(1).summary


def test_history_survives_restart_and_running_agents_become_interrupted(tmp_path):
    first = ClaudeRunner(tmp_path, tmp_path / "agents")
    first.launch_retour(D, R, prompt="simulé")
    first.runs[0].status = "en cours"  # comme si Jarvis s'était arrêté pendant le travail
    first._save()
    second = ClaudeRunner(tmp_path, tmp_path / "agents")
    assert [r.id for r in second.runs] == [1] and second.runs[0].status == "interrompu"
    assert isinstance(second.runs[0].started_at, datetime)
    second.launch_retour(D, R, prompt="encore")
    assert second.runs[0].id == 2


def test_max_running_agents(repo, fake_claude, tmp_path):
    runner = _runner(repo, fake_claude, tmp_path, max_running=0)
    result = runner.launch_retour(D, R, prompt="x")
    assert not result.ok and "Déjà 0 agent(s)" in result.message and runner.runs == []


def test_find_pr_keeps_the_last_link():
    assert find_pr("voir https://github.com/a/b/pull/1 puis https://github.com/a/b/pull/22.") == "https://github.com/a/b/pull/22"
    assert find_pr("pas de PR") == ""


def test_bad_branch_corrupt_history_and_orphan_dir(repo, fake_claude, tmp_path):
    runner = _runner(repo, fake_claude, tmp_path)
    assert agent_branch("fix", "acme", "../../Étape 2") == "fix-acme-etape-2"  # jamais de chemin ni de caractère hors ref git

    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agents.json").write_text("{tronqué")
    assert ClaudeRunner(repo, tmp_path / "agents").runs == []
    assert list((tmp_path / "agents").glob("agents.illisible-*"))

    (runner.worktrees_dir / "fix-acme-r-7").mkdir(parents=True)
    runner.launch_retour(D, R, prompt="x")
    _wait(runner, 1)
    assert runner.get(1).status == "échec" and "pas un worktree git" in runner.get(1).summary
