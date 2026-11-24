"""Description d'une demande BAU : le corps de sa page Notion, que la synchro SQL ne voit pas (elle ne lit que les colonnes).

À l'ouverture d'une demande, Jarvis fait lire sa page par Claude Code (`claude -p`, connecteur Notion, lecture seule,
modèle rapide) : une page à la fois, seulement celle qu'on regarde. Claude ne fait qu'appeler notion-fetch ; Jarvis va
chercher la réponse lui-même dans le transcript de la session (ou dans le fichier où Claude Code l'a rangée), en garde le
contenu (la partie <content> de la page) dans data/notion/_bau/descriptions/<réf>.json, hors git, avec la date de dernière
modification de la demande : une demande modifiée dans Notion est relue à sa prochaine ouverture.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path

from deploy_hub.bau import Demande, folder as bau_folder
from deploy_hub.safe_write import write_json
from deploy_hub.sources.sync_routine import NOTION_SERVERS, app_env
from deploy_hub.sync_notion import CLAUDE_PROJECTS, SAVED_TO, SyncError, _saved_text
from deploy_hub.config import PROJECT_ROOT

INSTRUCTION = "Suis la consigne reçue sur l'entrée standard (description d'une demande BAU pour Jarvis)."
PROMPT = """Lecture seule : ne modifie rien dans Notion, n'écris aucun fichier, ne recopie ni ne résume ce que tu lis.

