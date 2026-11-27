"""Lien avec InteCG (jeu de cartes de l'équipe) : un DAG ou un agent lancé demande un booster au serveur du jeu.
Aucun appel réseau : le POST est remplacé par un faux."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from deploy_hub import main
from deploy_hub.sources import intecg
from deploy_hub.sources.notifications import NotificationStore

GAME = "http://localhost:5173"
WHEN = datetime(2026, 11, 27, 10, 30)


class Answer:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {"credited": True, "message": "Booster bonus crédité.", "bonusBoosters": 2}

    def json(self):
        return self._body


class Seen(list):
    """Demandes reçues par le faux jeu : [(adresse, {cle, motif})]."""


@pytest.fixture
def game(monkeypatch):
    """InteCG branché, mais factice : note chaque demande reçue ({cle, motif}) et répond « crédité »."""
    monkeypatch.setenv("INTECG_URL", GAME + "/")
    seen, threads = Seen(), []

    def credit(key, label):
        return intecg.credit(key, label, post=lambda url, json, timeout: seen.append((url, json)) or Answer())
    earned = main.boosters.earned
    monkeypatch.setattr(main.boosters, "credit", credit)
    monkeypatch.setattr(main.boosters, "earned", lambda *a, **k: threads.append(earned(*a, **k)) or threads[-1])
    seen.wait = lambda: [t.join(5) for t in threads if t]  # la demande part en tâche de fond
    main.notifications.path.unlink(missing_ok=True)
    yield seen
    main.notifications.path.unlink(missing_ok=True)


def test_sans_adresse_rien_n_est_envoye(monkeypatch):
    def no_post(*_, **__):
        raise AssertionError("aucun appel attendu")
    for value in ("", "localhost:5173", "javascript:alert(1)"):
        monkeypatch.setenv("INTECG_URL", value)
        assert intecg.url() == "" and intecg.credit("dag:1", "DAG lancé", post=no_post) is None
        assert intecg.Boosters(credit=no_post).earned("dag", 1, WHEN, "DAG lancé") is None


@pytest.mark.parametrize("answer", [Answer(404, {}), Answer(400, {"error": "Clé d'événement invalide"}), Answer(200, [])])
def test_jeu_arrete_ou_reponse_inattendue_ne_casse_rien(monkeypatch, answer):
    monkeypatch.setenv("INTECG_URL", GAME)

    def down(*_, **__):
        raise OSError("connexion refusée")
    assert intecg.credit("dag:1", "DAG lancé", post=down) is None
    assert intecg.credit("dag:1", "DAG lancé", post=lambda *_, **__: answer) is None


def test_booster_gagne_annonce_une_fois_et_refus_passe_sous_silence(monkeypatch, tmp_path):
    monkeypatch.setenv("INTECG_URL", GAME)
    notes = NotificationStore(tmp_path / "notifications.json")
    answers = iter([{"credited": True, "message": "Booster bonus crédité.", "bonusBoosters": 3},
                    {"credited": False, "message": "Plafond du jour atteint (5 boosters Jarvis).", "bonusBoosters": 3}])
    boosters = intecg.Boosters(notes, credit=lambda key, label: next(answers))
    boosters.earned("dag", 1, WHEN, "DAG lancé", "Client Démo").join(5)
    boosters.earned("dag", 2, WHEN, "DAG lancé", "Client Démo").join(5)
    [note] = notes.all()
    assert (note.key, note.kind, note.title, note.link, note.client) == (
        "intecg:dag:1:20261127103000", "dag", "InteCG · booster gagné (DAG lancé)", GAME, "Client Démo")
    assert "3 booster(s) bonus" in note.detail


def test_un_dag_lance_demande_un_booster(game, fake_airflow):
    result, watch = main._launch_watched("etl_demo", "integration", main.fiches.get_deployment("client-demo"))
    assert result.ok and watch
    game.wait()
    [(url, body)] = game
    assert url == GAME + "/api/credits/jarvis" and body["motif"] == "DAG lancé"
    assert body["cle"].startswith(f"dag:{watch.id}:") and intecg_key(body["cle"])
    assert any(n.key == f"intecg:{body['cle']}" for n in main.notifications.all())


def test_un_agent_lance_demande_un_booster(game):
    r = main.claude.launch(main.fiches.get_deployment("client-demo"), "visit_histo")
    assert r.ok
    game.wait()
    assert [(body["motif"], body["cle"].startswith(f"agent:{main.claude.runs[0].id}:"), intecg_key(body["cle"])) for _, body in game] == [
        ("Agent lancé", True, True)]


def test_un_lancement_refuse_ne_demande_rien(game):
    client = TestClient(main.app)  # Airflow non configuré : rien n'est lancé
    client.post("/actions/injection/client-demo", data={"dag_id": "etl_demo", "padoa_env": "integration"})
    game.wait()
    assert game == []


def intecg_key(key: str) -> bool:
    """Forme acceptée par InteCG (server/dbPlugin.ts, JARVIS_KEY_RE)."""
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,59}", key))
