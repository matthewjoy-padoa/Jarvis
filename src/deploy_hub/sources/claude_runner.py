"""Lance Claude Code en headless (`claude -p`) sur une demande, dans un worktree dédié de l'orchestrator.

Chaque agent a son worktree, créé depuis origin/main sous JARVIS_WORKTREES_DIR : le checkout de travail de
l'orchestrator (celui qu'utilise l'Airflow local) n'est jamais touché. Le brief part sur l'entrée standard,
la sortie stream-json va dans data/agents/<id>.jsonl, l'historique dans data/agents/agents.json.
Sans --bare, l'agent charge comme une session normale ~/.claude/CLAUDE.md, ses hooks et le CLAUDE.md du dépôt.
"""

import inspect
import json
import os
import re
import signal
import getpass
import shutil
import subprocess
import textwrap
import threading
import unicodedata
from dataclasses import asdict, dataclass, field
from statistics import median
from datetime import datetime, timedelta
from pathlib import Path

from deploy_hub.safe_write import write_json
from deploy_hub.config import child_environment
from deploy_hub.models import DONE, IN_PROGRESS, KO, TODO, Deployment, RetourRecette, status_group
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
    num_turns: int | None = None
    category: str = ""  # type de ticket (type de données du retour, ou ressource) : sert au bilan par type
    # tokens annoncés par Claude Code à la fin de l'agent (None : pas annoncés, agent arrêté ou lancé avant leur relevé)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    # remplissage du contexte au dernier tour (ce que l'agent relit à chaque tour) et taille de la fenêtre du modèle
    context_tokens: int | None = None
    context_window: int | None = None
    session_id: str = ""  # session Claude Code de l'agent : reprise par « Répondre et relancer »
    reply_to: int | None = None  # agent dont celui-ci reprend le travail après une réponse à ses questions
    resume: str = ""  # session reprise (--resume) ; vide : nouvelle session
    relaunch_of: int | None = None  # agent en erreur que celui-ci relance avec le même brief

    @property
    def report(self) -> str:
        """Compte rendu affiché, sans le bloc de questions en JSON."""
        return report_of(self.summary)

    @property
    def context_pct(self) -> int | None:
        if not self.context_tokens or not self.context_window:
            return None
        return min(round(100 * self.context_tokens / self.context_window), 100)

    @property
    def token_parts(self) -> list[tuple[str, int]]:
        """(nature, nombre) des tokens relevés, dans l'ordre de TOKEN_KINDS."""
        return [(label, getattr(self, name)) for name, label, _ in TOKEN_KINDS if getattr(self, name) is not None]

    @property
    def tokens(self) -> int | None:
        parts = self.token_parts
        return sum(n for _, n in parts) if parts else None

    @property
    def duration(self) -> timedelta:
        return (self.ended_at or datetime.now()) - self.started_at

    @property
    def group(self) -> str:
        """Groupe d'affichage (badges g-*) : en cours, terminé ou KO."""
        return status_group(self.status, AGENT_STATUSES)

    @property
    def running(self) -> bool:
        return self.group == IN_PROGRESS


# (champ d'AgentRun, libellé, clé dans « usage » de l'évènement « result » de Claude Code)
TOKEN_KINDS = (("input_tokens", "Entrée", "input_tokens"), ("output_tokens", "Sortie", "output_tokens"),
               ("cache_read_tokens", "Cache relu", "cache_read_input_tokens"),
               ("cache_write_tokens", "Cache écrit", "cache_creation_input_tokens"))


def tokens_text(n: int | None) -> str:
    """« 950 », « 412 k », « 3,6 M »."""
    if n is None:
        return ""
    if n < 1000:
        return str(n)
    if n < 999_500:
        return f"{n / 1000:.0f} k"
    return f"{n / 1_000_000:.1f} M".replace(".", ",")


CONTEXT_WINDOW = 200_000  # fenêtre supposée tant que Claude Code n'a pas annoncé celle du modèle (à la fin de l'agent)


def read_context(run: "AgentRun", text: str) -> None:
    """Remplissage du contexte, lu dans le journal : ce que le dernier tour de l'agent a envoyé au modèle (entrée et cache).
    Marche aussi pendant que l'agent tourne. Les tours des sous-agents ne comptent pas : ils ont leur propre contexte."""
    spent, window = None, None
    for e in _events(text):
        if e.get("type") == "assistant" and not e.get("parent_tool_use_id"):
            found = message_of(e).get("usage")
            spent = found if isinstance(found, dict) else spent
        elif e.get("type") == "result":
            sizes = [m.get("contextWindow") for m in (e.get("modelUsage") or {}).values() if isinstance(m, dict)]
            window = max((s for s in sizes if isinstance(s, int)), default=None)
    if spent:
        keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        run.context_tokens = sum(spent.get(k) or 0 for k in keys)
        run.context_window = window or run.context_window or CONTEXT_WINDOW


