"""Requêtes Notion d'un client générées depuis les modèles communs (sync/requetes/) et sa configuration."""

import pytest

from deploy_hub import queries
from deploy_hub.queries import ConfigError, page_id_of, render

PAGE = "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize("value", [
    PAGE, "01234567-89ab-cdef-0123-456789abcdef", f"https://app.notion.com/p/{PAGE}",
    f"https://app.notion.com/p/padoa/Deploiement-Acme-{PAGE}?pvs=4", f"https://www.notion.so/{PAGE.upper()}",
])
def test_page_id_from_id_or_url(value):
    assert page_id_of(value) == PAGE


def test_page_id_required():
    with pytest.raises(ConfigError):
        page_id_of("pas un id")


def test_every_query_targets_the_client_page():
    out = render({"page_id": PAGE})
    assert set(out) == set(queries.NAMES)
    assert all(PAGE in sql and "{{" not in sql for sql in out.values())
    assert "OR \"Client\" IN" not in out["recette"]


def test_extra_recette_clients_are_quoted():
    out = render({"page_id": PAGE, "recette_clients": ["acme_pulse", "o'neil"]})
    assert "   OR \"Client\" IN ('acme_pulse', 'o''neil')" in out["recette"]
    assert "OR \"Client\" IN ('acme_pulse', 'o''neil'))" in out["recette-delta"]  # dans la parenthèse du WHERE
    assert "OR \"Client\"" not in out["actions"]


def test_write_puts_queries_in_the_client_data_dir(tmp_path):
    out = queries.write("acme", {"page_id": PAGE}, root=tmp_path)
    assert out == tmp_path / "data" / "notion" / "acme" / "requetes"
    assert sorted(p.stem for p in out.glob("*.sql")) == sorted(queries.NAMES)
