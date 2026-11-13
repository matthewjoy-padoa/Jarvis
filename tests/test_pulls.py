"""Suivi des PR par client : lecture de `gh pr list` (remplacé ici par une sortie fictive), rattachement, onglet PR."""

import json
import subprocess
from datetime import date

import pytest
from fastapi.testclient import TestClient

from deploy_hub import main
from deploy_hub.sources.pulls import GREEN, NO_CI, PENDING, RED, UNKNOWN, PullRequests, ci_state

TODAY = date(2026, 11, 13)


def pr(number, title, created="2026-11-10T08:00:00Z", updated=None, **more):
    return {"number": number, "title": title, "url": f"https://github.com/padoa/integration-orchestrator/pull/{number}",
            "author": {"login": "dev"}, "headRefName": f"branche-{number}", "createdAt": created, "updatedAt": updated or created,
            "isDraft": False, "reviewDecision": "", "statusCheckRollup": [], "reviewRequests": [], **more}


def source(*prs, **kw):
    """Faux gh : `pr list` rend toutes les PR, `pr view <n>` celle demandée. calls : les commandes reçues (list ou numéro)."""
    calls = []

    def gh(directory, command):
        calls.append(command[2] if command[2] == "list" else int(command[3]))
        return json.dumps(list(prs) if command[2] == "list" else next(p for p in prs if p["number"] == int(command[3])))
    return PullRequests("/nulle/part", gh, **kw), calls


@pytest.mark.parametrize(("checks", "state"), [
    ([], NO_CI),
    ([{"status": "COMPLETED", "conclusion": "SUCCESS"}, {"state": "SUCCESS"}], GREEN),
    ([{"status": "COMPLETED", "conclusion": "SUCCESS"}, {"status": "IN_PROGRESS", "conclusion": ""}], PENDING),
    ([{"state": "PENDING"}], PENDING),
    ([{"status": "IN_PROGRESS"}, {"status": "COMPLETED", "conclusion": "FAILURE"}], RED),  # un échec l'emporte sur ce qui tourne
    ([{"state": "ERROR"}], RED),
    ([{"status": "COMPLETED", "conclusion": "SKIPPED"}], GREEN),
])
def test_ci_state(checks, state):
    assert ci_state(checks) == state


def test_a_pr_goes_to_a_client_by_scope_agent_or_software():
    store, _ = source(pr(1, "[FIX](acme) Mapping des visites"), pr(2, "[FEAT](Autre) Rien à voir"), pr(3, "[FIX](uegar) Requête commune"),
                      pr(4, "Sans scope, ouverte par un agent"), pr(5, "[FEAT](autre, ACME-SST) Deux clients"))
    found, error = store.for_client("acme", ["acme-sst"], "Uegar", {"https://github.com/padoa/integration-orchestrator/pull/4"})
    assert not error and [(p.number, p.why) for p in found] == [(1, "client"), (3, "logiciel"), (4, "agent"), (5, "client")]


def test_review_and_waiting_days():
    store, _ = source(pr(1, "[FIX](acme) a", reviewRequests=[{"login": "relectrice"}], updated="2026-11-12T09:00:00Z"),
                      pr(2, "[FIX](acme) b", reviewDecision="APPROVED"), pr(3, "[FIX](acme) c", isDraft=True),
                      pr(4, "[FIX](acme) d", reviewDecision="CHANGES_REQUESTED"), pr(5, "[FIX](acme) e"))
    prs = store.for_client("acme", [], "", set())[0]
    assert [p.review[0] for p in prs] == ["relecture demandée", "approuvée", "brouillon", "changements demandés", "sans relecteur"]
    assert prs[0].reviewers == ["relectrice"] and (prs[0].age(TODAY), prs[0].idle(TODAY)) == (3, 1)


def test_github_is_read_once_per_period_unless_asked_and_details_only_for_the_client():
    store, calls = source(pr(1, "[FIX](acme) a"), pr(2, "[FIX](autre) b"))
    store.for_client("acme", [], "", set()), store.for_client("acme", [], "", set())
    assert calls == ["list", 1]  # la PR de l'autre client n'est jamais détaillée
    store.for_client("acme", [], "", set(), fresh=True)
    assert calls == ["list", 1, "list", 1]
    store.ttl = -1  # délai passé
    store.for_client("acme", [], "", set())
    assert len(calls) == 6


def test_a_gh_failure_is_a_message_not_a_crash():
    def broken(directory, command):
        raise subprocess.CalledProcessError(1, "gh", stderr="gh: To get started with GitHub CLI, please run: gh auth login")

    def missing(directory, command):
        raise FileNotFoundError

    prs, error = PullRequests("/nulle/part", broken).all()
    assert prs == [] and "gh auth login" in error
    assert "introuvable" in PullRequests("/nulle/part", missing).all()[1]
    assert "illisible" in PullRequests("/nulle/part", lambda d, c: "pas du json").all()[1]


def test_unreadable_details_leave_the_pr_listed_as_unknown():
    def gh(directory, command):
        if command[2] == "view":
            raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 502")
        return json.dumps([pr(1, "[FIX](acme) a")])

    found, error = PullRequests("/nulle/part", gh).for_client("acme", [], "", set())
    assert [(p.number, p.ci, p.review[0]) for p in found] == [(1, UNKNOWN, UNKNOWN)] and "#1" in error


def test_the_pr_tab_loads_the_list_and_shows_ci_reviewer_and_days(monkeypatch):
    store, _ = source(pr(7, "[FIX](client-demo) Dates de visite", statusCheckRollup=[{"status": "COMPLETED", "conclusion": "FAILURE"}],
                         reviewRequests=[{"login": "relectrice"}], created="2026-11-01T08:00:00Z"), pr(8, "[FIX](ailleurs) Autre client"))
    monkeypatch.setattr(main, "pulls", store)
    client = TestClient(main.app)
    tab = client.get("/deploiements/client-demo/pr")
    assert tab.status_code == 200 and 'hx-get="/deploiements/client-demo/pr/liste"' in tab.text
    html = client.get("/deploiements/client-demo/pr/liste").text
    assert "#7 ↗" in html and "Dates de visite" in html and "Autre client" not in html
    assert 'g-ko">rouge' in html and "relecture demandée" in html and "relectrice" in html and "Actualiser" in html


def test_the_pr_tab_says_when_github_cannot_be_read(monkeypatch):
    monkeypatch.setattr(main, "pulls", PullRequests("/nulle/part", lambda d, c: (_ for _ in ()).throw(FileNotFoundError())))
    html = TestClient(main.app).get("/deploiements/client-demo/pr/liste").text
    assert "introuvable" in html and "Liste indisponible." in html
    assert TestClient(main.app).get("/deploiements/inconnu/pr/liste").status_code == 404
