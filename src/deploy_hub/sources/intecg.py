"""InteCG, le jeu de cartes à collectionner de l'équipe (dépôt intecg, à part) : un DAG ou un agent lancé dans Jarvis
rapporte un booster bonus au joueur de cette machine.

Jarvis ne fait que prévenir le serveur du jeu (POST <INTECG_URL>/api/credits/jarvis, {cle, motif}) : c'est InteCG qui
crédite, qui ne compte une clé qu'une fois et qui plafonne les boosters du jour. Sans INTECG_URL (Réglages), rien n'est
envoyé. L'appel part en tâche de fond et n'échoue jamais : un jeu arrêté ne doit ni ralentir ni empêcher un lancement.
"""

import logging
import os
import threading
from datetime import datetime

import httpx

TIMEOUT_S = 60  # le jeu commite et pousse sa base (git) avant de répondre
log = logging.getLogger("uvicorn.error")


def url() -> str:
    """Adresse du jeu vue depuis la machine de Jarvis (http://localhost:5173 sur la VM) ; vide : pas de lien."""
    value = os.getenv("INTECG_URL", "").strip().rstrip("/")
    return value if value.startswith(("http://", "https://")) else ""


def credit(key: str, label: str, post=httpx.post) -> dict | None:
    """Demande un booster pour l'événement `key` (« dag:12:20261127103000 ») de libellé `label` (« DAG lancé »).
    Renvoie la réponse du jeu ({credited, message, bonusBoosters}), None si le lien est coupé ou le jeu injoignable."""
    base = url()
    if not base:
        return None
    try:
        response = post(f"{base}/api/credits/jarvis", json={"cle": key, "motif": label}, timeout=TIMEOUT_S)
        answer = response.json()
        if response.status_code != 200 or not isinstance(answer, dict):
            raise ValueError((isinstance(answer, dict) and answer.get("error")) or f"réponse {response.status_code} inattendue")
    except Exception as e:  # noqa: BLE001 : jeu arrêté, route absente, réponse illisible… jamais bloquant
        log.info(f"InteCG : pas de booster pour « {label} » ({e})")
        return None
    log.info(f"InteCG : {label} · {answer.get('message', '')}")
    return answer


class Boosters:
    """Prévient InteCG en tâche de fond et annonce le booster gagné dans les notifications de Jarvis."""

    def __init__(self, notifications=None, credit=credit):
        self.notifications, self.credit = notifications, credit

    def earned(self, kind: str, number: int, started: datetime, label: str, client: str = "") -> threading.Thread | None:
        """Un lancement vient d'avoir lieu : kind (dag ou agent, aussi l'icône de la notification), son numéro dans
        Jarvis et son heure, qui font la clé de l'événement (« dag:12:20261127103000 », unique même si la numérotation
        de Jarvis repart de 1). Renvoie la tâche lancée."""
        if not url():
            return None
        key = f"{kind}:{number}:{started:%Y%m%d%H%M%S}"
        thread = threading.Thread(target=self._work, args=(key, label, kind, client), name=f"intecg-{key}", daemon=True)
        thread.start()
        return thread

    def _work(self, key: str, label: str, kind: str, client: str) -> None:
        answer = self.credit(key, label)
        if answer and answer.get("credited") and self.notifications:
            left = answer.get("bonusBoosters")
            self.notifications.add(f"intecg:{key}", kind, "info", f"InteCG · booster gagné ({label})",
                                   f"{left} booster(s) bonus à ouvrir dans le jeu." if left else "", url(), client)