1. Outil notion-fetch, id = {url}
2. Réponds seulement « fini » (ou « ÉCHEC : <raison> » si l'outil a échoué).
"""
MODEL = "haiku"  # Claude ne fait qu'un appel d'outil : un modèle rapide suffit
MAX_READERS = 2  # lectures en même temps au plus ; les suivantes partent quand une se termine (la page redemande)
NOTION_FETCH = "notion-fetch"
CONTENT = re.compile(r"<content>\s*(.*?)\s*</content>", re.S)


# ------------------------------------------------------------------------------------------- page Notion → Markdown
HEADING_TAG = re.compile(r"<h([1-6])[^>]*>\s*(.*?)\s*</h\1>", re.S)
INLINE = (("b", "**"), ("strong", "**"), ("i", "*"), ("em", "*"), ("code", "`"), ("s", "~~"))
TAG = re.compile(r"</?[a-zA-Z][\w-]*(?:\s[^<>]*)?/?>")


def as_markdown(content: str) -> str:
    """Contenu d'une page Notion (« Notion-flavored Markdown » : Markdown et quelques balises) en Markdown simple, que la
    page affiche sans HTML brut (rendu.md). Gras, italique et code gardés ; titres en « # » ; images et autres blocs
    signalés ou réduits à leur texte. Rien n'est interprété : une balise inconnue est retirée, son texte reste."""
    text = HEADING_TAG.sub(lambda m: "\n" + "#" * min(int(m.group(1)) + 1, 6) + " " + " ".join(m.group(2).split()) + "\n", content)
    for tag, mark in INLINE:
        text = re.sub(rf"<{tag}(?:\s[^<>]*)?>(.*?)</{tag}>", lambda m: mark + m.group(1) + mark if m.group(1).strip() else m.group(1),
                      text, flags=re.S)
    text = text.replace(f"**{'**'}", "")  # « <b>a</b><b>b</b> » : pas de « **** » au milieu
    text = re.sub(r"<image\b[^>]*/?>", "*(image : voir la page Notion)*", text)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = TAG.sub("", text)  # <p />, <span>, <callout>, <mention-…>… : le texte reste, la balise part
    text = re.sub(r"^(#+ .*)$", r"\n\1", text, flags=re.M)  # titre ramené de fin de ligne : précédé d'une ligne vide
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def description_of(fetched: str) -> str:
    """Description d'une page Notion lue par notion-fetch (réponse JSON {"text": …} ou texte brut) : son contenu, en
    Markdown ; "" si la page n'a pas de contenu."""
    try:
        data = json.loads(fetched)
        fetched = str(data.get("text") or "") if isinstance(data, dict) else fetched
    except ValueError:
        pass
    found = CONTENT.search(fetched)
    return as_markdown(found.group(1)) if found else ""


def fetched_in_session(session: str, projects: Path = CLAUDE_PROJECTS) -> str | None:
    """Réponse de notion-fetch dans le transcript de cette session Claude Code (rendue en ligne, ou rangée dans un fichier
    quand elle est grosse) ; None si l'outil n'a pas répondu, ou en erreur."""
    asked, found = set(), None
    for path in projects.glob(f"*/{session}.jsonl"):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                content = json.loads(line)["message"]["content"]
            except (ValueError, KeyError, TypeError):
                continue
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use" and str(part.get("name", "")).endswith(NOTION_FETCH):
                    asked.add(part.get("id"))
                elif part.get("type") == "tool_result" and part.get("tool_use_id") in asked and not part.get("is_error"):
                    raw = part.get("content")
                    text = raw if isinstance(raw, str) else "".join(p.get("text", "") for p in raw or [] if isinstance(p, dict))
                    if not text.lstrip().startswith("{") and (m := SAVED_TO.search(text[:2000])):
                        try:
                            text = _saved_text(Path(m[1]))
                        except (SyncError, OSError):
                            continue
                    if "<page" in text:
                        found = text
    return found


class DescriptionReader:
    """Descriptions lues (data/notion/_bau/descriptions/) et lectures en cours, une par demande, MAX_READERS au plus."""

    def __init__(self, folder: Path | None = None, claude_bin: str | None = None, timeout_s: int = 180,
                 projects: Path = CLAUDE_PROJECTS):
        self._folder = folder
        self._claude_bin = claude_bin
        self.timeout = timeout_s
        self.projects = projects
        self._lock = threading.Lock()
        self._reading: dict[str, threading.Thread] = {}

    @property
    def folder(self) -> Path:
        return self._folder or bau_folder() / "descriptions"

    @property
    def claude_bin(self) -> str:
        return self._claude_bin or os.getenv("CLAUDE_BIN") or "claude"

    def saved(self, ref: str) -> dict:
        """{texte, modifie, lu} de la dernière lecture réussie, {erreur, …} d'un échec, ou {} si jamais lue."""
        try:
            data = json.loads((self.folder / f"{ref}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def is_fresh(self, x: Demande) -> bool:
        """Lue depuis la dernière modification connue de la demande (même un échec : on ne relit pas en boucle)."""
        return bool(saved := self.saved(x.ref)) and saved.get("modifie") == x.modified

    def text(self, x: Demande) -> str:
        """Description gardée (même d'une version plus ancienne de la page) ; "" si aucune."""
        return str(self.saved(x.ref).get("texte") or "")

    def reading(self, ref: str) -> bool:
        thread = self._reading.get(ref)
        return thread is not None and thread.is_alive()

    def problem(self) -> str:
        if not shutil.which(self.claude_bin):
            return f"Claude Code en ligne de commande introuvable ({self.claude_bin}) : renseigne CLAUDE_BIN dans Réglages."
        return ""

    def ensure(self, x: Demande, again: bool = False) -> bool:
        """Lance la lecture de la page si sa description n'est pas à jour (again : même si elle l'est). False si rien n'est
        parti : à jour, déjà en lecture, Claude Code absent, ou trop de lectures en cours (la page redemandera)."""
        if (not again and self.is_fresh(x)) or not x.notion_url or self.problem():
            return False
        with self._lock:
            if self.reading(x.ref) or sum(t.is_alive() for t in self._reading.values()) >= MAX_READERS:
                return False
            thread = threading.Thread(target=self._read, args=(x,), name=f"jarvis-bau-{x.ref}", daemon=True)
            self._reading[x.ref] = thread
            thread.start()
        return True

    def command(self, session: str) -> list[str]:
        return [self.claude_bin, "-p", INSTRUCTION, "--model", MODEL, "--output-format", "text", "--permission-mode", "dontAsk",
                "--session-id", session, "--allowedTools", *NOTION_SERVERS]

    def _read(self, x: Demande) -> None:
        session = str(uuid.uuid4())
        try:
            proc = subprocess.Popen(self.command(session), cwd=PROJECT_ROOT, env=app_env(), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            out, _ = proc.communicate(PROMPT.format(url=x.notion_url), timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            return self._save(x, erreur="délai dépassé")
        except OSError as e:
            return self._save(x, erreur=f"Claude Code n'a pas pu être lancé ({type(e).__name__})")
        fetched = fetched_in_session(session, self.projects)
        if fetched is None:
            return self._save(x, erreur="page non lue" + (f" : {out.strip()[-160:]}" if "ÉCHEC" in (out or "") else ""))
        self._save(x, texte=description_of(fetched))

    def _save(self, x: Demande, **result) -> None:
        kept = {} if "texte" in result else {k: v for k, v in self.saved(x.ref).items() if k == "texte"}  # un échec garde l'ancien texte
        write_json(self.folder / f"{x.ref}.json", {**kept, **result, "modifie": x.modified, "lu": datetime.now().isoformat(timespec="seconds")})
