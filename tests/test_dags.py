"""Lancement d'un DAG depuis une carte d'injection : catalogue, choix de la stack, confirmation prod, lancement par air.py."""

import json

import pytest
from conftest import airflow
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
    assert '<option value="acme-uegar-etl-vaccine"' not in html  # pas une ressource de la carte (proposé seulement à l'enchaînement)
    for env in ("integration", "snap", "recette"):
        assert f'<option value="{env}">' in html


def test_resource_groups_have_their_own_line(app, monkeypatch):
    groups = dag_catalogue.groups_for(["acme-uegar", "acme-pulse"])
    assert [(g["customer"], g["groupe"], g["resources"]) for g in groups] == [("acme-pulse", "firm_base", ("firm",)), ("acme-uegar", "firm_base", ("firm", "firm_status"))]
    html = app.get("/deploiements/acme/injections").text
    assert "Groupes de ressources" in html and 'id="acme-groupe-acme-uegar-firm_base" data-group="todo"' in html  # état : celui des cartes de ses ressources
    line = html[html.index('id="acme-groupe-acme-uegar-firm_base"'):]
    assert 'name="card" value="Groupe firm_base"' in line and 'name="dag_id" value="acme-uegar-etl_group-firm_base"' in line


def test_a_card_with_several_resources_is_under_each_of_them(app, monkeypatch):
    d = Deployment(slug="acme", client="Acme", software="Uegar", orchestrator=["acme-uegar"],
                   actions=[Action(title="Firms et statuts", injection=True, injection_status="A faire", resources=["firm", "firm_status"])])
    monkeypatch.setattr(main.fiches, "get_deployment", lambda slug: d)
    assert [(b["id"], b["resources"], len(b["cards"])) for b in d.injection_lines] == [("firm", ["firm"], 1), ("firm_status", ["firm_status"], 1)]
    page = app.get("/deploiements/acme/injections").text
    firm = page[page.index('id="acme-firm"'):page.index('id="acme-firm_status"')]
    assert firm.index('<option value="acme-uegar-etl-firm"') < firm.index('<option value="acme-uegar-etl_group-firm_base"')  # l'ETL de la ressource d'abord
    assert ">ETL</option>" in firm and ">ETL groupe firm_base</option>" in firm and ">Lancer</button>" in firm  # la liste dit ce qui part
    assert "Lancer le groupe" not in page  # un groupe ne se lance que de sa ligne, section « Groupes de ressources »


def test_a_line_shows_its_last_runs_and_leads_to_their_watch(app, monkeypatch, tmp_path):
    from datetime import datetime, timedelta
    from deploy_hub.sources.dag_watch import DagWatch, DagWatcher
    watcher = DagWatcher(tmp_path / "airflow", None, {})
    now = datetime.now()
    watcher.watches = [
        DagWatch(3, "acme", "Acme", "acme-uegar", "acme-uegar-etl_group-firm_base", "manual__3", "recette", "Groupe firm_base", now, "suivi en cours"),
        DagWatch(2, "acme", "Acme", "acme-uegar", "acme-uegar-etl-vaccine", "manual__2", "snap", "Vaccins", now - timedelta(hours=1), "vert", now),
        DagWatch(1, "acme", "Acme", "acme-uegar", "acme-uegar-etl-firm", "manual__1", "snap", "Firms", now - timedelta(hours=2), "en échec ou incomplet",
                 now - timedelta(hours=1, minutes=50), "firm en échec · voir le rapport"),
    ]
    monkeypatch.setattr(main, "dag_watch", watcher)
    page = app.get("/deploiements/acme/injections").text
    firm = page[page.index('id="acme-firm"'):page.index("Groupes de ressources")]
    # ligne de firm : son ETL (#1) et son groupe (#3), pas l'ETL d'une autre ressource (#2) ; la pastille dit le plus récent
    assert 'class="badge g-in_progress inj-last" href="/agents/suivis/3"' in firm and "recette · suivi en cours" in firm
    assert firm.index('href="/agents/suivis/3" title="Ouvrir') < firm.index('href="/agents/suivis/1" title="Ouvrir') and "/agents/suivis/2" not in firm
    assert "firm en échec · voir le rapport" in firm and "en échec ou incomplet · 10 min" in firm
    group = page[page.index('id="acme-groupe-acme-uegar-firm_base"'):]
    assert 'href="/agents/suivis/3" title="Ouvrir' in group and "/agents/suivis/1" not in group  # la ligne du groupe : ses DAG seulement
    monkeypatch.setattr(main, "dag_watch", DagWatcher(tmp_path / "vide", None, {}))
    assert "Aucun DAG lancé depuis Jarvis pour cette ligne." in app.get("/deploiements/acme/injections").text