def read_tokens(run: "AgentRun", result: dict) -> None:
    """Relève les tokens de l'évènement « result » ; rien à lire : les champs restent à None."""
    spent = result.get("usage")
    for name, _, key in TOKEN_KINDS:
        value = spent.get(key) if isinstance(spent, dict) else None
        setattr(run, name, value if isinstance(value, int) and not isinstance(value, bool) else None)


WAITING, ANSWERED = "question en attente", "répondu"
AGENT_STATUSES = {"préparation": IN_PROGRESS, "en cours": IN_PROGRESS, "terminé": DONE, WAITING: TODO, ANSWERED: DONE,
                  "échec": KO, "arrêté": KO, "délai dépassé": KO, "interrompu": KO}
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
INSTRUCTION = "Le brief de ta mission est sur l'entrée standard : lis-le en entier et suis-le."
RESUME_INSTRUCTION = "La réponse à tes questions est sur l'entrée standard : lis-la, puis reprends ta mission là où tu t'es arrêté."
QUESTIONS_HEADING = re.compile(r"^\W*questions?\b.*:\s*$", re.IGNORECASE)  # « Questions : », « **Questions ouvertes :** »


NUMBERED = re.compile(r"^\s*(?:\*\*)?\d+[.)](?:\*\*)?\s+(.*)$")  # « 1. … », « **2)** … »
BULLET = re.compile(r"^[-*•]\s+(.*)$")  # puce en début de ligne ; une puce en retrait est un détail de la question


HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")  # titre Markdown : « ## 3. Blocage n°2 : quel niveau de correctif ? »


def _clean(text: str) -> str:
    return text.strip().strip("*_ ").strip()


def _title(line: str) -> str:
    """Intitulé d'une question, sans ce qui l'habille dans le compte rendu : « ## », puce, numéro (« 3. »), gras. L'app
    numérote elle-même les questions et affiche l'intitulé sur une ligne : un « ## » y resterait tel quel."""
    text = line.strip()
    for wrapping in (HEADING, BULLET, NUMBERED):
        if found := wrapping.match(text):
            text = found.group(1).strip()
    return _clean(text)


def split_questions(summary: str) -> tuple[str, list[str]]:
    """(contexte, questions) du compte rendu. Contexte : ce que l'agent écrit avant ses questions (ce qu'il a compris, vérifié).
    Question : première ligne = la question, lignes suivantes = ses détails (contexte, options, recommandation), en Markdown.
    Après une ligne « Questions : » (consigne du brief), chaque élément numéroté ou à puce est une question ; sans cette ligne,
    chaque ligne qui finit par « ? », le reste du texte faisant le contexte. Une question posée en titre (« ## … ? ») emporte
    sa section, jusqu'au titre suivant, comme détail."""
    lines = (summary or "").splitlines()
    start = next((i for i, line in enumerate(lines) if QUESTIONS_HEADING.match(_clean(line))), None)
    items: list[tuple[str, list[str]]] = []  # (question, lignes de détail telles quelles)
    for line in lines[start + 1:] if start is not None else []:
        if item := NUMBERED.match(line) or BULLET.match(line):
            items.append((_clean(item.group(1)), []))
        elif items:
            items[-1][1].append(line)
        elif line.strip():
            items.append((_title(line), []))
    intro = lines[:start] if items else []
    if not items:  # sans « Questions : » : chaque ligne en « ? », le reste fait le contexte
        used: set[int] = set()  # lignes des questions et de leur section : hors du contexte
        for i in (i for i, line in enumerate(lines) if _clean(line).endswith("?")):
            if i in used:
                continue  # un « ? » dans la section d'une question en titre : c'est son détail
            end = i + 1
            if HEADING.match(lines[i]):
                end = next((j for j in range(i + 1, len(lines)) if HEADING.match(lines[j])), len(lines))
            items.append((_title(lines[i]), lines[i + 1:end]))
            used.update(range(i, end))
        intro = [line for i, line in enumerate(lines) if i not in used]
    # détails sans leur retrait commun : les sous-listes restent des sous-listes
    return "\n".join(intro).strip(), [
        q + (f"\n{d}" if (d := textwrap.dedent("\n".join(detail)).strip()) else "") for q, detail in items]


