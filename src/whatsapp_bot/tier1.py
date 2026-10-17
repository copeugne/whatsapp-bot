"""Tier 1 company matching for automatic event registration.

Loads company names from a flat file and checks event titles for matches.
When a match is found, the scraper auto-registers all linked users.

Two-tier matching:
- **Title match** → high confidence → auto-register + notify + calendar invite
- **Description match** → medium confidence → notify only (user decides)
"""

from __future__ import annotations

import base64
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from whatsapp_bot.security import redact_phone as _redact
from whatsapp_bot.utils import format_dt, normalize

if TYPE_CHECKING:
    from whatsapp_bot.evolution import EvolutionClient
    from whatsapp_bot.oauth import OAuthManager
    from whatsapp_bot.user_tokens import UserTokenStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Noise words & corporate suffixes
# ---------------------------------------------------------------------------

# Terms that are too generic / ambiguous for matching — skipped entirely.
_NOISE_WORDS: frozenset[str] = frozenset({
    "group", "the", "inc", "ltd", "co", "corp", "labs", "ai",
    "capital", "partners", "management", "international",
    "systems", "solutions", "technologies", "holdings",
    "energy", "health", "digital", "global",
    "strategy",  # too generic (Strategy& normalizes to just "strategy")
    "fair",      # "Job Fair", "Career Fair" != Meta AI (FAIR)
    "coach",     # "coaching", "AI Coach" != Coach (luxury brand)
    "mars",      # French for March — matches month, not Mars Inc.
    "man",       # "one man show" != Man Group
    "shell",     # "shell scripting" != Shell (oil company)
})

# Additional noise terms for description-only matching.
# These work fine in titles (where the company IS the subject) but cause
# too many false positives in descriptions (tool references, common words).
_DESC_NOISE: frozenset[str] = frozenset({
    # Platform/tool references in event logistics
    "discord", "slack", "github", "gitlab", "youtube", "google",
    "linkedin", "apple", "zoom", "notion", "airtable",
    "meta",       # 42's internal platform: meta.intra.42.fr
    # Short/ambiguous terms
    "ups",        # "start-ups" normalizes to "start ups"
    "bd",         # Becton Dickinson — "BD" too common
    "cms",        # Content Management System
    "fox",        # common word
    "benchmark",  # common business word
    "orange",     # common word / color
    "visa",       # immigration visa
    "ice",        # Intercontinental Exchange — common word
    "grab",       # common verb
    "arm",        # common word (Arm Holdings)
    "booking",    # "booking" as generic reservation word
    "news",       # "newsletter", "news de la pedago" != News Corp
    "block",      # blockchain "block" != Block Inc
    "square",     # "from square one" != Square
    "analysis",   # common word != Analysis Group
    "renaissance",# art history period != Renaissance Technologies
    "wise",       # "UX wise" != TransferWise
    "bain",       # French for "bath" != Bain & Company
    "chaos",      # chaos engineering != CHAOS Industries
    "sig",        # multi-sig wallet != SIG
    "vast",       # common adjective != Vast Data
    "canva",      # "Business Model Canvas" != Canva (design tool)
})

# Event kinds that may contain genuine company events in descriptions.
_DESC_TARGET_KINDS: frozenset[str] = frozenset({
    "extern", "conference", "meet_up", "partnership",
    "workshop",   # company-led training (Red Hat, Elastic, Microsoft Azure)
    "hackathon",  # company-sponsored hackathons (Thales, LVMH, Mistral, NVIDIA)
    "challenge",  # company competitions (Huawei, Dassault, HSBC)
})

# Corporate suffixes stripped from normalized multi-word terms to generate
# shorter search variants.  E.g. "mckinsey company" -> also generates "mckinsey".
_CORPORATE_SUFFIXES: list[str] = [
    "company",
    "group",
    "holdings", "holding",
    "inc", "incorporated",
    "ltd", "limited",
    "corp", "corporation",
    "platforms",
    "technologies",
    "solutions",
    "services",
    "electronics",
    "industries",
]

# Spelling variants: map of extra search terms to add for specific companies.
# Handles cases where the canonical name differs from common event spelling.
_SPELLING_VARIANTS: dict[str, list[str]] = {
    "Mastercard": ["Master Card"],
}


_normalize = normalize  # re-export shared implementation


@dataclass(frozen=True)
class SearchEntry:
    """A normalized search term linked back to the original company name."""

    term: str  # normalized search term
    company: str  # original company name from the file
    pattern: re.Pattern[str]  # pre-compiled word-boundary regex


