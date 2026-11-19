"""Un problème de réglage cité dans un message mène au champ concerné de la page Réglages."""

from fastapi.testclient import TestClient

from deploy_hub import env_file, main
from deploy_hub.sources.pulls import PullRequests


def test_settings_named_in_a_message_become_links_to_their_field():
    html = main.settings_links("Clone introuvable : renseigne ORCHESTRATOR_DIR dans Réglages ; connecte 1Password.")
    assert '<a href="/reglages#reglage-ORCHESTRATOR_DIR">ORCHESTRATOR_DIR</a>' in html
    assert '<a href="/reglages#1password">1Password</a>' in html


def test_the_longest_setting_name_wins_and_text_stays_escaped():
    html = main.settings_links("JARVIS_AGENT_MAX_TURNS <b>x</b>")
    assert '<a href="/reglages#reglage-JARVIS_AGENT_MAX_TURNS">JARVIS_AGENT_MAX_TURNS</a>' in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>" not in html


def test_every_setting_has_an_anchor_on_the_settings_page():
    page = TestClient(main.app).get("/reglages", headers={"origin": "http://testserver"}).text
    for key in env_file.BY_KEY:
        assert f'id="reglage-{key}"' in page, key


def test_pr_tab_names_the_missing_clone_and_its_setting(tmp_path):
    _, error = PullRequests(tmp_path / "absent").all(fresh=True)  # vrai gh : le dossier de travail manque
    assert error.startswith("Clone de integration-orchestrator introuvable") and "ORCHESTRATOR_DIR" in error


def test_pr_tab_names_a_missing_gh_when_the_clone_exists(tmp_path):
    def no_gh(*args, **kwargs):
        raise FileNotFoundError("gh")
    _, error = PullRequests(tmp_path, run=no_gh).all(fresh=True)
    assert error.startswith("`gh` (GitHub CLI) introuvable") and "ORCHESTRATOR_DIR" not in error
