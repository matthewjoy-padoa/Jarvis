"""Lance Claude Code en headless (`claude -p`) sur une demande, dans un worktree dédié de l'orchestrator.

Chaque agent a son worktree, créé depuis origin/main sous JARVIS_WORKTREES_DIR : le checkout de travail de
l'orchestrator (celui qu'utilise l'Airflow local) n'est jamais touché. Le brief part sur l'entrée standard,
la sortie stream-json va dans data/agents/<id>.jsonl, l'historique dans data/agents/agents.json.
Sans --bare, l'agent charge comme une session normale ~/.claude/CLAUDE.md, ses hooks et le CLAUDE.md du dépôt.
"""

import json
import os
import re
import signal
import subprocess
import threading
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from deploy_hub.config import ENV_FILE_KEYS
from deploy_hub.models import DONE, IN_PROGRESS, KO, Deployment, RetourRecette, status_group
from deploy_hub.sources import ActionResult


@dataclass
class AgentRun:
    id: int
    slug: str
    client: str
    subject: str
    branch: str
    prompt: str
    started_at: datetime
    status: str
    simulated: bool = False
    ended_at: datetime | None = None
    worktree: str = ""
    pr_url: str = ""
    cost_usd: float | None = None
    summary: str = ""  # dernier message de l'agent (son compte rendu) ou cause de l'échec

    @property
    def group(self) -> str:
        """Groupe d'affichage (badges g-*) : en cours, terminé ou KO."""
        return status_group(self.status, AGENT_STATUSES)

    @property
    def running(self) -> bool:
        return self.group == IN_PROGRESS


AGENT_STATUSES = {"préparation": IN_PROGRESS, "en cours": IN_PROGRESS, "terminé": DONE,
                  "échec": KO, "arrêté": KO, "délai dépassé": KO, "interrompu": KO}
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
INSTRUCTION = "Le brief de ta mission est sur l'entrée standard : lis-le en entier et suis-le."
# Outils accordés à l'agent (JARVIS_AGENT_ALLOWED_TOOLS, séparés par « ; ») : lire, modifier, tester, committer, pousser
# sa branche, ouvrir la PR. Le reste est refusé (pas de question en mode -p). guard.sh bloque push sur main et push forcé.
# Pas de Read/Edit/Write : sans chemin ils vaudraient partout. acceptEdits autorise déjà les modifications dans le worktree
# et les --add-dir, et la lecture y est libre ; le clone de travail de l'orchestrator reste ainsi hors d'atteinte.
DEFAULT_TOOLS = ("Bash(uv run *)", "Bash(uv sync *)", "Bash(ls *)", "Bash(cat *)",
                 "Bash(grep *)", "Bash(rg *)", "Bash(find *)", "Bash(head *)", "Bash(tail *)", "Bash(wc *)",
                 "Bash(git status *)", "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)", "Bash(git ls-files *)",
                 "Bash(git grep *)", "Bash(git blame *)", "Bash(git add *)", "Bash(git commit *)", "Bash(git push *)",
                 "Bash(gh pr create *)", "Bash(gh pr view *)")
SECRETS = ("NOTION_TOKEN", "AIRFLOW_USER", "AIRFLOW_PASSWORD")  # retirés aussi s'ils viennent de l'environnement du service


def agent_branch(*parts: str) -> str:
    """Nom de branche (et de dossier du worktree) sûr : ASCII minuscule, chiffres, tirets et « _ » (noms de ressources)."""
    text = unicodedata.normalize("NFKD", "-".join(parts)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9_]+", "-", text).strip("-_")[:100]


TO_FILL = "À COMPLÉTER"  # marqueur laissé dans le brief quand une info manque ; le lancement est refusé tant qu'il reste


def _tag(name: str, text: str) -> str:
    return f"<{name}>\n{text.strip()}\n</{name}>"


