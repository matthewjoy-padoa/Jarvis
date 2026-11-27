"""InteCG, le jeu de cartes à collectionner de l'équipe (dépôt intecg, à part) : un DAG ou un agent lancé dans Jarvis
a une chance de rapporter un booster bonus au joueur de cette machine.

Jarvis écrit lui-même dans la base du jeu, sans qu'aucun jeu ne tourne : cette base est faite de tables CSV en ajout
seul, versionnées dans le dépôt du jeu, où chaque joueur pousse ses lignes. Jarvis en garde un clone à lui
(data/intecg/depot, cloné au premier lancement avec le gh de la machine), y ajoute une ligne de credits.csv au nom du
compte GitHub de la machine, commite et pousse. Le booster apparaît dans le jeu du joueur, où qu'il tourne, à sa
synchro suivante.

Les règles sont celles du jeu (server/dbPlugin.ts, jarvisCredit), à garder accordées : un lancement n'est tiré qu'une
fois, et toujours avec le même résultat (tirage calculé sur le pseudo et la clé) ; la chance et le maximum du jour
sont lus dans src/game/config.ts du clone (JARVIS_DROP_CHANCE, JARVIS_DAILY_MAX). Seul un joueur déjà inscrit dans le
jeu est crédité. Tout part en tâche de fond et n'échoue jamais : un dépôt injoignable ne doit ni ralentir ni empêcher
un lancement. INTECG_REPO=aucun (Réglages) coupe les boosters.

Le lien InteCG du pied de page, lui, ne vaut que si le jeu est sur cette machine : il le démarre s'il ne tourne pas
(start), dépôt mis à jour en avance rapide, paquets npm installés s'ils manquent ou ont changé, puis « npm run dev »,
détaché, comme le fait Flow Digger. Jeu déjà lancé : Jarvis y emmène sans toucher à son dépôt.
"""

import csv
import hashlib
import io
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from deploy_hub.config import child_environment, data_dir
from deploy_hub.sources import flow_digger
from deploy_hub.sources.flow_digger import Launch

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
        return value if value.startswith(("http://", "https://")) else ""
    return DEFAULT_URL if folder() else ""


def game_url() -> str:
    """Où le jeu s'ouvre dans le navigateur de la personne : INTECG_URL si c'est une adresse http(s), sinon celle de
    « npm run dev » sur son poste."""
    value = os.getenv("INTECG_URL", "").strip().rstrip("/")
    return value if value.startswith(("http://", "https://")) else DEFAULT_URL


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


# ---------------------------------------------------------------- Boosters : écriture dans la base git du jeu
DEFAULT_REPO = "ThanhHuyPadoa/intecg"  # dépôt du jeu (GitHub) ; INTECG_REPO pour un autre, « aucun » pour couper les boosters
OFF = "aucun"
COUNTED = "Événement déjà compté."
LOST = "Pas de booster cette fois."
REASON = "Jarvis"  # début du motif des crédits Jarvis (JARVIS_REASON du jeu) : « Jarvis · DAG lancé · dag:12:… »
RULES = (0.05, 5)  # chance par lancement et maximum par jour, si src/game/config.ts du clone ne les dit pas
PSEUDO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,59}")  # forme d'une clé d'événement acceptée par le jeu
GIT_TIMEOUT = 60  # s, par commande git
PUSH_TRIES = 3  # un autre joueur a poussé entre-temps : on repart de la base à jour
RETRY_AFTER = timedelta(hours=1)  # dépôt injoignable (pas d'accès, réseau) : pas de nouvel essai à chaque lancement
RULES_FRESH = timedelta(hours=6)  # règles du clone jugées à jour : un tirage perdu n'a alors pas besoin de relire le dépôt
_writing = threading.Lock()  # un crédit à la fois dans le clone
_pseudo = ""  # compte GitHub de la machine, lu une fois
_down_until: datetime | None = None
_fetched: datetime | None = None  # dernière mise à jour du clone par ce processus


def repo() -> str:
    """Dépôt du jeu : « propriétaire/nom » sur GitHub, ou une adresse git. Vide : boosters coupés (INTECG_REPO=aucun)."""
    value = os.getenv("INTECG_REPO", "").strip()
    return "" if value.lower() == OFF or value.startswith("-") else value or DEFAULT_REPO  # « -… » serait lu comme une option de git


def base_dir() -> Path:
    """Clone du jeu que Jarvis tient pour lui seul : jamais celui où le jeu tourne (ils se gêneraient dans git)."""
    return data_dir() / "intecg" / "depot"


class Unavailable(Exception):
    """La base du jeu n'a pas pu être lue ou écrite : pas de booster cette fois, le lancement n'en souffre pas."""


