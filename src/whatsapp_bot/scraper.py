from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from whatsapp_bot.evolution import EvolutionClient
from whatsapp_bot.filters import EventFilter
from whatsapp_bot.ft_client import FtClient
from whatsapp_bot.utils import format_date_range as _format_range

if TYPE_CHECKING:
    from whatsapp_bot.button_events import ButtonEventMap
    from whatsapp_bot.oauth import OAuthManager
    from whatsapp_bot.tier1 import SearchEntry
    from whatsapp_bot.user_tokens import UserTokenStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Seen-events persistence
# ---------------------------------------------------------------------------


class SeenEvents:
    """Tracks which event IDs have already been posted.

    Saves to disk after every mutation so partially-posted batches survive crashes.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._ids: set[int] = set()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._ids = set()
            return
        text = self._path.read_text()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Corrupted seen-events file {self._path}: {exc}"
            ) from exc
        if not isinstance(data, list):
            raise RuntimeError(
                f"Corrupted seen-events file {self._path}: expected JSON array, got {type(data).__name__}"
            )
        self._ids = set(data)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(sorted(self._ids))
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(data)
        os.replace(tmp, self._path)

    @property
    def is_empty(self) -> bool:
        return len(self._ids) == 0

    def is_new(self, event_id: int) -> bool:
        return event_id not in self._ids

    def mark_seen(self, event_id: int) -> None:
        self._ids.add(event_id)
        self._save()

    def mark_many_seen(self, event_ids: list[int]) -> None:
        self._ids.update(event_ids)
        self._save()


# ---------------------------------------------------------------------------
# Event formatting
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Markdown patterns to strip or convert for WhatsApp compatibility.
# WhatsApp only supports *bold*, _italic_, ~strike~, and ```mono```.
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_HR_RE = re.compile(r"^-{3,}$", re.MULTILINE)
_MD_TRAILING_SPACES_RE = re.compile(r"  +$", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^- ", re.MULTILINE)


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub("", text)


def _md_to_whatsapp(text: str) -> str:
    """Convert Markdown formatting to WhatsApp-compatible text."""
    text = _MD_HEADING_RE.sub("", text)           # ## Heading → Heading
    text = _MD_BOLD_RE.sub(r"*\1*", text)         # **bold** → *bold*
    text = _MD_HR_RE.sub("", text)                 # --- → (removed)
    text = _MD_TRAILING_SPACES_RE.sub("", text)    # trailing spaces (md line breaks)
    text = _MD_BULLET_RE.sub("• ", text)           # - item → • item
    # Collapse 3+ consecutive blank lines into 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_42UP_PREFIX_RE = re.compile(r"^42up\s*[-–—]\s*", re.IGNORECASE)


def format_event_short(event: dict) -> str:
    """Format event header only (no description).  Fits in a poll name."""
    name = event.get("name", "Unnamed Event")
    when = _format_range(event.get("begin_at"), event.get("end_at"))
    location = event.get("location") or "TBD"
    location = _42UP_PREFIX_RE.sub("", location)
    max_people = event.get("max_people")
    nbr_subscribers = event.get("nbr_subscribers", 0)

    spots = f"{nbr_subscribers}/{max_people}" if max_people else "Unlimited"

    return "\n".join([
        f"*{name}*",
        when,
        location,
        spots,
    ])


_MAX_POLL_LEN = 480  # WhatsApp poll name limit is ~512; leave margin


def format_event(event: dict) -> str:
    """Full event formatting including description.

    Used for text messages (``/events`` command, tier 1 group posts)
    where there is no character limit.
    """
    header = format_event_short(event)

    desc_raw = event.get("description") or ""
    desc = _md_to_whatsapp(_strip_html(desc_raw))

    if desc:
        return header + f"\n\n{desc}"
    return header


def _truncate_description(desc: str, budget: int) -> str:
    """Truncate description at a clean boundary within *budget* chars."""
    if len(desc) <= budget:
        return desc
    truncated = desc[:budget]
    last_nl = truncated.rfind("\n")
    if last_nl > budget // 2:
        return truncated[:last_nl] + "..."
    last_sp = truncated.rfind(" ")
    if last_sp > budget // 2:
        return truncated[:last_sp] + "..."
    return truncated[:budget - 3] + "..."


async def format_event_poll(event: dict, *, gemini_api_key: str = "") -> str:
    """Format event for a WhatsApp poll name.

    If a Gemini API key is configured, the description is intelligently
    summarized to fit.  Otherwise falls back to truncation.
    """
    header = format_event_short(event)
    budget = _MAX_POLL_LEN - len(header) - 2  # 2 for "\n\n"

    if budget <= 20:
        return header

    desc_raw = event.get("description") or ""
    desc_clean = _md_to_whatsapp(_strip_html(desc_raw))
    if not desc_clean:
        return header

    # Short enough already — no summarization needed
    if len(desc_clean) <= budget:
        return header + f"\n\n{desc_clean}"

    # Try Gemini summarization
    if gemini_api_key:
        from whatsapp_bot.gemini import summarize_description

        summary = await summarize_description(
            gemini_api_key, desc_clean, budget,
        )
        if summary:
            return header + f"\n\n{summary}"

    # Fallback: truncate at a clean boundary
    return header + f"\n\n{_truncate_description(desc_clean, budget)}"


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------


async def fetch_all_future_events(
    ft_client: FtClient,
    campus_id: int,
    *,
    extra_params: dict[str, str] | None = None,
) -> list[dict]:
    """Paginate through all future events for a campus."""
    all_events: list[dict] = []
    page = 1
    while True:
        events, total = await ft_client.get_campus_events(
            campus_id, page_size=100, page_number=page, extra_params=extra_params
        )
        all_events.extend(events)
        if len(all_events) >= total or not events:
            break
        page += 1
        await asyncio.sleep(0.5)  # Rate-limit courtesy
    return all_events


# ---------------------------------------------------------------------------
# On-demand query for /events command
# ---------------------------------------------------------------------------


async def get_upcoming_events(
    ft_client: FtClient,
    campus_id: int,
    *,
    limit: int = 5,
    event_filter: EventFilter | None = None,
    filters_path: str | None = None,
) -> str:
    """Return a formatted string of the next N upcoming events."""
    if filters_path is not None:
        from dataclasses import replace

        from whatsapp_bot.filters import list_company_exclusions

        event_filter = replace(
            event_filter or EventFilter(),
            exclude_companies=list_company_exclusions(filters_path),
        )
    api_params = event_filter.to_api_params() if event_filter else None
    events = await fetch_all_future_events(
        ft_client, campus_id, extra_params=api_params
    )

    if event_filter and not event_filter.is_empty:
        filtered = []
        for e in events:
            reason = event_filter.rejection_reason(e)
            if reason:
                logger.info(
                    "/events filter: rejected '%s' (id=%s) — %s",
                    e.get("name"), e.get("id"), reason,
                )
            else:
                filtered.append(e)
        events = filtered

    if not events:
        return "No events match your filters." if event_filter else "No upcoming events found."

    parts = [format_event(e) for e in events[:limit]]
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Background scraper loop
# ---------------------------------------------------------------------------


async def run_scraper(
    ft_client: FtClient,
    evolution_client: EvolutionClient,
    *,
    instance: str,
    campus_id: int,
    group_jid: str,
    interval_seconds: int,
    seen_events_path: str,
    event_filter: EventFilter | None = None,
    button_event_map: ButtonEventMap | None = None,
    filters_path: str | None = None,
    tier1_entries: list[SearchEntry] | None = None,
    oauth_manager: OAuthManager | None = None,
    user_tokens: UserTokenStore | None = None,
    gemini_api_key: str = "",
) -> None:
    """Periodically poll 42 API for new events and post them to a WhatsApp group."""
    seen = SeenEvents(seen_events_path)
    first_run = seen.is_empty

    tier1_enabled = bool(tier1_entries and oauth_manager and user_tokens)

    logger.info(
        "Scraper started: campus=%d, group=%s, interval=%ds, buttons=%s, tier1=%s, gemini=%s",
        campus_id,
        group_jid,
        interval_seconds,
        button_event_map is not None,
        tier1_enabled,
        bool(gemini_api_key),
    )

    while True:
        try:
            await _poll_and_post(
                ft_client,
                evolution_client,
                seen=seen,
                instance=instance,
                campus_id=campus_id,
                group_jid=group_jid,
                first_run=first_run,
                event_filter=event_filter,
                button_event_map=button_event_map,
                filters_path=filters_path,
                tier1_entries=tier1_entries if tier1_enabled else None,
                oauth_manager=oauth_manager,
                user_tokens=user_tokens,
                gemini_api_key=gemini_api_key,
            )
            first_run = False
        except asyncio.CancelledError:
            logger.info("Scraper cancelled, shutting down")
            raise
        except Exception:
            logger.exception("Scraper cycle failed, will retry next interval")

        await asyncio.sleep(interval_seconds)


async def _poll_and_post(
    ft_client: FtClient,
    evolution_client: EvolutionClient,
    *,
    seen: SeenEvents,
    instance: str,
    campus_id: int,
    group_jid: str,
    first_run: bool,
    event_filter: EventFilter | None = None,
    button_event_map: ButtonEventMap | None = None,
    filters_path: str | None = None,
    tier1_entries: list[SearchEntry] | None = None,
    oauth_manager: OAuthManager | None = None,
    user_tokens: UserTokenStore | None = None,
    gemini_api_key: str = "",
) -> None:
    if filters_path is not None:
        from whatsapp_bot.filters import load_scraper_filter

        event_filter = load_scraper_filter(filters_path)
    api_params = event_filter.to_api_params() if event_filter else None
    events = await fetch_all_future_events(
        ft_client, campus_id, extra_params=api_params
    )
    logger.info("Fetched %d future events from campus %d", len(events), campus_id)

    if first_run:
        # Seed ALL event IDs regardless of filter
        ids = [e["id"] for e in events]
        seen.mark_many_seen(ids)
        logger.info("Seeding %d existing events (first run, no posting)", len(ids))
        return

    new_events = [e for e in events if seen.is_new(e["id"])]
    if not new_events:
        logger.debug("No new events")
        return

    # Apply client-side filter to new events before posting
    if event_filter and not event_filter.is_empty:
        filtered = []
        rejected_ids: list[int] = []
        for e in new_events:
            reason = event_filter.rejection_reason(e)
            if reason:
                logger.info(
                    "Scraper filter: rejected '%s' (id=%d) — %s",
                    e.get("name"), e["id"], reason,
                )
                rejected_ids.append(e["id"])
            else:
                filtered.append(e)
        # Mark rejected events as seen so they don't reappear every cycle
        if rejected_ids:
            seen.mark_many_seen(rejected_ids)
        new_events = filtered
        if not new_events:
            logger.debug("No new events after filtering")
            return

    logger.info("Posting %d new events to %s", len(new_events), group_jid)
    for event in new_events:
        event_id: int = event["id"]
        event_name = event.get("name", "Unnamed Event")
        msg = format_event(event)

        # --- Two-tier Tier 1 matching ---
        title_match: str | None = None
        desc_match: str | None = None

        if tier1_entries and oauth_manager and user_tokens:
            from whatsapp_bot.tier1 import (
                find_tier1_description_match,
                find_tier1_match,
            )

            title_match = find_tier1_match(event_name, tier1_entries)

            # Only check description if title didn't match
            if title_match is None:
                desc_match = find_tier1_description_match(event, tier1_entries)

        # --- Tier 1: TITLE match → auto-register + notify + calendar ---
        if title_match is not None:
            from whatsapp_bot.tier1 import auto_register_all_users

            logger.info(
                "Tier 1 TITLE match for event %d '%s' -> %s — auto-registering",
                event_id, event_name, title_match,
            )
            if oauth_manager is None or user_tokens is None:
                raise RuntimeError("Tier 1 title match but oauth_manager/user_tokens not configured")

            count = await auto_register_all_users(
                oauth_manager,
                user_tokens,
                evolution_client,
                instance,
                event,
                title_match,
            )

            # Post to group with an auto-registered annotation
            auto_note = f"\n\n_Auto-registered {count} user(s) (tier 1: {title_match})_"
            await evolution_client.send_text(instance, group_jid, msg + auto_note)

        # --- Tier 1: DESCRIPTION match → notify only (no registration) ---
        elif desc_match is not None:
            from whatsapp_bot.tier1 import notify_description_match

            logger.info(
                "Tier 1 DESC match for event %d '%s' -> %s — notifying users",
                event_id, event_name, desc_match,
            )
            if user_tokens is None:
                raise RuntimeError("Tier 1 desc match but user_tokens not configured")

            notified = await notify_description_match(
                user_tokens,
                evolution_client,
                instance,
                event,
                desc_match,
            )

            # Post to group with a desc-match annotation
            desc_note = (
                f"\n\n_Tier 1 company ({desc_match}) mentioned in description "
                f"— {notified} user(s) notified (not auto-registered)_"
            )
            await evolution_client.send_text(instance, group_jid, msg + desc_note)

        # --- Normal flow: poll or plain text ---
        elif button_event_map is not None:
            poll_name = await format_event_poll(
                event, gemini_api_key=gemini_api_key,
            )
            poll_response = await evolution_client.send_poll(
                instance,
                group_jid,
                name=poll_name,
                values=["Register", "Unregister"],
                selectable_count=1,
            )
            poll_msg_id = poll_response.get("key", {}).get("id")
            if poll_msg_id:
                button_event_map.track(poll_msg_id, event_id)
            else:
                logger.warning(
                    "send_poll response missing key.id for event %d, "
                    "poll-based registration won't work. Response: %s",
                    event_id, poll_response,
                )
        else:
            await evolution_client.send_text(instance, group_jid, msg)

        seen.mark_seen(event_id)
        logger.info("Posted event %d: %s", event_id, event_name)
        await asyncio.sleep(1)  # Avoid WhatsApp flooding