def retour_prompt(d: Deployment, r: RetourRecette, description: str = "") -> str:
    """Brief pré-rempli de l'agent sur un retour de recette, pensé pour Claude Code dans integration-orchestrator.

    Contexte et contenu du retour balisés, démarche tirée de kb/anatomie-orchestrator et des patterns
    correctif-au-niveau-logiciel / requete-client-doublon-logiciel, contrat en cinq lignes, compte rendu attendu.
    La personne qui lance l'agent n'a qu'à relire, et à compléter <precisions> si besoin.
    """
    software = r.software or d.software
    branch = agent_branch("fix", d.slug, r.id)
    context = [
        f"Client : {d.client} (slug Jarvis : {d.slug}) · logiciel du retour : {software} · logiciel(s) du client : {d.software}",
        f"Retour de recette : {r.id} · statut Notion : {r.status} · type de données : {r.data_type or 'non précisé'}"
        + (f" · type de retour : {r.type_retour}" if r.type_retour else ""),
        f"Ressource orchestrator : {r.resource}" if r.resource
        else f"Ressource orchestrator : à identifier à partir du type de données « {r.data_type or '?'} »",
    ]
    if r.due:
        context.append(f"Deadline : {r.due.strftime('%d/%m/%Y')}" + (f" · déploiement le {d.go_live.strftime('%d/%m/%Y')}" if d.go_live else ""))
    if r.notion_url:
        context.append(f"Page Notion du retour : {r.notion_url}")

    tags = {"Ligne du cahier de recette": "ligne_cahier", "Type de retour": None, "Commentaire client": "commentaire_client",
            "Exemple salarié / adhérent": "exemple", "Analyse padoa": "analyse_padoa", "Solutions possibles": "solutions_possibles",
            "Discussion INTE/OPS": "discussion_inte_ops", "Échanges / suivi": "echanges_suivi"}
    content = [_tag(tags[label], text) for label, text in r.content if tags.get(label)]
    if not content:
        content = [_tag("description", f"{TO_FILL} : ce retour n'a pas de contenu dans Notion. Décris l'écart constaté, "
                                       "l'attendu et un exemple (identifiant ou cas), puis retire cette consigne.")]

    parts = [
        "Tu corriges un retour de recette d'une reprise de données Padoa, dans le dépôt integration-orchestrator. "
        "Le client a constaté un écart entre ses données d'origine et ce qui a été injecté dans Padoa. "
        "Lis tout le brief avant d'agir.",
        _tag("contexte", "\n".join(context)),
        _tag("retour", "\n".join(content)),
        _tag("precisions", description.strip() or "(rien à ajouter)"),
        _tag("demarche", "\n".join([
            "1. Reformule en deux lignes l'écart : ce qui est attendu, ce qui est constaté. Si le retour ne permet pas de le dire, "
            "arrête-toi et liste tes questions au lieu de deviner.",
            "2. Trouve les fichiers qui interviennent pour ce client et cette ressource à chaque étape (extract SQL, mapping, settings, "
            "composant, transform, tests). Le code existe à deux niveaux : logiciel (`src/extract/softwares/<logiciel>/`, "
            "`src/mappings/software/<logiciel>/`, `src/lib/steps/transform/software/<logiciel>/`) et client en surcharge "
            "(`src/extract/customers/<client>/`, `src/lib/components/<logiciel ou version>/<client>/`). La clé du client et de sa "
            "version de logiciel sont dans `src/shared/constants/customers.py`.",
            "3. Avant de modifier une requête logiciel, vérifie qu'aucun client n'en a une copie : "
            "`git ls-files 'src/extract/customers/*/queries/<chemin relatif>.sql'`. Toutes les copies sont à traiter ensemble.",
            "4. Choisis le niveau du correctif : client si le problème est propre à ce client, logiciel s'il vient du logiciel "
            "(liste alors les autres clients concernés). Ne modifie `clean`, `compare` ou `load` que si c'est indispensable : "
            "ces étapes sont communes à tous les clients.",
            "5. Corrige au plus petit endroit, puis ajoute ou adapte un test qui échoue avant le correctif et passe après, "
            "avec des valeurs fictives.",
        ])),
        _tag("contrat", "\n".join([
            "- Entrée : ce brief, le code du client et de son logiciel dans integration-orchestrator, à jour de main.",
            f"- Sortie : un correctif sur la branche `{branch}` et une PR intitulée `[FIX]({d.slug}) <résumé>` "
            "(scope = le logiciel si le correctif est au niveau logiciel).",
            "- Validation : `uv run pytest tests/src/lib/steps/extract tests/src/lib/components/<logiciel>` vert, "
            "plus les tests que tu as ajoutés ; cite la commande et son résultat.",
            "- Limites : ne lis pas les logs des DAGs Airflow ; ne pousse pas sur main et ne force pas de push ; aucune donnée client "
            "(matricule, nom, exemple du retour) dans le code, les tests, les commits ou la PR ; pas de modification hors du périmètre du retour.",
            "- Résultat : la PR ouverte, ou un rapport qui dit précisément ce qui manque pour corriger.",
        ])),
        _tag("compte_rendu", "Termine par : la cause (fichier:ligne), le correctif et son niveau (client ou logiciel), les tests "
                             "(commande et résultat), le lien de la PR, et ce qu'il faudra vérifier en recette."),
    ]
    return "\n\n".join(parts)