def answer_text(questions: "list[Question | str]", answers: list[str], extra: str = "") -> str:
    """Réponse envoyée à l'agent : chaque question suivie de sa réponse, puis la précision libre. Vide si rien n'est rempli."""
    answers, extra = [a.strip() for a in answers], extra.strip()
    if not any(answers):  # aucune réponse aux questions : la précision seule (vide : la relance est refusée)
        return f"Précision : {extra}" if extra and questions else extra
    parts = [f"Question {i} : {q}\nRéponse : {a or '(pas de réponse)'}" for i, (q, a) in enumerate(zip(questions, answers), 1)]
    return "\n\n".join(parts + ([f"Précision : {extra}"] if extra else []))


QUESTIONS_BLOCK = re.compile(r"<questions>\s*(.*?)\s*</questions>", re.S)  # questions en JSON, à la fin du compte rendu


@dataclass
class Question:
    """Question d'un agent. Donnée en JSON (bloc <questions>) : options à cocher et recommandation ; en texte : détail Markdown."""
    text: str
    detail: str = ""  # contexte, en Markdown
    options: list[tuple[str, str]] = field(default_factory=list)  # (libellé, conséquence)
    recommended: int = 0  # numéro (1…) de l'option recommandée ; 0 : aucune
    why: str = ""

    def __str__(self) -> str:  # la question telle qu'elle repart à l'agent, avec la réponse
        return self.text + (f"\n{self.detail}" if self.detail else "")


def report_of(summary: str | None) -> str:
    """Compte rendu à afficher : le texte de l'agent, sans le bloc <questions> (données lues à part)."""
    return QUESTIONS_BLOCK.sub("", summary or "").strip()


def _structured_questions(summary: str) -> list[Question]:
    """Questions du dernier bloc <questions> : {"questions": [{question, contexte, options: [{libelle, consequence}],
    recommandation (libellé ou numéro), pourquoi}]}. JSON absent, invalide ou sans question : liste vide."""
    blocks = QUESTIONS_BLOCK.findall(summary or "")
    try:
        data = json.loads(re.sub(r"^```(?:json)?|```$", "", blocks[-1].strip()).strip()) if blocks else {}
    except ValueError:
        return []
    items = data.get("questions") if isinstance(data, dict) else None

    def text(value) -> str:
        return value.strip() if isinstance(value, str) else ""

    questions = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not text(item.get("question")):
            continue
        options = [(text(o.get("libelle")), text(o.get("consequence"))) for o in item.get("options") or []
                   if isinstance(o, dict) and text(o.get("libelle"))]
        reco, labels = item.get("recommandation"), [label for label, _ in options]
        recommended = (labels.index(text(reco)) + 1 if text(reco) in labels
                       else reco if type(reco) is int and 0 < reco <= len(options) else 0)
        questions.append(Question(_title(item["question"]), text(item.get("contexte")), options, recommended, text(item.get("pourquoi"))))
    return questions


def agent_questions(summary: str | None) -> tuple[str, list[Question]]:
    """(contexte, questions) d'un agent en attente : le bloc <questions> s'il est valide (options à cocher), sinon le texte
    (split_questions). Le contexte est ce que l'agent a écrit hors de ses questions."""
    report = report_of(summary)
    if structured := _structured_questions(summary or ""):
        return report, structured
    intro, questions = split_questions(report)
    return intro, [Question(text, detail) for text, _, detail in (q.partition("\n") for q in questions)]


def chosen_answer(question: Question, choice: str, text: str) -> str:
    """Réponse à une question : l'option cochée (son libellé), puis la précision libre."""
    n = int(choice) if choice.isdigit() else 0
    picked = [f"option « {question.options[n - 1][0]} »"] if 0 < n <= len(question.options) else []
    return " · ".join(picked + ([text.strip()] if text.strip() else []))


def final_status(run: "AgentRun") -> str:
    """« terminé » sans PR, sur des questions : l'agent attend une réponse, ce n'est pas un succès."""
    return WAITING if not run.pr_url and agent_questions(run.summary)[1] else "terminé"
