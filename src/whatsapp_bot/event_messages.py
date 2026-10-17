"""Persistent references to event messages posted by the bot."""

from __future__ import annotations

import json
import os
import secrets
import string
from dataclasses import asdict, dataclass
from pathlib import Path


_MAX_ENTRIES = 1000
_UID_ALPHABET = string.ascii_uppercase + string.digits


@dataclass(frozen=True, slots=True)
class EventMessage:
    """A bot-posted event message that an administrator may filter."""

    uid: str
    event_id: int
    message_id: str
    remote_jid: str


class EventMessageMap:
    """Maps six-character event UIDs to bot messages, persisted after changes."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._entries: dict[str, EventMessage] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text())
        if not isinstance(raw, dict):
            raise RuntimeError("Event message map must be a JSON object")
        for uid, value in raw.items():
            if not isinstance(value, dict):
                raise RuntimeError("Event message map entry must be an object")
            self._entries[uid] = EventMessage(
                uid=uid,
                event_id=int(value["event_id"]),
                message_id=str(value["message_id"]),
                remote_jid=str(value["remote_jid"]),
            )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {uid: asdict(entry) for uid, entry in self._entries.items()}
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True))
        os.replace(temporary, self._path)

    def _new_uid(self) -> str:
        while True:
            uid = "".join(secrets.choice(_UID_ALPHABET) for _ in range(6))
            if uid not in self._entries:
                return uid

    def new_uid(self) -> str:
        """Return an unused six-character reference for a new event message."""
        return self._new_uid()

    def track(
        self, uid: str, event_id: int, message_id: str, remote_jid: str
    ) -> EventMessage:
        """Store and return a new reference for a bot-posted event message."""
        entry = EventMessage(
            uid=uid.upper(),
            event_id=event_id,
            message_id=message_id,
            remote_jid=remote_jid,
        )
        self._entries[entry.uid] = entry
        while len(self._entries) > _MAX_ENTRIES:
            oldest = next(iter(self._entries))
            del self._entries[oldest]
        self._save()
        return entry

    def get(self, uid: str) -> EventMessage | None:
        """Return the tracked event message for a case-insensitive UID."""
        return self._entries.get(uid.upper())

    def get_by_message_id(self, message_id: str) -> EventMessage | None:
        """Return the tracked event message for an Evolution message ID."""
        return next(
            (entry for entry in self._entries.values() if entry.message_id == message_id),
            None,
        )

    def remove(self, uid: str) -> None:
        """Remove an entry after its WhatsApp message has been deleted."""
        self._entries.pop(uid.upper(), None)
        self._save()