def _run(command: list[str], cwd: Path | None = None) -> str:
    env = {**child_environment(), "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": os.getenv("GIT_SSH_COMMAND") or "ssh -o BatchMode=yes"}
    try:
        done = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unavailable(f"{command[0]} {command[1]} : {e}") from e
    if done.returncode:
        lines = [line for line in (done.stderr or done.stdout).strip().splitlines() if line.strip()]
        raise Unavailable(f"{command[0]} {command[1]} : {(lines[-1] if lines else done.returncode)}"[:300])
    return done.stdout.strip()


def _git(folder: Path, *args: str) -> str:
    return _run(["git", "-C", str(folder), *args])


def pseudo() -> str:
    """Le joueur : compte GitHub de la machine, trouvé comme le fait le jeu (INTECG_USER, sinon gh, sinon git)."""
    global _pseudo
    if not _pseudo:
        for find in (lambda: os.getenv("INTECG_USER", ""), lambda: _run(["gh", "api", "user", "--jq", ".login"]),
                     lambda: _run(["git", "config", "--global", "github.user"])):
            try:
                value = find().strip()
            except Unavailable:
                continue
            if PSEUDO.fullmatch(value):
                _pseudo = value
                break
        else:
            raise Unavailable("compte GitHub introuvable : « gh auth login » sur la machine de Jarvis, ou INTECG_USER")
    return _pseudo


def _fresh(folder: Path, source: str) -> None:
    """Clone à jour avec la branche main du dépôt du jeu, sans rien de local (ce clone n'appartient qu'à Jarvis)."""
    marker = folder.parent / "depot.source"  # dépôt d'où vient le clone : changé dans Réglages, on reclone
    if (folder / ".git").exists() and (not marker.is_file() or marker.read_text(encoding="utf-8") != source):
        shutil.rmtree(folder)
    if not (folder / ".git").exists():
        folder.parent.mkdir(parents=True, exist_ok=True)
        github = re.fullmatch(r"[\w.-]+/[\w.-]+", source) and not Path(source).exists()
        _run(["gh", "repo", "clone", source, str(folder), "--", "--quiet"] if github else ["git", "clone", "--quiet", source, str(folder)])
        marker.write_text(source, encoding="utf-8")
    else:
        _git(folder, "fetch", "--quiet", "origin", "main")
    _git(folder, "reset", "--quiet", "--hard", "origin/main")


def _table(folder: Path, name: str) -> list[dict]:
    path = folder / "database" / name
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8")))) if path.is_file() else []


def rules(folder: Path) -> tuple[float, int]:
    """(chance par lancement, maximum par jour) du jeu, lus dans son src/game/config.ts ; RULES s'ils n'y sont pas."""
    try:
        text = (folder / "src" / "game" / "config.ts").read_text(encoding="utf-8")
    except OSError:
        return RULES
    def read(name: str, pattern: str, convert, default):
        found = re.search(rf"^export const {name}\s*=\s*({pattern})\s*;", text, re.M)
        try:
            return convert(found.group(1)) if found else default
        except ValueError:
            return default
    # Valeur absente ou mal écrite dans le jeu : celle par défaut, plutôt que plus aucun booster.
    return read("JARVIS_DROP_CHANCE", r"[0-9.]+", float, RULES[0]), read("JARVIS_DAILY_MAX", r"\d+", int, RULES[1])


def roll(player: str, key: str) -> float:
    """Tirage d'un événement, de 0 (inclus) à 1 (exclu) : le même que jarvisRoll du jeu, toujours identique pour un
    joueur et une clé (relancer la demande ne retente pas sa chance)."""
    return int.from_bytes(hashlib.sha256(f"{player}\n{key}".encode()).digest()[:4], "big") / 2 ** 32


def _day(iso: str) -> str:
    """Jour d'une date de la base (ISO, UTC), à l'heure de Jarvis."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().date().isoformat()
    except ValueError:
        return ""


def decide(folder: Path, player: str, key: str, now: datetime) -> str:
    """Pourquoi ce lancement ne rapporte rien, "" s'il rapporte un booster. Mêmes règles, dans le même ordre, que le jeu."""
    if not any(row.get("pseudo") == player for row in _table(folder, "utilisateurs.csv")):
        return f"{player} n'est pas encore inscrit dans InteCG (ouvrir le jeu une première fois)."
    mine = [row for row in _table(folder, "credits.csv") if row.get("pseudo") == player and (row.get("motif") or "").startswith(f"{REASON} · ")]
    chance, most = rules(folder)
    if any(row["motif"].endswith(f" · {key}") for row in mine):
        return COUNTED
    if roll(player, key) >= chance:
        return LOST
    if sum(_day(row.get("date") or "") == now.astimezone().date().isoformat() for row in mine) >= most:
        return f"Maximum du jour atteint ({most} booster(s) Jarvis par jour)."
    return ""


