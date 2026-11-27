"""Lanceur réel des agents, avec un faux `claude` et un dépôt git jetable (aucun appel réseau, aucun vrai Claude)."""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from deploy_hub.sources.claude_runner import (INSTRUCTION, RESUME_INSTRUCTION, WAITING, AgentRun, ClaudeRunner, agent_branch,
                                               agent_questions, answer_text, chosen_answer, find_pr, final_status, read_log,
                                               report_of, split_questions)
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
    id, software, status, data_type, type_retour, resource, due, notion_url, content, ligne = "R-7", "", "Ouvert", "", "", "", None, "", [], ""


def _runner(repo: Path, claude: Path, tmp_path: Path, **kw) -> ClaudeRunner:
    return ClaudeRunner(repo, tmp_path / "agents", notifications=NotificationStore(tmp_path / "notifs.json"),
                        claude_bin=str(claude), add_dirs=(tmp_path / "vault",), **kw)


def _wait(runner: ClaudeRunner, run_id: int, timeout: float = 20) -> None:
    end = time.time() + timeout
    # Fin complète : statut final ET notification envoyée (finished() enregistre l'état avant de notifier).
    def done() -> bool:
        return not runner.get(run_id).running and (
            not runner.notifications or any(n.key.startswith(f"agent:{run_id}:") for n in runner.notifications.all()))
    while not done() and time.time() < end:
        time.sleep(0.05)
    assert done(), "l'agent ne s'est pas terminé"


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


def test_orchestrator_clones_are_found_under_the_home_folder_most_recently_fetched_first(tmp_path):
    """Les dépôts git dont l'origine est integration-orchestrator, le plus récemment mis à jour d'abord. Ni un autre dépôt,
    ni un worktree, ni un clone trop profond, ni node_modules ; un dossier personnel lui-même versionné ne bloque pas."""
    from deploy_hub.config import orchestrator_clones

    def make_clone(path, origin, fetched=None):
        path.mkdir(parents=True)
        _git(path, "init", "-q")
        _git(path, "remote", "add", "origin", origin)
        if fetched:
            (path / ".git" / "FETCH_HEAD").write_text("")
            os.utime(path / ".git" / "FETCH_HEAD", (fetched, fetched))
        return path
    url = "git@github.com:exemple/integration-orchestrator.git"
    old = make_clone(tmp_path / "integration-orchestrator", url, fetched=1_000)
    recent = make_clone(tmp_path / "Dev" / "integration-orchestrator", url, fetched=2_000)
    make_clone(tmp_path / "Dev" / "autre", "git@github.com:exemple/autre.git")
    make_clone(tmp_path / "a" / "b" / "c" / "d" / "integration-orchestrator", url)  # trop profond
    make_clone(tmp_path / "node_modules" / "integration-orchestrator", url)
    (tmp_path / "Dev" / "worktree").mkdir()
    (tmp_path / "Dev" / "worktree" / ".git").write_text("gitdir: ailleurs")  # worktree : .git est un fichier
    assert orchestrator_clones(tmp_path) == [recent, old]
    _git(tmp_path, "init", "-q")  # dossier personnel lui-même versionné (dotfiles) : on cherche quand même
    assert orchestrator_clones(tmp_path) == [recent, old]


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
    assert split_questions("Rien modifié.\n\nQuestions :\n1. Client ou logiciel")[1]
    assert split_questions("Rien modifié. Faut-il corriger au niveau client ou logiciel ?")[1]
    assert not split_questions("Cause : mapping.py:12. Tests verts.")[1] and not split_questions("")[1]


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
    worktree = runner.get(1).worktree
    result = runner.reply(1, "  Au niveau client.  ")
    assert result.ok and "Agent #1 reprend avec ta réponse" in result.message and result.run_id == "1"
    _wait(runner, 1)
    run = runner.get(1)
    assert len(runner.runs) == 1 and runner.follow_up(1) is None  # pas de nouvel agent : la même fiche a repris
    assert run.resume == "sess-fix-acme-r-7" and run.status == "terminé" and run.pr_url.endswith("/pull/4242") and run.worktree == worktree
    assert [p["status"] for p in run.passages] == [WAITING] and run.passages[0]["answer"] == "Au niveau client."
    assert ("réponse", "Au niveau client.") in runner.log(run)  # repère de reprise dans le journal, qui garde le premier passage
    assert run.duration >= run.passages[0]["ended_at"] - run.passages[0]["started_at"]
    assert run.num_turns == 5 and run.cost_usd == 0.42 and run.passages[0]["num_turns"] == 2  # totaux de la fiche, détail par passage
    reloaded = ClaudeRunner(repo, tmp_path / "agents").get(1)  # relu après redémarrage : passages et dates intacts
    assert reloaded.passages[0]["started_at"] == run.passages[0]["started_at"] and reloaded.duration == run.duration
    seen = json.loads((Path(run.worktree) / "argv.json").read_text())
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
    _wait(runner, 1)
    second = runner.get(1)
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
    _wait(runner, 1)
    run = runner.get(1)
    assert run.status == "échec" and not runner.reply(1, "encore").ok  # en erreur : on relance, on ne répond plus
    monkeypatch.setenv("FAKE_MODE", "ok")
    assert runner.relaunch(1).ok
    _wait(runner, 1)
    seen = json.loads((Path(run.worktree) / "argv.json").read_text())
    assert run.status == "terminé" and run.resume == "" and len(runner.runs) == 1
    assert [p["status"] for p in run.passages] == [WAITING, "échec"]
    assert seen["brief"].startswith("Brief relu") and "<reponse>\nAu niveau client." in seen["brief"]  # la réponse repart avec le brief


