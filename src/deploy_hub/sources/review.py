"""Relecture guidée d'un agent : ce qu'il a changé, les tests qu'il a lancés, ce qui n'a pas pu être vérifié.

Tout vient du journal de l'agent (data/agents/<id>.jsonl) et de son worktree (noms de fichiers et nombres de lignes
seulement). Rien n'est lancé à part `git diff --numstat` et `git status` dans le worktree.
"""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from deploy_hub.models import DONE
from deploy_hub.sources.claude_runner import AgentRun, _events, _tool_line

TEST_COMMAND = re.compile(r"\b(pytest|unittest)\b")
TEST_OUTCOME = re.compile(r"\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed)[^\n=]*")
SHARED_STEP = re.compile(r"(^|/)(clean|compare|load)\w*(/|\.py$)")  # étapes communes à tous les clients
SOFTWARE_LEVEL = re.compile(r"/softwares?/")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
OK, WARN = "ok", "warn"


@dataclass
class RanTests:
    command: str
    passed: bool
    outcome: str  # dernière ligne de résultat de pytest (« 12 passed in 3.4s »), si elle est dans le journal


@dataclass
class Review:
    files: list[tuple[str, str, str]] = field(default_factory=list)  # (chemin, lignes ajoutées, lignes retirées)
    from_git: bool = False  # False : fichiers relevés dans le journal (worktree absent), sans nombre de lignes
    uncommitted: int = 0
    tests: list[RanTests] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)  # (appel d'outil, erreur) hors lancements de tests
    checks: list[tuple[str, str]] = field(default_factory=list)  # (ok | warn, phrase)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(c.get("text", "") for c in content or [] if isinstance(c, dict))


def _git(worktree: Path, *args: str) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True, timeout=20, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def build(run: AgentRun, log: Path) -> Review:
    review = Review()
    calls, results = {}, {}  # appels d'outils et leurs résultats, par identifiant
    order = []
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    for e in _events(text):
        for block in (e.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                key = block.get("id") or f"sans-id-{len(order)}"
                calls[key] = block
                order.append(key)
            elif block.get("type") == "tool_result":
                results[block.get("tool_use_id") or (order[-1] if order else "")] = block

    edited = []
    for key in order:
        block, result = calls[key], results.get(key, {})
        args = block.get("input") or {}
        failed, output = bool(result.get("is_error")), _text(result.get("content"))
        command = str(args.get("command") or "")
        if block.get("name") == "Bash" and TEST_COMMAND.search(command):
            outcomes = TEST_OUTCOME.findall(output)
            review.tests.append(RanTests(command[:300], not failed and key in results, outcomes[-1].strip()[:120] if outcomes else ""))
        elif failed:
            review.refused.append((_tool_line(block), output.strip()[:300]))
        elif block.get("name") in EDIT_TOOLS and args.get("file_path"):
            path = str(args["file_path"])
            path = path.removeprefix(run.worktree.rstrip("/") + "/") if run.worktree else path
            if path not in edited:
                edited.append(path)

    worktree = Path(run.worktree) if run.worktree else None
    numstat = _git(worktree, "diff", "--numstat", "origin/main...HEAD") if worktree and worktree.is_dir() else None
    if numstat is not None:
        review.from_git = True
        review.files = [tuple(line.split("\t", 2)[i] for i in (2, 0, 1)) for line in numstat.splitlines() if line.count("\t") >= 2]
        review.uncommitted = len((_git(worktree, "status", "--porcelain") or "").splitlines())
    else:
        review.files = [(path, "", "") for path in edited]

    paths = [f[0] for f in review.files]
    add = lambda ok, yes, no: review.checks.append((OK, yes) if ok else (WARN, no))  # noqa: E731
    add(bool(run.pr_url), "PR ouverte.", "Pas de PR : rien n'a été proposé à la relecture.")
    add(bool(paths), f"{len(paths)} fichier(s) modifié(s).", "Aucun fichier modifié.")
    if not review.tests:
        review.checks.append((WARN, "Aucun test lancé par l'agent : le correctif n'est pas vérifié."))
    else:
        last = review.tests[-1]
        add(last.passed, f"Dernier lancement de tests passé{f' ({last.outcome})' if last.outcome else ''}.",
            f"Dernier lancement de tests en échec ou refusé{f' ({last.outcome})' if last.outcome else ''}.")
    if paths:
        add(any(p.startswith("tests/") for p in paths), "Un test a été ajouté ou modifié.", "Aucun test ajouté ni modifié.")
    shared = [p for p in paths if SHARED_STEP.search(p) and not p.startswith("tests/")]
    if shared:
        review.checks.append((WARN, f"Étape commune à tous les clients touchée (clean, compare ou load) : {', '.join(shared[:5])}."))
    software = [p for p in paths if SOFTWARE_LEVEL.search(p)]
    if software:
        review.checks.append((WARN, f"Correctif au niveau logiciel ({len(software)} fichier(s)) : d'autres clients sont concernés."))
    if review.uncommitted:
        review.checks.append((WARN, f"{review.uncommitted} fichier(s) non commité(s) dans le worktree : absents de la PR."))
    if review.refused:
        review.checks.append((WARN, f"{len(review.refused)} appel(s) d'outil refusé(s) ou en erreur : ce que l'agent n'a pas pu faire."))
    if run.group != DONE:
        review.checks.append((WARN, f"Agent {run.status} : son travail est peut-être incomplet."))
    return review