def _extract_search_terms(company: str) -> list[str]:
    """Generate search terms from a company name.

    Handles:
    - Slash-separated aliases: "Alphabet / Google" → ["Alphabet", "Google"]
    - Parenthetical aliases: "Block (Square/Cash App)" → ["Block", "Square", "Cash App"]
    - Full name is always included
    """
    terms: list[str] = []

    # Extract parenthetical content as additional aliases
    parens = re.findall(r"\(([^)]+)\)", company)
    for paren in parens:
        for part in paren.split("/"):
            part = part.strip()
            if part:
                terms.append(part)

    # Base name without parenthetical
    base = re.sub(r"\s*\([^)]*\)\s*", " ", company).strip()

    # Split on " / " for slash-separated aliases
    for part in base.split(" / "):
        part = part.strip()
        if part:
            terms.append(part)

    return terms


def load_tier1_companies(path: str) -> list[SearchEntry]:
    """Load company names from a flat file and build search entries.

    The file format is one company name per line.
    Lines starting with ``#`` are comments, blank lines are skipped.

    Returns a list of :class:`SearchEntry` sorted longest-first so that
    more specific names match before shorter ones.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("Tier 1 company file not found: %s", path)
        return []

    raw_names: list[str] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw_names.append(line)

    entries: list[SearchEntry] = []
    seen_terms: set[str] = set()

    def _add_entry(norm: str, company: str) -> None:
        if len(norm) < 2:
            return
        if norm in _NOISE_WORDS:
            return
        if norm in seen_terms:
            return
        seen_terms.add(norm)
        pattern = re.compile(r"\b" + re.escape(norm) + r"\b")
        entries.append(SearchEntry(term=norm, company=company, pattern=pattern))

    for company in raw_names:
        for raw_term in _extract_search_terms(company):
            norm = _normalize(raw_term)

            # Add the full normalized term
            _add_entry(norm, company)

            # Also generate a shorter variant by stripping trailing
            # corporate suffixes.  E.g. "mckinsey company" -> "mckinsey",
            # "meta platforms" -> "meta".
            for suffix in _CORPORATE_SUFFIXES:
                tail = " " + suffix
                if norm.endswith(tail) and len(norm) > len(tail):
                    short = norm[: -len(tail)].rstrip()
                    if len(short) >= 3:
                        _add_entry(short, company)

        # Add spelling variants for companies that have known alternate spellings.
        for variant_key, variants in _SPELLING_VARIANTS.items():
            if variant_key in company:
                for variant in variants:
                    _add_entry(_normalize(variant), company)

    # Sort longest first so "Goldman Sachs" matches before "Gold" etc.
    entries.sort(key=lambda e: len(e.term), reverse=True)

    logger.info(
        "Loaded %d search terms from %d companies (%s)",
        len(entries),
        len(raw_names),
        path,
    )
    return entries


def find_tier1_match(event_name: str, entries: list[SearchEntry]) -> str | None:
    """Check if *event_name* contains any tier 1 company name.

    Returns the original company name from the file, or ``None``.
    """
    if not event_name or not entries:
        return None

    return find_tier1_text_match(event_name, entries)


_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def _strip_urls(text: str) -> str:
    """Remove URLs from text before matching.

    URLs like ``https://teams.microsoft.com/...`` or
    ``https://meta.intra.42.fr/...`` normalize to tokens that contain
    company names (``microsoft``, ``meta``), causing false matches.
    Stripping them is a universal fix that doesn't need per-company
    noise entries.
    """
    return _URL_RE.sub("", text)


# These brands name both a company and a commonly used product. A bare
# product name must not trigger auto-registration or company notifications.
_PRODUCT_ALIASES: frozenset[str] = frozenset({"cursor"})

# Match context immediately around the candidate, not arbitrary words elsewhere
# in the description. In particular, engineers at Galadrim using Cursor are
# not engineers at Anysphere.
_ORG_BEFORE = re.compile(
    r"(?:\b(?:chez|joined)|"
    r"\b(?:work|working|works|job|jobs|career|careers|internship|stage|emploi|poste)"
    r"(?:\s+\w+){0,7}\s+(?:at|chez)|"
    r"\b(?:engineers?|ingenieurs?|teams?|equipes?|representatives?|representants?|"
    r"speakers?|intervenants?|recruiters?|recruteurs?)\s+(?:from|at|de|du|chez)|"
    r"\b(?:organized|organised|hosted|sponsored|presented)\s+(?:by|with)|"
    r"\b(?:hackathons?|challenges?|conferences?|presentations?|talks?|ateliers?|evenements?)\s+(?:by|par|avec|with)|"
    r"\b(?:organise|organisee|sponsorise|sponsorisee|presente|presentee)\s+par|"
    r"\b(?:company|entreprise|employer|employeur|sponsor|partenaire)|"
    r"\bpartners?(?:\s+\w+){0,6})\s+$"
)
_ORG_AFTER = re.compile(
    r"^\s+(?:is\s+(?:hiring|recruiting)|recrute|recrutent|embauche|"
    r"engineers?|ingenieurs?|representatives?|representants?|recruiters?|recruteurs?|"
    r"organise|organizes|organises|sponsors)\b"
)
_TOOL_CONTEXT = re.compile(
    r"\b(?:tools?|outils?|stack|tooling|skills|competences|"
    r"use|uses|using|utilise|utilisent|utilisons|utiliser|utilisation|"
    r"maitrise|maitriser|proficiency|experience with|experience using|"
    r"backend|frontend|databases?|bdd|bases de donnees|technologies|"
    r"mockups|maquettes|deployed|deploiement|apis?|llms?)\b"
)
_TOPIC_BEFORE = re.compile(
    r"\b(?:about|sur|learn|learning|apprendre|tutorial|tutoriel|"
    r"introduction to|initiation a|formation a|formation sur|look at)\s+$"
)


_EVENT_SUBJECT_AFTER = re.compile(
    r"^\s+(?:\w+\s+){0,2}(?:training|workshop|conference|presentation)\b"
)


def find_tier1_text_match(
    text: str,
    entries: list[SearchEntry],
    *,
    description: bool = False,
    allow_event_subject: bool = False,
) -> str | None:
    """Match company involvement, not a tool/skill/topic or a URL reference.

    Descriptions and ambiguous product aliases require local organization
    context. Event descriptions can also identify a company-led event by its
    leading subject, but generic tool/topic titles do not establish involvement.
    This is a deterministic contextual guard, not verified employer identity.
    """
    # Remove markup before normalizing so hrefs, attributes and HTML entities
    # cannot create company evidence. Keep visible anchor text.
    visible = html.unescape(re.sub(r"<[^>]*>", " ", text))
    normalized = _normalize(_strip_urls(visible))
    for entry in entries:
        requires_context = description or entry.term in _PRODUCT_ALIASES
        for match in entry.pattern.finditer(normalized):
            before = normalized[max(0, match.start() - 160):match.start()]
            after = normalized[match.end():match.end() + 100]
            organization = bool(_ORG_BEFORE.search(before) or _ORG_AFTER.search(after))
            event_subject = (
                allow_event_subject and match.start() == 0
                and entry.term not in _PRODUCT_ALIASES
                and entry.term not in _DESC_NOISE
                and bool(_EVENT_SUBJECT_AFTER.search(after))
            )
            if organization or event_subject:
                return entry.company
            if requires_context or _TOOL_CONTEXT.search(before) or _TOPIC_BEFORE.search(before):
                continue
            return entry.company
    return None


def find_tier1_description_match(
    event: dict,
    entries: list[SearchEntry],
) -> str | None:
    """Check if an event description mentions a tier 1 company.

    Only matches events with kinds likely to be genuine company events
    and skips terms that are too ambiguous in description context.

    URLs are stripped before matching so that links like
    ``teams.microsoft.com`` or ``meta.intra.42.fr`` don't cause
    false positives.

    Returns the matched company name, or ``None``.
    """
    description = event.get("description") or ""
    kind = event.get("kind") or ""

    if not description or not entries:
        return None

    # Only check event kinds that plausibly host external companies
    if kind not in _DESC_TARGET_KINDS:
        return None

    return find_tier1_text_match(
        description, entries, description=True, allow_event_subject=True,
    )


# ---------------------------------------------------------------------------
# Calendar (.ics) generation
# ---------------------------------------------------------------------------


def _ics_fold_line(line: str) -> str:
    """Fold a content line per RFC 5545 Section 3.1.

    Lines MUST be no longer than 75 octets (bytes). Continuation lines
    begin with a single SPACE character.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    # First line: up to 75 octets
    parts: list[str] = []
    remaining = encoded
    max_first = 75
    max_cont = 74  # 75 minus the leading SPACE
    chunk = remaining[:max_first]
    # Don't split in the middle of a multi-byte UTF-8 sequence.
    # Strip trailing continuation bytes (0x80-0xBF), then also strip
    # a leading byte (0xC0+) that lost its continuations.
    while chunk and chunk[-1:] >= b"\x80":
        if chunk[-1:] >= b"\xc0":
            chunk = chunk[:-1]
            break
        chunk = chunk[:-1]
    parts.append(chunk.decode("utf-8"))
    remaining = remaining[len(chunk):]
    while remaining:
        chunk = remaining[:max_cont]
        while chunk and chunk[-1:] >= b"\x80":
            if chunk[-1:] >= b"\xc0":
                chunk = chunk[:-1]
                break
            chunk = chunk[:-1]
        parts.append(" " + chunk.decode("utf-8"))
        remaining = remaining[len(chunk):]
    return "\r\n".join(parts)