def test_any_agent_ended_in_error_can_be_relaunched_with_the_same_brief(repo, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "fail")
    runner = _runner(repo, fake_claude, tmp_path)
    runner.launch_retour(D, R, prompt="Brief relu")
    _wait(runner, 1)
    monkeypatch.setenv("FAKE_MODE", "ok")
    worktree = runner.get(1).worktree
    result = runner.relaunch(1)
    assert result.ok and "Agent #1 relancé" in result.message
    _wait(runner, 1)
    run = runner.get(1)
    assert len(runner.runs) == 1 and run.worktree == worktree and run.status == "terminé" and run.resume == ""
    seen = json.loads((Path(run.worktree) / "argv.json").read_text())
    assert seen["brief"] == "Brief relu" and "--resume" not in seen["argv"]
    assert not runner.relaunch(1).ok and not runner.relaunch(99).ok  # terminé, puis inconnu


@pytest.mark.parametrize("status", ["arrêté", "délai dépassé", "interrompu"])
def test_every_error_status_is_relaunchable(tmp_path, status):
    runner = ClaudeRunner(tmp_path, tmp_path / "agents")
    runner.problem = lambda: None
    runner._start = lambda run: None
    runner.runs.append(__import__("deploy_hub.sources.claude_runner", fromlist=["AgentRun"]).AgentRun(
        1, "acme", "Acme", "retour R-1", "fix-acme-r-1", "Brief", datetime(2026, 11, 18, 9), status))
    assert runner.relaunch(1).ok and len(runner.runs) == 1 and runner.get(1).prompt == "Brief" and runner.get(1).passages[0]["status"] == status


def test_each_question_is_listed_with_its_details():
    summary = ("Je n'ai rien modifié : choix métier.\n\n**Questions :**\n"
               "1. **Correctif au niveau client ou logiciel ?**\n   - client : seul ce client\n   - logiciel : tous les clients\n"
               "2) Faut-il un test de non-régression ?\n")
    assert split_questions(summary)[1] == ["Correctif au niveau client ou logiciel ?\n- client : seul ce client\n- logiciel : tous les clients",
                                     "Faut-il un test de non-régression ?"]
    assert split_questions("Questions :\n- Quelle date ?\n- Quel client ?")[1] == ["Quelle date ?", "Quel client ?"]


