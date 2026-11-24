"""« Jarvis est à relancer » : le dossier a été mis à jour par git pull, le serveur tourne encore sur l'ancien code.
« Nouvelle version disponible » : publiée sur la branche suivie, vue par git fetch ; bouton « Mettre à jour et redémarrer »."""
import inspect
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient

from deploy_hub import main
from deploy_hub.config import START_ENVIRON
from deploy_hub.sources import running_code
from deploy_hub.sources.running_code import (
    published,  # la vraie : conftest la remplace dans les autres tests
)

app = TestClient(main.app)


def git(repo, *args):
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "-C", str(repo), *identity, *args], check=True, capture_output=True)


def commit(repo, path, text):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", path)
    return running_code.head(repo)


def test_the_code_is_newer_only_when_the_app_itself_changed(tmp_path):
    assert running_code.head(tmp_path) == "" and running_code.newer_code("abc", tmp_path) == ""  # pas un dépôt git
    git(tmp_path, "init", "-q")
    started = commit(tmp_path, "src/app.py", "v1")
    assert len(started) == 40 and running_code.newer_code(started, tmp_path) == ""  # même commit
    commit(tmp_path, "CHANGELOG.md", "une ligne")
    commit(tmp_path, "tests/test_app.py", "un test")
    assert running_code.newer_code(started, tmp_path) == ""  # ni le code ni les dépendances : rien à relancer
    deployed = commit(tmp_path, "src/app.py", "v2")
    assert running_code.newer_code(started, tmp_path) == deployed != started
    assert running_code.newer_code(deployed, tmp_path) == ""  # relancé : le processus a chargé ce commit
    locked = commit(tmp_path, "uv.lock", "dépendances")
    assert running_code.newer_code(deployed, tmp_path) == locked
    assert running_code.newer_code("", tmp_path) == ""  # commit de lancement inconnu : on ne dit rien


def test_git_missing_is_not_an_error(tmp_path, monkeypatch):
    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")
    monkeypatch.setattr(running_code.subprocess, "run", no_git)
    assert running_code.head(tmp_path) == "" and running_code.newer_code("abc", tmp_path) == ""


