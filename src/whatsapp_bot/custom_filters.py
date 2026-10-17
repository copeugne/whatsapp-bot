from __future__ import annotations

import json
import os
from pathlib import Path


class CustomEventFilters:
    def __init__(self, path: str, initial_terms: tuple[str, ...] = ()) -> None:
        self._path = Path(path)
        self._terms: list[str] = []
        self._load()
        if self._merge(initial_terms):
            self._save()

    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text())
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise RuntimeError("Custom event filters must be a JSON array of strings")
        self._terms = []
        self._merge(tuple(item for item in raw if item.strip()))

    def _merge(self, terms: tuple[str, ...]) -> bool:
        changed = False
        known = {item.casefold() for item in self._terms}
        for term in terms:
            value = term.strip()
            if value and value.casefold() not in known:
                self._terms.append(value)
                known.add(value.casefold())
                changed = True
        return changed

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._terms, ensure_ascii=False))
        os.replace(temporary, self._path)

    @property
    def terms(self) -> tuple[str, ...]:
        return tuple(self._terms)

    def add(self, term: str) -> bool:
        value = term.strip()
        if value.casefold() in {item.casefold() for item in self._terms}:
            return False
        self._terms.append(value)
        self._save()
        return True

    def remove(self, term: str) -> bool:
        value = term.strip()
        for existing in self._terms:
            if existing.casefold() == value.casefold():
                self._terms.remove(existing)
                self._save()
                return True
        return False
