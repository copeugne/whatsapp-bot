"""42 API offers scraper — posts new job/internship offers to WhatsApp.

Works similarly to the event scraper (``scraper.py``) but for the
``GET /v2/offers`` endpoint.  Key differences:

- Offers are **informational** — there is no registration endpoint.
- No poll-based registration flow; offers are posted as plain text.
- Tier 1 matching notifies users (never auto-registers).
- Offers have ``valid_at`` / ``invalid_at`` instead of ``begin_at`` / ``end_at``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from whatsapp_bot.evolution import EvolutionClient
from whatsapp_bot.ft_client import FtClient

if TYPE_CHECKING:
    from whatsapp_bot.filters import OfferFilter
    from whatsapp_bot.tier1 import SearchEntry
    from whatsapp_bot.user_tokens import UserTokenStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Seen-offers persistence (mirrors SeenEvents from scraper.py)
# ---------------------------------------------------------------------------


class SeenOffers:
    """Tracks which offer IDs have already been posted.

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
                f"Corrupted seen-offers file {self._path}: {exc}"
            ) from exc
        if not isinstance(data, list):
            raise RuntimeError(
                f"Corrupted seen-offers file {self._path}: expected JSON array, got {type(data).__name__}"
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

    def is_new(self, offer_id: int) -> bool:
        return offer_id not in self._ids

    def mark_seen(self, offer_id: int) -> None:
        self._ids.add(offer_id)
        self._save()

    def mark_many_seen(self, offer_ids: list[int]) -> None:
        self._ids.update(offer_ids)
        self._save()


# ---------------------------------------------------------------------------
# Offer formatting
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub("", text)


def _format_validity(valid_at: str | None, invalid_at: str | None) -> str:
    """Format the offer validity period."""
    if not valid_at:
        return "No deadline"
    try:
        start = datetime.fromisoformat(valid_at)
    except ValueError:
        return valid_at

    start_str = start.strftime("%b %d, %Y")

    if not invalid_at:
        return f"From {start_str}"
    try:
        end = datetime.fromisoformat(invalid_at)
    except ValueError:
        return f"From {start_str}"

    end_str = end.strftime("%b %d, %Y")
    return f"{start_str} - {end_str}"


def _format_duration(offer: dict) -> str:
    """Format the offer duration range."""
    min_d = offer.get("min_duration")
    max_d = offer.get("max_duration")
    if not min_d and not max_d:
        return ""
    if min_d and max_d:
        if min_d == max_d:
            return f"Duration: {min_d} months"
        return f"Duration: {min_d}-{max_d} months"
    if min_d:
        return f"Duration: {min_d}+ months"
    return f"Duration: up to {max_d} months"


def _format_contract_type(contract_type: str | None) -> str:
    """Human-readable contract type."""
    if not contract_type:
        return "Unknown"
    mapping = {
        # Actual values returned by the 42 API
        "stage": "Stage (Internship)",
        "cdd": "CDD (Fixed-term)",
        "cdi": "CDI (Permanent)",
        "freelance": "Freelance",
        # Legacy/doc values (kept for safety)
        "internship": "Internship",
        "apprenticeship": "Apprenticeship",
        "part_time": "Part-time",
        "full_time": "Full-time",
        "other": "Other",
    }
    return mapping.get(contract_type, contract_type.replace("_", " ").title())


def _format_location(offer: dict) -> str:
    """Format offer location from ``full_address`` or legacy city/country fields."""
    # The 42 API returns full_address as a comma-separated string
    full_address = offer.get("full_address") or ""
    if full_address:
        # Trim redundant whitespace but keep the full address
        return full_address.strip().rstrip(",").strip()
    # Fallback to legacy fields (in case API changes back)
    parts = []
    city = offer.get("city")
    country = offer.get("country")
    if city:
        parts.append(city)
    if country and country != city:
        parts.append(country)
    return ", ".join(parts) if parts else "Not specified"


def format_offer_short(offer: dict) -> str:
    """Format offer header only (title, contract, location, validity)."""
    title = offer.get("title") or "Untitled Offer"
    contract = _format_contract_type(offer.get("contract_type"))
    location = _format_location(offer)
    validity = _format_validity(offer.get("valid_at"), offer.get("invalid_at"))

    lines = [
        f"*{title}*",
        f"Type: {contract}",
        f"Location: {location}",
        validity,
    ]

    salary = offer.get("salary")
    if salary:
        lines.insert(2, f"Salary: {salary}")

    duration = _format_duration(offer)
    if duration:
        lines.append(duration)

    return "\n".join(lines)


def offer_url(offer: dict) -> str:
    """Student-facing offer URL uses the numeric API ID, not the slug."""
    offer_id = offer.get("id")
    if offer_id is None:
        return ""
    if isinstance(offer_id, bool) or not isinstance(offer_id, int) or offer_id <= 0:
        raise ValueError("Offer URL requires a positive integer offer ID")
    return f"https://companies.intra.42.fr/en/offers/{offer_id}"


def format_offer(offer: dict) -> str:
    """Full offer formatting including description.

    Used for text messages (``/offers`` command, tier 1 group posts).
    """
    header = format_offer_short(offer)

    # Prefer little_description (summary), fall back to big_description
    desc_raw = offer.get("little_description") or ""
    if not desc_raw:
        desc_raw = offer.get("big_description") or ""

    desc = _strip_html(desc_raw).strip()

    # Truncate long descriptions for WhatsApp readability
    if len(desc) > 500:
        last_nl = desc[:500].rfind("\n")
        if last_nl > 250:
            desc = desc[:last_nl] + "..."
        else:
            last_sp = desc[:497].rfind(" ")
            if last_sp > 250:
                desc = desc[:last_sp] + "..."
            else:
                desc = desc[:497] + "..."

    link = offer_url(offer)

    parts = [header]
    if desc:
        parts.append(desc)
    if link:
        parts.append(link)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------


async def fetch_all_offers(
    ft_client: FtClient,
    *,
    extra_params: dict[str, str] | None = None,
) -> list[dict]:
    """Paginate through all offers matching the given filters."""
    all_offers: list[dict] = []
    page = 1
    while True:
        offers, total = await ft_client.get_offers(
            page_size=100, page_number=page, extra_params=extra_params,
        )
        all_offers.extend(offers)
        if len(all_offers) >= total or not offers:
            break
        page += 1
        await asyncio.sleep(0.5)  # Rate-limit courtesy
    return all_offers


# ---------------------------------------------------------------------------
# On-demand query for /offers command
# ---------------------------------------------------------------------------


async def get_upcoming_offers(
    ft_client: FtClient,
    campus_id: int,
    *,
    limit: int = 5,
    offer_filter: OfferFilter | None = None,
    filters_path: str | None = None,
) -> str:
    """Return a formatted string of the latest offers for a campus."""
    from dataclasses import replace

    from whatsapp_bot.filters import OfferFilter, list_company_exclusions

    if filters_path is not None:
        offer_filter = replace(
            offer_filter or OfferFilter(),
            exclude_companies=list_company_exclusions(filters_path),
        )

    params: dict[str, str] = {
        "filter[campus_id]": str(campus_id),
        "filter[valid]": "true",
        "sort": "-created_at",
    }
    # Merge server-side API params from the filter
    if offer_filter:
        params.update(offer_filter.to_api_params())

    offers = await fetch_all_offers(ft_client, extra_params=params)

    # Apply client-side filtering (keyword, exclude_titles, exclude_patterns)
    if offer_filter and not offer_filter.is_empty:
        filtered = []
        for o in offers:
            reason = offer_filter.rejection_reason(o)
            if reason:
                logger.info(
                    "/offers filter: rejected '%s' (id=%s) — %s",
                    o.get("title"), o.get("id"), reason,
                )
            else:
                filtered.append(o)
        offers = filtered

    if not offers:
        return (
            "No offers match your filters."
            if offer_filter
            else "No active offers found."
        )

    parts = [format_offer(o) for o in offers[:limit]]
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Tier 1 matching for offers
# ---------------------------------------------------------------------------


def _find_tier1_offer_match(
    offer: dict,
    entries: list[SearchEntry],
) -> str | None:
    """Check if an offer's title contains a tier 1 company name.

    Returns the original company name, or ``None``.
    """
    from whatsapp_bot.tier1 import find_tier1_match

    title = offer.get("title") or ""
    return find_tier1_match(title, entries)


def _find_tier1_offer_desc_match(
    offer: dict,
    entries: list[SearchEntry],
) -> str | None:
    """Check if an offer's descriptions mention a tier 1 company.

    Checks both ``little_description`` and ``big_description``.
    Uses the same URL-stripping and noise filtering as event description matching.
    """
    from whatsapp_bot.tier1 import _DESC_NOISE, _URL_RE
    from whatsapp_bot.utils import normalize

    little = offer.get("little_description") or ""
    big = offer.get("big_description") or ""
    combined = f"{little} {big}".strip()

    if not combined or not entries:
        return None

    # Strip URLs and normalize
    norm_desc = normalize(_URL_RE.sub("", combined))

    for entry in entries:
        if entry.term in _DESC_NOISE:
            continue
        if entry.pattern.search(norm_desc):
            return entry.company

    return None


async def _notify_tier1_offer(
    user_tokens: UserTokenStore,
    evolution_client: EvolutionClient,
    instance: str,
    offer: dict,
    matched_company: str,
    *,
    match_type: str = "title",
) -> int:
    """Notify all linked users about a tier 1 company offer.

    Offers are informational — we NEVER auto-register (there's no
    registration endpoint). Always notify-only.

    Returns the number of users notified.
    """
    from whatsapp_bot.security import redact_phone

    title = offer.get("title") or "Untitled Offer"
    contract = _format_contract_type(offer.get("contract_type"))
    location = _format_location(offer)
    validity = _format_validity(offer.get("valid_at"), offer.get("invalid_at"))

    url = offer_url(offer)
    link = f"\n{url}" if url else ""

    where = "title" if match_type == "title" else "description"
    notification = (
        f"*Tier 1 company offer detected ({where} match)*\n\n"
        f"*{title}*\n"
        f"Company: {matched_company}\n"
        f"Type: {contract}\n"
        f"Location: {location}\n"
        f"Validity: {validity}"
        f"{link}\n\n"
        f"_Check the offer on the 42 intra for details._"
    )

    all_users = user_tokens.items()
    if not all_users:
        return 0

    notified = 0
    for number, _token in all_users:
        try:
            await evolution_client.send_text(instance, number, notification)
            notified += 1
        except Exception:
            logger.warning(
                "Failed to send offer notification to %s", redact_phone(number)
            )

    logger.info(
        "Sent tier 1 offer notification to %d users for '%s' (%s)",
        notified, title, matched_company,
    )
    return notified


# ---------------------------------------------------------------------------
# Background offers scraper loop
# ---------------------------------------------------------------------------


async def run_offers_scraper(
    ft_client: FtClient,
    evolution_client: EvolutionClient,
    *,
    instance: str,
    campus_id: int,
    group_jid: str,
    interval_seconds: int,
    seen_offers_path: str,
    filters_path: str | None = None,
    offer_filter: OfferFilter | None = None,
    tier1_entries: list[SearchEntry] | None = None,
    user_tokens: UserTokenStore | None = None,
) -> None:
    """Periodically poll 42 API for new offers and post them to a WhatsApp group."""

    seen = SeenOffers(seen_offers_path)
    first_run = seen.is_empty

    tier1_enabled = bool(tier1_entries and user_tokens)

    logger.info(
        "Offers scraper started: campus=%d, group=%s, interval=%ds, tier1=%s, filter=%s",
        campus_id,
        group_jid,
        interval_seconds,
        tier1_enabled,
        offer_filter is not None,
    )

    while True:
        try:
            await _poll_and_post_offers(
                ft_client,
                evolution_client,
                seen=seen,
                instance=instance,
                campus_id=campus_id,
                group_jid=group_jid,
                first_run=first_run,
                offer_filter=offer_filter,
                filters_path=filters_path,
                tier1_entries=tier1_entries if tier1_enabled else None,
                user_tokens=user_tokens,
            )
            first_run = False
        except asyncio.CancelledError:
            logger.info("Offers scraper cancelled, shutting down")
            raise
        except Exception:
            logger.exception("Offers scraper cycle failed, will retry next interval")

        await asyncio.sleep(interval_seconds)


async def _poll_and_post_offers(
    ft_client: FtClient,
    evolution_client: EvolutionClient,
    *,
    seen: SeenOffers,
    instance: str,
    campus_id: int,
    group_jid: str,
    first_run: bool,
    filters_path: str | None = None,
    offer_filter: OfferFilter | None = None,
    tier1_entries: list[SearchEntry] | None = None,
    user_tokens: UserTokenStore | None = None,
) -> None:
    if filters_path is not None:
        from whatsapp_bot.filters import load_offers_scraper_filter

        offer_filter = load_offers_scraper_filter(filters_path)
    params: dict[str, str] = {
        "filter[campus_id]": str(campus_id),
        "filter[valid]": "true",
        "sort": "-created_at",
    }
    # Merge server-side API params from the scraper filter
    if offer_filter:
        params.update(offer_filter.to_api_params())

    offers = await fetch_all_offers(ft_client, extra_params=params)
    logger.info("Fetched %d valid offers for campus %d", len(offers), campus_id)

    if first_run:
        # Seed ALL offer IDs regardless of filter
        ids = [o["id"] for o in offers]
        seen.mark_many_seen(ids)
        logger.info("Seeding %d existing offers (first run, no posting)", len(ids))
        return

    new_offers = [o for o in offers if seen.is_new(o["id"])]
    if not new_offers:
        logger.debug("No new offers")
        return

    # Apply client-side filter to new offers before posting
    if offer_filter and not offer_filter.is_empty:
        filtered = []
        rejected_ids: list[int] = []
        for o in new_offers:
            reason = offer_filter.rejection_reason(o)
            if reason:
                logger.info(
                    "Offers scraper filter: rejected '%s' (id=%d) — %s",
                    o.get("title"), o["id"], reason,
                )
                rejected_ids.append(o["id"])
            else:
                filtered.append(o)
        # Mark rejected offers as seen so they don't reappear every cycle
        if rejected_ids:
            seen.mark_many_seen(rejected_ids)
        new_offers = filtered
        if not new_offers:
            logger.debug("No new offers after filtering")
            return

    logger.info("Posting %d new offers to %s", len(new_offers), group_jid)
    for offer in new_offers:
        offer_id: int = offer["id"]
        offer_title = offer.get("title") or "Untitled Offer"
        msg = format_offer(offer)

        # --- Tier 1 matching (notify only — offers have no registration) ---
        title_match: str | None = None
        desc_match: str | None = None

        if tier1_entries and user_tokens:
            title_match = _find_tier1_offer_match(offer, tier1_entries)
            if title_match is None:
                desc_match = _find_tier1_offer_desc_match(offer, tier1_entries)

        if title_match is not None:
            assert user_tokens is not None  # set together with tier1_entries
            logger.info(
                "Tier 1 TITLE match for offer %d '%s' -> %s — notifying users",
                offer_id, offer_title, title_match,
            )
            notified = await _notify_tier1_offer(
                user_tokens,
                evolution_client,
                instance,
                offer,
                title_match,
                match_type="title",
            )
            tier1_note = (
                f"\n\n_Tier 1 company ({title_match}) — "
                f"{notified} user(s) notified_"
            )
            await evolution_client.send_text(instance, group_jid, msg + tier1_note)

        elif desc_match is not None:
            assert user_tokens is not None  # set together with tier1_entries
            logger.info(
                "Tier 1 DESC match for offer %d '%s' -> %s — notifying users",
                offer_id, offer_title, desc_match,
            )
            notified = await _notify_tier1_offer(
                user_tokens,
                evolution_client,
                instance,
                offer,
                desc_match,
                match_type="description",
            )
            desc_note = (
                f"\n\n_Tier 1 company ({desc_match}) mentioned in description "
                f"— {notified} user(s) notified_"
            )
            await evolution_client.send_text(instance, group_jid, msg + desc_note)

        else:
            # Normal offer — post as plain text (no poll, offers are informational)
            await evolution_client.send_text(instance, group_jid, msg)

        seen.mark_seen(offer_id)
        logger.info("Posted offer %d: %s", offer_id, offer_title)
        await asyncio.sleep(1)  # Avoid WhatsApp flooding
