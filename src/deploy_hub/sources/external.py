"""État des sources externes : Notion (jeton NOTION_TOKEN, lecture directe par l'app) et Slack (pas encore connecté)."""

from dataclasses import dataclass


@dataclass
class ExternalStatus:
    name: str
    connected: bool
    hint: str


def external_statuses(notion_token: bool = False, notion_problem: str = "") -> list[ExternalStatus]:
    if notion_token:
        hint = "Jeton présent : bouton « Rafraîchir » actif"
    elif notion_problem:  # référence 1Password illisible (session op expirée, op absent…)
        hint = f"NOTION_TOKEN est une référence 1Password illisible : {notion_problem} (op signin, puis relance Jarvis)"
    else:
        hint = "Intégration interne à créer, jeton NOTION_TOKEN à mettre dans .env (voir README)"
    return [
        ExternalStatus("Notion", notion_token, hint),
        ExternalStatus("Slack", False, "App bot à créer, canaux du déploiement à choisir"),
    ]