def test_launch_on_a_chosen_stack(app, fake_airflow):
    r = app.post("/actions/injection/acme", data={"dag_id": "acme-uegar-etl-firm", "padoa_env": "snap", "card": "Injection des firms"})
    assert "acme-uegar-etl-firm lancé sur snap" in r.text
    assert main.airflow.runs[0].padoa_env == "snap" and main.airflow.runs[0].card == "Injection des firms"


def test_without_airflow_nothing_is_launched(app):
    """Plus de mode démo : sans Airflow configuré, Jarvis dit quoi régler au lieu de simuler un lancement."""
    r = app.post("/actions/injection/acme", data={"dag_id": "acme-uegar-etl-firm", "padoa_env": "snap"})
    assert "Airflow non configuré" in r.text and "AIRFLOW_BASE_URL" in r.text and "Simulation" not in r.text and not main.airflow.runs


def test_prod_is_disabled(app):
    assert '<option value="prod">' not in app.get("/deploiements/acme/injections").text
    r = app.post("/actions/injection/acme", data={"dag_id": "acme-uegar-etl-firm", "padoa_env": "prod"}, headers={"HX-Prompt": "PROD"})
    assert "prod désactivé" in r.text and not any(run.padoa_env == "prod" for run in main.airflow.runs)


def test_prod_needs_typed_confirmation_once_enabled(app, monkeypatch, fake_airflow):
    monkeypatch.setattr(main, "PADOA_ENVS", ("integration", "snap", "recette", "prod"))
    monkeypatch.setattr("deploy_hub.sources.airflow.PADOA_ENVS", ("integration", "snap", "recette", "prod"))
    data = {"dag_id": "acme-uegar-etl-firm", "padoa_env": "prod"}
    assert "tape PROD pour confirmer" in app.post("/actions/injection/acme", data=data).text
    assert "sur prod" in app.post("/actions/injection/acme", data=data, headers={"HX-Prompt": "PROD"}).text


def test_unknown_dag_or_stack_is_refused(app):
    assert "pas un DAG de Acme" in app.post("/actions/injection/acme", data={"dag_id": "autre-etl-firm", "padoa_env": "integration"}).text
    assert "Environnement inconnu" in app.post("/actions/injection/acme", data={"dag_id": "acme-uegar-etl-firm", "padoa_env": "demo"}).text


def test_launch_goes_through_air_py(airflow_server):
    """Le vrai air.py (sous-processus) contre un faux Airflow HTTP : sortie de pause, run avec padoa_env, identifiants."""
    server = airflow_server(airflow(dag={"is_paused": True}))
    result = server.source().trigger_dag("acme-uegar-etl-firm", "recette", "Acme", "carte")
    assert result.ok and not result.simulated and result.run_id == "manual__1"
    assert result.message == "acme-uegar-etl-firm lancé sur recette (run manual__1)"
    patch = next(r for r in server.seen if r.method == "PATCH")  # DAG en pause : sorti de pause, sinon le run attendrait
    assert patch.params["update_mask"] == ["is_paused"] and patch.body == {"is_paused": False}
    assert server.posted_runs("acme-uegar-etl-firm") == [{"conf": {"padoa_env": "recette"}}]
    assert all(r.headers["authorization"].startswith("Basic ") for r in server.seen)


def test_launch_warns_when_a_run_is_already_going(airflow_server):
    busy = airflow(runs=[{"dag_run_id": "manual__0", "state": "running"}])
    result = airflow_server(busy).source().trigger_dag("acme-uegar-etl-firm", "snap")
    assert result.ok and "⚠️ 1 run(s) déjà en cours pour ce DAG." in result.message


def test_inactive_dag_is_refused_before_any_run(airflow_server):
    server = airflow_server(airflow(dag={"is_active": False}))
    result = server.source().trigger_dag("acme-uegar-etl-firm", "snap")
    assert not result.ok and "n'est plus chargé par Airflow" in result.message and server.posted_runs("acme-uegar-etl-firm") == []


@pytest.mark.parametrize(("status", "message"), [(401, "refuse l'accès (401)"), (403, "refuse l'accès (403)"), (404, "absent de cet Airflow")])
def test_launch_errors_in_jarvis_words(airflow_server, status, message):
    result = airflow_server(lambda request: (status, {})).source().trigger_dag("acme-uegar-etl-firm", "integration")
    assert not result.ok and message in result.message and ".claude/air.env" not in result.message


