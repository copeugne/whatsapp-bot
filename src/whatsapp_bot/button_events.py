import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


_MAX_ENTRIES = 1000  # keep at most this many mappings; oldest are pruned


class ButtonEventMap:
    """Persistent message_id → event_id mapping.

    When the scraper sends a button message, we store the sent message ID
    mapped to the 42 event ID. When a button click arrives, we look up
    the event from ``contextInfo.stanzaId``.

    Follows the SeenEvents pattern: saves to disk after every mutation.
    Automatically prunes old entries when exceeding ``_MAX_ENTRIES``.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._map: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._map = {}
            return
        text = self._path.read_text().strip()
        if not text:
            self._map = {}
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Corrupted button-events file {self._path}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(
                f"Corrupted button-events file {self._path}: expected JSON object, got {type(data).__name__}"
            )
        self._map = {k: int(v) for k, v in data.items()}
        logger.info("Loaded %d button→event mappings from %s", len(self._map), self._path)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self._map)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(data)
        os.replace(tmp, self._path)

    def _prune(self) -> None:
        """Remove oldest entries if map exceeds max size.

        Dict insertion order (Python 3.7+) is used — oldest keys are first.
        """
        if len(self._map) <= _MAX_ENTRIES:
            return
        excess = len(self._map) - _MAX_ENTRIES
        keys_to_remove = list(self._map.keys())[:excess]
        for k in keys_to_remove:
            del self._map[k]
        logger.debug("Pruned %d old button→event mappings", excess)

    def track(self, message_id: str, event_id: int) -> None:
        self._map[message_id] = event_id
        self._prune()
        self._save()
        logger.debug("Tracked message %s → event %d", message_id, event_id)

    def get_event_id(self, message_id: str) -> int | None:
        return self._map.get(message_id)
