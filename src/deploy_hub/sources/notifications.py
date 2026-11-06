"""Notifications de l'app : agent terminé, changements urgents après une synchro, deadline du jour.

Stockées dans data/notifications.json (exclu de git : titres de retours et de cartes). Chaque notification a une clé :
une même clé n'est jamais notifiée deux fois (ex. « deadline:<url>:<date> »). Les 500 plus récentes sont gardées.
"""

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

MAX_KEPT = 500
LEVELS = ("urgent", "info")
KINDS = {"agent": "bot", "sync": "sync", "deadline": "calendar", "dag": "play"}  # icônes de templates/_icons.html
_lock = threading.Lock()


@dataclass
class Notification:
    id: int
    key: str
    kind: str  # agent | sync | deadline | dag
    level: str  # urgent | info
    title: str
    detail: str = ""
    link: str = ""
    client: str = ""
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    read: bool = False

    def __post_init__(self):
        # Liens Notion enregistrés avant la correction du « /p/ » (models.notion_link) : rendus ouvrables.
        from deploy_hub.models import notion_link
        self.link = notion_link(self.link)

    @property
    def icon(self) -> str:
        return KINDS.get(self.kind, "bell")

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.created)


class NotificationStore:
    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else []

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items[-MAX_KEPT:], ensure_ascii=False), encoding="utf-8")

    def add_many(self, notes: list[dict]) -> list[Notification]:
        """Ajoute les notifications dont la clé est nouvelle ; renvoie celles ajoutées."""
        with _lock:
            items = self._load()
            keys = {i["key"] for i in items}
            next_id = max((i["id"] for i in items), default=0) + 1
            added = []
            for n in notes:
                if n["key"] in keys:
                    continue
                note = Notification(id=next_id, **n)
                items.append(asdict(note))
                keys.add(note.key)
                added.append(note)
                next_id += 1
            if added:
                self._save(items)
            return added

    def add(self, key: str, kind: str, level: str, title: str, detail: str = "", link: str = "", client: str = "") -> Notification | None:
        added = self.add_many([{"key": key, "kind": kind, "level": level, "title": title, "detail": detail, "link": link, "client": client}])
        return added[0] if added else None

    def all(self) -> list[Notification]:
        return [Notification(**i) for i in reversed(self._load())]

    def unread(self) -> list[Notification]:
        return [n for n in self.all() if not n.read]

    def after(self, last_id: int) -> list[Notification]:
        return [n for n in self.all() if n.id > last_id]

    @property
    def state_path(self) -> Path:
        return self.path.with_name("etat-veilles.json")

    def load_state(self) -> dict:
        """État des veilles (ex. dernière pastille vue par client), à côté du fichier des notifications."""
        return json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.is_file() else {}

    def save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def mark_read(self, ids: list[int] | None = None) -> None:
        with _lock:
            items = self._load()
            for i in items:
                if ids is None or i["id"] in ids:
                    i["read"] = True
            self._save(items)
