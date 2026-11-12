"""Chaque stack, par chaque point d'entrée : ce qui part vraiment vers Airflow (corps du POST /dagRuns) et ce que suit air.py.

Si `padoa_env` manque dans la conf d'un run, l'orchestrator prend la Variable Airflow PADOA_ENV
(dags/dag_params/dag_params.py, PADOA_ENV) : Jarvis doit donc toujours l'envoyer, et l'envoyer juste.
"""

import json
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from deploy_hub import main
from deploy_hub.models import Action, Deployment, ResourceInjection
from deploy_hub.sources.airflow import PADOA_ENVS, AirflowSource
from deploy_hub.sources.dag_watch import DagWatcher

STACKS = ("integration", "snap", "recette")
DAG = "acme-uegar-etl-firm"


@pytest.fixture
def live(tmp_path, monkeypatch):
    """Airflow « réel » simulé par httpx : chaque requête est notée ; air.py remplacé par un script qui sort aussitôt."""
    d = Deployment(slug="acme", client="Acme", software="Uegar", orchestrator=["acme-uegar"],
                   actions=[Action(title="Injection des firms", injection=True, injection_status="A faire", resources=["firm"])],
                   resources=[ResourceInjection(name="firm", status="A faire", dag_id=DAG)])
    catalogue = tmp_path / "catalogue.json"
    catalogue.write_text(json.dumps({"clients": {"acme-uegar": {"dags": {DAG: {"type": "etl", "ressources": ["firm"]}}}}}))
    monkeypatch.setattr(main.dag_catalogue, "CATALOGUE", catalogue)
    monkeypatch.setattr(main.fiches, "get_deployment", lambda slug: d if slug == "acme" else None)
    monkeypatch.setattr(main.fiches, "list_deployments", lambda: [d])
    sent = []

    def handler(request):
        sent.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"dag_id": DAG, "is_paused": False})
        return httpx.Response(200, json={"dag_run_id": f"manual__{len(sent)}", "state": "queued"})

    monkeypatch.setattr(main, "airflow", AirflowSource("http://airflow.test", "user", "secret", transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(main, "dag_watch", DagWatcher(tmp_path / "airflow", None, {"AIRFLOW_URL": "http://airflow.test"},
                                                      command=[sys.executable, "-c", "pass"]))
    return TestClient(main.app), sent


def runs_posted(sent):
    return [json.loads(r.content) for r in sent if r.method == "POST" and r.url.path == f"/api/v1/dags/{DAG}/dagRuns"]


ENTRY_POINTS = {  # tous les formulaires de lancement de Jarvis postent vers l'une de ces deux routes
    "carte d'injection, CED, console >dag": ("/actions/injection/acme", {"dag_id": DAG, "card": "Injection des firms"}),
    "ressource de la fiche": (f"/actions/dag/{DAG}", {}),
}


@pytest.mark.parametrize("stack", STACKS)
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_each_stack_reaches_airflow_and_the_watch(live, entry, stack):
    app, sent = live
    url, data = ENTRY_POINTS[entry]
    page = app.post(url, data={**data, "padoa_env": stack})
    assert f"lancé sur {stack}" in page.text
    assert runs_posted(sent) == [{"conf": {"padoa_env": stack}}]  # exactement un run, sur cette stack
    assert main.dag_watch.watches[0].padoa_env == stack and main.airflow.runs[0].padoa_env == stack


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_no_stack_no_launch(live, entry):
    """Sans stack choisie, rien ne part : sinon Airflow prendrait sa stack par défaut (Variable PADOA_ENV)."""
    app, sent = live
    url, data = ENTRY_POINTS[entry]
    assert app.post(url, data=data).status_code == 422 and runs_posted(sent) == []


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("stack", ["prod", "demo", "Integration"])
def test_prod_and_unknown_stacks_never_reach_airflow(live, entry, stack):
    app, sent = live
    url, data = ENTRY_POINTS[entry]
    app.post(url, data={**data, "padoa_env": stack}, headers={"HX-Prompt": "PROD"})
    assert runs_posted(sent) == [] and main.dag_watch.watches == []


def test_every_launch_form_offers_exactly_the_test_stacks(live, monkeypatch):
    """Chaque formulaire propose integration, snap, recette — jamais prod — et poste vers une route vérifiée ci-dessus."""
    app, _ = live
    assert PADOA_ENVS == STACKS
    pages = {"onglet Injections (cartes et ressources)": app.get("/deploiements/acme/injections").text,
             "console >dag": app.get(f"/palette/dag/acme/{DAG}").text}
    for name, html in pages.items():
        for stack in STACKS:
            assert f'<option value="{stack}">' in html, name
        assert '<option value="prod">' not in html, name
    assert f'hx-post="/actions/dag/{DAG}"' in pages["onglet Injections (cartes et ressources)"]  # bouton de la ressource
    assert 'hx-post="/actions/injection/acme"' in pages["console >dag"]
