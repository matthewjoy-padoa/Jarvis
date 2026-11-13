"""Shell de la machine où tourne Jarvis (la VM), dans l'onglet « Shell » de la console : un pseudo-terminal relié par WebSocket.

Désactivé par défaut : JARVIS_SHELL=1 l'active, à ne mettre que sur la VM (décision de Matthew du 2026-11-12).
Garde-fous, tous exigés à l'ouverture :
- connexion locale seulement (le service écoute sur 127.0.0.1, joint depuis le Mac par transfert de port) ;
- origine de la page = l'hôte de Jarvis : une page d'un autre site ouverte dans le navigateur ne peut pas s'y connecter ;
- jeton tiré à chaque démarrage de Jarvis, glissé dans la page : un autre site ne peut pas le lire.
Le shell est celui de l'utilisateur du service (shell de connexion, dossier personnel), sans les secrets de Jarvis.
Fermer la page ou changer de page termine la session (SIGHUP au groupe de processus du shell).
"""

import asyncio
import fcntl
import json
import os
import pty
import pwd
import secrets
import signal
import struct
import subprocess
import sys
import termios
import threading
from urllib.parse import urlsplit

from fastapi import WebSocket, WebSocketDisconnect

from deploy_hub.config import child_environment

TOKEN = secrets.token_urlsafe(32)  # nouveau à chaque démarrage : une page restée ouverte d'avant doit être rechargée
LOCAL_HOSTS = {"127.0.0.1", "::1"}  # adresse de la connexion
# Nom d'hôte demandé (en-tête Host) : un site qui ferait pointer son propre nom vers 127.0.0.1 (DNS rebinding) aurait la même
# origine que l'hôte appelé, et pourrait lire le jeton de la page ; on n'accepte donc que les noms locaux.
LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}
REFUSED = 1008  # code WebSocket « policy violation »


def enabled() -> bool:
    return os.environ.get("JARVIS_SHELL") == "1"


def refusal(ws: WebSocket) -> str:
    """Raison du refus, ou "" si la connexion est acceptable."""
    if not enabled():
        return "Shell désactivé : JARVIS_SHELL=1 dans la configuration du service, sur la VM seulement."
    if not ws.client or ws.client.host not in LOCAL_HOSTS:
        return "Connexion refusée : le shell n'accepte que les connexions locales (transfert de port)."
    origin, host = ws.headers.get("origin", ""), ws.headers.get("host", "")
    if urlsplit(f"//{host}").hostname not in LOCAL_NAMES:
        return "Connexion refusée : Jarvis doit être ouvert par localhost (transfert de port)."
    if not origin or urlsplit(origin).netloc != host:
        return "Connexion refusée : la page ne vient pas de Jarvis."
    if not secrets.compare_digest(ws.query_params.get("token", ""), TOKEN):
        return "Connexion refusée : jeton absent ou périmé (recharge la page)."
    return ""


def _environment() -> dict[str, str]:
    env = {k: v for k, v in child_environment().items() if not k.startswith("JARVIS_SHELL")}
    # SHELL = le shell lancé ; LANG UTF-8 pour les caractères des thèmes (➜, ✗) si le service n'en a pas.
    return {**env, "TERM": "xterm-256color", "COLORTERM": "truecolor", "SHELL": login_shell(), "LANG": env.get("LANG") or "C.UTF-8"}


def _resize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(2, min(rows, 500)), max(2, min(cols, 1000)), 0, 0))


# Lanceur : la session (setsid) est créée par subprocess ; il prend le pseudo-terminal comme terminal de contrôle
# (Ctrl+C, Ctrl+Z, tâches de fond), puis devient le shell de connexion. Pas de fork maison : dans un serveur à threads,
# l'enfant d'un fork peut rester bloqué sur un verrou avant même son exec (constaté sur macOS, Python 3.14).
LAUNCHER = "import fcntl, os, sys, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execvp(sys.argv[1], [sys.argv[1], '-l'])"


def login_shell() -> str:
    """Le shell de connexion de l'utilisateur (/etc/passwd) : zsh avec Oh My Zsh sur la VM, quel que soit l'environnement du
    service (systemd ne fournit pas toujours $SHELL). JARVIS_SHELL_PROGRAM le remplace (tests)."""
    try:
        declared = pwd.getpwuid(os.getuid()).pw_shell
    except KeyError:
        declared = ""
    return os.environ.get("JARVIS_SHELL_PROGRAM") or declared or os.environ.get("SHELL") or "/bin/bash"


def spawn(cols: int = 100, rows: int = 30) -> tuple[subprocess.Popen, int]:
    """Lance le shell de connexion dans un pseudo-terminal ; renvoie (processus, descripteur du maître)."""
    master, slave = pty.openpty()
    _resize(master, cols, rows)
    try:
        proc = subprocess.Popen([sys.executable, "-c", LAUNCHER, login_shell()],
                                stdin=slave, stdout=slave, stderr=slave, cwd=os.path.expanduser("~"), env=_environment(),
                                start_new_session=True, close_fds=True)
    finally:
        os.close(slave)
    return proc, master


async def _end(proc: subprocess.Popen) -> None:
    """SIGHUP au groupe du shell (comme une fenêtre de terminal fermée), puis SIGKILL s'il traîne ; le processus est récolté."""
    for sig in (signal.SIGHUP, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        for _ in range(40):
            if proc.poll() is not None:
                return
            await asyncio.sleep(0.05)
    proc.poll()


async def bridge(ws: WebSocket) -> None:
    """Relie le WebSocket au pseudo-terminal : octets du shell → navigateur ; frappes et redimensionnements → shell."""
    reason = refusal(ws)
    if reason:
        await ws.close(code=REFUSED, reason=reason)
        return
    await ws.accept()
    proc, fd = spawn()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def read_pty() -> None:
        """Lecture bloquante dans un thread : kqueue (macOS) ne signale pas un pseudo-terminal prêt, epoll (Linux) si ;
        un thread marche partout. b"" quand le shell sort (exit, Ctrl+D) ou que la session est fermée."""
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            try:
                loop.call_soon_threadsafe(queue.put_nowait, data)
            except RuntimeError:  # boucle déjà fermée : la session est finie
                return
            if not data:
                return

    async def to_browser() -> None:
        while data := await queue.get():
            await ws.send_bytes(data)
        await ws.close(code=1000, reason="Session terminée.")

    threading.Thread(target=read_pty, name=f"shell-{proc.pid}", daemon=True).start()
    pump = asyncio.create_task(to_browser())
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("text"):
                event = json.loads(message["text"])
                if event.get("type") == "input":
                    os.write(fd, str(event.get("data", "")).encode())
                elif event.get("type") == "resize":
                    _resize(fd, int(event.get("cols", 100)), int(event.get("rows", 30)))
    except (WebSocketDisconnect, RuntimeError, OSError, ValueError):
        pass
    finally:
        pump.cancel()
        await _end(proc)
        os.close(fd)
