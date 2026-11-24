"""Description d'une demande BAU : le corps de sa page Notion, que la synchro SQL ne voit pas (elle ne lit que les colonnes).

La synchro charge d'abord les demandes (et finit) ; les descriptions se lisent ensuite en fond, sans rien bloquer :
- catch_up, appelé chaque minute par Jarvis : un lot de BATCH_SIZE pages à relire (demandes ouvertes jamais lues, ou
  modifiées dans Notion depuis leur lecture ; les plus récentes d'abord), puis le lot suivant à la minute d'après,
  jusqu'à ce que tout soit à jour ; ensuite, seules les demandes modifiées sont relues ;
- ensure, à l'ouverture d'une demande dont la description n'est pas encore à jour : sa page seule, tout de suite.

Les deux font lire les pages par Claude Code (`claude -p`, connecteur Notion, lecture seule, modèle rapide) : Claude ne
fait qu'appeler notion-fetch ; Jarvis prend les réponses lui-même dans le transcript de la session (ou dans le fichier où
Claude Code a rangé une grosse réponse) et garde le contenu de chaque page (sa partie <content>, en Markdown) dans
data/notion/_bau/descriptions/<réf>.json, hors git, avec la date de dernière modification de la demande lue.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from deploy_hub.bau import Demande, folder as bau_folder
from deploy_hub.safe_write import write_json
from deploy_hub.sources.sync_routine import MCP_OUTPUT_TOKENS, NOTION_SERVERS, app_env
from deploy_hub.sync_notion import CLAUDE_PROJECTS, SAVED_TO, SyncError, _page_id, _saved_text
from deploy_hub.config import PROJECT_ROOT

INSTRUCTION = "Suis la consigne reçue sur l'entrée standard (descriptions de demandes BAU pour Jarvis)."
PROMPT = """Lecture seule : ne modifie rien dans Notion, n'écris aucun fichier, ne recopie ni ne résume ce que tu lis.

