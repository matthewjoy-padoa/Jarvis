"""Heure de Jarvis : Europe/Paris par défaut, même sur un serveur en UTC (la VM)."""
import os
import time
from datetime import datetime, timezone

from deploy_hub import config


def test_jarvis_is_on_french_time_whatever_the_server_clock(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    monkeypatch.delenv("JARVIS_TZ", raising=False)
    assert config.apply_timezone() == "Europe/Paris" and os.environ["TZ"] == "Europe/Paris"
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc).astimezone()
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc).astimezone()
    assert (winter.hour, summer.hour) == (13, 14)  # heure d'hiver UTC+1, heure d'été UTC+2


def test_another_timezone_can_be_chosen_and_a_wrong_one_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_TZ", "America/Montreal")
    assert config.apply_timezone() == "America/Montreal"
    assert datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc).astimezone().hour == 7
    monkeypatch.setenv("JARVIS_TZ", "Pas/Un_Fuseau")
    assert config.apply_timezone() == "Europe/Paris"
    monkeypatch.delenv("JARVIS_TZ")
    config.apply_timezone()  # les autres tests restent à l'heure française
