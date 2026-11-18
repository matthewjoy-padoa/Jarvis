"""Source du catalogue des DAGs : une branche locale du clone orchestrator (ORCHESTRATOR_DIR), par défaut sa branche
courante, lue telle que commitée (les modifications non commitées ne comptent pas).

La branche est extraite par `git archive` (tout le dépôt : la config importe aussi tests/) dans un dossier temporaire : jamais
de checkout, le clone n'est pas touché. sync/dags/generer_catalogue.py en tire un catalogue par branche dans data/dags/,
et celui de la branche choisie est copié dans data/dags/catalogue.json, que sources/dags.py lit avant le catalogue
versionné (sync/dags/catalogue.json, copie figée de main). Sans clone ou sans génération réussie : le catalogue versionné.

La génération part au démarrage, au choix d'une branche, sur « Régénérer », et quand la branche avance (vérifié chaque
minute par main._push_round) ; jamais pendant l'affichage d'une page. Un commit en échec n'est retenté que par « Régénérer ».
Lancer un DAG qui n'existe que sur la branche suppose que l'Airflow de la VM charge cette branche : l'aperçu d'air.py le dit.
"""

import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from deploy_hub.config import PROJECT_ROOT, child_environment

GENERATOR = PROJECT_ROOT / "sync" / "dags" / "generer_catalogue.py"  # Python et paquets : son en-tête « /// script »
GENERATE_TIMEOUT_S = 15 * 60  # premier passage : uv télécharge Python 3.12 et les paquets


