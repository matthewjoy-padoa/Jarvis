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
    assert "à jour</td>" in sources and "<code>v0.2.0</code> (commit <code>1111111</code>)" in sources
    newer[0], disk[0] = "2222222bbbb", "0.3.0"  # git pull d'une PR fusionnée : code et numéro de version ont changé
    main._check_restart()
    main._check_restart()
    for page in ("/", "/sources", "/reglages"):
        text = app.get(page).text
        assert "<strong>Jarvis est à relancer.</strong> Son dossier est passé à <code>v0.3.0</code>, mais le serveur tourne encore sur <code>v0.2.0</code>" in text
    assert "à relancer</td>" in app.get("/sources").text
    notes = [n for n in main.notifications.all() if n.key == "relance:2222222"]
    assert len(notes) == 1 and notes[0].level == "urgent" and "v0.3.0" in notes[0].detail  # une fois par commit déployé
    newer[0], disk[0] = "3333333cccc", "0.2.0"  # code changé sans numéro monté : les commits, faute de mieux
    main._check_restart()
    assert "passé à <code>3333333</code>, mais le serveur tourne encore sur <code>1111111</code>" in app.get("/").text
    newer[0] = ""  # retour au commit de lancement
    main._check_restart()
    assert "Jarvis est à relancer" not in app.get("/").text
    assert "_check_restart" in __import__("inspect").getsource(main._push_round)  # revu à chaque tour de la boucle de fond
