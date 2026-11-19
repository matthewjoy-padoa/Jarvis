"""Texte Markdown écrit par un agent (compte rendu, questions) mis en forme dans les pages.

CommonMark sans HTML brut : une balise écrite par l'agent est affichée telle quelle, jamais interprétée, et les liens
« javascript: » sont refusés par markdown-it. Les retours à la ligne simples sont gardés, comme dans le texte de l'agent.
"""

from functools import lru_cache

from markdown_it import MarkdownIt
from markupsafe import Markup

_md = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")


@lru_cache(maxsize=256)  # un compte rendu ne change plus une fois écrit : rendu une fois
def _render(text: str) -> str:
    return _md.render(text)


def md(text: str | None) -> Markup:
    """Bloc : paragraphes, listes, code, tableaux."""
    return Markup(_render(text or ""))


def md_inline(text: str | None) -> Markup:
    """Une ligne (titre d'une question) : gras, code, liens, sans paragraphe autour."""
    return Markup(_md.renderInline(text or ""))
