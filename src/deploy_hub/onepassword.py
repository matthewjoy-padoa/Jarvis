"""1Password CLI (op) pour Jarvis : lire une référence op://, se connecter depuis la page Réglages, savoir si on l'est.

Même méthode que Flow Digger (bouton Login) : le mot de passe passe à « op signin » par l'entrée standard, sans terminal,
et n'est jamais écrit ni journalisé. Différence : la session obtenue reste dans ce module, en mémoire, et n'est donnée
qu'aux commandes op de Jarvis ; elle n'entre pas dans l'environnement transmis aux agents, au shell ni à Flow Digger.
"""

import json
import os
import re
import shutil
import subprocess

TIMEOUT = 60
SESSION_LINE = re.compile(r'^export (OP_SESSION_\w+)="([^"]+)"', re.M)  # sortie de « op signin » pour bash
_session: dict[str, str] = {}  # OP_SESSION_<compte> → jeton de session, en mémoire seulement


def installed() -> bool:
    return shutil.which("op") is not None


def run(*args: str, stdin: str = "") -> subprocess.CompletedProcess:
    """Commande op, sans terminal (rien ne peut être demandé dans celui de Jarvis), avec la session de la page Réglages."""
    return subprocess.run(["op", *args], input=stdin, capture_output=True, text=True, timeout=TIMEOUT,
                          env={**os.environ, **_session, "SHELL": "/bin/bash"}, start_new_session=True)


def message(stderr: str) -> str:
    """Dernière ligne d'erreur de op, sans l'horodatage « [ERROR] 2026/11/13 09:21:36 »."""
    line = (stderr.strip().splitlines() or [""])[-1]
    return re.sub(r"^\[ERROR\]\s+\S+\s+\S+\s+", "", line)[:200]


def status() -> dict:
    """Connecté ou non, avec quel compte (adresse et e-mail : jamais un secret) ; comptes ajoutés à op sinon."""
    if not installed():
        return {"installed": False, "signed_in": False, "accounts": [], "error": "1Password CLI (op) absent de cette machine"}
    try:
        who = run("whoami", "--format", "json")
        if who.returncode == 0:
            data = json.loads(who.stdout or "{}")
            return {"installed": True, "signed_in": True, "url": data.get("url"), "email": data.get("email"), "accounts": [], "error": ""}
        listed = run("account", "list", "--format", "json")
        accounts = json.loads(listed.stdout or "[]") if listed.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return {"installed": True, "signed_in": False, "accounts": [], "error": f"op ne répond pas ({type(e).__name__})"}
    return {"installed": True, "signed_in": False, "accounts": [{"url": a.get("url"), "email": a.get("email")} for a in accounts],
            "error": "" if accounts else "aucun compte ajouté à op : « op account add » une fois dans un terminal de la machine"}


def signin(password: str) -> dict:
    """Connexion avec le mot de passe du compte (jamais gardé) ; la session reste en mémoire jusqu'à signout ou expiration."""
    if not installed():
        return status()
    if not password:
        return {**status(), "error": "mot de passe requis"}
    try:
        result = run("signin", stdin=password + "\n")
    except subprocess.TimeoutExpired:
        return {**status(), "error": f"op signin n'a pas répondu en {TIMEOUT} s"}
    sessions = SESSION_LINE.findall(result.stdout)
    if result.returncode or not sessions:
        return {**status(), "error": message(result.stderr) or "op signin a échoué"}
    _session.update(dict(sessions))
    return status()


def signout() -> dict:
    """Ferme la session ouverte depuis la page Réglages et l'oublie."""
    if installed() and _session:
        try:
            run("signout")
        except subprocess.TimeoutExpired:
            pass
    _session.clear()
    return status()
