"""Lancement d'un DAG depuis une carte d'injection : catalogue, choix de la stack, confirmation prod, appel Airflow."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from deploy_hub import main
from deploy_hub.models import Action, Deployment
from deploy_hub.sources import dags as dag_catalogue
from deploy_hub.sources.airflow import AirflowSource

CATALOGUE = {"clients": {
    "acme-uegar": {"logiciel": "uegar", "dags": {
        "acme-uegar-etl_group-firm_base": {"type": "etl_group", "groupe": "firm_base", "ressources": ["firm", "firm_status"]},
        "acme-uegar-etl-firm": {"type": "etl", "ressources": ["firm"]},
        "acme-uegar-etl-vaccine": {"type": "etl", "ressources": ["vaccine"]},
    }},
    "acme-pulse": {"logiciel": "pulse", "ced": {"dag_id": "acme-pulse-ced", "ressources": {
        "employee": [{"requete": "query_file_type_46", "niveau": "logiciel"}, {"requete": "query_file_type_pilote", "niveau": "client"}]}},
        "dags": {
        "acme-pulse-etl_group-firm_base": {"type": "etl_group", "groupe": "firm_base", "ressources": ["firm"]},
    }},
}}


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps(CATALOGUE))
    monkeypatch.setattr(dag_catalogue, "CATALOGUE", path)
    return path


def test_dags_for_a_card_resource_first_then_groups(catalogue):
    choices = dag_catalogue.dags_for(["acme-uegar", "acme-pulse"], ["firm"])
    assert [c.dag_id for c in choices] == ["acme-uegar-etl-firm", "acme-pulse-etl_group-firm_base", "acme-uegar-etl_group-firm_base"]
    assert choices[0].label == "ETL ressource · firm (acme-uegar)"
    assert dag_catalogue.dags_for(["acme-uegar"], ["visit_histo"]) == []
    assert dag_catalogue.dags_for([], ["firm"]) == []


@pytest.fixture
def app(catalogue, monkeypatch):
    d = Deployment(slug="acme", client="Acme", software="Uegar", orchestrator=["acme-uegar"],
                   actions=[Action(title="Injection des firms", injection=True, injection_status="A faire", resources=["firm"])])
    monkeypatch.setattr(main.fiches, "get_deployment", lambda slug: d if slug == "acme" else None)
    monkeypatch.setattr(main.fiches, "list_deployments", lambda: [d])
    monkeypatch.setattr(main, "airflow", AirflowSource(""))
    return TestClient(main.app)


def test_injection_tab_offers_the_card_dags_and_the_stacks(app):
    html = app.get("/deploiements/acme/injections").text
    assert '<option value="acme-uegar-etl-firm"' in html and '<option value="acme-uegar-etl_group-firm_base"' in html
    assert "acme-uegar-etl-vaccine" not in html  # pas une ressource de la carte
    for env in ("integration", "snap", "recette"):
        assert f'<option value="{env}">' in html


def test_launch_on_a_chosen_stack(app):
    r = app.post("/actions/injection/acme", data={"dag_id": "acme-uegar-etl-firm", "padoa_env": "snap", "card": "Injection des firms"})
    assert "Simulation : acme-uegar-etl-firm sur snap" in r.text
    assert main.airflow.runs[0].padoa_env == "snap" and main.airflow.runs[0].card == "Injection des firms"


def test_prod_is_disabled(app):
    assert '<option value="prod">' not in app.get("/deploiements/acme/injections").text
    r = app.post("/actions/injection/acme", data={"dag_id": "acme-uegar-etl-firm", "padoa_env": "prod"}, headers={"HX-Prompt": "PROD"})
    assert "prod désactivé" in r.text and not any(run.padoa_env == "prod" for run in main.airflow.runs)


def test_prod_needs_typed_confirmation_once_enabled(app, monkeypatch):
    monkeypatch.setattr(main, "PADOA_ENVS", ("integration", "snap", "recette", "prod"))
    monkeypatch.setattr("deploy_hub.sources.airflow.PADOA_ENVS", ("integration", "snap", "recette", "prod"))
    data = {"dag_id": "acme-uegar-etl-firm", "padoa_env": "prod"}
    assert "tape PROD pour confirmer" in app.post("/actions/injection/acme", data=data).text
    assert "sur prod" in app.post("/actions/injection/acme", data=data, headers={"HX-Prompt": "PROD"}).text


def test_unknown_dag_or_stack_is_refused(app):
    assert "pas un DAG de Acme" in app.post("/actions/injection/acme", data={"dag_id": "autre-etl-firm", "padoa_env": "integration"}).text
    assert "Environnement inconnu" in app.post("/actions/injection/acme", data={"dag_id": "acme-uegar-etl-firm", "padoa_env": "demo"}).text


def airflow_with(handler):
    return AirflowSource("http://airflow.test", "user", "secret", transport=httpx.MockTransport(handler))


def test_live_trigger_sends_padoa_env():
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"dag_id": "x", "is_paused": True})
        return httpx.Response(200, json={"dag_run_id": "manual__1", "state": "queued"})

    result = airflow_with(handler).trigger_dag("acme-uegar-etl-firm", "recette", "Acme", "carte")
    assert result.ok and not result.simulated and "manual__1" in result.message and "en pause" in result.message
    post = seen[-1]
    assert post.url.path == "/api/v1/dags/acme-uegar-etl-firm/dagRuns" and json.loads(post.content) == {"conf": {"padoa_env": "recette"}}
    assert post.headers["authorization"].startswith("Basic ")


@pytest.mark.parametrize(("status", "message"), [(401, "refuse l'accès"), (404, "absent de cet Airflow")])
def test_live_trigger_errors(status, message):
    result = airflow_with(lambda request: httpx.Response(status, json={})).trigger_dag("acme-uegar-etl-firm", "integration")
    assert not result.ok and message in result.message


def test_ced_dag_is_known_and_listed(catalogue):
    assert dag_catalogue.known_dag(["acme-pulse"], "acme-pulse-ced") and not dag_catalogue.known_dag(["acme-uegar"], "acme-pulse-ced")
    assert [c["dag_id"] for c in dag_catalogue.ced_for(["acme-uegar", "acme-pulse"])] == ["acme-pulse-ced"]


@pytest.mark.parametrize(("title", "injection", "expected"), [
    ("[INTE] [UEGAR] Injecter les fichiers", False, True),
    ("Injection CED des documents", True, True),
    ("Imprimer les fichiers de formation", False, False),  # pas une carte d'intégration
    ("[INTE] Injecter les firms", False, False),
])
def test_ced_cards_rule(title, injection, expected):
    assert Action(title=title, injection=injection).is_ced is expected


def test_ced_view_and_launch(app, monkeypatch):
    d = main.fiches.get_deployment("acme")
    d.orchestrator = ["acme-pulse"]
    d.actions.append(Action(title="[INTE] [PULSE] Injecter les fichiers"))
    html = app.get("/deploiements/acme/injections?vue=ced").text
    assert "<code>acme-pulse-ced</code>" in html and "type 46" in html and 'class="req req-client"' in html
    assert "Injecter les fichiers" in html
    r = app.post("/actions/injection/acme", data={"dag_id": "acme-pulse-ced", "padoa_env": "recette", "card": "CED acme-pulse"})
    assert "Simulation : acme-pulse-ced sur recette" in r.text
    assert "Par ressource" in app.get("/deploiements/acme/injections").text  # vue par défaut : groupée
