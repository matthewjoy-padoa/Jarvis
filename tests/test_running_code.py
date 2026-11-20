"""« Jarvis est à relancer » : le dossier a été mis à jour par git pull, le serveur tourne encore sur l'ancien code."""
import subprocess

from fastapi.testclient import TestClient

from deploy_hub import main
from deploy_hub.sources import running_code

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


def test_every_page_says_jarvis_must_be_restarted_until_it_is(monkeypatch):
    newer = [""]
    monkeypatch.setattr(main, "_code_started", "1111111aaaa")
    monkeypatch.setitem(main.templates.env.globals, "jarvis_commit", "1111111")
    monkeypatch.setattr(main.running_code, "newer_code", lambda started: newer[0])
    monkeypatch.setattr(main, "_code_newer", "")
    main._check_restart()
    home = app.get("/").text
    assert "Jarvis est à relancer" not in home and 'title="Commit du code qui tourne">1111111<' in home
    assert "à jour" in app.get("/sources").text
    newer[0] = "2222222bbbb"  # git pull : le code de l'app a changé
    main._check_restart()
    main._check_restart()
    for page in ("/", "/sources", "/reglages"):
        text = app.get(page).text
        assert "<strong>Jarvis est à relancer.</strong>" in text and "<code>2222222</code>" in text and "<code>1111111</code>" in text
    assert "à relancer</td>" in app.get("/sources").text
    notes = [n for n in main.notifications.all() if n.key == "relance:2222222"]
    assert len(notes) == 1 and notes[0].level == "urgent" and "2222222" in notes[0].detail  # une fois par commit déployé
    newer[0] = ""  # retour au commit de lancement
    main._check_restart()
    assert "Jarvis est à relancer" not in app.get("/").text
    assert "_check_restart" in __import__("inspect").getsource(main._push_round)  # revu à chaque tour de la boucle de fond
