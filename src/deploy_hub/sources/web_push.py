"""Notifications du Mac même sans onglet Jarvis ouvert (Web Push), tout installé côté serveur (la VM).

Le navigateur s'abonne une fois (service worker static/sw.js) ; Jarvis pousse ensuite chaque nouvelle notification au
service de push du navigateur (Google pour Chrome, Apple pour Safari), qui la remet au Mac même onglet fermé et tunnel coupé.
Le contenu est chiffré de bout en bout (RFC 8291) : le service de push ne voit qu'un message illisible.

Fichiers, dans data/push/ (exclu de git) : vapid.pem (clé privée du serveur, créée au premier lancement),
abonnements.json (un par navigateur), etat.json (dernière notification poussée).
"""

import base64
import json
import logging
import threading
from pathlib import Path

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from deploy_hub.sources.notifications import Notification, NotificationStore

# Contact exigé par VAPID (RFC 8292) : une URL https sans chemin (py_vapid refuse les chemins), pas besoin d'adresse e-mail.
CONTACT = "https://github.com"
MAX_PER_ROUND = 5  # au-delà, seules les plus récentes partent : pas de rafale après une longue coupure
TTL = 12 * 3600  # une notification non remise en 12 h (Mac éteint) est abandonnée
GONE = (404, 410)  # abonnement expiré ou révoqué côté navigateur : on l'oublie

log = logging.getLogger("uvicorn.error")
_lock = threading.Lock()


def _read(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class WebPush:
    def __init__(self, folder: Path, send=webpush):
        self.folder = folder
        self.key_path = folder / "vapid.pem"
        self.subs_path = folder / "abonnements.json"
        self.state_path = folder / "etat.json"
        self._send = send  # remplaçable dans les tests

    def _vapid(self) -> Vapid:
        with _lock:
            if not self.key_path.is_file():
                self.folder.mkdir(parents=True, exist_ok=True)
                vapid = Vapid()
                vapid.generate_keys()
                vapid.save_key(str(self.key_path))
                self.key_path.chmod(0o600)
            return Vapid.from_file(str(self.key_path))

    def public_key(self) -> str:
        """Clé publique du serveur (applicationServerKey du navigateur), en base64url sans remplissage."""
        raw = self._vapid().public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def subscriptions(self) -> list[dict]:
        return _read(self.subs_path, [])

    def subscribe(self, sub: dict) -> None:
        if not isinstance(sub, dict) or not isinstance(sub.get("keys"), dict) or not str(sub.get("endpoint") or "").startswith("https://"):
            raise ValueError("abonnement incomplet (adresse https du service de push et clés attendues)")
        with _lock:
            subs = [s for s in self.subscriptions() if s["endpoint"] != sub["endpoint"]]
            _write(self.subs_path, subs + [{"endpoint": sub["endpoint"], "keys": sub["keys"]}])

    def unsubscribe(self, endpoint: str) -> None:
        with _lock:
            _write(self.subs_path, [s for s in self.subscriptions() if s["endpoint"] != endpoint])

    def send(self, note: Notification) -> int:
        """Pousse une notification à chaque navigateur abonné ; renvoie le nombre de remises acceptées."""
        payload = json.dumps({"title": note.title, "body": note.detail, "tag": note.key, "link": note.link,
                              "urgent": note.level == "urgent", "id": note.id}, ensure_ascii=False)
        vapid, ok, gone = self._vapid(), 0, []
        for sub in self.subscriptions():
            try:
                self._send(subscription_info=sub, data=payload, vapid_private_key=vapid,
                           vapid_claims={"sub": CONTACT}, ttl=TTL, timeout=10)
                ok += 1
            except WebPushException as e:
                status = getattr(e.response, "status_code", None)
                if status in GONE:
                    gone.append(sub["endpoint"])
                log.warning("Web Push refusé (%s) : %s", status, str(e)[:200])
            except Exception as e:  # réseau coupé, service de push injoignable : on retentera au tour suivant
                log.warning("Web Push impossible : %s", str(e)[:200])
        for endpoint in gone:
            self.unsubscribe(endpoint)
        return ok

    def pump(self, store: NotificationStore) -> list[Notification]:
        """Pousse les notifications arrivées depuis le dernier tour. Au tout premier tour, mémorise sans rien pousser."""
        latest = max((n.id for n in store.all()), default=0)
        state = _read(self.state_path, {})
        if "last_id" not in state or not self.subscriptions():
            _write(self.state_path, {"last_id": latest})  # sans abonné, rien à rattraper plus tard
            return []
        fresh = sorted(store.after(state["last_id"]), key=lambda n: n.id)[-MAX_PER_ROUND:]
        for note in fresh:
            self.send(note)
        _write(self.state_path, {"last_id": max([latest, *(n.id for n in fresh)])})  # une notification arrivée pendant le tour ne repart pas
        return fresh