def generate_ics(event: dict, matched_company: str) -> str:
    """Generate an iCalendar (.ics) string for an event.

    The resulting file is compatible with Apple Calendar, Google Calendar,
    and Outlook. When received as a WhatsApp document attachment on iPhone,
    tapping it offers to add the event to Apple Calendar.
    """
    event_id = event.get("id", 0)
    name = event.get("name", "42 Event")
    location = event.get("location") or ""
    description = event.get("description") or ""

    # Parse dates
    begin_at = event.get("begin_at") or ""
    end_at = event.get("end_at") or ""

    def _to_ics_dt(iso_str: str) -> str:
        """Convert ISO 8601 to iCalendar UTC format: 20250215T140000Z"""
        if not iso_str:
            return ""
        try:
            dt = datetime.fromisoformat(iso_str)
            return dt.strftime("%Y%m%dT%H%M%SZ")
        except ValueError:
            return ""

    dtstart = _to_ics_dt(begin_at)
    dtend = _to_ics_dt(end_at)

    if not dtstart:
        return ""  # Can't create calendar event without a start time

    # Escape special characters for iCalendar text fields
    def _ics_escape(text: str) -> str:
        return (
            text.replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n")
        )

    # Build clean description (strip HTML, truncate)
    desc_clean = re.sub(r"<[^>]+>", "", description).strip()
    if len(desc_clean) > 500:
        desc_clean = desc_clean[:500] + "..."
    desc_clean = f"Tier 1: {matched_company}\n\n{desc_clean}" if desc_clean else f"Tier 1: {matched_company}"

    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//WhatsApp Bot//42 Events//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:42-event-{event_id}@42paris",
        f"DTSTAMP:{now}",
        f"DTSTART:{dtstart}",
    ]
    if dtend:
        lines.append(f"DTEND:{dtend}")
    lines.extend([
        f"SUMMARY:{_ics_escape(name)}",
        f"LOCATION:{_ics_escape(location)}",
        f"DESCRIPTION:{_ics_escape(desc_clean)}",
        "STATUS:CONFIRMED",
        "BEGIN:VALARM",
        "TRIGGER:-PT30M",
        "ACTION:DISPLAY",
        f"DESCRIPTION:Reminder: {_ics_escape(name)}",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ])

    return "\r\n".join(_ics_fold_line(line) for line in lines)


