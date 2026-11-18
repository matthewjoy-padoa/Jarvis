"""Source du catalogue des DAGs : le clone orchestrator local (ORCHESTRATOR_DIR) tel qu'il est sur le disque, comme le
lit l'Airflow local (checkout en cours, modifications non commitées comprises). Pour changer de DAGs, on change de
branche ou on modifie le clone : Jarvis suit.

sync/dags/generer_catalogue.py lit le clone sans le modifier (PYTHONDONTWRITEBYTECODE : pas de __pycache__ écrit) et
écrit data/dags/catalogue.json, marqué de la version du clone ; sources/dags.py le lit avant le catalogue versionné
(sync/dags/catalogue.json, copie figée de main), qui sert sans clone ou tant que rien n'a été généré.

Version du clone = dernier commit + empreinte des modifications non commitées (toute modification compte : une
génération prend quelques secondes, en tâche de fond). Régénéré au démarrage, sur « Régénérer », et quand la version
change (relevé chaque minute par main._push_round) ; jamais pendant l'affichage d'une page, qui montre le dernier relevé.
Une version en échec n'est retentée que par « Régénérer ».
"""

import hashlib
import json
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from deploy_hub.config import PROJECT_ROOT, child_environment
from deploy_hub.sources import dags as dag_catalogue

GENERATOR = PROJECT_ROOT / "sync" / "dags" / "generer_catalogue.py"  # Python et paquets : son en-tête « /// script »
GENERATE_TIMEOUT_S = 15 * 60  # premier passage : uv télécharge Python 3.12 et les paquets


@dataclass(frozen=True)
class Checkout:
    label: str  # branche extraite, ou « HEAD détachée »
    version: str  # « hash date », plus « + modifs <empreinte> » s'il y a des modifications non commitées