1. Outil notion-fetch pour chacune de ces pages, toutes dans le même message (plusieurs appels d'outil à la fois), id = l'adresse telle quelle :
{urls}
2. Réponds seulement « fini » (ou « ÉCHEC : <raison> » si l'outil a échoué).
"""
MODEL = "haiku"  # Claude ne fait qu'appeler un outil : un modèle rapide suffit
MAX_READERS = 2  # lectures d'une seule page (ouverture d'une demande) en même temps au plus
BATCH_SIZE = 15  # pages par lot de fond : les petites réponses restent dans le contexte du modèle
RETRY_AFTER = timedelta(minutes=10)  # lot de fond sans aucune réponse (Claude déconnecté…) : pas de nouvel essai avant
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


def fetched_pages_in_session(session: str, projects: Path = CLAUDE_PROJECTS) -> dict[str, str]:
    """Réponses de notion-fetch dans le transcript de cette session Claude Code (rendues en ligne, ou rangées dans un
    fichier quand elles sont grosses) : {id de la page demandée : réponse}. Les erreurs de l'outil sont laissées de côté."""
    asked, found = {}, {}
    for path in projects.glob(f"*/{session}.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                content = json.loads(line)["message"]["content"]
            except (ValueError, KeyError, TypeError):
                continue
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use" and str(part.get("name", "")).endswith(NOTION_FETCH):
                    asked[part.get("id")] = _page_id(str((part.get("input") or {}).get("id") or ""))
                elif part.get("type") == "tool_result" and part.get("tool_use_id") in asked and not part.get("is_error"):
                    raw = part.get("content")
                    text = raw if isinstance(raw, str) else "".join(p.get("text", "") for p in raw or [] if isinstance(p, dict))
                    if not text.lstrip().startswith("{") and (m := SAVED_TO.search(text[:2000])):
                        try:
                            text = _saved_text(Path(m[1]))
                        except (SyncError, OSError):
                            continue
                    if "<page" in text:
                        found[asked[part["tool_use_id"]]] = text
    return found


def descriptions_dir(root: Path = PROJECT_ROOT) -> Path:
    return bau_folder(root) / "descriptions"


def saved(ref: str, root: Path = PROJECT_ROOT, folder: Path | None = None) -> dict:
    """{texte, modifie, lu} de la dernière lecture réussie, {erreur, …} d'un échec, ou {} si jamais lue."""
    try:
        data = json.loads(((folder or descriptions_dir(root)) / f"{ref}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(ref: str, modified: str, folder: Path, **result) -> None:
    """Range une lecture : le texte (même vide), ou une erreur qui garde l'ancien texte ; avec la date de la demande lue."""
    kept = {} if "texte" in result else {k: v for k, v in saved(ref, folder=folder).items() if k == "texte"}
    write_json(folder / f"{ref}.json", {**kept, **result, "modifie": modified, "lu": datetime.now().isoformat(timespec="seconds")})


class DescriptionReader:
    """Descriptions lues (data/notion/_bau/descriptions/), lecture de fond par lots et lectures d'une page à l'ouverture."""

    def __init__(self, folder: Path | None = None, claude_bin: str | None = None, timeout_s: int = 300,
                 projects: Path = CLAUDE_PROJECTS):
        self._folder = folder
        self._claude_bin = claude_bin
        self.timeout = timeout_s
        self.projects = projects
        self._lock = threading.Lock()
        self._reading: dict[str, threading.Thread] = {}  # lectures d'une page (ouverture d'une demande)
        self._batch: threading.Thread | None = None  # lot de fond en cours
        self._batch_refs: set[str] = set()
        self._batch_failed: datetime | None = None

    @property
    def folder(self) -> Path:
        return self._folder or descriptions_dir()

    @property
    def claude_bin(self) -> str:
        return self._claude_bin or os.getenv("CLAUDE_BIN") or "claude"

    def saved(self, ref: str) -> dict:
        return saved(ref, folder=self.folder)

    def is_fresh(self, x: Demande) -> bool:
        """Lue depuis la dernière modification connue de la demande (même un échec : on ne relit pas en boucle)."""
        return bool(saved := self.saved(x.ref)) and saved.get("modifie") == x.modified

    def text(self, x: Demande) -> str:
        """Description gardée (même d'une version plus ancienne de la page) ; "" si aucune."""
        return str(self.saved(x.ref).get("texte") or "")

    @property
    def batch_running(self) -> bool:
        return self._batch is not None and self._batch.is_alive()

    def reading(self, ref: str) -> bool:
        thread = self._reading.get(ref)
        return (thread is not None and thread.is_alive()) or (self.batch_running and ref in self._batch_refs)

    def problem(self) -> str:
        if not shutil.which(self.claude_bin):
            return f"Claude Code en ligne de commande introuvable ({self.claude_bin}) : renseigne CLAUDE_BIN dans Réglages."
        return ""

    def stale(self, demandes: list[Demande]) -> list[Demande]:
        """Demandes ouvertes dont la description n'est pas à jour, les plus récemment modifiées d'abord."""
        todo = [x for x in demandes if x.is_open and x.notion_url and not self.is_fresh(x)]
        return sorted(todo, key=lambda x: x.modified, reverse=True)

    def progress(self, demandes: list[Demande]) -> dict:
        """Où en est la lecture des descriptions des demandes ouvertes : {lues, total, en_cours}."""
        opened = [x for x in demandes if x.is_open and x.notion_url]
        return {"lues": sum(self.is_fresh(x) for x in opened), "total": len(opened), "en_cours": self.batch_running}

    # --- lecture de fond, par lots ----------------------------------------------------------------------------------
    def catch_up(self, demandes: list[Demande]) -> bool:
        """Lance un lot de BATCH_SIZE pages à relire, s'il y en a et qu'aucun lot ne tourne ; True s'il est parti.
        Appelé chaque minute : les lots s'enchaînent jusqu'à ce que tout soit à jour. Après un lot sans aucune réponse,
        pas de nouvel essai avant RETRY_AFTER."""
        if self.batch_running or self.problem():
            return False
        if self._batch_failed and datetime.now() - self._batch_failed < RETRY_AFTER:
            return False
        with self._lock:
            todo = [x for x in self.stale(demandes) if not self.reading(x.ref)][:BATCH_SIZE]
            if not todo:
                return False
            self._batch_refs = {x.ref for x in todo}
            self._batch = threading.Thread(target=self._read_batch, args=(todo,), name="jarvis-bau-descriptions", daemon=True)
            self._batch.start()
        return True

    def _read_batch(self, todo: list[Demande]) -> None:
        pages, error = self._fetch(todo)
        for x in todo:
            if (text := pages.get(_page_id(x.notion_url))) is not None:
                self._save(x, texte=description_of(text))
            elif pages:  # le lot a marché, pas cette page : notée, elle sera relue si la demande change (ou sur « Réessayer »)
                self._save(x, erreur="page non lue")
        self._batch_failed = None if pages else datetime.now()

    # --- une page, à l'ouverture d'une demande -----------------------------------------------------------------------
    def ensure(self, x: Demande, again: bool = False) -> bool:
        """Lance la lecture de la page si sa description n'est pas à jour (again : même si elle l'est). False si rien n'est
        parti : à jour, déjà en lecture (seule ou dans un lot), Claude Code absent, ou trop de lectures en cours."""
        if (not again and self.is_fresh(x)) or not x.notion_url or self.problem():
            return False
        with self._lock:
            if self.reading(x.ref) or sum(t.is_alive() for t in self._reading.values()) >= MAX_READERS:
                return False
            thread = threading.Thread(target=self._read, args=(x,), name=f"jarvis-bau-{x.ref}", daemon=True)
            self._reading[x.ref] = thread
            thread.start()
        return True

    def _read(self, x: Demande) -> None:
        pages, error = self._fetch([x])
        if (text := pages.get(_page_id(x.notion_url))) is not None:
            self._save(x, texte=description_of(text))
        else:  # toujours noté : sinon la page, qui redemande toutes les 3 s, relancerait une lecture à chaque fois
            self._save(x, erreur=error or "page non lue")

    # --- commun ------------------------------------------------------------------------------------------------------
    def command(self, session: str) -> list[str]:
        return [self.claude_bin, "-p", INSTRUCTION, "--model", MODEL, "--output-format", "text", "--permission-mode", "dontAsk",
                "--session-id", session, "--allowedTools", *NOTION_SERVERS]

    def _fetch(self, todo: list[Demande]) -> tuple[dict[str, str], str]:
        """Fait lire ces pages par Claude Code ; ({id de page : réponse}, raison d'un échec)."""
        session = str(uuid.uuid4())
        env = {**app_env(), "MAX_MCP_OUTPUT_TOKENS": MCP_OUTPUT_TOKENS}  # grosses pages rangées dans des fichiers, pas dans le contexte
        try:
            proc = subprocess.Popen(self.command(session), cwd=PROJECT_ROOT, env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            out, _ = proc.communicate(PROMPT.format(urls="\n".join(f"- {x.notion_url}" for x in todo)), timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out = "délai dépassé"
        except OSError as e:
            return {}, f"Claude Code n'a pas pu être lancé ({type(e).__name__})"
        try:  # même après un délai dépassé : les pages déjà lues sont prises
            pages = fetched_pages_in_session(session, self.projects)
        except Exception as e:  # noqa: BLE001 : un échec est toujours rendu, jamais une exception qui tuerait le fil
            return {}, f"réponse illisible ({type(e).__name__})"
        if pages:
            return pages, ""
        return {}, (out or "").strip()[-160:] if "ÉCHEC" in (out or "") or out == "délai dépassé" else ""

    def _save(self, x: Demande, **result) -> None:
        save(x.ref, x.modified, self.folder, **result)
