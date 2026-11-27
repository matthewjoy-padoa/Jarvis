"""Lien avec InteCG (jeu de cartes de l'équipe) : un DAG ou un agent lancé tente un booster, écrit par Jarvis dans la
base git du jeu ; lien du pied de page qui démarre le jeu s'il est sur la machine.
Aucun réseau : le « dépôt du jeu » est un dépôt git local, créé pour le test."""

import os
import socket
import subprocess
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from deploy_hub import main
from deploy_hub.sources import intecg
from deploy_hub.sources.notifications import NotificationStore

WHEN = datetime(2026, 11, 27, 10, 30).astimezone()
CONFIG = "export const JARVIS_DROP_CHANCE = {chance};\nexport const JARVIS_DAILY_MAX = {most};\nexport const JARVIS_REASON = \"Jarvis\";\n"


def git(folder, *args) -> str:
    return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, check=True).stdout.strip()


def keys(player: str, chance: float, won: bool, count: int = 1) -> list[str]:
    """Clés d'événement dont le tirage est gagnant (ou perdant) pour ce joueur, à cette chance."""
    found = [f"dag:{i}:20261127103000" for i in range(2000) if (intecg.roll(player, f"dag:{i}:20261127103000") < chance) == won]
    return found[:count]


@pytest.fixture
def game(tmp_path, monkeypatch):
    """Dépôt du jeu, local : jade y est inscrite, la chance est de 50 % et le maximum de 2 par jour. `game.seed` est un
    clone d'où l'on pousse comme un autre joueur ; `game.credits()` lit la table sur la branche main du dépôt."""
    origin, seed = tmp_path / "origin.git", tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True, capture_output=True)
    for name, value in (("user.name", "seed"), ("user.email", "seed@example.test")):
        git(seed, "config", name, value)
    (seed / "database").mkdir()
    (seed / "src" / "game").mkdir(parents=True)
    (seed / ".gitattributes").write_text("database/*.csv merge=union\n")
    (seed / "database" / "utilisateurs.csv").write_text("pseudo,inscrit_le\njade,2026-11-26T10:00:00.000Z\n")
    (seed / "database" / "credits.csv").write_text("pseudo,date,nombre,motif\n")
    (seed / "database" / "ouvertures.csv").write_text("pseudo,date,jour,source,prix,cartes\n")
    (seed / "src" / "game" / "config.ts").write_text(CONFIG.format(chance=0.5, most=2))
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "init")
    git(seed, "push", "-q", "origin", "HEAD:main")
    monkeypatch.setenv("INTECG_REPO", str(origin))
    monkeypatch.setenv("INTECG_USER", "jade")
    monkeypatch.setattr(intecg, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(intecg, "_pseudo", "")
    monkeypatch.setattr(intecg, "_down_until", None)
    monkeypatch.setattr(intecg, "_fetched", None)

    def push(path: str, text: str, message: str = "db(autre): ligne") -> None:
        git(seed, "pull", "-q", "origin", "main")
        with (seed / path).open("a") as f:
            f.write(text)
        git(seed, "commit", "-q", "-am", message)
        git(seed, "push", "-q", "origin", "HEAD:main")
    return SimpleNamespace(origin=origin, seed=seed, push=push, credits=lambda: git(origin, "show", "main:database/credits.csv").splitlines()[1:],
                           last=lambda: git(origin, "log", "-1", "--format=%s|%an", "main"))


# ------------------------------------------------------------ Boosters écrits dans la base du jeu
def test_tirage_identique_a_celui_du_jeu():
    # Valeurs de jarvisRoll (server/dbPlugin.ts du jeu) : sha256(« pseudo\\nclé »), quatre premiers octets / 2^32.
    assert intecg.roll("jade", "dag:12") == 0.49524494260549545
    assert intecg.roll("matthewjoy-padoa", "agent:7:20261127103000") == 0.12879474903456867


def test_lancement_gagnant_ecrit_une_ligne_au_nom_du_joueur_et_la_pousse(game):
    won, lost = keys("jade", 0.5, True)[0], keys("jade", 0.5, False)[0]
    assert intecg.credit(lost, "DAG lancé", WHEN) == {"credited": False, "message": "Pas de booster cette fois.", "bonusBoosters": 0}
    assert game.credits() == [] and game.last() == "init|seed"
    assert intecg.credit(won, "DAG lancé", WHEN) == {"credited": True, "message": "Booster bonus crédité.", "bonusBoosters": 1}
    assert game.credits() == [f"jade,2026-11-27T09:30:00.000Z,1,Jarvis · DAG lancé · {won}"]
    assert game.last() == "db(jade): booster Jarvis|jade"
    assert intecg.credit(won, "DAG lancé", WHEN + timedelta(hours=1)) == {"credited": False, "message": "Événement déjà compté.", "bonusBoosters": 1}
    assert len(game.credits()) == 1


def test_tirage_perdu_ne_relit_pas_le_depot_tant_que_les_regles_sont_recentes(game, monkeypatch):
    lost = keys("jade", 0.5, False, 3)
    assert not intecg.credit(lost[0], "DAG lancé", WHEN)["credited"]  # premier lancement : le dépôt est lu
    runs, run = [], intecg._run
    monkeypatch.setattr(intecg, "_run", lambda command, cwd=None: runs.append(command) or run(command, cwd))
    assert intecg.credit(lost[1], "DAG lancé", WHEN + timedelta(hours=1)) == {"credited": False, "message": "Pas de booster cette fois.", "bonusBoosters": 0}
    assert runs == []
    assert not intecg.credit(lost[2], "DAG lancé", WHEN + timedelta(hours=7))["credited"] and runs  # règles trop vieilles : relues
    runs.clear()
    assert intecg.credit(keys("jade", 0.5, True)[0], "DAG lancé", WHEN + timedelta(hours=8))["credited"] and runs  # gagnant : toujours la base à jour


def test_maximum_du_jour_et_boosters_bonus_deja_ouverts(game):
    first, second, third, tomorrow = keys("jade", 0.5, True, 4)
    assert intecg.credit(first, "DAG lancé", WHEN)["credited"] and intecg.credit(second, "Agent lancé", WHEN)["credited"]
    assert intecg.credit(third, "DAG lancé", WHEN) == {"credited": False, "message": "Maximum du jour atteint (2 booster(s) Jarvis par jour).", "bonusBoosters": 2}
    game.push("database/ouvertures.csv", "jade,2026-11-27T11:00:00.000Z,2026-11-27,bonus,0,carte-a\n")
    assert intecg.credit(tomorrow, "DAG lancé", WHEN + timedelta(days=1)) == {"credited": True, "message": "Booster bonus crédité.", "bonusBoosters": 2}
    assert len(game.credits()) == 3


def test_regles_lues_dans_le_jeu_sinon_celles_par_defaut(game, tmp_path):
    intecg.credit(keys("jade", 0.5, False)[0], "DAG lancé", WHEN)
    assert intecg.rules(intecg.base_dir()) == (0.5, 2)
    assert intecg.rules(tmp_path / "sans-config") == intecg.RULES == (0.05, 5)


def test_joueur_pas_inscrit_dans_le_jeu_rien_n_est_ecrit(game, monkeypatch):
    monkeypatch.setenv("INTECG_USER", "inconnu")
    key = keys("inconnu", 0.5, True)[0]
    answer = intecg.credit(key, "DAG lancé", WHEN)
    assert not answer["credited"] and "pas encore inscrit" in answer["message"] and game.credits() == []


def test_un_autre_joueur_pousse_entre_temps_les_deux_lignes_restent(game, monkeypatch):
    fresh, calls = intecg._fresh, []

    def fresh_then_someone_pushes(folder, source):
        fresh(folder, source)
        calls.append(1)
        if len(calls) == 1:  # après la lecture de Jarvis, avant son push
            game.push("database/credits.csv", "bob,2026-11-27T09:00:00.000Z,2,RDD livrée\n")
    monkeypatch.setattr(intecg, "_fresh", fresh_then_someone_pushes)
    won = keys("jade", 0.5, True)[0]
    assert intecg.credit(won, "DAG lancé", WHEN)["credited"] and len(calls) == 2
    assert game.credits() == ["bob,2026-11-27T09:00:00.000Z,2,RDD livrée", f"jade,2026-11-27T09:30:00.000Z,1,Jarvis · DAG lancé · {won}"]


def test_base_injoignable_pas_de_booster_et_pas_de_nouvel_essai_avant_une_heure(game, monkeypatch, tmp_path):
    monkeypatch.setenv("INTECG_REPO", str(tmp_path / "absent.git"))
    runs, run = [], intecg._run
    monkeypatch.setattr(intecg, "_run", lambda command, cwd=None: runs.append(command) or run(command, cwd))
    key = keys("jade", 0.5, True)[0]
    assert intecg.credit(key, "DAG lancé", WHEN) is None and len(runs) == 1
    assert intecg.credit(key, "DAG lancé", WHEN + timedelta(minutes=30)) is None and len(runs) == 1
    monkeypatch.setenv("INTECG_REPO", str(game.origin))
    assert intecg.credit(key, "DAG lancé", WHEN + timedelta(hours=2))["credited"]


def test_table_absente_du_depot_regle_illisible_et_depot_change(game, monkeypatch, tmp_path):
    git(game.seed, "rm", "-q", "database/credits.csv")
    (game.seed / "src" / "game" / "config.ts").write_text("export const JARVIS_DROP_CHANCE = 0.5.;\nexport const JARVIS_DAILY_MAX = 2;\n")
    git(game.seed, "commit", "-q", "-am", "table retirée, règle mal écrite")
    git(game.seed, "push", "-q", "origin", "HEAD:main")
    won = keys("jade", 0.05, True)[0]  # chance illisible : celle par défaut
    assert intecg.credit(won, "DAG lancé", WHEN)["credited"]
    assert game.credits() == [f"jade,2026-11-27T09:30:00.000Z,1,Jarvis · DAG lancé · {won}"]
    assert intecg.rules(intecg.base_dir()) == (0.05, 2)
    # Dépôt changé dans Réglages : le clone est refait depuis le nouveau.
    other = tmp_path / "autre.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(game.origin), str(other)], check=True, capture_output=True)
    monkeypatch.setenv("INTECG_REPO", str(other))
    second = keys("jade", 0.05, True, 2)[1]
    assert intecg.credit(second, "DAG lancé", WHEN)["credited"]
    assert len(git(other, "show", "main:database/credits.csv").splitlines()) == 3 and len(game.credits()) == 1


def test_push_abouti_malgre_son_erreur_le_booster_est_bien_annonce(game, monkeypatch):
    real = intecg._git

    def push_then_fail(folder, *args):
        out = real(folder, *args)
        if args[0] == "push":
            raise intecg.Unavailable("git push : délai dépassé")
        return out
    monkeypatch.setattr(intecg, "_git", push_then_fail)
    won = keys("jade", 0.5, True)[0]
    assert intecg.credit(won, "DAG lancé", WHEN) == {"credited": True, "message": "Booster bonus crédité.", "bonusBoosters": 1}
    assert len(game.credits()) == 1


def test_boosters_coupes_ou_cle_invalide_rien_n_est_tente(game, monkeypatch):
    def no_command(*_, **__):
        raise AssertionError("aucune commande attendue")
    monkeypatch.setattr(intecg, "_run", no_command)
    assert intecg.credit("clé invalide, x", "DAG lancé", WHEN) is None
    for value in ("aucun", "Aucun", "--upload-pack=x"):
        monkeypatch.setenv("INTECG_REPO", value)
        assert intecg.repo() == "" and intecg.credit("dag:1", "DAG lancé", WHEN) is None
        assert intecg.Boosters(credit=no_command).earned("dag", 1, WHEN, "DAG lancé") is None
    monkeypatch.setenv("INTECG_REPO", "")
    assert intecg.repo() == intecg.DEFAULT_REPO


def test_booster_gagne_annonce_une_fois_et_refus_passe_sous_silence(monkeypatch, tmp_path):
    monkeypatch.setenv("INTECG_REPO", "equipe/jeu")
    notes = NotificationStore(tmp_path / "notifications.json")
    answers = iter([{"credited": True, "message": "Booster bonus crédité.", "bonusBoosters": 3},
                    {"credited": False, "message": "Pas de booster cette fois.", "bonusBoosters": 3}, None])
    boosters = intecg.Boosters(notes, credit=lambda key, label: next(answers))
    for number in (1, 2, 3):
        boosters.earned("dag", number, WHEN, "DAG lancé", "Client Démo").join(5)
    [note] = notes.all()
    assert (note.key, note.kind, note.title, note.link, note.client) == (
        "intecg:dag:1:20261127103000", "dag", "InteCG · booster gagné (DAG lancé)", intecg.DEFAULT_URL, "Client Démo")
    assert "3 booster(s) bonus" in note.detail


@pytest.fixture
def tried(monkeypatch):
    """Boosters branchés, sans base : note chaque tentative (clé, libellé) faite par un lancement."""
    monkeypatch.setenv("INTECG_REPO", "equipe/jeu")
    seen, threads = [], []
    earned = main.boosters.earned
    monkeypatch.setattr(main.boosters, "credit", lambda key, label: seen.append((key, label)))
    monkeypatch.setattr(main.boosters, "earned", lambda *a, **k: threads.append(earned(*a, **k)) or threads[-1])
    return SimpleNamespace(seen=seen, wait=lambda: [t.join(5) for t in threads if t])  # la tentative part en tâche de fond


def test_un_dag_lance_tente_un_booster(tried, fake_airflow):
    result, watch = main._launch_watched("etl_demo", "integration", main.fiches.get_deployment("client-demo"))
    assert result.ok and watch
    tried.wait()
    [(key, label)] = tried.seen
    assert label == "DAG lancé" and key.startswith(f"dag:{watch.id}:") and intecg.KEY.fullmatch(key)


def test_un_agent_lance_tente_un_booster(tried):
    assert main.claude.launch(main.fiches.get_deployment("client-demo"), "visit_histo").ok
    tried.wait()
    [(key, label)] = tried.seen
    assert label == "Agent lancé" and key.startswith(f"agent:{main.claude.runs[0].id}:") and intecg.KEY.fullmatch(key)


def test_un_lancement_refuse_ne_tente_rien(tried):
    client = TestClient(main.app)  # Airflow non configuré : rien n'est lancé
    client.post("/actions/injection/client-demo", data={"dag_id": "etl_demo", "padoa_env": "integration"})
    tried.wait()
    assert tried.seen == []


def test_animation_chargee_quand_les_boosters_sont_branches_meme_sans_jeu_sur_la_machine(monkeypatch):
    assert "booster.js" not in TestClient(main.app).get("/").text  # INTECG_REPO=aucun dans les tests
    monkeypatch.setenv("INTECG_REPO", "equipe/jeu")
    page = TestClient(main.app).get("/").text
    assert 'booster.js?v=3" data-game="http://localhost:5173" defer' in page and "/outils/intecg" not in page
    monkeypatch.setenv("INTECG_URL", "http://localhost:5199")
    assert 'data-game="http://localhost:5199"' in TestClient(main.app).get("/").text
    monkeypatch.setenv("INTECG_URL", "javascript:alert(1)")
    assert 'data-game="http://localhost:5173"' in TestClient(main.app).get("/").text
    monkeypatch.setenv("INTECG_URL", "http://localhost:5199")
    sober = TestClient(main.app, cookies={"jarvis_affichage": "sobre"}).get("/").text  # vue classique : l'animation aussi
    assert 'data-game="http://localhost:5199"' in sober


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
    assert 'action="/outils/intecg"' in page and "booster.js" not in page  # le lien ne dépend pas des boosters, coupés ici
    sober = TestClient(main.app, cookies={"jarvis_affichage": "sobre"}).get("/").text  # vue classique : le lien aussi
    assert 'action="/outils/intecg"' in sober
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