# Outils accordés à l'agent (JARVIS_AGENT_ALLOWED_TOOLS, séparés par « ; ») : lire, modifier, tester, committer, pousser
# sa branche, ouvrir la PR. Le reste est refusé (pas de question en mode -p). guard.sh bloque push sur main et push forcé.
# Pas de Read/Edit/Write : sans chemin ils vaudraient partout. acceptEdits autorise déjà les modifications dans le worktree
# et les --add-dir, et la lecture y est libre ; le clone de travail de l'orchestrator reste ainsi hors d'atteinte.
DEFAULT_TOOLS = ("Bash(uv run *)", "Bash(uv sync *)", "Bash(ls *)", "Bash(cat *)",
                 "Bash(grep *)", "Bash(rg *)", "Bash(find *)", "Bash(head *)", "Bash(tail *)", "Bash(wc *)",
                 "Bash(git status *)", "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)", "Bash(git ls-files *)",
                 "Bash(git grep *)", "Bash(git blame *)", "Bash(git add *)", "Bash(git commit *)", "Bash(git push *)",
                 "Bash(gh pr create *)", "Bash(gh pr view *)")


def duration_text(d: timedelta | None) -> str:
    """« 45 s », « 12 min », « 1 h 05 »."""
    if d is None:
        return ""
    seconds = max(int(d.total_seconds()), 0)
    if seconds < 60:
        return f"{seconds} s"
    return f"{seconds // 60} min" if seconds < 3600 else f"{seconds // 3600} h {seconds % 3600 // 60:02d}"


UNSORTED = "non précisé"


def usage(runs: list[AgentRun]) -> dict | None:
    """Bilan des agents réels terminés (ni simulés ni en cours) : total, et une ligne par type de ticket, le plus gourmand en tokens d'abord."""
    ended = [r for r in runs if not r.simulated and not r.running and r.ended_at]
    if not ended:
        return None

    def line(label: str, group: list[AgentRun]) -> dict:
        costs = [r.cost_usd for r in group if r.cost_usd is not None]
        spent = [r.tokens for r in group if r.tokens is not None]
        filled = [r.context_pct for r in group if r.context_pct is not None]
        kinds = [(label, sum(getattr(r, name) or 0 for r in group)) for name, label, _ in TOKEN_KINDS] if spent else []
        return {"label": label, "count": len(group), "prs": sum(bool(r.pr_url) for r in group),
                "time": sum((r.duration for r in group), timedelta()), "median": median(r.duration for r in group),
                "cost": sum(costs) if costs else None, "mean_cost": sum(costs) / len(costs) if costs else None,
                "tokens": sum(spent) if spent else None, "mean_tokens": round(sum(spent) / len(spent)) if spent else None,
                "token_kinds": kinds, "context_pct": round(sum(filled) / len(filled)) if filled else None,
                "context_max": max(filled) if filled else None}

    groups: dict[str, list[AgentRun]] = {}
    for r in ended:
        groups.setdefault(r.category or UNSORTED, []).append(r)
    rows = sorted((line(label, group) for label, group in groups.items()), key=lambda row: (-(row["tokens"] or 0), -row["count"], row["label"]))
    return {"total": line("Tous", ended), "rows": rows}


def agent_branch(*parts: str) -> str:
    """Nom de branche (et de dossier du worktree) sûr : ASCII minuscule, chiffres, tirets et « _ » (noms de ressources)."""
    text = unicodedata.normalize("NFKD", "-".join(parts)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9_]+", "-", text).strip("-_")[:100]


BRANCH_MAX = 60  # caractères : un nom de branche qu'on lit d'un coup d'œil
BRANCH_WORDS = 6  # premiers mots de la ligne du cahier de recette gardés dans le nom


def retour_branch(d: Deployment, r: RetourRecette, runs: list[AgentRun] = ()) -> str:
    """Branche d'un agent sur un retour : celle du dernier agent lancé sur ce retour (reprise, relance), sinon un nom qui
    dit le sujet, fix-<client>-<ressource ou type de données>-<premiers mots de la ligne>, sans numéro : les chiffres de
    la ligne sont retirés (ni matricule ni identifiant dans un nom poussé sur GitHub). Le numéro du retour ne s'ajoute
    que si le nom ne dit rien (ligne et type vides) ou qu'une autre branche d'agent le porte déjà."""
    subject = f"retour {r.id}"
    if (previous := next((run for run in runs if run.slug == d.slug and run.subject == subject), None)):
        return previous.branch
    topic = [r.resource or r.data_type or ""] + re.sub(r"\d+", " ", r.ligne or "").split()[:BRANCH_WORDS]
    branch = agent_branch("fix", d.slug, *topic)
    if len(branch) > BRANCH_MAX:
        branch = branch[:BRANCH_MAX].rsplit("-", 1)[0]
    taken = {run.branch for run in runs if (run.slug, run.subject) != (d.slug, subject)}
    if branch == agent_branch("fix", d.slug) or branch in taken:
        branch = agent_branch(branch, r.id)
    return branch


