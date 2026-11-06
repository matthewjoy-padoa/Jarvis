"""Sources de données et d'actions. Chaque source a un mode démo ; le mode live reste à brancher."""

from dataclasses import dataclass


@dataclass
class ActionResult:
    ok: bool
    message: str
    simulated: bool = True
