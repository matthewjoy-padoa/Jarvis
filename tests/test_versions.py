"""Numéro de version : pyproject.toml, uv.lock et la dernière version publiée du CHANGELOG concordent. Les PR s'ajoutent
sous « À venir » sans toucher au numéro ; il monte quand une version, qui en regroupe plusieurs, est publiée."""
import re
import tomllib

from deploy_hub.config import PROJECT_ROOT
from deploy_hub.sources import running_code

PUBLISHED = re.compile(r"\[(0\.\d+\.\d+)\] · \d{4}-\d{2}-\d{2}")


def test_pyproject_lock_and_changelog_carry_the_same_version():
    version = running_code.version()
    assert re.fullmatch(r"0\.\d+\.\d+", version), "numérotation 0.X.Y : Jarvis ne passe jamais en v1"
    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = next(p["version"] for p in lock["package"] if p["name"] == "deploy-hub")
    assert locked == version, "numéro de pyproject.toml changé sans « uv lock »"
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = re.findall(r"^## (.+)$", changelog, re.M)
    published = [s for s in sections if s != "À venir"]
    assert "À venir" not in sections[1:], "une seule section « À venir », en tête"
    assert (m := PUBLISHED.fullmatch(published[0])) and m[1] == version, \
        "la dernière version publiée du CHANGELOG (« ## [0.X.Y] · AAAA-MM-JJ ») est celle de pyproject.toml"
