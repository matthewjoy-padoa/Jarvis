"""Ce qui n'appartient qu'à moi : tickets épinglés, notes libres et to-do list par client, rappels.

Tout est dans data/perso.json (hors git : titres de tickets et notes). Rien n'est écrit dans Notion.
"""

import json
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from deploy_hub.safe_write import write_json

NOTE_MAX = 20_000  # caractères par note
LABEL_MAX = 200
_lock = threading.Lock()


@dataclass
class Reminder:
    id: int
    due: date
    label: str
    slug: str = ""  # client concerné ("" : aucun)
    kind: str = ""  # retour | action ("" : rappel libre, sans ticket)
    ref: str = ""

    def to_json(self) -> dict:
        return {**asdict(self), "due": self.due.isoformat()}

    @classmethod
    def from_json(cls, raw: dict) -> "Reminder":
        return cls(**{**raw, "due": date.fromisoformat(raw["due"])})


def remind_date(when: str, day: str = "", today: date | None = None) -> date | None:
    """Date d'un rappel : demain, lundi (le prochain, jamais aujourd'hui), semaine (dans 7 jours) ou une date AAAA-MM-JJ à venir."""
    today = today or date.today()
    if when == "demain":
        return today + timedelta(days=1)
    if when == "lundi":
        return today + timedelta(days=7 - today.weekday())
    if when == "semaine":
        return today + timedelta(days=7)
    try:
        found = date.fromisoformat(day)
    except ValueError:
        return None
    return found if found >= today else None


class PersonalStore:
    def __init__(self, path: Path):
        self.path = path
        self._cache: tuple[int, dict] | None = None  # (mtime du fichier, contenu) : relu seulement s'il a changé

    def _read(self) -> dict:
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        # Les clés inconnues sont gardées : une version de Jarvis qui ne les connaît pas encore ne les efface pas en réécrivant.
        return {**data, "pins": data.get("pins", []), "notes": data.get("notes", {}), "reminders": data.get("reminders", []),
                "todos": data.get("todos", {}), "todo_seq": data.get("todo_seq", 0)}

    def _load(self) -> dict:
        """Contenu pour lecture seule (ne pas le modifier : il est partagé entre les appels)."""
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            mtime = 0
        cache = self._cache
        if cache is None or cache[0] != mtime:
            cache = self._cache = (mtime, self._read())
        return cache[1]

    def _change(self, edit):
        """Lit, modifie et réécrit d'un bloc (fichier temporaire puis remplacement) ; renvoie ce que renvoie « edit »."""
        with _lock:
            data = self._read()
            out = edit(data)
            write_json(self.path, data)
            self._cache = None
            return out

    # ------------------------------------------------------------ Épingles
    def pins(self, slug: str = "") -> list[dict]:
        """Tickets épinglés (le plus récent d'abord), de tous les clients ou d'un seul."""
        return [p for p in reversed(self._load()["pins"]) if not slug or p["slug"] == slug]

    def is_pinned(self, slug: str, kind: str, ref: str) -> bool:
        return any((p["slug"], p["kind"], p["ref"]) == (slug, kind, ref) for p in self._load()["pins"])

    def toggle_pin(self, slug: str, kind: str, ref: str) -> bool:
        """Épingle ou désépingle ; renvoie le nouvel état."""
        def edit(data):
            kept = [p for p in data["pins"] if (p["slug"], p["kind"], p["ref"]) != (slug, kind, ref)]
            pinned = len(kept) == len(data["pins"])
            if pinned:
                kept.append({"slug": slug, "kind": kind, "ref": ref, "at": datetime.now().isoformat(timespec="seconds")})
            data["pins"] = kept
            return pinned
        return self._change(edit)

    # ------------------------------------------------------------ Notes
    def note(self, slug: str) -> str:
        return self._load()["notes"].get(slug, "")

    def set_note(self, slug: str, text: str) -> None:
        text = text.strip()[:NOTE_MAX]

        def edit(data):
            if text:
                data["notes"][slug] = text
            else:
                data["notes"].pop(slug, None)
        self._change(edit)

    # ------------------------------------------------------------ To-do list
    def todos(self, slug: str) -> list[dict]:
        """Tâches d'un client ("" : générales), {id, label, done} : à faire d'abord, puis faites, chacune dans l'ordre d'ajout."""
        return sorted(self._load()["todos"].get(slug, []), key=lambda t: t["done"])

    def todos_left(self, slug: str) -> int:
        return sum(not t["done"] for t in self._load()["todos"].get(slug, []))

    def add_todo(self, slug: str, label: str) -> None:
        label = " ".join(label.split())[:LABEL_MAX]
        if not label:
            return

        def edit(data):
            data["todo_seq"] += 1  # compteur gardé : le numéro d'une tâche supprimée n'est jamais redonné (une autre vue
            # de la même liste, restée ouverte, ne doit pas cocher une nouvelle tâche à la place de l'ancienne)
            data["todos"].setdefault(slug, []).append({"id": data["todo_seq"], "label": label, "done": False})
        self._change(edit)

    def _edit_todo(self, todo_id: int, drop: bool) -> str | None:
        """Coche ou décoche cette tâche (drop : la supprime) ; renvoie son client (None : tâche inconnue)."""
        def edit(data):
            for slug, items in data["todos"].items():
                for t in items:
                    if t["id"] == todo_id:
                        if drop:
                            items.remove(t)
                        else:
                            t["done"] = not t["done"]
                        if not items:
                            del data["todos"][slug]
                        return slug
            return None
        return self._change(edit)

    def toggle_todo(self, todo_id: int) -> str | None:
        return self._edit_todo(todo_id, drop=False)

    def remove_todo(self, todo_id: int) -> str | None:
        return self._edit_todo(todo_id, drop=True)

    # ------------------------------------------------------------ Rappels
    def reminders(self, slug: str | None = None) -> list[Reminder]:
        """Rappels par date ; slug donné : ceux de ce client et ceux qui n'en ont pas."""
        found = [Reminder.from_json(r) for r in self._load()["reminders"]]
        return sorted((r for r in found if slug is None or r.slug in ("", slug)), key=lambda r: (r.due, r.id))

    def due(self, today: date) -> list[Reminder]:
        """Rappels arrivés à échéance (brief du matin et notifications)."""
        return [r for r in self.reminders() if r.due <= today]

    def reminders_for(self, slug: str, kind: str, ref: str) -> list[Reminder]:
        return [r for r in self.reminders() if (r.slug, r.kind, r.ref) == (slug, kind, ref)]

    def add_reminder(self, due: date, label: str, slug: str = "", kind: str = "", ref: str = "") -> Reminder:
        def edit(data):
            reminder = Reminder(max((r["id"] for r in data["reminders"]), default=0) + 1, due, label.strip()[:LABEL_MAX], slug, kind, ref)
            data["reminders"].append(reminder.to_json())
            return reminder
        return self._change(edit)

    def remove_reminder(self, reminder_id: int) -> None:
        def edit(data):
            data["reminders"] = [r for r in data["reminders"] if r["id"] != reminder_id]
        self._change(edit)
