"""Dernière synchro d'un client : vérification sans changement comprise, affichée près du bouton et sur sa carte."""
import json
from html import unescape
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from deploy_hub import main, sync_notion
from deploy_hub.main import when_text
from deploy_hub.models import Deployment
from deploy_hub.sources import watchers
from deploy_hub.sources.fiches import FicheSource


def test_when_text_reads_like_a_person():
    today = datetime.combine(date.today(), datetime.min.time()).replace(hour=14, minute=4)
    assert when_text(today) == "aujourd'hui à 14:04"
    assert when_text(today - timedelta(days=1)) == "hier à 14:04"
    old = today.replace(year=today.year - 1)
    assert when_text(old) == f"le {old:%d/%m/%Y} à 14:04" and when_text(None) == ""


def test_last_sync_is_the_latest_of_writing_and_checking():
    d = Deployment(slug="acme", client="Acme", software="Pulse", synced_at="10/11/2026 09:00", checked_at="17/11/2026 14:04")
    assert d.last_sync == datetime(2026, 11, 17, 14, 4)
    assert Deployment(slug="b", client="B", software="", synced_at="", checked_at="").last_sync is None
    assert Deployment(slug="c", client="C", software="", synced_at="pas une date").last_sync is None
    assert watchers.last_sync(d) == d.last_sync  # l'alerte « synchro en retard » suit la même date


def test_a_check_without_change_is_recorded_and_shown_on_the_fiche(tmp_path):
    sync_notion.mark_checked(["acme"], tmp_path)
    seen = json.loads((tmp_path / "data" / "notion" / "verifications.json").read_text(encoding="utf-8"))
    assert datetime.strptime(seen["acme"], "%d/%m/%Y %H:%M").date() == date.today()
    fiche = tmp_path / "data" / "deploiements" / "acme.md"
    fiche.parent.mkdir(parents=True)
    fiche.write_text("---\nclient: Acme\nsynced_at: 01/01/2026 09:00\n---\n", encoding="utf-8")
    d = FicheSource(fiche.parent).get_deployment("acme")
    assert d.checked_at == seen["acme"] and d.last_sync.date() == date.today()  # vérifié aujourd'hui, même sans réécriture


def test_fiche_and_card_show_the_last_sync(monkeypatch):
    now = datetime.now().replace(second=0, microsecond=0)
    d = Deployment(slug="acme", client="Acme", software="Pulse", synced_at=now.strftime("%d/%m/%Y %H:%M"))
    monkeypatch.setattr(main.fiches, "list_deployments", lambda: [d])
    monkeypatch.setattr(main.fiches, "get_deployment", lambda slug: d if slug == "acme" else None)
    client = TestClient(main.app)
    fiche = unescape(client.get("/deploiements/acme/synthese").text)
    assert f"Dernière synchro : aujourd'hui à {now:%H:%M}" in fiche
    assert fiche.index("Dernière synchro") < fiche.index('hx-post="/actions/rafraichir/acme"')  # à côté du bouton
    assert f"Dernière synchro : aujourd'hui à {now:%H:%M}" in unescape(client.get("/clients").text)  # sur la carte du client
    d.synced_at = ""
    assert "Jamais synchronisé" in client.get("/deploiements/acme/synthese").text