TO_FILL = "À COMPLÉTER"  # marqueur laissé dans le brief quand une info manque ; le lancement est refusé tant qu'il reste


def _tag(name: str, text: str) -> str:
    return f"<{name}>\n{text.strip()}\n</{name}>"


def retour_prompt(d: Deployment, r: RetourRecette, description: str = "", branch: str = "") -> str:
    """Brief pré-rempli de l'agent sur un retour de recette, pensé pour Claude Code dans integration-orchestrator.

    Contexte et contenu du retour balisés, démarche tirée de kb/anatomie-orchestrator et des patterns
    correctif-au-niveau-logiciel / requete-client-doublon-logiciel, contrat en cinq lignes, compte rendu attendu.
    La personne qui lance l'agent n'a qu'à relire, et à compléter <precisions> si besoin.
    """
    software = r.software or d.software
    branch = branch or retour_branch(d, r)
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
            "arrête-toi et pose tes questions au lieu de deviner (voir <compte_rendu>).",
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
                             "(commande et résultat), le lien de la PR, et ce qu'il faudra vérifier en recette. "
                             "Si tu t'arrêtes sur des questions (retour trop flou, choix métier), ne modifie rien, n'ouvre pas de PR et "
                             "écris d'abord en Markdown ce que tu as compris de l'écart et ce que tu as vérifié (fichiers, requêtes, "
                             "fichier:ligne, sans donnée client), sans lister tes questions. Termine par un bloc <questions>…</questions> "
                             "qui ne contient que du JSON valide : {\"questions\": [{\"question\": \"en une phrase\", \"contexte\": "
                             "\"ce que tu as constaté et où, en Markdown\", \"options\": [{\"libelle\": \"court\", \"consequence\": "
                             "\"ce que ce choix implique\"}], \"recommandation\": \"le libellé de l'option que tu recommandes\", "
                             "\"pourquoi\": \"en une phrase\"}]}. La personne verra chaque question avec ses options à cocher et ta "
                             "recommandation pré-cochée ; tu reprendras ensuite là où tu t'es arrêté, avec ses réponses."),
    ]
    return "\n\n".join(parts)


