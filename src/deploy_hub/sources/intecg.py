"""InteCG, le jeu de cartes à collectionner de l'équipe (dépôt intecg, à part) : un DAG ou un agent lancé dans Jarvis
rapporte un booster bonus au joueur de cette machine.

Jarvis ne fait que prévenir le serveur du jeu (POST <adresse>/api/credits/jarvis, {cle, motif}) : c'est InteCG qui
décide (tirage au sort à chaque lancement), qui ne compte une clé qu'une fois et qui plafonne les boosters du jour. L'appel part en tâche de fond et
n'échoue jamais : un jeu arrêté ne doit ni ralentir ni empêcher un lancement.

Le lien InteCG du pied de page démarre le jeu s'il ne tourne pas (start) : dépôt mis à jour en avance rapide, paquets
npm installés s'ils manquent ou ont changé, puis « npm run dev », détaché, comme le fait Flow Digger. Jeu déjà lancé :
Jarvis y emmène sans toucher à son dépôt (le jeu commite et pousse sa base lui-même, et se met à jour au passage).

Sans dépôt du jeu sur la machine ni INTECG_URL (Réglages), il n'y a ni lien ni booster.
"""

import logging
import os
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from deploy_hub.config import child_environment, data_dir
from deploy_hub.sources import flow_digger
from deploy_hub.sources.flow_digger import Launch

TIMEOUT_S = 60  # le jeu commite et pousse sa base (git) avant de répondre
CANDIDATES = ("~/Dev/Apps/intecg", "~/Dev/intecg", "~/intecg")
DEFAULT_URL = "http://localhost:5173"  # port par défaut de « npm run dev » (Vite)
INSTALL_TIMEOUT = 300  # s, « npm install »
log = logging.getLogger("uvicorn.error")
_starting = threading.Lock()  # deux clics rapprochés : le second attend le premier au lieu de lancer un second serveur


def folder() -> Path | None:
    """Dossier du dépôt du jeu : INTECG_DIR, sinon le premier emplacement habituel qui contient package.json."""
    paths = [os.environ["INTECG_DIR"]] if os.getenv("INTECG_DIR") else CANDIDATES
    return next((p for p in (Path(c).expanduser() for c in paths) if (p / "package.json").is_file()), None)


def url() -> str:
    """Adresse du jeu, vue depuis la machine de Jarvis comme depuis le navigateur : INTECG_URL, sinon celle de
    « npm run dev » si le dépôt du jeu est sur la machine. Vide : pas de lien avec InteCG."""
    value = os.getenv("INTECG_URL", "").strip().rstrip("/")
    if value:
        return value if value.startswith(("http://", "https://")) and _readable(value) else ""
    return DEFAULT_URL if folder() else ""


def _readable(address: str) -> bool:
    """Hôte et port lisibles : « http://localhost:abc » passe le contrôle de Réglages, mais pas urlsplit (ValueError)."""
    try:
        urlsplit(address).port
    except ValueError:
        return False
    return True


def _address() -> tuple[str, int]:
    parts = urlsplit(url() or DEFAULT_URL)
    return parts.hostname or "localhost", parts.port or (443 if parts.scheme == "https" else 80)


def running() -> bool:
    try:
        with socket.create_connection(_address(), timeout=0.5):  # « localhost » : Vite n'écoute parfois qu'en IPv6
            return True
    except OSError:
        return False


def _stale_packages(repo: Path) -> bool:
    """Paquets npm absents, ou package-lock.json changé depuis leur installation (une mise à jour en a ajouté)."""
    installed, lock = repo / "node_modules" / ".package-lock.json", repo / "package-lock.json"
    return not installed.is_file() or (lock.is_file() and lock.stat().st_mtime > installed.stat().st_mtime)


def start(wait: float = 40, force: bool = False) -> Launch:
    """Démarre le jeu s'il ne répond pas et attend qu'il écoute ; ne lance jamais un second serveur. Avant de le
    démarrer, met son dépôt à jour (avance rapide seulement) ; si la mise à jour échoue, ne lance rien sauf avec
    force=True (version déjà présente)."""
    if running():
        return Launch(url=url())
    with _starting:
        return _start(wait, force)


def _start(wait: float, force: bool) -> Launch:
    if running():
        return Launch(url=url())  # lancé pendant qu'on attendait le verrou
    repo = folder()
    if not repo:
        return Launch(error="InteCG introuvable : clone le dépôt intecg sur la machine de Jarvis (par exemple dans "
                            "~/Dev/Apps/intecg) ou indique son dossier dans INTECG_DIR (Réglages).")
    updated = ""
    if not force:
        ok, updated = flow_digger.update(repo)
        if not ok:
            return Launch(error=f"Mise à jour d'InteCG impossible, il n'a pas été lancé. {updated}", can_force=True)
    env, journal = child_environment(), data_dir() / "intecg.log"
    journal.parent.mkdir(parents=True, exist_ok=True)
    try:
        with journal.open("ab") as out:  # détaché : survit à un redémarrage de Jarvis
            note = f"mis à jour {updated}" if updated else ("sans mise à jour (lancé quand même)" if force else "déjà à jour")
            out.write(f"--- Jarvis : démarrage d'InteCG, {note} ---\n".encode())
            out.flush()
            if _stale_packages(repo):
                install = subprocess.run(["npm", "install", "--no-audit", "--no-fund"], cwd=repo, env=env, stdin=subprocess.DEVNULL,
                                         stdout=out, stderr=subprocess.STDOUT, timeout=INSTALL_TIMEOUT)
                if install.returncode:
                    return Launch(error=f"« npm install » a échoué dans {repo} : son journal est dans {journal}.")
            subprocess.Popen(["npm", "run", "dev", "--", "--port", str(_address()[1]), "--strictPort"], cwd=repo, env=env,
                             stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    except subprocess.TimeoutExpired:
        return Launch(error=f"« npm install » n'a pas fini en {INSTALL_TIMEOUT} s : son journal est dans {journal}.")
    except OSError as e:
        return Launch(error=f"Impossible de lancer InteCG ({e.strerror or e}) : Node et npm sont-ils installés sur la machine de Jarvis ?")
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if running():
            return Launch(url=url())
        time.sleep(0.3)
    return Launch(error=f"InteCG ne répond pas sur le port {_address()[1]} après {wait:.0f} s : son journal est dans {journal}.")


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
