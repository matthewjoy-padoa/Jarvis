"""Relecture guidée d'un agent (journal fictif, dépôt git jetable) et bilan coût / durée par type de ticket."""

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from deploy_hub import main
from deploy_hub.sources import review
from deploy_hub.sources.claude_runner import AgentRun, duration_text, usage

T0 = datetime(2026, 11, 13, 9, 0)


def run(run_id=1, minutes=10, cost=1.0, category="AMT", status="terminé", **more) -> AgentRun:
    return AgentRun(run_id, "client-demo", "Client Démo", f"retour R-{run_id}", f"fix-client-demo-r-{run_id}", "brief", T0, status,
                    ended_at=T0 + timedelta(minutes=minutes), cost_usd=cost, category=category, **more)


def write_log(path: Path, *steps) -> Path:
    """steps : (outil, arguments, erreur ?, sortie)."""
    lines = []
    for n, (name, args, failed, output) in enumerate(steps):
        lines.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{n}", "name": name, "input": args}]}})
        lines.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{n}", "is_error": failed, "content": output}]}})
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return path


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def worktree(tmp_path: Path, files: dict[str, str]) -> Path:
    """Dépôt avec origin/main, puis un commit de l'« agent » qui ajoute ces fichiers."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "t@t"), _git(work, "config", "user.name", "t")
    (work / "a.txt").write_text("v1\n")
    _git(work, "add", "."), _git(work, "commit", "-qm", "init"), _git(work, "push", "-q", "origin", "HEAD:main")
    _git(work, "checkout", "-qb", "fix")
    for name, text in files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(text)
    _git(work, "add", "."), _git(work, "commit", "-qm", "correctif")
    return work


def test_review_reads_the_diff_the_tests_and_what_was_refused(tmp_path):
    work = worktree(tmp_path, {"src/mappings/software/uegar/visit.py": "a\nb\n", "tests/test_visit.py": "c\n"})
    (work / "brouillon.txt").write_text("oublié\n")
    log = write_log(tmp_path / "1.jsonl",
                    ("Bash", {"command": "uv run pytest tests/test_visit.py"}, True, "1 failed, 2 passed in 0.5s"),
                    ("Edit", {"file_path": f"{work}/src/mappings/software/uegar/visit.py"}, False, "ok"),
                    ("Bash", {"command": "uv run pytest tests/test_visit.py"}, False, "===== 3 passed in 0.4s ====="),
                    ("Bash", {"command": "gh pr merge 12"}, True, "Permission refusée"))
    r = review.build(run(worktree=str(work), pr_url="https://github.com/padoa/integration-orchestrator/pull/12"), log)
    assert r.from_git and r.files == [("src/mappings/software/uegar/visit.py", "2", "0"), ("tests/test_visit.py", "1", "0")]
    assert [(t.passed, t.outcome) for t in r.tests] == [(False, "1 failed, 2 passed in 0.5s"), (True, "3 passed in 0.4s")]
    assert r.refused == [("Bash gh pr merge 12", "Permission refusée")] and r.uncommitted == 1
    ok = [text for level, text in r.checks if level == review.OK]
    warn = " ".join(text for level, text in r.checks if level == review.WARN)
    assert ok == ["PR ouverte.", "2 fichier(s) modifié(s).", "Dernier lancement de tests passé (3 passed in 0.4s).", "Un test a été ajouté ou modifié."]
    assert "niveau logiciel (1 fichier(s))" in warn and "1 fichier(s) non commité(s)" in warn and "1 appel(s) d'outil refusé(s)" in warn


def test_review_without_worktree_falls_back_on_the_log_and_flags_what_is_missing(tmp_path):
    log = write_log(tmp_path / "2.jsonl", ("Write", {"file_path": "/ailleurs/wt/src/lib/steps/clean/visit.py"}, False, "ok"),
                    ("Write", {"file_path": "/ailleurs/wt/src/lib/steps/clean/visit.py"}, False, "ok"))
    r = review.build(run(worktree="/ailleurs/wt", status="échec"), log)
    assert not r.from_git and r.files == [("src/lib/steps/clean/visit.py", "", "")]
    warn = " ".join(text for level, text in r.checks if level == review.WARN)
    for expected in ("Pas de PR", "Aucun test lancé", "Aucun test ajouté", "Étape commune à tous les clients", "Agent échec"):
        assert expected in warn


def test_agent_page_shows_the_review_once_the_agent_has_ended(tmp_path, monkeypatch):
    done, working, fake = run(1), run(2, status="en cours"), run(3, simulated=True)
    working.ended_at = None
    monkeypatch.setattr(main.claude, "runs", [done, working, fake])
    monkeypatch.setattr(main.claude, "state_dir", tmp_path)
    write_log(tmp_path / "1.jsonl", ("Bash", {"command": "uv run pytest tests"}, False, "4 passed in 1.0s"))
    client = TestClient(main.app)
    page = client.get("/agents/1").text
    assert "Relecture guidée" in page and "4 passed in 1.0s" in page and "Pas de PR" in page and "durée 10 min" in page
    assert "Relecture guidée" not in client.get("/agents/2").text  # en cours : pas encore
    assert "Relecture guidée" not in client.get("/agents/3").text  # simulé : rien à relire
    assert "Relecture guidée" in client.get("/agents/1/journal").text  # dernier rafraîchissement du journal


def test_duration_text():
    assert [duration_text(timedelta(seconds=s)) for s in (45, 60, 725, 3900)] == ["45 s", "1 min", "12 min", "1 h 05"]


def test_usage_by_ticket_type_leaves_out_simulated_and_running_agents():
    working = run(5, status="en cours")
    working.ended_at = None
    runs = [run(1, 10, 1.0, "AMT", pr_url="https://github.com/padoa/integration-orchestrator/pull/1"), run(2, 30, 3.0, "AMT"),
            run(3, 5, None, "", status="arrêté"), run(4, 20, 9.0, "Visites", simulated=True), working]
    found = usage(runs)
    assert (found["total"]["count"], found["total"]["prs"], found["total"]["cost"]) == (3, 1, 4.0)
    assert found["total"]["time"] == timedelta(minutes=45) and found["total"]["median"] == timedelta(minutes=10)
    amt, other = found["rows"]
    assert (amt["label"], amt["count"], amt["mean_cost"], amt["median"]) == ("AMT", 2, 2.0, timedelta(minutes=20))
    assert (other["label"], other["cost"], other["mean_cost"]) == ("non précisé", None, None)
    assert usage([run(4, simulated=True)]) is None


def test_agents_page_shows_duration_cost_and_the_summary(monkeypatch):
    monkeypatch.setattr(main.claude, "runs", [run(1, 12, 0.5, "AMT"), run(2, 70, 1.5, "Visites historiques")])
    page = TestClient(main.app).get("/agents").text
    assert "<th>Durée</th><th>Coût</th>" in page and "12 min" in page and "1 h 10" in page and "0.50 $" in page
    assert "Coût et durée" in page and "Visites historiques" in page and "2.00 $" in page  # coût total
    monkeypatch.setattr(main.claude, "runs", [])
    assert "Coût et durée" not in TestClient(main.app).get("/agents").text


def test_the_ticket_type_is_kept_at_launch(tmp_path):
    from deploy_hub.sources.claude_runner import ClaudeRunner

    class D:
        slug, client, software = "acme", "Acme", "Uegar"

    class R:
        id, software, status, data_type, type_retour, resource, due, notion_url, content = "R-7", "", "Ouvert", "AMT", "", "amt", None, "", []

    runner = ClaudeRunner(tmp_path, tmp_path / "agents")  # simulation
    runner.launch_retour(D, R, prompt="Brief")
    runner.launch(D, "visit_histo")
    assert [r.category for r in runner.runs] == ["visit_histo", "AMT"]
    assert [r.category for r in ClaudeRunner(tmp_path, tmp_path / "agents").runs] == ["visit_histo", "AMT"]  # relu depuis agents.json
