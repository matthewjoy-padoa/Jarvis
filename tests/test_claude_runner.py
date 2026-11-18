"""Lanceur réel des agents, avec un faux `claude` et un dépôt git jetable (aucun appel réseau, aucun vrai Claude)."""

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from deploy_hub.sources.claude_runner import (ANSWERED, INSTRUCTION, RESUME_INSTRUCTION, WAITING, ClaudeRunner, agent_branch,
                                               answer_text, find_pr, questions_of, read_log, split_questions)
from deploy_hub.sources.notifications import NotificationStore

from conftest import instant_agents

FAKE_CLAUDE = r'''
import json, os, sys, time
brief = sys.stdin.read()
mode = os.environ.get("FAKE_MODE", "ok")
open("argv.json", "w").write(json.dumps({"argv": sys.argv[1:], "brief": brief, "cwd": os.getcwd(),
                                         "secret": os.environ.get("AIRFLOW_PASSWORD")}))
def say(e): print(json.dumps(e), flush=True)
say({"type": "system", "subtype": "init", "session_id": "sess-" + os.path.basename(os.getcwd())})
say({"type": "assistant", "message": {"content": [{"type": "text", "text": "Je lis le brief."},
     {"type": "tool_use", "name": "Bash", "input": {"command": "uv run pytest tests"}}]}})
say({"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "Permission refusée"}]}})
# contexte injecté par un hook au démarrage : ses liens de PR ne sont pas ceux de l'agent
say({"type": "system", "subtype": "hook_response", "output": "Backlog : https://github.com/padoa/integration-orchestrator/pull/3629"})
if mode == "slow":
    time.sleep(30)
if mode == "question":
    say({"type": "result", "subtype": "success", "num_turns": 2, "session_id": "sess-" + os.path.basename(os.getcwd()),
         "result": "Je n'ai rien modifié.\n\n**Questions :**\n1. Correctif au niveau client ou logiciel ?"})
    sys.exit(0)
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
    origin = tmp_path / "integration-orchestrator.git"
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
    return ClaudeRunner(repo, tmp_path / "agents", notifications=NotificationStore(tmp_path / "notifs.json"),
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


def test_missing_orchestrator_is_refused_with_what_to_do(tmp_path, fake_claude):
    """Plus de simulation : sans clone de l'orchestrator, Jarvis le dit au lieu de faire semblant."""
    result = _runner(tmp_path / "absent", fake_claude, tmp_path).launch_retour(D, R, prompt="x")
    assert not result.ok and not result.simulated and "ORCHESTRATOR_DIR" in result.message


def test_missing_claude_is_refused_with_what_to_do(repo, tmp_path):
    result = ClaudeRunner(repo, tmp_path / "agents", claude_bin=str(tmp_path / "pas-de-claude")).launch_retour(D, R, prompt="x")
    assert not result.ok and "CLAUDE_BIN" in result.message


def test_worktree_failure_ends_in_failure(tmp_path, fake_claude):
    broken = tmp_path / "pas-un-depot"
    _git(tmp_path, "init", "-q", str(broken))  # se dit clone de l'orchestrator, mais son origin n'existe pas : le worktree échoue
    _git(broken, "remote", "add", "origin", str(tmp_path / "integration-orchestrator-absent.git"))  # jamais le réseau
    runner = _runner(tmp_path / "pas-un-depot", fake_claude, tmp_path)
    runner.launch_retour(D, R, prompt="x")
    _wait(runner, 1)
    assert runner.get(1).status == "échec" and "Worktree impossible" in runner.get(1).summary


def test_history_survives_restart_and_running_agents_become_interrupted(tmp_path):
    first = instant_agents(ClaudeRunner(tmp_path, tmp_path / "agents"))
    first.launch_retour(D, R, prompt="fini aussitôt")
    first.runs[0].status = "en cours"  # comme si Jarvis s'était arrêté pendant le travail
    first._save()
    second = instant_agents(ClaudeRunner(tmp_path, tmp_path / "agents"))
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


def test_find_pr_ignores_links_injected_by_hooks():
    hook = json.dumps({"type": "system", "subtype": "hook_response", "output": "voir https://github.com/a/b/pull/9"})
    shell = json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "https://github.com/a/b/pull/12"}]}})
    assert find_pr(hook) == ""
    assert find_pr(f"{shell}\n{hook}") == "https://github.com/a/b/pull/12"


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


def test_a_claude_folder_owned_by_root_is_refused_with_the_chown_to_type(repo, fake_claude, tmp_path):
    """Vu sur la VM : <orchestrator>/.claude appartenait à root, les worktrees des agents ne pouvaient pas s'y créer."""
    import os
    claude_dir = repo / ".claude"
    claude_dir.mkdir(exist_ok=True)
    claude_dir.chmod(0o555)  # non inscriptible, comme un dossier à root
    try:
        if os.access(claude_dir, os.W_OK):  # root : tout est inscriptible, le cas ne se reproduit pas
            pytest.skip("lancé en root")
        result = _runner(repo, fake_claude, tmp_path).launch_retour(D, R, prompt="x")
        assert not result.ok and "sudo chown -R" in result.message and str(claude_dir) in result.message
    finally:
        claude_dir.chmod(0o755)


def test_jarvis_itself_is_never_taken_for_the_orchestrator(tmp_path, fake_claude):
    """Vu sur la VM : ORCHESTRATOR_DIR vide valait « . », le dossier de Jarvis ; l'agent a travaillé dans un clone de Jarvis."""
    from deploy_hub.config import PROJECT_ROOT
    result = _runner(PROJECT_ROOT, fake_claude, tmp_path).launch_retour(D, R, prompt="x")
    assert not result.ok and "n'est pas un clone de integration-orchestrator" in result.message


def test_an_empty_orchestrator_setting_falls_back_to_the_default(monkeypatch):
    from deploy_hub.config import load_settings
    monkeypatch.setenv("ORCHESTRATOR_DIR", "")
    assert str(load_settings().orchestrator_dir).endswith("integration-orchestrator")


def test_log_events_whose_message_is_text_do_not_break_the_agent_page(tmp_path):
    """Claude Code 2.1.326 écrit des événements où « message » est un texte : la page de l'agent plantait (erreur 500)."""
    from datetime import datetime
    from deploy_hub.sources import review
    from deploy_hub.sources.claude_runner import AgentRun, message_of
    log = tmp_path / "1.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in (
        {"type": "system", "message": "texte simple"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
    )), encoding="utf-8")
    run = AgentRun(1, "acme", "Acme", "retour R-1", "fix-acme-r-1", "brief", datetime(2026, 11, 18, 9), "terminé")
    assert message_of({"message": "texte"}) == {} and message_of({"message": {"a": 1}}) == {"a": 1}
    review.build(run, log)  # ne lève plus


def test_questions_are_recognised_only_when_the_report_asks_them():
    assert questions_of("Rien modifié.\n\nQuestions :\n1. Client ou logiciel")
    assert questions_of("Rien modifié. Faut-il corriger au niveau client ou logiciel ?")
    assert not questions_of("Cause : mapping.py:12. Tests verts.") and not questions_of("")


def test_an_agent_that_stops_on_questions_waits_for_an_answer(repo, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "question")
    runner = _runner(repo, fake_claude, tmp_path)
    runner.launch_retour(D, R, prompt="Brief relu")
    _wait(runner, 1)
    run = runner.get(1)
    assert run.status == WAITING and run.group == "todo" and not run.pr_url  # pas un « terminé » vert
    assert run.session_id == "sess-fix-acme-r-7"
    notif = runner.notifications.all()[0]
    assert WAITING in notif.title and notif.level == "warning" and "client ou logiciel" in notif.detail


def test_the_answer_resumes_the_same_session_in_the_same_worktree(repo, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "question")
    runner = _runner(repo, fake_claude, tmp_path)
    runner.launch_retour(D, R, prompt="Brief relu")
    _wait(runner, 1)
    monkeypatch.setenv("FAKE_MODE", "ok")
    result = runner.reply(1, "  Au niveau client.  ")
    assert result.ok and "Agent #2 lancé" in result.message
    _wait(runner, 2)
    first, second = runner.get(1), runner.get(2)
    assert first.status == ANSWERED and runner.follow_up(1) is second
    assert second.reply_to == 1 and second.resume == "sess-fix-acme-r-7" and second.branch == first.branch
    assert second.status == "terminé" and second.pr_url.endswith("/pull/4242") and second.worktree == first.worktree
    seen = json.loads((Path(second.worktree) / "argv.json").read_text())
    assert seen["argv"][:2] == ["-p", RESUME_INSTRUCTION] and seen["argv"][seen["argv"].index("--resume") + 1] == "sess-fix-acme-r-7"
    assert seen["brief"] == "Au niveau client."  # la session a déjà le brief : seule la réponse part


def test_without_the_same_worktree_the_answer_restarts_from_the_brief(repo, fake_claude, tmp_path, monkeypatch):
    """Vu sur la VM : l'agent n°1 a tourné ailleurs (ORCHESTRATOR_DIR vide) ; sa session n'est pas reprenable depuis le bon dépôt."""
    monkeypatch.setenv("FAKE_MODE", "question")
    runner = _runner(repo, fake_claude, tmp_path)
    runner.launch_retour(D, R, prompt="Brief relu")
    _wait(runner, 1)
    _git(repo, "worktree", "remove", "--force", runner.get(1).worktree)  # worktree disparu : la session n'est plus reprenable
    monkeypatch.setenv("FAKE_MODE", "ok")
    assert runner.reply(1, "Au niveau client.").ok
    _wait(runner, 2)
    second = runner.get(2)
    seen = json.loads((Path(second.worktree) / "argv.json").read_text())
    assert second.resume == "" and "--resume" not in seen["argv"] and seen["argv"][:2] == ["-p", INSTRUCTION]
    assert seen["brief"].startswith("Brief relu") and "client ou logiciel ?" in seen["brief"] and "<reponse>\nAu niveau client." in seen["brief"]


def test_a_reply_needs_a_waiting_agent_and_an_answer(repo, fake_claude, tmp_path, monkeypatch):
    runner = _runner(repo, fake_claude, tmp_path)
    runner.launch_retour(D, R, prompt="x")
    _wait(runner, 1)
    assert not runner.reply(1, "oui").ok and not runner.reply(99, "oui").ok  # terminé avec PR, puis inconnu
    monkeypatch.setenv("FAKE_MODE", "question")
    runner.launch_retour(D, R, prompt="x")
    _wait(runner, 2)
    refused = runner.reply(2, "   ")
    assert not refused.ok and "Écris ta réponse" in refused.message and runner.get(2).status == WAITING


def test_agents_finished_on_questions_before_this_state_are_reclassified(tmp_path):
    state = tmp_path / "agents"
    state.mkdir()
    base = {"slug": "acme", "client": "Acme", "subject": "retour R-1", "branch": "fix-acme-r-1", "prompt": "x",
            "started_at": "2026-11-18T09:10:00", "ended_at": "2026-11-18T09:11:00", "status": "terminé"}
    (state / "agents.json").write_text(json.dumps([
        {**base, "id": 2, "summary": "Cause : a.py:1", "pr_url": "https://github.com/padoa/integration-orchestrator/pull/1"},
        {**base, "id": 1, "summary": "Rien modifié. Client ou logiciel ?"}]))
    runner = ClaudeRunner(tmp_path, state)
    assert runner.get(1).status == WAITING and runner.get(2).status == "terminé"


def test_when_the_resumed_agent_fails_the_answer_can_be_sent_again_from_the_brief(repo, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "question")
    runner = _runner(repo, fake_claude, tmp_path)
    runner.launch_retour(D, R, prompt="Brief relu")
    _wait(runner, 1)
    monkeypatch.setenv("FAKE_MODE", "fail")  # ex. session introuvable : --resume échoue
    assert runner.reply(1, "Au niveau client.").ok
    _wait(runner, 2)
    assert runner.get(2).status == "échec" and runner.get(1).status == WAITING
    monkeypatch.setenv("FAKE_MODE", "ok")
    assert runner.reply(1, "Au niveau client.").ok
    _wait(runner, 3)
    third = runner.get(3)
    assert third.resume == "" and runner.follow_up(1) is third and third.status == "terminé" and runner.get(1).status == ANSWERED


def test_any_agent_ended_in_error_can_be_relaunched_with_the_same_brief(repo, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "fail")
    runner = _runner(repo, fake_claude, tmp_path)
    runner.launch_retour(D, R, prompt="Brief relu")
    _wait(runner, 1)
    monkeypatch.setenv("FAKE_MODE", "ok")
    result = runner.relaunch(1)
    assert result.ok and "Agent #2 lancé" in result.message
    _wait(runner, 2)
    first, second = runner.get(1), runner.get(2)
    assert runner.relaunched(1) is second and second.relaunch_of == 1 and second.branch == first.branch
    assert second.worktree == first.worktree and second.status == "terminé" and second.resume == ""
    seen = json.loads((Path(second.worktree) / "argv.json").read_text())
    assert seen["brief"] == "Brief relu" and "--resume" not in seen["argv"]
    assert not runner.relaunch(2).ok and not runner.relaunch(99).ok  # terminé, puis inconnu


@pytest.mark.parametrize("status", ["arrêté", "délai dépassé", "interrompu"])
def test_every_error_status_is_relaunchable(tmp_path, status):
    runner = ClaudeRunner(tmp_path, tmp_path / "agents")
    runner.problem = lambda: None
    runner._start = lambda run: None
    runner.runs.append(__import__("deploy_hub.sources.claude_runner", fromlist=["AgentRun"]).AgentRun(
        1, "acme", "Acme", "retour R-1", "fix-acme-r-1", "Brief", datetime(2026, 11, 18, 9), status))
    assert runner.relaunch(1).ok and runner.get(2).prompt == "Brief"


def test_each_question_is_listed_with_its_details():
    summary = ("Je n'ai rien modifié : choix métier.\n\n**Questions :**\n"
               "1. **Correctif au niveau client ou logiciel ?**\n   - client : seul ce client\n   - logiciel : tous les clients\n"
               "2) Faut-il un test de non-régression ?\n")
    assert questions_of(summary) == ["Correctif au niveau client ou logiciel ?\n- client : seul ce client\n- logiciel : tous les clients",
                                     "Faut-il un test de non-régression ?"]
    assert questions_of("Questions :\n- Quelle date ?\n- Quel client ?") == ["Quelle date ?", "Quel client ?"]


def test_without_a_questions_line_each_line_ending_with_a_question_mark_is_one():
    """Agents lancés avant la consigne « Questions : » (ex. l'agent n°1 de la VM)."""
    assert questions_of("Rien modifié.\n- Faut-il corriger côté client ?\nAutre point.\nEt la date ?") == [
        "Faut-il corriger côté client ?", "Et la date ?"]
    assert questions_of("Cause : a.py:12. Tests verts.") == []


def test_the_answer_pairs_each_question_with_its_answer():
    text = answer_text(["Client ou logiciel ?", "Un test ?"], ["Client", ""], "Merci")
    assert text == "Question 1 : Client ou logiciel ?\nRéponse : Client\n\nQuestion 2 : Un test ?\nRéponse : (pas de réponse)\n\nPrécision : Merci"
    assert answer_text(["Q ?"], ["  "]) == ""  # rien de rempli : la relance est refusée
    assert answer_text(["Q ?"], [""], " Merci ") == "Précision : Merci"


def test_the_context_written_before_the_questions_is_kept_apart():
    summary = ("J'ai lu `mapping.py`.\nL'écart vient du code NAF.\n\nQuestions :\n"
               "1. Client ou logiciel ?\n   - **Contexte** : mapping.py:12\n   - **Options** :\n     - client : seul ce client\n"
               "     - logiciel : tous\n   - **Recommandation** : client\n2. Un test ?")
    intro, questions = split_questions(summary)
    assert intro == "J'ai lu `mapping.py`.\nL'écart vient du code NAF."
    assert questions[0] == ("Client ou logiciel ?\n- **Contexte** : mapping.py:12\n- **Options** :\n  - client : seul ce client\n"
                            "  - logiciel : tous\n- **Recommandation** : client")  # sous-liste gardée
    assert questions[1] == "Un test ?"
    assert split_questions("Rien modifié.\nCôté client ?\nFin.") == ("Rien modifié.\nFin.", ["Côté client ?"])
