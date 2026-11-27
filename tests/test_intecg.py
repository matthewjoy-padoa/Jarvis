"""Lien avec InteCG (jeu de cartes de l'équipe) : un DAG ou un agent lancé demande un booster au serveur du jeu.
Aucun appel réseau : le POST est remplacé par un faux."""

import os
import socket
import time
from datetime import datetime
from types import SimpleNamespace

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


# ------------------------------------------------------------ Bouton InteCG : mise à jour, démarrage, ouverture
@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Faux dépôt du jeu (package.json, paquets installés), port libre où rien n'écoute, aucune commande lancée."""
    folder = tmp_path / "intecg"
    (folder / "node_modules").mkdir(parents=True)
    (folder / "package.json").write_text("{}")
    (folder / "node_modules" / ".package-lock.json").write_text("{}")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("INTECG_DIR", str(folder))
    monkeypatch.setenv("INTECG_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(intecg, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(intecg.flow_digger, "update", lambda repo: (True, ""))
    game = SimpleNamespace(path=folder, port=port, ran=[])
    monkeypatch.setattr(intecg.subprocess, "run", lambda cmd, **k: game.ran.append(cmd) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(intecg.subprocess, "Popen", lambda cmd, **k: game.ran.append(cmd))
    return game


def test_adresse_par_defaut_seulement_si_le_jeu_est_sur_la_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("INTECG_URL", "")
    monkeypatch.setenv("INTECG_DIR", str(tmp_path))
    assert intecg.folder() is None and intecg.url() == ""
    (tmp_path / "package.json").write_text("{}")
    assert intecg.folder() == tmp_path and intecg.url() == intecg.DEFAULT_URL
    monkeypatch.setenv("INTECG_URL", "http://vm:5174/")
    assert intecg.url() == "http://vm:5174" and intecg._address() == ("vm", 5174)


@pytest.mark.parametrize("address", ["http://localhost:abc", "http://localhost:99999", "http://[::1"])
def test_adresse_illisible_pas_de_lien_ni_d_erreur(monkeypatch, address):
    """Port ou hôte mal saisi dans Réglages (qui ne vérifie que « http:// ») : comme sans adresse, ni lien ni booster,
    au lieu d'une erreur 500 au clic sur le lien InteCG."""
    def refused(*a, **k):
        raise ConnectionRefusedError  # aucun jeu à l'écoute, quoi qui tourne sur la machine du développeur
    monkeypatch.setenv("INTECG_URL", address)
    monkeypatch.setattr(intecg.socket, "create_connection", refused)
    assert intecg.url() == "" and intecg.running() is False
    client = TestClient(main.app)
    assert "/outils/intecg" not in client.get("/").text
    assert client.post("/outils/intecg", headers={"origin": "http://testserver"}).status_code == 200  # page « introuvable », pas 500


def test_jeu_deja_lance_on_y_emmene_sans_rien_lancer(repo):
    with socket.socket() as listening:
        listening.bind(("127.0.0.1", repo.port))
        listening.listen()
        assert intecg.start() == intecg.Launch(url=f"http://127.0.0.1:{repo.port}")
    assert repo.ran == []


def test_jeu_arrete_demarre_sur_son_port_apres_mise_a_jour(repo, monkeypatch):
    seen = []
    monkeypatch.setattr(intecg.flow_digger, "update", lambda folder: seen.append(folder) or (True, "a1b2c3d → e4f5a6b"))
    launch = intecg.start(wait=0.1)
    assert seen == [repo.path] and repo.ran == [["npm", "run", "dev", "--", "--port", str(repo.port), "--strictPort"]]
    assert "ne répond pas" in launch.error  # rien n'écoute : le faux lancement ne démarre rien
    assert "mis à jour a1b2c3d → e4f5a6b" in (repo.path.parent / "data" / "intecg.log").read_text()


def test_paquets_installes_s_ils_manquent_ou_si_le_verrou_a_change(repo):
    (repo.path / "node_modules" / ".package-lock.json").unlink()
    intecg.start(wait=0)
    assert [cmd[:2] for cmd in repo.ran] == [["npm", "install"], ["npm", "run"]]
    repo.ran.clear()
    (repo.path / "node_modules" / ".package-lock.json").write_text("{}")
    (repo.path / "package-lock.json").write_text("{}")
    os.utime(repo.path / "package-lock.json", (time.time() + 60,) * 2)
    intecg.start(wait=0)
    assert [cmd[:2] for cmd in repo.ran] == [["npm", "install"], ["npm", "run"]]


def test_mise_a_jour_impossible_rien_n_est_lance_sauf_si_on_force(repo, monkeypatch):
    monkeypatch.setattr(intecg.flow_digger, "update", lambda folder: (False, "git fetch a échoué : réseau"))
    launch = intecg.start(wait=0)
    assert launch.can_force and "git fetch a échoué" in launch.error and repo.ran == []
    intecg.start(wait=0, force=True)
    assert [cmd[:3] for cmd in repo.ran] == [["npm", "run", "dev"]]


def test_installation_en_echec_ou_depot_absent_dit_quoi_faire(repo, monkeypatch):
    (repo.path / "node_modules" / ".package-lock.json").unlink()
    monkeypatch.setattr(intecg.subprocess, "run", lambda cmd, **k: SimpleNamespace(returncode=1))
    assert "npm install" in intecg.start(wait=0).error and repo.ran == []
    (repo.path / "package.json").unlink()
    assert "INTECG_DIR" in intecg.start(wait=0).error


def test_bouton_de_la_barre_et_route(repo):
    client = TestClient(main.app)
    page = client.get("/").text
    assert 'action="/outils/intecg"' in page and "/static/booster.js" in page
    assert client.post("/outils/intecg").status_code == 403  # pas depuis une page de Jarvis
    with socket.socket() as listening:
        listening.bind(("127.0.0.1", repo.port))
        listening.listen()
        r = client.post("/outils/intecg", headers={"origin": "http://testserver"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, f"http://127.0.0.1:{repo.port}")
    assert client.get("/outils/intecg", follow_redirects=False).headers["location"] == "/"


def test_sans_jeu_ni_adresse_pas_de_bouton(monkeypatch):
    page = TestClient(main.app).get("/").text
    assert "/outils/intecg" not in page and "booster.js" not in page
