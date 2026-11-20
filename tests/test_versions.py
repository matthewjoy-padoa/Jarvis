"""Le numéro de version monte à chaque PR : pyproject.toml, uv.lock et la première section du CHANGELOG concordent."""
import re
import tomllib

from deploy_hub.config import PROJECT_ROOT
from deploy_hub.sources import running_code


def test_pyproject_lock_and_changelog_carry_the_same_version():
    version = running_code.version()
    assert re.fullmatch(r"0\.\d+\.\d+", version), "numérotation 0.X.Y : Jarvis ne passe jamais en v1"
    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = next(p["version"] for p in lock["package"] if p["name"] == "deploy-hub")
    assert locked == version, "numéro de pyproject.toml changé sans « uv lock »"
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    first = re.search(r"^## (.+)$", changelog, re.M).group(1)
    assert re.fullmatch(rf"\[{re.escape(version)}\] · \d{{4}}-\d{{2}}-\d{{2}}", first), \
        "chaque PR monte le numéro : la première section du CHANGELOG est « ## [<numéro de pyproject.toml>] · AAAA-MM-JJ »"