class ClaudeRunner:
    """Agents Claude : simulés (mode demo) ou réels (mode live). L'historique survit aux redémarrages de Jarvis."""

    def __init__(self, orchestrator_dir: Path, state_dir: Path, live: bool = False, notifications=None,
                 worktrees_dir: Path | None = None, claude_bin: str = "claude", add_dirs: tuple[Path, ...] = (),
                 allowed_tools: tuple[str, ...] = DEFAULT_TOOLS, permission_mode: str = "acceptEdits",
                 timeout_min: int = 90, max_running: int = 2, model: str = ""):
        self.orchestrator_dir = orchestrator_dir
        self.live = live
        self.notifications = notifications  # NotificationStore : « agent terminé »
        self.state_dir = state_dir
        self.state_file = state_dir / "agents.json"
        self.worktrees_dir = worktrees_dir or orchestrator_dir / ".claude" / "worktrees" / "jarvis"  # ignoré par git
        self.claude_bin = claude_bin
        self.add_dirs = add_dirs
        self.allowed_tools = allowed_tools
        self.permission_mode = permission_mode
        self.timeout = timeout_min * 60
        self.max_running = max_running
        self.model = model
        self._lock = threading.RLock()  # runs et agents.json : threads des agents et requêtes
        self._procs: dict[int, subprocess.Popen] = {}
        self._stopped: set[int] = set()
        self.runs: list[AgentRun] = self._load()

    @classmethod
    def from_env(cls, orchestrator_dir: Path, live: bool, notifications, state_dir: Path) -> "ClaudeRunner":
        """Réglages JARVIS_AGENT_*, CLAUDE_BIN, JARVIS_WORKTREES_DIR : seules les variables renseignées remplacent
        les valeurs par défaut de __init__."""
        env = os.environ.get
        kw = {
            "worktrees_dir": env("JARVIS_WORKTREES_DIR") and Path(env("JARVIS_WORKTREES_DIR")).expanduser(),
            "claude_bin": env("CLAUDE_BIN"),
            "add_dirs": tuple(Path(p).expanduser() for p in env("JARVIS_AGENT_ADD_DIRS", "").split(":") if p.strip()),
            "allowed_tools": tuple(t.strip() for t in env("JARVIS_AGENT_ALLOWED_TOOLS", "").split(";") if t.strip()),
            "permission_mode": env("JARVIS_AGENT_PERMISSION_MODE"),
            "timeout_min": env("JARVIS_AGENT_TIMEOUT_MIN") and int(env("JARVIS_AGENT_TIMEOUT_MIN")),
            "max_running": env("JARVIS_AGENT_MAX") and int(env("JARVIS_AGENT_MAX")),
            "model": env("JARVIS_AGENT_MODEL"),
        }
        return cls(orchestrator_dir, state_dir, live, notifications, **{k: v for k, v in kw.items() if v})

    # --- historique -------------------------------------------------------------------------------------------
    def _load(self) -> list[AgentRun]:
        path = self.state_file
        if not path.is_file():
            return []
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # fichier abîmé : mis de côté, Jarvis démarre quand même
            path.replace(path.with_suffix(f".illisible-{datetime.now():%Y%m%d-%H%M%S}"))
            return []
        runs = []
        for item in items:
            for key in ("started_at", "ended_at"):  # écrites en ISO par _save
                item[key] = datetime.fromisoformat(item[key]) if item.get(key) else None
            run = AgentRun(**{k: v for k, v in item.items() if k in AgentRun.__dataclass_fields__})
            if run.running:  # le process est parti avec l'ancienne exécution de Jarvis
                run.status, run.ended_at = "interrompu", run.ended_at or datetime.now()
                run.summary = run.summary or "Jarvis a redémarré pendant que l'agent travaillait."
            runs.append(run)
        return runs

    def _save(self) -> None:
        with self._lock:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")  # écriture atomique : jamais d'historique à moitié écrit
            tmp.write_text(json.dumps([asdict(r) for r in self.runs], ensure_ascii=False, default=datetime.isoformat), encoding="utf-8")
            tmp.replace(self.state_file)

    def get(self, run_id: int) -> AgentRun | None:
        return next((r for r in self.runs if r.id == run_id), None)

    def log_path(self, run: AgentRun) -> Path:
        return self.state_dir / f"{run.id}.jsonl"

    def log(self, run: AgentRun) -> list[tuple[str, str]]:
        return read_log(self.log_path(run))

    # --- lancement --------------------------------------------------------------------------------------------
    def build_command(self) -> list[str]:
        """Le brief n'est pas dans la commande (trop long) : il part sur l'entrée standard."""
        cmd = [self.claude_bin, "-p", INSTRUCTION, "--output-format", "stream-json", "--verbose",
               "--permission-mode", self.permission_mode]
        if self.model:
            cmd += ["--model", self.model]
        for d in self.add_dirs:
            cmd += ["--add-dir", str(d)]
        return cmd + ["--allowedTools", *self.allowed_tools]  # en dernier : l'option prend tous les arguments qui suivent

    def _launch(self, d: Deployment, subject: str, branch: str, prompt: str) -> ActionResult:
        with self._lock:
            if any(r.running and r.branch == branch for r in self.runs):
                return ActionResult(False, f"Un agent travaille déjà sur la branche {branch} : attends sa fin ou arrête-le (Agents).", not self.live)
            if self.live and sum(r.running for r in self.runs) >= self.max_running:
                return ActionResult(False, f"Déjà {self.max_running} agent(s) en cours : attends qu'un se termine.", False)
            run = AgentRun(max((r.id for r in self.runs), default=0) + 1, d.slug, d.client, subject, branch, prompt,
                           datetime.now(), "préparation", simulated=not self.live)
            self.runs.insert(0, run)
        if not self.live:
            self.finished(run, "terminé")
            return ActionResult(True, f"Simulation : agent #{run.id} sur {subject}, branche {branch}, dans {self.orchestrator_dir}")
        self._save()
        threading.Thread(target=self._work, args=(run,), name=f"agent-{run.id}", daemon=True).start()
        return ActionResult(True, f"Agent #{run.id} lancé sur {subject}, branche {branch}. Suivi dans Agents.", False)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.orchestrator_dir), *args], capture_output=True, text=True, timeout=300, check=check)

    def prepare_worktree(self, branch: str) -> Path:
        """Worktree de l'agent depuis origin/main ; relancé sur la même branche, il reprend le worktree existant."""
        path = self.worktrees_dir / branch
        if path.is_dir():
            if not (path / ".git").exists():
                raise RuntimeError(f"{path} existe mais n'est pas un worktree git : supprime-le, puis relance.")
            return path
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self._git("fetch", "origin", "main")
        if self._git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
            self._git("worktree", "add", str(path), branch)
        else:
            self._git("worktree", "add", "-b", branch, str(path), "origin/main")
        return path

    def _env(self) -> dict[str, str]:
        """Environnement du service, sans les secrets ni les réglages venus du .env de Jarvis."""
        return {k: v for k, v in os.environ.items() if k not in SECRETS and k not in ENV_FILE_KEYS}

    def _work(self, run: AgentRun) -> None:
        try:
            run.worktree = str(self.prepare_worktree(run.branch))
        except Exception as e:  # noqa: BLE001 : tout échec doit finir en notification, pas en thread mort
            cause = (e.stderr or e.stdout or "").strip()[-400:] if isinstance(e, subprocess.CalledProcessError) else str(e)
            run.summary = f"Worktree impossible : {cause}"
            return self.finished(run, "échec")
        run.status = "en cours"
        self._save()
        log = self.log_path(run)
        timed_out = False
        try:
            with log.open("w", encoding="utf-8") as out:
                proc = subprocess.Popen(self.build_command(), cwd=run.worktree, stdin=subprocess.PIPE, stdout=out,
                                        stderr=subprocess.STDOUT, text=True, env=self._env())
                self._procs[run.id] = proc
                try:
                    proc.stdin.write(run.prompt)
                    proc.stdin.close()
                except BrokenPipeError:  # claude est déjà sorti (non connecté, option refusée) : son message est au journal
                    pass
                try:
                    code = proc.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    code = self._interrupt(proc)
        except Exception as e:  # noqa: BLE001
            run.summary = f"Lancement impossible : {e}"
            return self.finished(run, "échec")
        finally:
            self._procs.pop(run.id, None)
        text = log.read_text(encoding="utf-8", errors="replace")  # lu une fois : résultat final et lien de la PR
        result = next((e for e in reversed(list(_events(text))) if e.get("type") == "result"), {})
        run.cost_usd = result.get("total_cost_usd")
        run.summary = (result.get("result") or "").strip()
        run.pr_url = find_pr(text)
        if run.id in self._stopped:
            status = "arrêté"
        elif timed_out:
            status = "délai dépassé"
        elif code == 0 and result.get("subtype", "success") == "success":
            status = "terminé"
        else:
            status = "échec"
            run.summary = run.summary or f"Code de sortie {code} : voir le journal."
        self.finished(run, status)

    @staticmethod
    def _interrupt(proc: subprocess.Popen) -> int:
        """SIGINT termine proprement le tour en cours ; sinon arrêt forcé."""
        proc.send_signal(signal.SIGINT)
        try:
            return proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()

    def stop(self, run_id: int) -> bool:
        proc = self._procs.get(run_id)
        if not proc:
            return False
        self._stopped.add(run_id)
        threading.Thread(target=self._interrupt, args=(proc,), daemon=True).start()
        return True

    def finished(self, run: AgentRun, status: str) -> None:
        """Fin d'un agent (summary et pr_url déjà remplis) : met à jour son état, l'enregistre et notifie."""
        run.status = status
        run.ended_at = datetime.now()
        self._save()
        if self.notifications:
            ok = run.group == DONE
            body = run.pr_url or f"{run.subject} · branche {run.branch}" + ("" if ok or not run.summary else f" · {run.summary[:200]}")
            title = f"Agent #{run.id} {status}{' (simulé)' if run.simulated else ''} · {run.client}"
            self.notifications.add(f"agent:{run.id}:{run.started_at.isoformat()}", "agent", "info" if ok else "warning",
                                   title, body, f"/agents/{run.id}", run.client)

    def launch(self, d: Deployment, resource: str) -> ActionResult:
        prompt = f"Ressource {resource} du client {d.client} (logiciel {d.software}) : lis la fiche de déploiement, code, teste, ouvre la PR."
        return self._launch(d, resource, agent_branch(d.slug, resource), prompt)

    def launch_retour(self, d: Deployment, r: RetourRecette, description: str = "", prompt: str = "") -> ActionResult:
        """prompt : le brief relu (et éventuellement modifié) par la personne qui lance l'agent ; sinon il est construit ici."""
        return self._launch(d, f"retour {r.id}", agent_branch("fix", d.slug, r.id), prompt.strip() or retour_prompt(d, r, description))