def test_without_a_questions_line_each_line_ending_with_a_question_mark_is_one():
    """Agents lancés avant la consigne « Questions : » (ex. l'agent n°1 de la VM)."""
    assert split_questions("Rien modifié.\n- Faut-il corriger côté client ?\nAutre point.\nEt la date ?")[1] == [
        "Faut-il corriger côté client ?", "Et la date ?"]
    assert split_questions("Cause : a.py:12. Tests verts.")[1] == []


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


def _block(data) -> str:
    return f"J'ai lu `a.py`.\n\n<questions>\n{json.dumps(data, ensure_ascii=False)}\n</questions>"


def test_questions_given_as_json_come_with_their_options_and_recommendation():
    data = {"questions": [
        {"question": "Client ou logiciel ?", "contexte": "a.py:12", "options": [{"libelle": "client", "consequence": "un client"},
         {"libelle": "logiciel", "consequence": "tous"}, {"consequence": "sans libellé : ignorée"}], "recommandation": "logiciel",
         "pourquoi": "plus simple"},
        {"question": "Un test ?", "options": [{"libelle": "oui"}, {"libelle": "non"}], "recommandation": 1},
        {"contexte": "sans question : ignorée"}]}
    intro, questions = agent_questions(_block(data))
    assert intro == "J'ai lu `a.py`." and len(questions) == 2
    first, second = questions
    assert first.options == [("client", "un client"), ("logiciel", "tous")] and first.recommended == 2 and first.why == "plus simple"
    assert str(first) == "Client ou logiciel ?\na.py:12" and second.recommended == 1 and second.options[0] == ("oui", "")
    assert report_of(_block(data)) == "J'ai lu `a.py`."
    fenced = _block(data).replace("<questions>\n", "<questions>\n```json\n").replace("\n</questions>", "\n```\n</questions>")
    assert len(agent_questions(fenced)[1]) == 2  # bloc entouré de ``` : lu quand même


def test_an_invalid_or_missing_json_block_falls_back_to_the_text():
    broken = "Rien modifié.\nQuestions :\n1. Côté client ?\n<questions>{pas du json</questions>"
    intro, questions = agent_questions(broken)
    assert intro == "Rien modifié." and [q.text for q in questions] == ["Côté client ?"] and not questions[0].options
    assert agent_questions(_block({"questions": []}))[1] == []  # bloc vide et aucune ligne en « ? » : pas de question
    assert agent_questions("Cause : a.py:1. Tests verts.") == ("Cause : a.py:1. Tests verts.", [])


def test_the_chosen_option_and_the_free_text_make_the_answer():
    from deploy_hub.sources.claude_runner import Question
    q = Question("Client ou logiciel ?", options=[("client", ""), ("logiciel", "")])
    assert chosen_answer(q, "2", "") == "option « logiciel »"
    assert chosen_answer(q, "1", " et ajoute un test ") == "option « client » · et ajoute un test"
    assert chosen_answer(q, "0", "autre chose") == "autre chose" and chosen_answer(q, "9", "") == chosen_answer(q, "x", "") == ""


def test_an_agent_that_only_gives_json_questions_waits_for_an_answer():
    from deploy_hub.sources.claude_runner import AgentRun
    run = AgentRun(1, "acme", "Acme", "retour R-1", "fix-acme-r-1", "x", datetime(2026, 11, 18, 9), "en cours",
                   summary=_block({"questions": [{"question": "Quelle date de bascule"}]}))  # sans « ? » : seul le JSON la signale
    assert final_status(run) == "question en attente" and run.report == "J'ai lu `a.py`."