def test_the_version_is_read_from_pyproject_on_disk(tmp_path):
    assert running_code.version(tmp_path) == ""  # pas de pyproject.toml
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "deploy-hub"\nversion = "0.3.0"\n', encoding="utf-8")
    assert running_code.version(tmp_path) == "0.3.0"
    (tmp_path / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    assert running_code.version(tmp_path) == ""  # illisible


def test_every_page_says_jarvis_must_be_restarted_until_it_is(monkeypatch):
    newer, disk = [""], ["0.2.0"]
    monkeypatch.setattr(main, "_code_started", "1111111aaaa")
    monkeypatch.setattr(main, "_version_started", "0.2.0")
    monkeypatch.setitem(main.templates.env.globals, "jarvis_version", "0.2.0")
    monkeypatch.setitem(main.templates.env.globals, "jarvis_commit", "1111111")
    monkeypatch.setattr(main.running_code, "newer_code", lambda started: newer[0])
    monkeypatch.setattr(main.running_code, "version", lambda: disk[0])
    monkeypatch.setattr(main, "_restart", ())
    main._check_restart()
    home = app.get("/").text
    assert "Jarvis est à relancer" not in home and 'title="Historique des versions · commit 1111111">v0.2.0</a></footer>' in home
    sources = app.get("/sources").text
    assert "à jour</td>" in sources and "Version <code>v0.2.0</code> en service (commit <code>1111111</code>)" in sources
    newer[0], disk[0] = "2222222bbbb", "0.3.0"  # git pull d'une PR fusionnée : code et numéro de version ont changé
    main._check_restart()
    main._check_restart()
    for page in ("/", "/sources", "/reglages"):
        text = app.get(page).text
        assert "<strong>Jarvis est à relancer.</strong> La version <code>v0.3.0</code> est installée, mais c'est encore la <code>v0.2.0</code> qui tourne" in text
    assert "à relancer</td>" in app.get("/sources").text
    notes = [n for n in main.notifications.all() if n.key == "relance:2222222"]
    assert len(notes) == 1 and notes[0].level == "urgent" and "v0.3.0" in notes[0].detail  # une fois par commit déployé
    newer[0], disk[0] = "3333333cccc", "0.2.0"  # code changé sans numéro monté : les commits, faute de mieux
    main._check_restart()
    assert "La version <code>3333333</code> est installée, mais c'est encore la <code>1111111</code> qui tourne" in app.get("/").text
    newer[0] = ""  # retour au commit de lancement
    main._check_restart()
    assert "Jarvis est à relancer" not in app.get("/").text
    assert "_check_restart" in __import__("inspect").getsource(main._push_round)  # revu à chaque tour de la boucle de fond



@pytest.fixture
def clone(tmp_path):
    """Dépôt « GitHub » (nu) et clone qui le suit, comme le dossier de Jarvis sur la VM : (clone, publier(chemin, texte))."""
    remote, work, folder = tmp_path / "github.git", tmp_path / "auteur", tmp_path / "jarvis"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(tmp_path, "clone", "-q", str(remote), str(work))
    git(work, "checkout", "-q", "-b", "main")
    commit(work, "pyproject.toml", '[project]\nname = "deploy-hub"\nversion = "0.8.1"\n')
    git(work, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(remote), str(folder))

    def publish(path, text):
        made = commit(work, path, text)
        git(work, "push", "-q", "origin", "main")
        return made[:7]
    return folder, publish


def test_a_published_version_is_seen_without_git_pull(clone, tmp_path):
    folder, publish = clone
    assert published(folder) == running_code.Published()  # rien de neuf
    publish("CHANGELOG.md", "une ligne")
    assert published(folder).commit == ""  # ni le code ni les dépendances : rien à proposer
    publish("src/app.py", "v2")
    seen = publish("pyproject.toml", '[project]\nname = "deploy-hub"\nversion = "0.9.0"\n')
    assert published(folder) == running_code.Published(commit=seen, version="0.9.0")
    assert running_code.head(folder) != seen and not (folder / "src").exists()  # rien n'est tiré
    git(folder, "pull", "-q")
    assert published(folder) == running_code.Published()  # à jour
    git(folder, "remote", "set-url", "origin", str(tmp_path / "disparu.git"))
    publish("src/app.py", "v3")
    assert published(folder).error.startswith("git fetch a échoué")  # même fetch que Flow Digger (flow_digger.behind)


@pytest.fixture
def published_state(monkeypatch):
    monkeypatch.setattr(main, "_code_started", "1111111aaaa")
    monkeypatch.setattr(main, "_version_started", "0.8.1")
    monkeypatch.setattr(main, "_restart", ())
    monkeypatch.setattr(main, "_published", running_code.Published())
    found = [running_code.Published(commit="2222222aaaa", version="0.9.0")]
    monkeypatch.setattr(main.running_code, "published", lambda: found[0])
    return found


def test_the_published_version_is_looked_up_apart_every_ten_minutes(monkeypatch):
    started = []
    monkeypatch.setattr(main.threading, "Thread", lambda target, args, **k: type("T", (), {"start": lambda self: started.append(args)})())
    monkeypatch.setattr(main, "_published_checked", time.monotonic() - main.PUBLISHED_EVERY_S)
    main._check_published()
    main._check_published()  # moins de 10 minutes après : pas de nouveau git fetch
    assert started == [("Nouvelle version publiée", main._look_published)]  # à part : le réseau ne retarde pas la boucle
    assert "_check_published" in inspect.getsource(main._push_round)


def test_every_page_offers_the_published_version(published_state):
    main._look_published()
    main._look_published()
    for page in ("/", "/sources", "/reglages"):
        text = app.get(page).text
        assert "<strong>Nouvelle version de Jarvis disponible.</strong> La version <code>v0.9.0</code> est publiée, c'est encore la <code>v0.8.1</code> qui tourne." in text
        assert 'hx-post="/jarvis/mise-a-jour"' in text and "Mettre à jour et redémarrer" in text
    assert "nouvelle version</td>" in app.get("/sources").text
    notes = [n for n in main.notifications.all() if n.key == "publiee:2222222aaaa"]
    assert len(notes) == 1 and "v0.9.0" in notes[0].detail  # une fois par commit publié
    published_state[0] = running_code.Published(commit="3333333cccc", version="0.8.1")  # correction : numéro pas monté
    main._look_published()
    assert "La version <code>3333333</code> est publiée, c'est encore la <code>1111111</code>" in app.get("/").text
    published_state[0] = running_code.Published()
    main._look_published()
    assert "Nouvelle version de Jarvis" not in app.get("/").text


def test_after_a_git_pull_the_button_only_restarts(published_state, monkeypatch):
    monkeypatch.setattr(main, "_restart", ("v0.9.0", "v0.8.1"))
    text = app.get("/").text
    assert "Jarvis est à relancer." in text and ">Redémarrer</span>" in text and "Nouvelle version" not in text


@pytest.fixture
def update(monkeypatch):
    """Bouton « Mettre à jour et redémarrer » sans rien toucher : ce que la route a appelé, dans l'ordre."""
    calls = []
    monkeypatch.setattr(main.flow_digger, "update", lambda root: calls.append(("update", root)) or (True, "1111111 → 2222222"))
    monkeypatch.setattr(main.running_code, "sync_dependencies", lambda started: calls.append(("sync", started)) or "")
    monkeypatch.setattr(main.running_code, "reloads_itself", lambda: False)
    monkeypatch.setattr(main.running_code, "restart", lambda: calls.append("restart"))
    monkeypatch.setattr(main, "_restart_blockers", list)
    monkeypatch.setattr(main, "_published", running_code.Published(commit="2222222", version="0.9.0"))
    return calls


SAME = {"origin": "http://testserver"}


def test_the_button_updates_then_restarts_jarvis(update):
    r = app.post("/jarvis/mise-a-jour", headers=SAME)
    assert update == [("update", main.PROJECT_ROOT), ("sync", main._code_started), "restart"]
    assert f'data-wait-boot="{main._boot}"' in r.text and "Redémarrage de Jarvis" in r.text
    assert main._published == running_code.Published()  # le bandeau ne la propose plus pendant le redémarrage
    assert app.get("/jarvis/demarrage").json() == {"boot": main._boot}


def test_the_button_is_refused_from_another_site_or_while_work_is_running(update, monkeypatch):
    assert app.post("/jarvis/mise-a-jour", headers={"origin": "https://site-hostile.example"}).status_code == 403
    monkeypatch.setattr(main, "_restart_blockers", lambda: ["1 agent Claude en cours", "une synchro Notion"])
    r = app.post("/jarvis/mise-a-jour", headers=SAME)
    assert "Pas maintenant : 1 agent Claude en cours, une synchro Notion" in r.text and update == []


def test_a_second_click_waits_for_the_same_restart(update, monkeypatch):
    monkeypatch.setattr(main.running_code, "restarting", lambda: True)
    assert "data-wait-boot" in app.post("/jarvis/mise-a-jour", headers=SAME).text and update == []


def test_after_a_git_pull_the_button_only_restarts_without_fetching(update, monkeypatch):
    monkeypatch.setattr(main, "_restart", ("v0.9.0", "v0.8.1"))
    assert "data-wait-boot" in app.post("/jarvis/mise-a-jour", headers=SAME).text
    assert update == [("sync", main._code_started), "restart"]  # code déjà sur le disque : ni git fetch ni mise à jour


def test_a_failed_update_does_not_restart(update, monkeypatch):
    monkeypatch.setattr(main.flow_digger, "update", lambda root: (False, "mise à jour impossible sans fusion"))
    assert "Mise à jour impossible : mise à jour impossible sans fusion." in app.post("/jarvis/mise-a-jour", headers=SAME).text
    assert update == []
    monkeypatch.setattr(main.flow_digger, "update", lambda root: (True, ""))
    monkeypatch.setattr(main.running_code, "sync_dependencies", lambda started: "uv sync a échoué : réseau")
    assert "pas les dépendances (uv sync a échoué : réseau) : Jarvis n&#39;est pas redémarré" in app.post("/jarvis/mise-a-jour", headers=SAME).text
    assert update == []


def test_with_uvicorn_reload_the_update_is_enough(update, monkeypatch):
    monkeypatch.setattr(main.running_code, "reloads_itself", lambda: True)
    assert "data-wait-boot" in app.post("/jarvis/mise-a-jour", headers=SAME).text
    assert update == [("update", main.PROJECT_ROOT), ("sync", main._code_started)]


def test_restart_blockers_name_what_would_be_cut(monkeypatch):
    monkeypatch.setattr(main.claude, "runs", [type("Run", (), {"running": True, "simulated": False})()] * 2)
    monkeypatch.setattr(type(main.sync_routine), "running", property(lambda self: True))
    monkeypatch.setattr(type(main.client_directory), "running", property(lambda self: False))
    assert main._restart_blockers() == ["2 agents Claude en cours", "une synchro Notion"]


def test_dependencies_are_synced_only_when_they_changed(tmp_path, monkeypatch):
    uv, args = tmp_path / "uv", tmp_path / "args"
    uv.write_text(f"#!/bin/sh\necho \"$@\" > {args}\necho 'réseau coupé' >&2\nexit $CODE\n")
    uv.chmod(0o755)
    monkeypatch.setenv("UV", str(uv))
    monkeypatch.setenv("CODE", "0")
    monkeypatch.setattr(running_code.sys, "prefix", str(tmp_path / ".venv"))  # Python d'un environnement virtuel
    repo = tmp_path / "jarvis"
    git(tmp_path, "init", "-q", str(repo))
    started = commit(repo, "uv.lock", "v1")
    commit(repo, "src/app.py", "v2")
    assert running_code.sync_dependencies(started, repo) == "" and not args.exists()  # dépendances inchangées : rien
    commit(repo, "uv.lock", "v2")
    assert running_code.sync_dependencies(started, repo) == "" and args.read_text() == "sync --frozen --active\n"
    monkeypatch.setenv("CODE", "1")
    assert running_code.sync_dependencies(started, repo) == "uv sync a échoué : réseau coupé"
    monkeypatch.setattr(running_code.sys, "prefix", running_code.sys.base_prefix)  # Python du système : on n'y touche pas
    assert running_code.sync_dependencies(started, repo) == ""


def test_restart_replaces_the_process_with_the_same_command(monkeypatch):
    killed, execs = [], []
    monkeypatch.setattr(running_code.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(running_code.os, "execve", lambda *args: execs.append(args))
    running_code.exec_if_requested()
    assert execs == []  # arrêt ordinaire : rien
    try:
        running_code.restart(delay=0)
        for _ in range(100):
            if killed:
                break
            time.sleep(0.01)
        assert killed == [(os.getpid(), running_code.signal.SIGINT)] and running_code.restarting()  # comme un Ctrl-C
        running_code.exec_if_requested()
        assert execs == [(sys.executable, sys.orig_argv, START_ENVIRON)]
    finally:
        running_code._requested.clear()


APP = '''
import os, secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI
from deploy_hub.sources import running_code

BOOT = secrets.token_hex(4)

@asynccontextmanager
async def lifespan(app):
    yield
    running_code.exec_if_requested()

app = FastAPI(lifespan=lifespan)

@app.get("/boot")
def boot():
    return {"boot": BOOT, "pid": os.getpid()}

@app.post("/restart")
def restart():
    running_code.restart(0.2)
    return {}
'''


def test_a_real_uvicorn_restarts_itself_in_the_same_process(tmp_path):
    """Pour de vrai : uvicorn s'arrête proprement, se relance par exec (même PID, même port), et répond de nouveau."""
    (tmp_path / "petite_app.py").write_text(APP)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "petite_app:app", "--app-dir", str(tmp_path),
                                             "--port", str(port), "--log-level", "warning"])
    url = f"http://127.0.0.1:{port}"

    def boot(old=None):
        for _ in range(200):
            try:
                with urllib.request.urlopen(f"{url}/boot", timeout=1) as r:
                    seen = json.load(r)
                if seen["boot"] != old:
                    return seen
            except OSError:
                pass
            time.sleep(0.05)
        raise AssertionError("le serveur ne répond pas")
    try:
        first = boot()
        urllib.request.urlopen(urllib.request.Request(f"{url}/restart", method="POST"), timeout=5).read()
        second = boot(first["boot"])
        assert second["pid"] == first["pid"] == server.pid and server.poll() is None
    finally:
        server.terminate()
        server.wait(10)