def _events(text: str):
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            yield {"type": "raw", "text": line}
            continue
        if isinstance(event, dict):
            yield event


def find_pr(text: str) -> str:
    found = PR_URL.findall(text or "")
    return found[-1] if found else ""


def _tool_line(block: dict) -> str:
    args = block.get("input") or {}
    shown = args.get("command") or args.get("file_path") or args.get("pattern") or args.get("path") or ""
    return f"{block.get('name', '?')} {str(shown)[:300]}".strip()


def read_log(log: Path) -> list[tuple[str, str]]:
    """Journal lisible d'un agent : (« texte » | « outil » | « refus » | « fin » | « brut », contenu)."""
    lines = []
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    for e in _events(text):
        kind = e.get("type")
        if kind == "assistant":
            for block in (e.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    lines.append(("texte", block["text"].strip()))
                elif block.get("type") == "tool_use":
                    lines.append(("outil", _tool_line(block)))
        elif kind == "user":
            for block in (e.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    content = block.get("content")
                    text = content if isinstance(content, str) else " ".join(c.get("text", "") for c in content or [] if isinstance(c, dict))
                    lines.append(("refus", text.strip()[:400]))
        elif kind == "result":
            lines.append(("fin", f"{e.get('subtype', '')} · {e.get('num_turns', '?')} tours"
                                 + (f" · {e['total_cost_usd']:.2f} $" if isinstance(e.get("total_cost_usd"), (int, float)) else "")))
        elif kind == "raw":
            lines.append(("brut", e["text"][:400]))
    return lines