def ics_to_base64(ics_content: str) -> str:
    """Encode .ics content as a base64 data URI for Evolution API."""
    raw = base64.b64encode(ics_content.encode("utf-8")).decode("ascii")
    return f"data:text/calendar;base64,{raw}"


# ---------------------------------------------------------------------------
# Auto-registration helper
# ---------------------------------------------------------------------------


async def auto_register_all_users(
    oauth_manager: OAuthManager,
    user_tokens: UserTokenStore,
    evolution_client: EvolutionClient,
    instance: str,
    event: dict,
    matched_company: str,
) -> int:
    """Auto-register every linked user for a tier 1 event.

    Iterates all users in :class:`UserTokenStore`, refreshes expired tokens
    where possible, registers each user via the 42 API, and sends a
    personal notification with a calendar invite (.ics).

    Returns the number of users successfully registered.
    """
    from whatsapp_bot.ft_client import FtAPIError
    from whatsapp_bot.user_tokens import UserToken

    event_id: int = event["id"]
    event_name: str = event.get("name", "Unnamed Event")

    all_users = user_tokens.items()
    if not all_users:
        logger.info("No linked users to auto-register for event %d", event_id)
        return 0

    # Pre-generate calendar invite once
    ics_content = generate_ics(event, matched_company)
    ics_b64 = ics_to_base64(ics_content) if ics_content else ""

    registered = 0

    for number, token in all_users:
        # --- Refresh expired token ---
        if token.is_expired:
            if token.refresh_token:
                try:
                    token_data = await oauth_manager.refresh_token(token.refresh_token)
                    token = UserToken(
                        access_token=token_data["access_token"],
                        refresh_token=token_data.get("refresh_token"),
                        expires_at=oauth_manager.token_expires_at(
                            token_data.get("expires_in", 7200)
                        ),
                        user_id=token.user_id,
                    )
                    user_tokens.set(number, token)
                    logger.info("Refreshed token for %s during auto-register", number)
                except FtAPIError:
                    logger.warning(
                        "Token refresh failed for %s, skipping auto-register", number
                    )
                    continue
            else:
                logger.warning(
                    "Token expired (no refresh) for %s, skipping auto-register", number
                )
                continue

        # --- Register ---
        try:
            already = await oauth_manager.is_registered(
                token.access_token, event_id, token.user_id
            )
            if already:
                logger.info(
                    "User %s already registered for event %d, skipping",
                    number, event_id,
                )
                continue

            await oauth_manager.register_for_event(
                token.access_token, event_id, token.user_id
            )
            registered += 1
        except FtAPIError as exc:
            logger.warning(
                "Auto-registration failed for %s (event %d): %s",
                number, event_id, exc.detail,
            )
            continue

        # --- Notify (isolated so failure doesn't block remaining users) ---
        try:
            notification = _build_registration_notification(event, matched_company)
            await evolution_client.send_text(instance, number, notification)

            if ics_b64:
                safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", event_name)[:50]
                await evolution_client.send_media(
                    instance,
                    number,
                    media_type="document",
                    url=ics_b64,
                    caption="Tap to add to your calendar",
                    file_name=f"{safe_name}.ics",
                )

            logger.info(
                "Auto-registered user %s (42 id=%d) for tier 1 event %d (%s)",
                _redact(number), token.user_id, event_id, matched_company,
            )
        except Exception:
            logger.exception(
                "Registered user %s for event %d but WhatsApp notification failed",
                _redact(number), event_id,
            )

    return registered


