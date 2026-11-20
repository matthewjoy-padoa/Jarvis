"""Écriture sûre des fichiers (texte et JSON) : un échec ou un arrêt pendant l'écriture laisse l'ancien fichier intact."""

import json
import os
import stat
from datetime import datetime

import pytest

from deploy_hub import safe_write
from deploy_hub.safe_write import write_json, write_text


def test_writes_json_with_accents_and_creates_the_folder(tmp_path):
    path = tmp_path / "data" / "notion" / "état.json"
    write_json(path, {"client": "Prévéam", "quand": datetime(2026, 11, 20, 9, 30)}, default=datetime.isoformat)
    assert json.loads(path.read_text(encoding="utf-8")) == {"client": "Prévéam", "quand": "2026-11-20T09:30:00"}
    assert "Prévéam" in path.read_text(encoding="utf-8")  # accents gardés, pas d'\\u00e9
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and list(path.parent.iterdir()) == [path]


def test_options_are_passed_to_json(tmp_path):
    write_json(tmp_path / "a.json", {"b": 1, "a": 2}, indent=1, sort_keys=True)
    assert (tmp_path / "a.json").read_text(encoding="utf-8") == '{\n "a": 2,\n "b": 1\n}'


def test_a_failing_write_keeps_the_old_file_and_leaves_no_temporary(tmp_path):
    path = tmp_path / "notifications.json"
    write_json(path, [{"id": 1}])
    with pytest.raises(TypeError):  # contenu impossible à écrire en JSON : échec au milieu de l'écriture
        write_json(path, [{"id": 2, "objet": object()}])
    assert json.loads(path.read_text()) == [{"id": 1}] and list(tmp_path.iterdir()) == [path]


def test_an_interruption_before_the_swap_keeps_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "agents.json"
    write_json(path, ["ancien"])

    def stopped(src, dst):  # Jarvis arrêté entre l'écriture du temporaire et le remplacement
        raise KeyboardInterrupt
    monkeypatch.setattr(safe_write.os, "replace", stopped)
    with pytest.raises(KeyboardInterrupt):
        write_json(path, ["nouveau"])
    monkeypatch.undo()
    assert json.loads(path.read_text()) == ["ancien"] and list(tmp_path.iterdir()) == [path]
    assert os.path.exists(path)


def test_text_files_are_written_safely_too(tmp_path):
    path = tmp_path / "config" / "clients" / "acme.yaml"
    write_text(path, "client: Acmé\n")
    with pytest.raises(TypeError):
        write_text(path, None)  # échec pendant l'écriture : l'ancien contenu reste
    assert path.read_text(encoding="utf-8") == "client: Acmé\n" and list(path.parent.iterdir()) == [path]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
