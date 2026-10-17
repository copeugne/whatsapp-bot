"""Shared utility functions used across multiple modules."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime


def normalize(text: str) -> str:
    """Lowercase, strip accents, collapse non-alphanumeric to single space."""
    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def format_dt(iso_str: str | None) -> str:
    """Format an ISO datetime string for display (e.g. 'Mon Feb 15, 14:00').

    Returns ``'TBD'`` for empty/missing values, or the raw string on parse failure.
    """
    if not iso_str:
        return "TBD"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%a %b %d, %H:%M")
    except ValueError:
        return iso_str


def format_date_range(begin_iso: str | None, end_iso: str | None) -> str:
    """Format a begin/end pair into a compact date range.

    Same day:  ``"Feb 16, 13:15-15:30"``
    Different: ``"Feb 16, 13:15 - Feb 17, 09:00"``
    """
    if not begin_iso:
        return "TBD"
    try:
        begin = datetime.fromisoformat(begin_iso)
    except ValueError:
        return begin_iso

    if not end_iso:
        return begin.strftime("%b %d, %H:%M")
    try:
        end = datetime.fromisoformat(end_iso)
    except ValueError:
        return begin.strftime("%b %d, %H:%M")

    if begin.date() == end.date():
        return f"{begin.strftime('%b %d, %H:%M')}-{end.strftime('%H:%M')}"
    return f"{begin.strftime('%b %d, %H:%M')} - {end.strftime('%b %d, %H:%M')}"