def _bonus_left(folder: Path, player: str) -> int:
    """Boosters bonus à ouvrir : crédités moins ouverts, comme le compte le jeu."""
    def number(value) -> int:
        return max(0, int(value)) if str(value).strip().lstrip("-").isdigit() else 0
    granted = sum(number(row.get("nombre")) for row in _table(folder, "credits.csv") if row.get("pseudo") == player)
    used = sum(row.get("pseudo") == player and row.get("source") == "bonus" for row in _table(folder, "ouvertures.csv"))
    return max(0, granted - used)


def _append(folder: Path, player: str, key: str, label: str, now: datetime) -> None:
    """Une ligne de plus dans credits.csv, au format du jeu (une ligne par crédit, valeur entre guillemets au besoin)."""
    path = folder / "database" / "credits.csv"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    if not text.strip():
        text = ""
        writer.writerow(["pseudo", "date", "nombre", "motif"])
    stamp = now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    writer.writerow([player, stamp, "1", f"{REASON} · {label} · {key}"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + ("" if not text or text.endswith("\n") else "\n") + out.getvalue(), encoding="utf-8")


def credit(key: str, label: str, now: datetime | None = None) -> dict | None:
    """Tente le booster de l'événement `key` (« dag:12:20261127103000 ») de libellé `label` (« DAG lancé ») : s'il est
    gagné, l'écrit dans la base du jeu et le pousse. Renvoie {credited, message, bonusBoosters}, None si les boosters
    sont coupés ou la base injoignable (nouvel essai au plus tôt une heure après)."""
    global _down_until, _fetched
    source, now = repo(), now or datetime.now().astimezone()
    if not source or not KEY.fullmatch(key) or (_down_until and now < _down_until):
        return None
    folder = base_dir()
    with _writing:
        try:
            player = pseudo()
            # La plupart des lancements perdent leur tirage : avec des règles relues depuis peu, pas besoin d'aller
            # chercher la base pour le savoir (une clé perdante n'a jamais pu être comptée).
            if _fetched and now - _fetched < RULES_FRESH and roll(player, key) >= rules(folder)[0]:
                log.info(f"InteCG : {label} · {LOST}")
                return {"credited": False, "message": LOST, "bonusBoosters": _bonus_left(folder, player)}
            pushed = False
            for _ in range(PUSH_TRIES):
                _fresh(folder, source)
                _fetched = now
                refused = decide(folder, player, key, now)
                if pushed and refused == COUNTED:
                    refused = ""  # le push d'avant avait abouti malgré son erreur (délai dépassé) : la ligne est bien dans la base
                elif not refused:
                    _append(folder, player, key, label, now)
                    _git(folder, "add", "--", "database/credits.csv")  # table peut-être nouvelle dans le dépôt
                    _git(folder, "-c", f"user.name={player}", "-c", f"user.email={player}@users.noreply.github.com",
                         "commit", "--quiet", "-m", f"db({player}): booster Jarvis", "--", "database/credits.csv")
                    pushed = True
                    try:
                        _git(folder, "push", "--quiet", "origin", "HEAD:main")
                    except Unavailable:
                        continue  # la base a bougé entre-temps (ou le push n'a pas répondu) : on repart d'elle
                answer = {"credited": not refused, "message": refused or "Booster bonus crédité.", "bonusBoosters": _bonus_left(folder, player)}
                break
            else:
                raise Unavailable(f"base du jeu non poussée après {PUSH_TRIES} essais")
        except (Unavailable, OSError, ValueError) as e:
            _down_until = now + RETRY_AFTER
            log.warning(f"InteCG : pas de booster pour « {label} », nouvel essai dans une heure au plus tôt ({e})")
            return None
    _down_until = None
    log.info(f"InteCG : {label} · {answer['message']}")
    return answer


class Boosters:
    """Tente le booster en tâche de fond et annonce celui qui est gagné dans les notifications de Jarvis."""

    def __init__(self, notifications=None, credit=credit):
        self.notifications, self.credit = notifications, credit

    def earned(self, kind: str, number: int, started: datetime, label: str, client: str = "") -> threading.Thread | None:
        """Un lancement vient d'avoir lieu : kind (dag ou agent, aussi l'icône de la notification), son numéro dans
        Jarvis et son heure, qui font la clé de l'événement (« dag:12:20261127103000 », unique même si la numérotation
        de Jarvis repart de 1). Renvoie la tâche lancée."""
        if not repo():
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
                                   f"{left} booster(s) bonus à ouvrir dans le jeu." if left else "", game_url(), client)
