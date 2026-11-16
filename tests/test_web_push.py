"""Web Push : clé du serveur, abonnements, envoi chiffré, tours d'envoi, routes."""

import base64
import json
import os

import http_ece
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
from pywebpush import WebPushException

from deploy_hub import main
from deploy_hub.sources.notifications import NotificationStore
from deploy_hub.sources.web_push import CONTACT, MAX_PER_ROUND, WebPush

b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "x", "auth": "y"}}


class Recorder:
    """Remplace pywebpush.webpush : garde les envois ; status > 0 simule un refus du service de push."""

    def __init__(self, status=0):
        self.calls, self.status = [], status

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.status:
            raise WebPushException("refusé", response=type("R", (), {"status_code": self.status})())


@pytest.fixture
def store(tmp_path):
    return NotificationStore(tmp_path / "notifications.json")


def test_server_key_is_created_once_and_private(tmp_path):
    push = WebPush(tmp_path / "push")
    key = push.public_key()
    assert len(base64.urlsafe_b64decode(key + "==")) == 65  # point P-256 non compressé, attendu par le navigateur
    assert push.public_key() == key
    assert oct(push.key_path.stat().st_mode & 0o777) == "0o600"


def test_subscriptions_are_deduplicated_by_endpoint(tmp_path):
    push = WebPush(tmp_path / "push")
    push.subscribe(SUB)
    push.subscribe({**SUB, "keys": {"p256dh": "x2", "auth": "y2"}})
    assert push.subscriptions() == [{**SUB, "keys": {"p256dh": "x2", "auth": "y2"}}]
    with pytest.raises(ValueError):
        push.subscribe({"endpoint": "https://e"})
    with pytest.raises(ValueError):
        push.subscribe({**SUB, "endpoint": "http://ailleurs.test/x"})  # jamais d'envoi en clair
    push.unsubscribe(SUB["endpoint"])
    assert push.subscriptions() == []


def test_first_round_only_remembers_then_new_notes_are_pushed(tmp_path, store):
    sent = Recorder()
    push = WebPush(tmp_path / "push", send=sent)
    push.subscribe(SUB)
    store.add("ancienne", "sync", "info", "Déjà là")
    assert push.pump(store) == [] and sent.calls == []
    store.add("k1", "agent", "urgent", "Agent terminé", "PR ouverte", "/agents")
    assert [n.key for n in push.pump(store)] == ["k1"]
    call = sent.calls[0]
    assert call["subscription_info"] == SUB and call["vapid_claims"] == {"sub": CONTACT} and call["ttl"] > 0
    assert json.loads(call["data"]) == {"title": "Agent terminé", "body": "PR ouverte", "tag": "k1", "link": "/agents", "urgent": True, "id": 2}
    assert push.pump(store) == []  # rien de neuf


def test_no_subscriber_means_nothing_to_catch_up(tmp_path, store):
    sent = Recorder()
    push = WebPush(tmp_path / "push", send=sent)
    push.pump(store)
    store.add("k1", "sync", "info", "Pendant que personne n'était abonné")
    push.pump(store)
    push.subscribe(SUB)
    assert push.pump(store) == [] and sent.calls == []


def test_a_burst_is_capped_to_the_latest(tmp_path, store):
    sent = Recorder()
    push = WebPush(tmp_path / "push", send=sent)
    push.subscribe(SUB)
    push.pump(store)
    for i in range(MAX_PER_ROUND + 3):
        store.add(f"k{i}", "sync", "info", f"N{i}")
    assert [n.key for n in push.pump(store)] == [f"k{i}" for i in range(3, MAX_PER_ROUND + 3)]


def test_expired_subscription_is_forgotten(tmp_path, store):
    push = WebPush(tmp_path / "push", send=Recorder(status=410))
    push.subscribe(SUB)
    push.pump(store)
    store.add("k1", "sync", "info", "N")
    push.pump(store)
    assert push.subscriptions() == []


def test_other_refusal_keeps_the_subscription(tmp_path, store):
    push = WebPush(tmp_path / "push", send=Recorder(status=500))
    push.subscribe(SUB)
    push.pump(store)
    store.add("k1", "sync", "info", "N")
    push.pump(store)
    assert push.subscriptions() == [SUB]


def test_payload_is_end_to_end_encrypted(tmp_path, store):
    """Envoi réel par pywebpush (requête interceptée) : le corps est illisible, seul le navigateur abonné le déchiffre."""
    browser_key, auth = ec.generate_private_key(ec.SECP256R1()), os.urandom(16)
    sub = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc",
           "keys": {"p256dh": b64(browser_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)), "auth": b64(auth)}}
    seen = {}

    class Session:
        def post(self, url, data=None, headers=None, timeout=None):
            seen.update(url=url, data=data, headers=headers)
            return type("R", (), {"status_code": 201, "text": ""})()

    from pywebpush import webpush
    push = WebPush(tmp_path / "push", send=lambda **kw: webpush(**kw, requests_session=Session()))
    push.subscribe(sub)
    push.pump(store)
    store.add("k1", "deadline", "info", "Échéance du jour : Recette 2", "Client Démo")
    push.pump(store)
    assert seen["url"] == sub["endpoint"] and "Recette" not in seen["data"].decode("latin-1")
    assert seen["headers"]["authorization"].startswith("vapid t=")
    clear = json.loads(http_ece.decrypt(seen["data"], private_key=browser_key, auth_secret=auth, version="aes128gcm"))
    assert clear["title"] == "Échéance du jour : Recette 2" and clear["body"] == "Client Démo"


def test_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "web_push", WebPush(tmp_path / "push", send=Recorder()))
    client = TestClient(main.app)
    assert client.post("/notifications/push/abonnement", json=SUB).status_code == 403  # sans origine Jarvis : refusé
    assert client.post("/notifications/push/abonnement", json=SUB, headers={"origin": "https://ailleurs.test"}).status_code == 403
    assert main.web_push.subscriptions() == []
    client.headers["origin"] = "http://testserver"
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["content-type"].startswith("text/javascript") and "showNotification" in sw.text
    assert len(client.get("/notifications/push/cle").json()["key"]) == 87
    assert client.post("/notifications/push/abonnement", json={"endpoint": "x"}).status_code == 400
    assert client.post("/notifications/push/abonnement", json=SUB).json() == {"ok": True, "abonnes": 1}
    assert client.post("/notifications/push/test").json() == {"envoyees": 1, "abonnes": 1}
    client.post("/notifications/push/desabonnement", json={"endpoint": SUB["endpoint"]})
    assert main.web_push.subscriptions() == []