async def notify_description_match(
    user_tokens: UserTokenStore,
    evolution_client: EvolutionClient,
    instance: str,
    event: dict,
    matched_company: str,
) -> int:
    """Notify all linked users about a tier 1 company in an event description.

    Unlike :func:`auto_register_all_users`, this does NOT register anyone.
    It sends a heads-up so users can decide to register themselves.

    Returns the number of users notified.
    """
    event_name: str = event.get("name", "Unnamed Event")

    all_users = user_tokens.items()
    if not all_users:
        return 0

    when = format_dt(event.get("begin_at"))
    location = event.get("location") or "TBD"

    notification = (
        f"*Tier 1 company detected in event description*\n\n"
        f"*{event_name}*\n"
        f"When: {when}\n"
        f"Where: {location}\n"
        f"Company: {matched_company}\n\n"
        f"_This company was mentioned in the event description, not "
        f"the title. You have NOT been auto-registered. Check the event "
        f"details and register manually if interested._"
    )

    notified = 0
    for number, _token in all_users:
        try:
            await evolution_client.send_text(instance, number, notification)
            notified += 1
        except Exception:
            logger.warning(
                "Failed to send desc-match notification to %s", _redact(number)
            )

    logger.info(
        "Sent description-match notification to %d users for event '%s' (%s)",
        notified, event_name, matched_company,
    )
    return notified


# ---------------------------------------------------------------------------
# Notification formatting helpers
# ---------------------------------------------------------------------------


_format_event_dt = format_dt  # re-export shared implementation


def _build_registration_notification(event: dict, matched_company: str) -> str:
    """Build a rich notification message for auto-registered events."""
    name = event.get("name", "Unnamed Event")
    kind = event.get("kind", "event")
    begin = _format_event_dt(event.get("begin_at"))
    end = _format_event_dt(event.get("end_at"))
    location = event.get("location") or "TBD"
    max_people = event.get("max_people")
    nbr_subs = event.get("nbr_subscribers", 0)

    spots = f"{nbr_subs}/{max_people}" if max_people else "Unlimited"

    return (
        f"*You have been auto-registered for a Tier 1 event*\n\n"
        f"*{name}*\n"
        f"Company: {matched_company}\n"
        f"Type: {kind}\n"
        f"When: {begin} - {end}\n"
        f"Where: {location}\n"
        f"Spots: {spots}\n\n"
        f"_If you cannot attend, unregister on the intra to avoid "
        f"community service._"
    )