def _git(repo: Path, *args: str) -> bytes | None:
    """Sortie de git (sans invite) ; None en cas d'échec, si git est absent ou trop lent."""
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=60, check=False,
                             stdin=subprocess.DEVNULL, env={**child_environment(), "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _meta(path: Path) -> dict:
    """En-tête d'un catalogue (sans la liste des clients), par le cache de sources/dags.py (relu seulement s'il a changé)."""
    try:
        return {k: v for k, v in dag_catalogue.catalogue(path).items() if k != "clients"}
    except (OSError, ValueError):
        return {}


class DagSource:
    def __init__(self, repo: Path, target: Path, generator: list[str] | None = None):
        """target : catalogue écrit (data/dags/catalogue.json, lu par sources/dags.py).
        generator : commande du générateur (remplacée dans les tests) ; reçoit le dossier du clone et --sortie."""
        self.repo, self.target = repo, target
        self.generator = generator or ["uv", "run", "--script", str(GENERATOR)]
        self._lock = threading.Lock()
        self.running = ""  # checkout en cours de génération (vide : rien en cours)
        self._failed: dict[str, str] = {}  # version → message : pas de nouvel essai seul
        # Dernier relevé du clone (refresh : démarrage, chaque minute, « Régénérer ») : l'affichage ne relance pas git,
        # qui coûte ~300 ms sur le clone (diff et nouveaux fichiers parmi ~13 000).
        self.seen: Checkout | None = None
        self.checked = False

    def is_clone(self) -> bool:
        return (self.repo / ".git").exists()  # même règle que ClaudeRunner.problem()

    def current(self) -> Checkout | None:
        """Checkout du clone et sa version ; None sans clone."""
        commit = (_git(self.repo, "log", "-1", "--format=%h %cs") or b"").decode().strip() if self.is_clone() else ""
        if not commit:
            return None
        label = (_git(self.repo, "symbolic-ref", "--short", "-q", "HEAD") or b"").decode().strip() or "HEAD détachée"
        # Modifications non commitées : l'Airflow local les lit, le catalogue aussi. Fichiers suivis : leur diff ;
        # fichiers nouveaux (absents du diff) : chemin, taille et date de modification, pour voir chaque nouvelle édition.
        digest, changed = hashlib.sha1(), False
        diff = _git(self.repo, "diff", "HEAD") or b""
        if diff:
            digest.update(diff)
            changed = True
        for new in filter(None, (_git(self.repo, "ls-files", "--others", "--exclude-standard", "-z") or b"").split(b"\0")):
            try:
                st = (self.repo / new.decode()).stat()
            except OSError:
                continue
            digest.update(new + f":{st.st_size}:{st.st_mtime_ns}".encode())
            changed = True
        return Checkout(label, f"{commit} + modifs {digest.hexdigest()[:8]}" if changed else commit)

    def _ready(self, checkout: Checkout) -> bool:
        """Rien à générer : version déjà générée, ou déjà en échec (seul « Régénérer » la retente)."""
        return checkout.version in self._failed or _meta(self.target).get("commit_orchestrator") == checkout.version

    def refresh(self, force: bool = False) -> None:
        """Relève le clone et génère le catalogue en tâche de fond s'il date d'une autre version.
        force (« Régénérer ») : même si cette version est déjà générée ou en échec."""
        checkout = self.seen = self.current()
        self.checked = True
        if not checkout:
            return
        if force:
            self._failed.pop(checkout.version, None)
        elif self._ready(checkout):
            return
        with self._lock:
            if self.running:
                return
            self.running = checkout.label
        threading.Thread(target=self._generate_while_needed, args=(checkout,), name="catalogue-dags", daemon=True).start()

    def _generate_while_needed(self, checkout: Checkout) -> None:
        """Génère, puis recommence tant que le clone a changé entre-temps (autre branche, nouvelles modifications).
        `running` reste posé jusqu'à la fin : vide veut dire qu'il n'y a vraiment plus rien à faire."""
        nxt: Checkout | None = checkout
        while nxt:
            self._generate(nxt)
            nxt = self.seen = self.current()
            with self._lock:
                nxt = None if not nxt or self._ready(nxt) else nxt
                self.running = nxt.label if nxt else ""

    def _generate(self, checkout: Checkout) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="jarvis-catalogue-") as tmp:
                partial = Path(tmp) / "catalogue.json"
                env = {**child_environment(), "AIRFLOW__CORE__DAGS_FOLDER": str(self.repo), "PYTHONDONTWRITEBYTECODE": "1"}
                run = subprocess.run([*self.generator, str(self.repo), "--sortie", str(partial)], capture_output=True, text=True,
                                     env=env, timeout=GENERATE_TIMEOUT_S, check=False, cwd=PROJECT_ROOT)
                if run.returncode or not partial.is_file():
                    lines = [line for line in (run.stderr or run.stdout).splitlines() if line.strip()]
                    raise RuntimeError(lines[-1][:300] if lines else f"code de sortie {run.returncode}")
                # Jarvis note lui-même la version : le générateur ne voit que le commit, pas les modifications.
                catalogue = {**json.loads(partial.read_text(encoding="utf-8")), "commit_orchestrator": checkout.version,
                             "branche": checkout.label}
                self.target.parent.mkdir(parents=True, exist_ok=True)
                staged = self.target.with_suffix(".tmp")
                staged.write_text(json.dumps(catalogue, ensure_ascii=False), encoding="utf-8")
                os.replace(staged, self.target)  # atomique : un lecteur voit l'ancien catalogue ou le nouveau
                self._failed.pop(checkout.version, None)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
            self._failed[checkout.version] = f"{checkout.label} : {e}"

    def status(self, versioned: Path) -> dict:
        """Ce qu'affiche l'onglet Injections, d'après le dernier relevé du clone (lecture seule : ne lance rien, pas de git)."""
        meta = _meta(self.target)
        local = bool(meta)
        meta = meta or _meta(versioned)
        try:
            made = datetime.strptime(meta.get("genere_le", ""), "%Y-%m-%d %H:%M")
        except ValueError:  # absent ou dans un autre format : pas affiché
            made = None
        return {
            "repo": str(self.repo),
            "missing_clone": self.checked and self.seen is None,
            "checkout": self.seen,
            "local": local,
            "label": meta.get("branche", ""),
            "commit": meta.get("commit_orchestrator", ""),
            "made": made,
            "outdated": local and self.seen is not None and meta.get("commit_orchestrator") != self.seen.version,
            "running": self.running,
            "error": self._failed.get(self.seen.version, "") if self.seen else "",
        }