def test_unpause_refused_is_named(airflow_server):
    in_pause = airflow(dag={"is_paused": True})

    def no_patch(request):
        return (403, {}) if request.method == "PATCH" else in_pause(request)

    server = airflow_server(no_patch)
    result = server.source().trigger_dag("acme-uegar-etl-firm", "snap")
    assert not result.ok and "refuse de l'en sortir" in result.message and server.posted_runs("acme-uegar-etl-firm") == []


def test_unreachable_airflow_is_named():
    result = AirflowSource("http://127.0.0.1:9", "user", "secret").trigger_dag("acme-uegar-etl-firm", "snap")  # port 9 : rien n'écoute
    assert not result.ok and "Airflow injoignable (http://127.0.0.1:9)" in result.message


def test_credentials_never_on_the_command_line(airflow_server, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "jeton-test")
    monkeypatch.setattr("deploy_hub.config.ENV_FILE_KEYS", set())
    seen = {}

    def spy(args, **kwargs):
        seen["args"], seen["env"] = args, kwargs["env"]
        raise OSError("arrêt du test")

    monkeypatch.setattr("deploy_hub.sources.airflow.subprocess.run", spy)
    airflow_server().source(password="mot-de-passe-test").trigger_dag("acme-uegar-etl-firm", "snap")
    assert "mot-de-passe-test" not in " ".join(seen["args"]) and seen["env"]["AIRFLOW_PASSWORD"] == "mot-de-passe-test"
    assert seen["args"][-5:] == ["run", "--yes", "--", "acme-uegar-etl-firm", "snap"]
    assert "NOTION_TOKEN" not in seen["env"]  # air.py ne reçoit que ce qu'il lui faut, pas les secrets de Jarvis


def test_preview_route_shows_resources_and_launches_nothing(app, monkeypatch, airflow_server):
    server = airflow_server(airflow(tasks=[{"task_id": "transform_and_load_firm.load"}, {"task_id": "extract"}]))
    monkeypatch.setattr(main, "airflow", server.source())
    r = app.post("/actions/apercu", data={"dag_id": "acme-uegar-etl-firm", "padoa_env": "recette"}).json()
    assert r["ok"] and "Ressources : firm" in r["text"] and "Stack : recette" in r["text"] and "--yes" not in r["text"]
    assert not any(req.method in ("POST", "PATCH") for req in server.seen)


def test_prod_needs_the_typed_authorization_down_to_air_py(airflow_server):
    """Même avec la prod réactivée, seul le formulaire où PROD a été tapé autorise air.py à lancer en prod."""
    server = airflow_server()
    source = server.source()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("deploy_hub.sources.airflow.PADOA_ENVS", ("integration", "snap", "recette", "prod"))
        refused = source.trigger_dag("acme-uegar-etl-firm", "prod")
        allowed = source.trigger_dag("acme-uegar-etl-firm", "prod", prod_authorized=True)
    assert not refused.ok and "Refusé" in refused.message and allowed.ok
    assert server.posted_runs("acme-uegar-etl-firm") == [{"conf": {"padoa_env": "prod"}}]  # un seul run : celui autorisé


def test_option_like_dag_id_is_not_an_option(airflow_server):
    result = airflow_server().source().preview("-h", "snap")
    assert "usage:" not in result.message and "DAG   : -h" in result.message


def test_preview_refuses_what_launch_refuses(app):
    assert app.post("/actions/apercu", data={"dag_id": "acme-uegar-etl-firm", "padoa_env": "snap"}).json() == {
        "ok": False, "text": "Airflow non configuré : renseigne AIRFLOW_BASE_URL, AIRFLOW_USER et AIRFLOW_PASSWORD dans Réglages."}
    assert "prod désactivé" in app.post("/actions/apercu", data={"dag_id": "x", "padoa_env": "prod"}).json()["text"]


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


def test_ced_view_and_launch(app, monkeypatch, fake_airflow):
    d = main.fiches.get_deployment("acme")
    d.orchestrator = ["acme-pulse"]
    d.actions.append(Action(title="[INTE] [PULSE] Injecter les fichiers"))
    html = app.get("/deploiements/acme/injections?vue=ced").text
    assert "<code>acme-pulse-ced</code>" in html and "type 46" in html and 'class="req req-client"' in html
    assert "Injecter les fichiers" in html
    r = app.post("/actions/injection/acme", data={"dag_id": "acme-pulse-ced", "padoa_env": "recette", "card": "CED acme-pulse"})
    assert "acme-pulse-ced lancé sur recette" in r.text
    assert "Par ressource" in app.get("/deploiements/acme/injections").text  # vue par défaut : groupée
