"""Écriture sûre des fichiers de Jarvis (data/ et config/ : notifications, annuaire, états des synchros, caches, agents,
suivis de DAG, fiches, configuration des clients, .env…).

Le contenu va d'abord dans un fichier temporaire au nom unique du même dossier, forcé sur le disque (fsync), puis remplace
l'ancien d'un bloc (os.replace) : un arrêt brutal pendant l'écriture (Jarvis tué, VM coupée) laisse l'ancien fichier intact
au lieu d'un fichier à moitié écrit, que Jarvis relirait comme vide ; un lecteur voit l'ancien contenu ou le nouveau, jamais
un mélange. Le fichier écrit n'est lisible que par son propriétaire (600), comme le reste des données.
"""
import json
import os
import tempfile
from pathlib import Path


def write_text(path: Path, text: str) -> None:
    """Écrit text (UTF-8) dans path, d'un bloc ; crée le dossier au besoin."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # créé en 600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json(path: Path, data, *, indent: int | None = None, sort_keys: bool = False, default=None) -> None:
    """Écrit data en JSON (accents gardés) dans path, d'un bloc. json.dumps en une fois : l'encodeur C, pas json.dump."""
    write_text(path, json.dumps(data, ensure_ascii=False, indent=indent, sort_keys=sort_keys, default=default))