@dataclass(frozen=True)
class Branch:
    name: str
    commit: str  # « hash date », comme commit_orchestrator du catalogue
    current: bool  # branche extraite dans le clone


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess | None:
    """git sans invite (identifiants, éditeur) ; None si git est absent ou trop lent."""
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=300, check=False,
                              stdin=subprocess.DEVNULL, env={**child_environment(), "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError):
        return None


def _write_json(path: Path, data: dict) -> None:
    """Écriture atomique : un lecteur voit l'ancien fichier ou le nouveau, jamais un fichier à moitié écrit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _meta(path: Path) -> dict:
    """En-tête d'un catalogue (sans la liste des clients)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k != "clients"}


class DagSource:
    def __init__(self, repo: Path, state_dir: Path, generator: list[str] | None = None):
        """generator : commande du générateur (remplacée dans les tests) ; reçoit le dossier extrait et --sortie."""
        self.repo, self.state_dir = repo, state_dir
        self.active = state_dir / "catalogue.json"  # copie du catalogue de la branche choisie, lue par sources/dags.py
        self.generator = generator or ["uv", "run", "--script", str(GENERATOR)]
        self._lock = threading.Lock()
        self.running = ""  # branche en cours de génération (vide : rien en cours)
        self._failed: dict[tuple[str, str], str] = {}  # (branche, commit) → message : pas de nouvel essai seul
        try:
            self._wanted = json.loads((state_dir / "source.json").read_text(encoding="utf-8")).get("branche", "")
        except (OSError, ValueError):
            self._wanted = ""

    # --- branches -------------------------------------------------------------------------------------------------
    def branches(self) -> list[Branch]:
        """Branches locales du clone, la courante d'abord ; vide sans clone git."""
        head = _git(self.repo, "symbolic-ref", "--short", "-q", "HEAD")
        refs = _git(self.repo, "for-each-ref", "refs/heads", "--format=%(refname:short)\t%(objectname:short)\t%(committerdate:short)")
        if not refs or refs.returncode:
            return []
        current = head.stdout.decode().strip() if head and head.returncode == 0 else ""
        out = [Branch(name, f"{sha} {day}", name == current)
               for name, sha, day in (line.split("\t") for line in refs.stdout.decode().splitlines())]
        return sorted(out, key=lambda b: (not b.current, b.name))

    def chosen(self) -> Branch | None:
        """Branche choisie dans Jarvis si elle existe encore, sinon la branche courante du clone."""
        branches = self.branches()
        return next((b for b in branches if b.name == self._wanted), None) or next((b for b in branches if b.current), None)

    def path(self, branch: str) -> Path:
        """Un fichier par branche : nom lisible, plus une empreinte (feat/x et feat_x ne partagent pas le même fichier)."""
        return self.state_dir / f"catalogue-{re.sub(r'[^\w.-]', '_', branch)}-{hashlib.sha1(branch.encode()).hexdigest()[:8]}.json"

    def _publish(self, branch: Branch | None) -> None:
        """catalogue.json = celui de la branche s'il a été généré ; sinon retiré (le catalogue versionné sert)."""
        source = self.path(branch.name) if branch else None
        if source and source.is_file():
            tmp = self.active.with_suffix(".tmp")
            tmp.write_bytes(source.read_bytes())
            os.replace(tmp, self.active)
        else:
            self.active.unlink(missing_ok=True)

    # --- actions --------------------------------------------------------------------------------------------------
    def choose(self, name: str, force: bool = False) -> bool:
        """Retient la branche (une branche locale du clone), publie son catalogue s'il existe et le (re)génère si besoin.
        force : « Régénérer », même si le commit est déjà généré ou en échec. False si la branche n'existe pas."""
        branch = next((b for b in self.branches() if b.name == name), None)
        if not branch:
            return False
        _write_json(self.state_dir / "source.json", {"branche": name})
        self._wanted = name
        self._publish(branch)
        self.refresh(force)
        return True

    def refresh(self, force: bool = False) -> None:
        """Génère en tâche de fond le catalogue de la branche choisie s'il manque ou date d'un autre commit.
        Un commit en échec n'est pas regénéré seul (sinon : génération et rechargement en boucle) ; force le fait."""
        branch = self.chosen()
        if not branch:
            return
        key = (branch.name, branch.commit)
        if force:
            self._failed.pop(key, None)
        elif key in self._failed or _meta(self.path(branch.name)).get("commit_orchestrator") == branch.commit:
            return
        with self._lock:
            if self.running:
                return
            self.running = branch.name
        threading.Thread(target=self._generate_while_needed, args=(branch,), name="catalogue-dags", daemon=True).start()

    def _generate_while_needed(self, branch: Branch) -> None:
        """Génère, puis enchaîne tant que la branche choisie (qui a pu changer entre-temps) n'est pas à jour.
        `running` reste posé jusqu'à la fin : vide veut dire qu'il n'y a vraiment plus rien à faire."""
        while branch:
            self._generate(branch)
            nxt = self.chosen()
            ready = nxt and ((nxt.name, nxt.commit) in self._failed
                             or _meta(self.path(nxt.name)).get("commit_orchestrator") == nxt.commit)
            if not nxt or ready:
                self._publish(nxt)  # avant de libérer `running` : qui voit « fini » voit le nouveau catalogue
            with self._lock:
                branch = None if not nxt or ready else nxt
                self.running = branch.name if branch else ""

    def _generate(self, branch: Branch) -> None:
        key = (branch.name, branch.commit)
        try:
            with tempfile.TemporaryDirectory(prefix="jarvis-orchestrator-") as tmp:
                tree, archive = Path(tmp) / "orchestrator", Path(tmp) / "branche.tar"
                out = _git(self.repo, "archive", "--format=tar", "-o", str(archive), branch.name)
                if not out or out.returncode:
                    raise RuntimeError(f"git archive : {out.stderr.decode().strip()[-300:] if out else 'git introuvable'}")
                with tarfile.open(archive) as tar:
                    tar.extractall(tree, filter="data")
                partial = Path(tmp) / "catalogue.json"
                env = {**child_environment(), "AIRFLOW__CORE__DAGS_FOLDER": str(tree)}
                run = subprocess.run([*self.generator, str(tree), "--sortie", str(partial)], capture_output=True, text=True,
                                     env=env, timeout=GENERATE_TIMEOUT_S, check=False, cwd=PROJECT_ROOT)
                if run.returncode or not partial.is_file():
                    lines = [line for line in (run.stderr or run.stdout).splitlines() if line.strip()]
                    raise RuntimeError(lines[-1][:300] if lines else f"code de sortie {run.returncode}")
                # L'arbre extrait n'a pas de .git : Jarvis note lui-même d'où vient le catalogue.
                catalogue = {**json.loads(partial.read_text(encoding="utf-8")), "commit_orchestrator": branch.commit,
                             "branche": branch.name}
                _write_json(self.path(branch.name), catalogue)
                self._failed.pop(key, None)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, tarfile.TarError) as e:
            self._failed[key] = f"{branch.name} : {e}"

    # --- affichage ------------------------------------------------------------------------------------------------
    def status(self, versioned: Path) -> dict:
        """Ce qu'affiche l'onglet Injections (lecture seule : ne lance rien)."""
        branches = self.branches()
        branch = next((b for b in branches if b.name == self._wanted), None) or next((b for b in branches if b.current), None)
        meta = _meta(self.active) if self.active.is_file() else {}
        from_branch = bool(branch and meta.get("branche") == branch.name)
        meta = meta if from_branch else _meta(versioned)
        try:
            made = datetime.strptime(meta.get("genere_le", ""), "%Y-%m-%d %H:%M")
        except ValueError:  # absent ou dans un autre format : pas affiché
            made = None
        return {
            "repo": str(self.repo),
            "branches": branches,
            "chosen": branch,
            "from_branch": from_branch,
            "commit": meta.get("commit_orchestrator", ""),
            "made": made,
            "outdated": from_branch and meta.get("commit_orchestrator") != branch.commit,
            "running": self.running,
            "error": self._failed.get((branch.name, branch.commit), "") if branch else "",
        }