def test_a_question_asked_as_a_heading_shows_without_its_markdown_and_takes_its_section():
    from deploy_hub.sources.claude_runner import agent_questions
    report = ("J'ai lu le ticket.\n\n## 1. Ce que j'ai vérifié\nLe transform lit la colonne.\n\n"
              "## 2. Blocage n°1 : quelle source ?\nDeux colonnes :\n- `a`\n- `b`\n\nLaquelle est la bonne ?\n\n"
              "## 3. Blocage n°2 : quel niveau de correctif ?\n**Recommandation** : le logiciel.\n\n## 4. Suite\nJe reprends ensuite.")
    intro, questions = agent_questions(report)
    assert [q.text for q in questions] == ["Blocage n°1 : quelle source ?", "Blocage n°2 : quel niveau de correctif ?"]  # ni « ## » ni numéro
    assert questions[0].detail == "Deux colonnes :\n- `a`\n- `b`\n\nLaquelle est la bonne ?"  # sa section, « ? » compris
    assert questions[1].detail == "**Recommandation** : le logiciel."
    assert "Blocage" not in intro and "## 1. Ce que j'ai vérifié" in intro and "## 4. Suite" in intro  # le reste fait le contexte
    block = '<questions>{"questions": [{"question": "## 3. Blocage n°2 : quel niveau ?", "options": [{"libelle": "Client"}]}]}</questions>'
    assert agent_questions("Compte rendu.\n" + block)[1][0].text == "Blocage n°2 : quel niveau ?"  # bloc JSON : même nettoyage
    assert [q.text for q in agent_questions("Contexte.\n1. Faut-il garder la colonne ?\n- **Et la date ?**")[1]] == ["Faut-il garder la colonne ?", "Et la date ?"]


def test_retour_branch_says_the_subject_without_numbers():
    """Branche d'un agent sur un retour : le sujet en clair, sans numéro ; même branche pour une reprise ; numéro en dernier recours."""
    from types import SimpleNamespace as S
    from deploy_hub.sources.claude_runner import AgentRun, retour_branch
    d = S(slug="presoa")
    r = S(id="23AA58", resource="", data_type="CED/GED", ligne="Factures multi version 2024 du salarié 200082222 et autres pièces")
    assert retour_branch(d, r) == "fix-presoa-ced-ged-factures-multi-version-du-salarie-et"
    assert len(retour_branch(d, S(id="X", resource="visit_histo", data_type="", ligne="mot " * 40))) <= 60
    assert retour_branch(d, S(id="R-9", resource="", data_type="", ligne="")) == "fix-presoa-r-9"  # rien de parlant
    older = AgentRun(1, "presoa", "Presoa", "retour 23AA58", "fix-presoa-23aa58", "x", datetime(2026, 11, 18, 9), "terminé")
    assert retour_branch(d, r, [older]) == "fix-presoa-23aa58"  # reprise : la branche déjà lancée
    other = AgentRun(2, "presoa", "Presoa", "retour 11BB22", retour_branch(d, r), "x", datetime(2026, 11, 18, 9), "terminé")
    assert retour_branch(d, r, [other]) == "fix-presoa-ced-ged-factures-multi-version-du-salarie-et-23aa58"


def test_a_finished_agent_is_archived_with_its_worktree_and_comes_back(repo, fake_claude, tmp_path):
    runner = _runner(repo, fake_claude, tmp_path)
    runner.launch_retour(D, R, prompt="Brief")
    _wait(runner, 1)
    worktree = Path(runner.get(1).worktree)
    assert worktree.is_dir() and runner.archivable(runner.get(1))
    (worktree / "argv.json").unlink()  # trace du faux Claude : un fichier non suivi, git garderait le worktree
    result = runner.archive(1)
    assert result.ok and runner.get(1) is None and [r.id for r in runner.archived()] == [1]
    assert not worktree.exists() and "fix-acme-r-7" in _git(repo, "branch")  # worktree retiré, branche gardée
    assert runner.find(1).archived_at and not runner.archive(1).ok  # lisible, pas deux fois
    assert ClaudeRunner(repo, tmp_path / "agents").get(1) is None  # au redémarrage, il reste aux archives
    assert runner.unarchive(1).ok and runner.get(1).archived_at is None and runner.archived() == []


def test_an_agent_at_work_or_waiting_for_an_answer_is_not_archived(tmp_path):
    runner = ClaudeRunner(tmp_path, tmp_path / "agents")
    for n, status in ((1, "en cours"), (2, WAITING)):
        runner.runs.append(AgentRun(n, "acme", "Acme", f"retour R-{n}", f"fix-{n}", "x", datetime(2026, 11, 1, 9), status))
    assert not runner.archive(1).ok and not runner.archive(2).ok and len(runner.runs) == 2


