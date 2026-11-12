"""Console Jarvis (vraie console) : commandes sur le client en cours, texte de terminal, confirmation avant toute action."""

import json

import pytest
from fastapi.testclient import TestClient

from deploy_hub import main
from deploy_hub.main import CLIENT_COOKIE, app
from deploy_hub.models import Action, Deployment, RetourRecette

DAGS = {"acme-uegar-etl-visit_histo": {"type": "etl", "ressources": ["visit_histo"]},
        "acme-uegar-etl_group-medical": {"type": "etl_group", "groupe": "medical", "ressources": ["visit_histo", "vaccine"]},
        "acme-uegar-etl-firm": {"type": "etl", "ressources": ["firm"]}}


def run(c: TestClient, ligne: str) -> dict:
    r = c.post("/console", data={"ligne": ligne})
    assert r.status_code == 200
    return r.json()


def text(out: dict) -> str:
    return "\n".join(out["lines"])


@pytest.fixture
def acme(tmp_path, monkeypatch):
    d = Deployment(slug="acme", client="Acme", software="Uegar", orchestrator=["acme-uegar"],
                   injection_counts={"Vérification KO inté/snap": 1, "A faire": 3},  # totaux Notion, comme la synthèse
                   actions=[Action(title="Injection des firms", injection=True, injection_status="Vérification KO inté/snap", resources=["firm"])],
                   retours=[RetourRecette(id="R-7", data_type="AMT", status="À faire"), RetourRecette(id="R-8", data_type="AMT", status="Validé")])
    other = Deployment(slug="globex", client="Globex", software="Pulse")
    catalogue = tmp_path / "catalogue.json"
    catalogue.write_text(json.dumps({"clients": {"acme-uegar": {"dags": DAGS}}}))
    monkeypatch.setattr(main.dag_catalogue, "CATALOGUE", catalogue)
    monkeypatch.setattr(main.fiches, "get_deployment", lambda slug: {"acme": d, "globex": other}.get(slug))
    monkeypatch.setattr(main.fiches, "list_deployments", lambda: [d, other])
    c = TestClient(app)
    c.cookies.set(CLIENT_COOKIE, "acme")
    return c


def test_help_lists_every_command_and_the_prompt_names_the_client(acme):
    out = run(acme, "aide")
    assert out["prompt"] == "jarvis:acme"
    for name in ("client", "etat", "coince", "retards", "cherche", "dags", "lancer", "agent", "rafraichir", "suivis", "ouvrir"):
        assert name in text(out)
    assert "lancer &lt;dag&gt; &lt;stack&gt;" in text(run(acme, "help lancer"))  # alias et détail d'une commande


def test_unknown_command_suggests_the_closest(acme):
    out = text(run(acme, "coinse"))
    assert "commande inconnue" in out and "coince" in out


def test_client_shows_and_switches_the_current_client(acme):
    assert "Client en cours : Acme" in text(run(acme, "client")) and "globex" in text(run(acme, "client"))
    r = acme.post("/console", data={"ligne": "client glo"})
    assert r.json()["prompt"] == "jarvis:globex" and r.cookies.get(CLIENT_COOKIE) == "globex"
    assert "aucun client" in text(run(acme, "client zzz"))


def test_status_and_blocking_points(acme):
    etat = text(run(acme, "etat"))
    assert "Acme" in etat and "1 retour(s) ouvert(s) sur 2" in etat and "1 KO" in etat
    coince = text(run(acme, "coince"))
    assert "t-head" in coince and "firm" in coince and 'href="/deploiements/acme/injections#acme-firm"' in coince


def test_dags_lists_the_client_catalogue_filtered_by_words(acme):
    out = text(run(acme, "dags visit"))
    assert "acme-uegar-etl-visit_histo" in out and "acme-uegar-etl_group-medical" in out and "acme-uegar-etl-firm" not in out


def test_launch_asks_for_confirmation_and_targets_the_existing_route(acme):
    out = run(acme, "lancer etl-visit_histo snap")
    assert out["confirm"] == {"question": "Lancer acme-uegar-etl-visit_histo sur snap ? (o/N)", "url": "/actions/injection/acme",
                              "data": {"dag_id": "acme-uegar-etl-visit_histo", "padoa_env": "snap", "card": "Console Jarvis"}}
    assert main.airflow.runs == [] or all(r.dag_id != "acme-uegar-etl-visit_histo" for r in main.airflow.runs)  # rien n'est parti


@pytest.mark.parametrize(("ligne", "attendu"), [
    ("lancer etl-visit_histo prod", "prod est désactivée"),
    ("lancer etl-visit_histo demo", "stack refusée"),
    ("lancer etl snap", "3 DAGs"),  # ambigu : rien n'est proposé au lancement
    ("lancer inconnu snap", "aucun DAG"),
    ("lancer visit", "Usage"),
])
def test_launch_refuses_what_is_not_clear(acme, ligne, attendu):
    out = run(acme, ligne)
    assert out["confirm"] is None and attendu in text(out)


def test_agent_opens_the_retour_with_its_agent_block(acme):
    out = run(acme, "agent r-7")
    assert out["navigate"] == "/deploiements/acme/ticket/retour/R-7?agent=1" and out["ticket"] is True
    assert "clos" in text(run(acme, "agent R-8")) and run(acme, "agent R-8")["navigate"] == ""


def test_refresh_and_open_and_screen_commands(acme):
    assert run(acme, "rafraichir")["confirm"]["url"] == "/actions/rafraichir/acme"
    assert run(acme, "ouvrir rec")["navigate"] == "/deploiements/acme/recette"
    assert run(acme, "clear")["clear"] and run(acme, "exit")["close"] and run(acme, "historique")["history"]


def test_output_is_escaped(acme):
    assert "<script>" not in text(run(acme, "<script>alert(1)</script>"))


def test_commands_need_a_current_client_except_the_free_ones(monkeypatch):
    monkeypatch.setattr(main.fiches, "list_deployments", lambda: [])
    c = TestClient(app)
    assert "Aucun client en cours" in text(run(c, "etat")) and "client" in text(run(c, "aide"))
    assert run(c, "etat")["prompt"] == "jarvis:~"