class ClaudeRunner:
    """Agents Claude, lancés pour de vrai (claude -p). L'historique survit aux redémarrages de Jarvis."""

    def __init__(self, orchestrator_dir: Path, state_dir: Path, notifications=None,
                 worktrees_dir: Path | None = None, claude_bin: str = "claude", add_dirs: tuple[Path, ...] = (),
                 allowed_tools: tuple[str, ...] = DEFAULT_TOOLS, permission_mode: str = "acceptEdits",
                 timeout_min: int = 90, max_running: int = 2, model: str = "", fallback_model: str = "", effort: str = "",
                 max_turns: int = 0, max_budget_usd: int = 0):
        self.orchestrator_dir = orchestrator_dir
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
        self.fallback_model = fallback_model
        self.effort = effort
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self._lock = threading.RLock()  # runs et agents.json : threads des agents et requêtes
        self._procs: dict[int, subprocess.Popen] = {}
        self._stopped: set[int] = set()
        self.runs: list[AgentRun] = self._load()

    @staticmethod
    def _env_options() -> dict:
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
            "fallback_model": env("JARVIS_AGENT_FALLBACK_MODEL"),
            "effort": env("JARVIS_AGENT_EFFORT"),
            "max_turns": env("JARVIS_AGENT_MAX_TURNS") and int(env("JARVIS_AGENT_MAX_TURNS")),
            "max_budget_usd": env("JARVIS_AGENT_MAX_BUDGET_USD") and int(env("JARVIS_AGENT_MAX_BUDGET_USD")),
        }
        return {k: v for k, v in kw.items() if v}

    @classmethod
    def from_env(cls, orchestrator_dir: Path, notifications, state_dir: Path) -> "ClaudeRunner":
        return cls(orchestrator_dir, state_dir, notifications, **cls._env_options())

    def reconfigure(self) -> None:
        """Réglages relus (page Réglages) : appliqués aux prochains agents ; historique et agents en cours gardés.
        Une variable vidée reprend la valeur par défaut de __init__."""
        defaults = {k: v.default for k, v in inspect.signature(ClaudeRunner.__init__).parameters.items()}
        options = {**{k: defaults[k] for k in ("claude_bin", "add_dirs", "allowed_tools", "permission_mode", "timeout_min",
                                               "max_running", "model", "fallback_model", "effort", "max_turns", "max_budget_usd")}, **self._env_options()}
        with self._lock:
            self.worktrees_dir = options.get("worktrees_dir") or self.orchestrator_dir / ".claude" / "worktrees" / "jarvis"
            self.timeout = options.pop("timeout_min") * 60
            options.pop("worktrees_dir", None)
            for key, value in options.items():
                setattr(self, key, value)

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
            if run.status == "terminé":  # agents finis avant l'état « question en attente »
                run.status = final_status(run)
            if run.running:  # le process est parti avec l'ancienne exécution de Jarvis
                run.status, run.ended_at = "interrompu", run.ended_at or datetime.now()
                run.summary = run.summary or "Jarvis a redémarré pendant que l'agent travaillait."
            runs.append(run)
        return runs

    def _save(self) -> None:
        with self._lock:
            write_json(self.state_file, [asdict(r) for r in self.runs], default=datetime.isoformat)

    def get(self, run_id: int) -> AgentRun | None:
        return next((r for r in self.runs if r.id == run_id), None)

    def log_path(self, run: AgentRun) -> Path:
        return self.state_dir / f"{run.id}.jsonl"

    def refresh_context(self, run: AgentRun) -> None:
        """Agent en cours : relit dans son journal où en est son contexte (à la fin, _work l'enregistre)."""
        log = self.log_path(run)
        if run.running and not run.simulated and log.is_file():
            read_context(run, log.read_text(encoding="utf-8", errors="replace"))

    def log(self, run: AgentRun) -> list[tuple[str, str]]:
        return read_log(self.log_path(run))

    # --- lancement --------------------------------------------------------------------------------------------
    def build_command(self, resume: str = "") -> list[str]:
        """Le brief n'est pas dans la commande (trop long) : il part sur l'entrée standard. resume : session reprise,
        l'entrée standard porte alors la réponse aux questions de l'agent."""
        cmd = [self.claude_bin, "-p", RESUME_INSTRUCTION if resume else INSTRUCTION, "--output-format", "stream-json", "--verbose",
               "--permission-mode", self.permission_mode]
        if resume:
            cmd += ["--resume", resume]
        if self.model:
            cmd += ["--model", self.model]
        for option, value in (("--fallback-model", self.fallback_model), ("--effort", self.effort), ("--max-turns", self.max_turns),
                              ("--max-budget-usd", self.max_budget_usd)):
            if value:
                cmd += [option, str(value)]
        for d in self.add_dirs:
            cmd += ["--add-dir", str(d)]
        return cmd + ["--allowedTools", *self.allowed_tools]  # en dernier : l'option prend tous les arguments qui suivent

    def problem(self) -> str | None:
        """Ce qui empêche de lancer un agent (None : rien) : dit quoi régler, au lieu de faire semblant."""
        if not shutil.which(self.claude_bin):
            return f"Claude Code en ligne de commande introuvable ({self.claude_bin}) : installe-le ou renseigne CLAUDE_BIN dans Réglages."
        if not (self.orchestrator_dir / ".git").exists():
            return f"Clone de integration-orchestrator introuvable ({self.orchestrator_dir}) : renseigne ORCHESTRATOR_DIR dans Réglages."
        origin = subprocess.run(["git", "-C", str(self.orchestrator_dir), "remote", "get-url", "origin"], capture_output=True, text=True).stdout
        if "integration-orchestrator" not in origin:  # Jarvis lui-même (ORCHESTRATOR_DIR vide) ou un autre dépôt : vu sur la VM
            return (f"{self.orchestrator_dir.resolve()} n'est pas un clone de integration-orchestrator (origin : {origin.strip() or 'aucun'}) : "
                    "renseigne ORCHESTRATOR_DIR dans Réglages, ex. /home/padoa/Dev/integration-orchestrator.")
        # Les worktrees vont dans <orchestrator>/.claude/worktrees/jarvis : un .claude à root (sudo, conteneur) les bloque.
        existing = next(p for p in (self.worktrees_dir, *self.worktrees_dir.parents) if p.exists())
        if not os.access(existing, os.W_OK):
            owned = self.orchestrator_dir / ".claude" if self.orchestrator_dir / ".claude" in (existing, *existing.parents) else existing
            user = getpass.getuser()
            return f"Jarvis ne peut pas écrire dans {existing} (worktrees des agents) : sudo chown -R {user}:{user} {owned}"
        return None

    def _launch(self, slug: str, client: str, subject: str, branch: str, prompt: str, category: str = "", **extra) -> ActionResult:
        if (problem := self.problem()):
            return ActionResult(False, problem, False)
        with self._lock:
            if any(r.running and r.branch == branch for r in self.runs):
                return ActionResult(False, f"Un agent travaille déjà sur la branche {branch} : attends sa fin ou arrête-le (Agents).", False)
            if sum(r.running for r in self.runs) >= self.max_running:
                return ActionResult(False, f"Déjà {self.max_running} agent(s) en cours : attends qu'un se termine.", False)
            run = AgentRun(max((r.id for r in self.runs), default=0) + 1, slug, client, subject, branch, prompt,
                           datetime.now(), "préparation", category=category, **extra)
            self.runs.insert(0, run)
        self._save()
        self._start(run)
        return ActionResult(True, f"Agent #{run.id} lancé sur {subject}, branche {branch}. Suivi dans Agents.", False)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.orchestrator_dir), *args], capture_output=True, text=True, timeout=300, check=check)

    def worktree_of(self, branch: str) -> Path:
        return self.worktrees_dir / branch

    def prepare_worktree(self, branch: str) -> Path:
        """Worktree de l'agent depuis origin/main ; relancé sur la même branche, il reprend le worktree existant."""
        path = self.worktree_of(branch)
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

    def _start(self, run: AgentRun) -> None:
        threading.Thread(target=self._work, args=(run,), name=f"agent-{run.id}", daemon=True).start()

    def _env(self) -> dict[str, str]:
        """Environnement du service, sans les secrets ni les réglages venus du .env de Jarvis."""
        return child_environment()

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
                proc = subprocess.Popen(self.build_command(run.resume), cwd=run.worktree, stdin=subprocess.PIPE, stdout=out,
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
        run.num_turns = result.get("num_turns")
        read_tokens(run, result)
        read_context(run, text)
        run.summary = (result.get("result") or "").strip()
        run.pr_url = find_pr(text)
        run.session_id = result.get("session_id") or run.session_id
        if run.id in self._stopped:
            status = "arrêté"
        elif timed_out:
            status = "délai dépassé"
        elif code == 0 and result.get("subtype", "success") == "success":
            status = final_status(run)
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
        parent = self.get(run.reply_to) if run.reply_to else None
        if parent and parent.status == WAITING and run.group != KO:  # la réponse a servi : l'agent d'origine n'attend plus
            parent.status = ANSWERED
        self._save()
        if self.notifications:
            ok = run.group == DONE
            report = "" if ok else run.report
            body = run.pr_url or f"{run.subject} · branche {run.branch}" + (f" · {report[:200]}" if report else "")
            title = f"Agent #{run.id} {status}{' (simulé)' if run.simulated else ''} · {run.client}"
            self.notifications.add(f"agent:{run.id}:{run.started_at.isoformat()}", "agent", "info" if ok else "warning",
                                   title, body, f"/agents/{run.id}", run.client)

    def launch(self, d: Deployment, resource: str) -> ActionResult:
        prompt = f"Ressource {resource} du client {d.client} (logiciel {d.software}) : lis la fiche de déploiement, code, teste, ouvre la PR."
        return self._launch(d.slug, d.client, resource, agent_branch(d.slug, resource), prompt, resource)

    def launch_retour(self, d: Deployment, r: RetourRecette, description: str = "", prompt: str = "") -> ActionResult:
        """prompt : le brief relu (et éventuellement modifié) par la personne qui lance l'agent ; sinon il est construit ici."""
        branch = self.retour_branch(d, r)
        return self._launch(d.slug, d.client, f"retour {r.id}", branch,
                            prompt.strip() or retour_prompt(d, r, description, branch), r.data_type or r.resource or "")

    def retour_branch(self, d: Deployment, r: RetourRecette) -> str:
        with self._lock:
            return retour_branch(d, r, list(self.runs))

    def _child(self, run_id: int, link: str) -> AgentRun | None:
        """Agent le plus récent qui pointe vers run_id par le champ link (reply_to ou relaunch_of)."""
        return next((r for r in self.runs if getattr(r, link) == run_id), None)

    def latest_on(self, branch: str) -> AgentRun | None:
        """Dernier agent lancé sur cette branche (donc sur ce retour), quel que soit son état."""
        return next((r for r in self.runs if r.branch == branch), None)

    def follow_up(self, run_id: int) -> AgentRun | None:
        """Agent relancé avec la réponse aux questions de run_id."""
        return self._child(run_id, "reply_to")

    @staticmethod
    def can_reply(run: AgentRun, follow_up: AgentRun | None) -> bool:
        """En attente de réponse, et aucune suite en cours (une suite en échec laisse répondre à nouveau)."""
        return run.status == WAITING and not (follow_up and follow_up.running)

    def relaunched(self, run_id: int) -> AgentRun | None:
        """Agent qui relance run_id après son erreur."""
        return self._child(run_id, "relaunch_of")

    def relaunch(self, run_id: int) -> ActionResult:
        """Relance d'un agent fini en erreur (échec, arrêté, délai dépassé, interrompu) : nouvel agent, même branche et même brief,
        nouvelle session. Le worktree existant est repris, avec ce que l'agent y avait déjà fait."""
        old = self.get(run_id)
        if not old or old.group != KO:
            return ActionResult(False, f"L'agent #{run_id} n'est pas en erreur : rien à relancer.", False)
        prompt = old.prompt if not old.resume else "\n\n".join([self._brief(old), _tag("reponse", old.prompt)])  # reprise : réponse seule
        return self._launch(old.slug, old.client, old.subject, old.branch, prompt, old.category, relaunch_of=old.id)

    def _brief(self, run: AgentRun) -> str:
        """Brief d'origine : un agent qui a repris une session n'a reçu que la réponse, on remonte à celui qui l'a lancée."""
        while run.resume and run.reply_to and (parent := self.get(run.reply_to)):
            run = parent
        return run.prompt

    def reply(self, run_id: int, answer: str) -> ActionResult:
        """Réponse aux questions d'un agent : nouvel agent sur la même branche. Si le worktree n'a pas bougé, il reprend la session
        Claude (--resume) et garde ce qu'il avait lu ; sinon il repart du brief, avec le compte rendu et la réponse."""
        old, answer = self.get(run_id), (answer or "").strip()
        previous = old and self.follow_up(old.id)  # suite déjà tentée, donc en échec : on ne reprend plus sa session
        if not old or not self.can_reply(old, previous):
            return ActionResult(False, f"L'agent #{run_id} n'attend pas de réponse.", False)
        if not answer:
            return ActionResult(False, "Écris ta réponse avant de relancer l'agent.", False)
        here = self.worktree_of(old.branch)  # celui que prepare_worktree reprendra
        same_place = not previous and bool(old.session_id) and old.worktree == str(here) and (here / ".git").exists()
        prompt = answer if same_place else "\n\n".join([
            self._brief(old),
            _tag("echange_precedent", f"Un agent (#{old.id}) a commencé ce travail et s'est arrêté sur des questions. Son compte rendu :\n{old.summary}"),
            _tag("reponse", answer),
            "Reprends la mission en tenant compte de cette réponse."])
        result = self._launch(old.slug, old.client, old.subject, old.branch, prompt, old.category,
                              reply_to=old.id, resume=old.session_id if same_place else "")
        return result


def message_of(event: dict) -> dict:
    """Message d'un événement du journal de claude -p : un objet en général, mais Claude Code écrit aussi des événements
    où « message » est un simple texte (vu avec 2.1.326 : la page de l'agent plantait en erreur 500)."""
    message = event.get("message") if isinstance(event, dict) else None
    return message if isinstance(message, dict) else {}


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
    """Dernier lien de PR cité par l'agent (ses messages, la sortie de ses outils, son compte rendu). Les évènements
    « system » (démarrage, réponses des hooks) sont ignorés : un hook de la session peut y injecter les liens d'autres PR."""
    found = []
    for e in _events(text or ""):
        if e.get("type") != "system":
            found += PR_URL.findall(e["text"] if e.get("type") == "raw" else json.dumps(e, ensure_ascii=False))
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
            for block in message_of(e).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    lines.append(("texte", block["text"].strip()))
                elif block.get("type") == "tool_use":
                    lines.append(("outil", _tool_line(block)))
        elif kind == "user":
            for block in message_of(e).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    content = block.get("content")
                    text = content if isinstance(content, str) else " ".join(c.get("text", "") for c in content or [] if isinstance(c, dict))
                    lines.append(("refus", text.strip()[:400]))
        elif kind == "result":
            lines.append(("fin", f"{e.get('subtype', '')} · {e.get('num_turns', '?')} tours"))
        elif kind == "raw":
            lines.append(("brut", e["text"][:400]))
    return lines