def test_tidying_archives_old_agents_then_erases_old_logs_once_a_day(tmp_path):
    runner = ClaudeRunner(tmp_path, tmp_path / "agents", archive_days=30, log_days=90)
    now = datetime(2026, 11, 24, 9)
    for n, days in ((1, 100), (2, 40), (3, 2)):
        ended = now - timedelta(days=days)
        runner.runs.append(AgentRun(n, "acme", "Acme", f"retour R-{n}", f"fix-{n}", "x", ended, "terminé", ended_at=ended))
        runner.log_path(runner.runs[-1]).parent.mkdir(parents=True, exist_ok=True)
        runner.log_path(runner.runs[-1]).write_text("{}\n")
    runner.runs.append(AgentRun(4, "acme", "Acme", "retour R-4", "fix-4", "x", now - timedelta(days=60), WAITING,
                                ended_at=now - timedelta(days=60)))
    runner.tidy(now)
    assert [r.id for r in runner.runs] == [3, 4]  # récent, et celui qui attend ta réponse
    archived = {r.id: r for r in runner.archived()}
    assert set(archived) == {1, 2} and archived[1].log_removed and not archived[2].log_removed
    assert not runner.log_path(archived[1]).exists() and runner.log_path(archived[2]).exists()
    runner.runs[0].ended_at = now - timedelta(days=50)
    runner.tidy(now + timedelta(hours=2))
    assert len(runner.runs) == 2  # pas avant le lendemain
    runner.tidy(now + timedelta(days=1, hours=1))
    assert [r.id for r in runner.runs] == [4]


def test_the_order_chosen_in_a_column_is_kept(tmp_path):
    runner = ClaudeRunner(tmp_path, tmp_path / "agents")
    for n in (1, 2, 3):
        runner.runs.append(AgentRun(n, "acme", "Acme", f"retour R-{n}", f"fix-{n}", "x", datetime(2026, 11, 1, 9), "terminé"))
    runner.reorder([3, 1, 99])
    assert [(r.id, r.rank) for r in ClaudeRunner(tmp_path, tmp_path / "agents").runs] == [(1, 1), (2, None), (3, 0)]


def test_a_worktree_with_uncommitted_work_is_kept_when_archiving(repo, fake_claude, tmp_path):
    runner = _runner(repo, fake_claude, tmp_path)
    runner.launch_retour(D, R, prompt="Brief")
    _wait(runner, 1)
    worktree = Path(runner.get(1).worktree)
    result = runner.archive(1)  # argv.json, non suivi, est encore là
    assert result.ok and "Worktree gardé" in result.message and worktree.is_dir()


def test_an_agent_can_be_renamed_and_pinned_and_a_pinned_agent_is_never_tidied_away(tmp_path):
    runner = ClaudeRunner(tmp_path, tmp_path / "agents", archive_days=30)
    old = datetime(2026, 9, 1, 9)
    runner.runs.append(AgentRun(1, "acme", "Acme", "retour R-1", "fix-1", "x", old, "terminé", ended_at=old))
    runner.runs.append(AgentRun(2, "acme", "Acme", "retour R-2", "fix-2", "x", old, "terminé", ended_at=old))
    assert runner.rename(1, "  Champ   de vision\n manquant ").title == "Champ de vision manquant" and runner.get(1).name == "Champ de vision manquant"
    assert runner.rename(1, "x" * 300).title == "x" * 120 and runner.rename(1, "  ").name == "retour R-1"  # vide : le sujet
    assert runner.toggle_pin(1).pinned and runner.rename(99, "?") is None
    runner.tidy(datetime(2026, 11, 24, 9))
    assert [r.id for r in runner.runs] == [1]  # épinglé : reste ; l'autre, fini depuis plus de 30 jours, est archivé
    assert runner.rename(2, "Archivé renommé").title == "Archivé renommé" and runner.archived()[0].name == "Archivé renommé"
    assert ClaudeRunner(tmp_path, tmp_path / "agents").get(1).pinned  # gardé au redémarrage
