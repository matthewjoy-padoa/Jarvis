"""État des sources externes : Notion (jeton NOTION_TOKEN, lecture directe par l'app) et Slack (pas encore connecté)."""

from dataclasses import dataclass


@dataclass
class ExternalStatus:
    name: str
    connected: bool
    hint: str


def external_statuses(notion_token: bool = False) -> list[ExternalStatus]:
    if notion_token:
        hint = "Jeton présent : bouton « Rafraîchir » actif"
    else:
        hint = "Intégration interne à créer, jeton NOTION_TOKEN à renseigner dans Réglages"
    return [
        ExternalStatus("Notion", notion_token, hint),
        ExternalStatus("Slack", False, "App bot à créer, canaux du déploiement à choisir"),
    ]
