from __future__ import annotations

import json
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from whatsapp_bot.utils import normalize as _normalize

logger = logging.getLogger(__name__)


def _company_terms(data: dict) -> tuple[str, ...]:
    terms = data.get("companies", {}).get("exclude_names", [])
    if not isinstance(terms, list) or not all(isinstance(t, str) for t in terms):
        raise ValueError("companies.exclude_names must be an array of strings")
    if any(not _normalize(t) for t in terms):
        raise ValueError("Company exclusions must contain letters or digits")
    return tuple(dict.fromkeys(t.strip() for t in terms))


def list_company_exclusions(path: str = "config/filters.toml") -> tuple[str, ...]:
    config_path = Path(path)
    if not config_path.exists():
        return ()
    with config_path.open("rb") as stream:
        return _company_terms(tomllib.load(stream))


def company_rejection_reason(item: dict, companies: tuple[str, ...]) -> str | None:
    """Match literal normalized company phrases, never partial words or regexes."""
    company = item.get("company") or ""
    if isinstance(company, dict):
        company = company.get("name") or ""
    fields = [item.get(key) or "" for key in (
        "name", "title", "description", "little_description", "big_description",
    )]
    fields.append(company)
    normalized = [" " + _normalize(value) + " " for value in fields if isinstance(value, str)]
    for excluded in companies:
        needle = _normalize(excluded)
        if needle and any(" " + needle + " " in text for text in normalized):
            return f"mentions excluded company '{excluded}'"
    return None


def _write_company_exclusions(path: str, terms: tuple[str, ...]) -> None:
    config_path = Path(path)
    text = config_path.read_text()
    rendered = "exclude_names = [\n" + "".join(
        f"    {json.dumps(term, ensure_ascii=False)},\n" for term in terms
    ) + "]\n"
    # Replace only this section, preserving event/offer settings and comments.
    section = re.search(r"(?ms)^\[companies\][^\n]*\n.*?(?=^\[|\Z)", text)
    if section:
        body = section.group()
        assignment = re.search(r"(?m)^exclude_names\s*=\s*\[", body)
        if assignment:
            # TOML parsing identifies the real array end, even when a quoted
            # company name contains a closing bracket, quote or backslash.
            for end in range(assignment.end(), len(body)):
                if body[end] != "]":
                    continue
                try:
                    tomllib.loads(body[assignment.start():end + 1])
                except tomllib.TOMLDecodeError:
                    continue
                body = body[:assignment.start()] + rendered.rstrip() + body[end + 1:]
                break
            else:
                raise ValueError(f"Invalid companies.exclude_names array in {path}")
        else:
            body = body.rstrip() + "\n" + rendered
        updated = text[:section.start()] + body.rstrip() + "\n\n" + text[section.end():]
    else:
        updated = text.rstrip() + "\n\n[companies]\n" + rendered
    # Refuse invalid output rather than corrupting persistent configuration.
    _company_terms(tomllib.loads(updated))
    temporary = config_path.with_suffix(".tmp")
    temporary.write_text(updated)
    os.replace(temporary, config_path)


def add_company_exclusion(term: str, path: str = "config/filters.toml") -> bool:
    value = term.strip()
    if len(value) > 80 or not _normalize(value):
        raise ValueError("Use a company name with letters or digits, up to 80 characters.")
    terms = list_company_exclusions(path)
    if _normalize(value) in {_normalize(item) for item in terms}:
        return False
    _write_company_exclusions(path, terms + (value,))
    return True


def remove_company_exclusion(term: str, path: str = "config/filters.toml") -> bool:
    terms = list_company_exclusions(path)
    kept = tuple(item for item in terms if _normalize(item) != _normalize(term))
    if len(kept) == len(terms):
        return False
    _write_company_exclusions(path, kept)
    return True

VALID_KINDS: frozenset[str] = frozenset({
    "association",
    "challenge",
    "conference",
    "event",
    "extern",
    "hackathon",
    "meet_up",
    "other",
    "workshop",
})


@dataclass(frozen=True)
class EventFilter:
    """Client-side + server-side filter for 42 campus events.

    All fields are optional. ``None`` / ``False`` means "no filter".
    Sequence fields use ``tuple`` (not ``list``) to preserve immutability
    of the frozen dataclass.
    """

    kinds: tuple[str, ...] | None = field(default=None)
    keyword: str | None = field(default=None)
    available_only: bool = field(default=False)
    exclude_names: tuple[str, ...] | None = field(default=None)
    exclude_patterns: tuple[re.Pattern[str], ...] | None = field(default=None)
    exclude_companies: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return (
            not self.kinds
            and not self.keyword
            and not self.available_only
            and not self.exclude_names
            and not self.exclude_patterns
            and not self.exclude_companies
        )

    def to_api_params(self) -> dict[str, str]:
        """Return query-string params for server-side kind filtering."""
        if not self.kinds:
            return {}
        return {"filter[kind]": ",".join(self.kinds)}

    def rejection_reason(self, event: dict) -> str | None:
        """Return a human-readable reason the event is rejected, or ``None`` if it passes."""
        company_reason = company_rejection_reason(event, self.exclude_companies)
        if company_reason:
            return company_reason
        event_name = event.get("name") or "Unnamed"

        if self.exclude_names or self.exclude_patterns:
            name_norm = _normalize(event_name)

            if self.exclude_names:
                for excluded in self.exclude_names:
                    if _normalize(excluded) in name_norm:
                        return f"name matches excluded term '{excluded}'"

            if self.exclude_patterns:
                for pattern in self.exclude_patterns:
                    if pattern.search(name_norm):
                        return f"name matches excluded pattern '{pattern.pattern}'"

        if self.kinds:
            kind = event.get("kind")
            if kind not in self.kinds:
                return f"kind '{kind}' not in {self.kinds}"

        if self.keyword:
            needle = self.keyword.lower()
            name = event_name.lower()
            desc = (event.get("description") or "").lower()
            if needle not in name and needle not in desc:
                return f"keyword '{self.keyword}' not found in name or description"

        if self.available_only:
            max_people = event.get("max_people")
            if max_people is not None:
                nbr_subscribers = event.get("nbr_subscribers", 0)
                if nbr_subscribers >= max_people:
                    return f"no spots available ({nbr_subscribers}/{max_people})"

        return None

    def matches(self, event: dict) -> bool:
        """Return ``True`` if *event* passes every active filter."""
        return self.rejection_reason(event) is None


def parse_events_args(raw: str) -> EventFilter:
    """Parse the text after ``/events`` into an :class:`EventFilter`.

    Raises :class:`ValueError` for unrecognised tokens.
    """
    tokens = raw.split()
    if not tokens:
        return EventFilter()

    # Extract keyword: everything after the "search" token
    keyword: str | None = None
    if "search" in tokens:
        idx = tokens.index("search")
        keyword_tokens = tokens[idx + 1 :]
        if not keyword_tokens:
            raise ValueError("'search' requires a query after it. Example: /events search AI")
        keyword = " ".join(keyword_tokens)
        tokens = tokens[:idx]

    # Extract available flag
    available_only = "available" in tokens
    if available_only:
        tokens = [t for t in tokens if t != "available"]

    # Remaining tokens must be valid kinds
    kinds: tuple[str, ...] | None = None
    if tokens:
        bad = [t for t in tokens if t not in VALID_KINDS]
        if bad:
            raise ValueError(
                f"Unknown filter token: {', '.join(repr(b) for b in bad)}. "
                f"Valid kinds: {', '.join(sorted(VALID_KINDS))}"
            )
        kinds = tuple(tokens)

    return EventFilter(kinds=kinds, keyword=keyword, available_only=available_only)


def format_help() -> str:
    """Return WhatsApp-formatted help text for ``/events``."""
    kinds_list = ", ".join(sorted(VALID_KINDS))
    return (
        "*/events* — show upcoming 42 campus events\n"
        "\n"
        "*Syntax:*\n"
        "/events — show all upcoming events\n"
        "/events <kind> — filter by event kind\n"
        "/events search <query> — keyword search in name/description\n"
        "/events available — only events with open spots\n"
        "\n"
        "Combine freely:\n"
        "/events workshop search AI\n"
        "/events available search hack\n"
        "/events workshop conference\n"
        "\n"
        f"*Valid kinds:* {kinds_list}"
    )


# ---------------------------------------------------------------------------
# Offer filters
# ---------------------------------------------------------------------------

# Contract types actually returned by the 42 API
VALID_CONTRACT_TYPES: frozenset[str] = frozenset({
    "stage",
    "cdi",
    "cdd",
    "freelance",
})

VALID_TARGETS: frozenset[str] = frozenset({
    "student",
    "student_and_alumni",
})


@dataclass(frozen=True)
class OfferFilter:
    """Server-side + client-side filter for 42 offers.

    Server-side params (sent to ``GET /v2/offers``):
    - ``contract_types`` → ``filter[contract_type]``
    - ``target`` → ``filter[target]``
    - ``city`` → ``filter[city]``
    - ``country`` → ``filter[country]``

    Client-side params (applied after fetching):
    - ``keyword`` → case-insensitive substring search in title + descriptions
    - ``exclude_titles`` → reject offers whose normalized title contains term
    - ``exclude_patterns`` → reject offers whose normalized title matches regex
    """

    contract_types: tuple[str, ...] | None = field(default=None)
    target: str | None = field(default=None)
    city: str | None = field(default=None)
    country: str | None = field(default=None)
    keyword: str | None = field(default=None)
    exclude_titles: tuple[str, ...] | None = field(default=None)
    exclude_patterns: tuple[re.Pattern[str], ...] | None = field(default=None)
    exclude_companies: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return (
            not self.contract_types
            and not self.target
            and not self.city
            and not self.country
            and not self.keyword
            and not self.exclude_titles
            and not self.exclude_patterns
            and not self.exclude_companies
        )

    def to_api_params(self) -> dict[str, str]:
        """Return query-string params for server-side filtering."""
        params: dict[str, str] = {}
        if self.contract_types:
            params["filter[contract_type]"] = ",".join(self.contract_types)
        if self.target:
            params["filter[target]"] = self.target
        if self.city:
            params["filter[city]"] = self.city
        if self.country:
            params["filter[country]"] = self.country
        return params

    def rejection_reason(self, offer: dict) -> str | None:
        """Return a reason the offer is rejected, or ``None`` if it passes."""
        company_reason = company_rejection_reason(offer, self.exclude_companies)
        if company_reason:
            return company_reason
        title = offer.get("title") or "Untitled"

        if self.exclude_titles or self.exclude_patterns:
            title_norm = _normalize(title)

            if self.exclude_titles:
                for excluded in self.exclude_titles:
                    if _normalize(excluded) in title_norm:
                        return f"title matches excluded term '{excluded}'"

            if self.exclude_patterns:
                for pattern in self.exclude_patterns:
                    if pattern.search(title_norm):
                        return f"title matches excluded pattern '{pattern.pattern}'"

        if self.keyword:
            needle = self.keyword.lower()
            t = title.lower()
            little = (offer.get("little_description") or "").lower()
            big = (offer.get("big_description") or "").lower()
            if needle not in t and needle not in little and needle not in big:
                return f"keyword '{self.keyword}' not found in title or descriptions"

        return None

    def matches(self, offer: dict) -> bool:
        """Return ``True`` if *offer* passes every active filter."""
        return self.rejection_reason(offer) is None


def parse_offers_args(raw: str) -> OfferFilter:
    """Parse the text after ``/offers`` into an :class:`OfferFilter`.

    Syntax::

        /offers                         — all active offers
        /offers <contract_type>         — filter by type (stage, cdi, cdd, freelance)
        /offers search <query>          — keyword search in title/descriptions
        /offers <type> search <query>   — combine

    Raises :class:`ValueError` for unrecognised tokens.
    """
    tokens = raw.split()
    if not tokens:
        return OfferFilter()

    # Extract keyword: everything after the "search" token
    keyword: str | None = None
    if "search" in tokens:
        idx = tokens.index("search")
        keyword_tokens = tokens[idx + 1:]
        if not keyword_tokens:
            raise ValueError(
                "'search' requires a query after it. Example: /offers search python"
            )
        keyword = " ".join(keyword_tokens)
        tokens = tokens[:idx]

    # Remaining tokens must be valid contract types
    contract_types: tuple[str, ...] | None = None
    if tokens:
        bad = [t for t in tokens if t not in VALID_CONTRACT_TYPES]
        if bad:
            raise ValueError(
                f"Unknown filter token: {', '.join(repr(b) for b in bad)}. "
                f"Valid types: {', '.join(sorted(VALID_CONTRACT_TYPES))}"
            )
        contract_types = tuple(tokens)

    return OfferFilter(contract_types=contract_types, keyword=keyword)


def format_offers_help() -> str:
    """Return WhatsApp-formatted help text for ``/offers``."""
    types_list = ", ".join(sorted(VALID_CONTRACT_TYPES))
    return (
        "*/offers* — show active 42 job/internship offers\n"
        "\n"
        "*Syntax:*\n"
        "/offers — show all active offers\n"
        "/offers <type> — filter by contract type\n"
        "/offers search <query> — keyword search in title/description\n"
        "\n"
        "Combine freely:\n"
        "/offers stage search python\n"
        "/offers cdi cdd\n"
        "/offers search devops\n"
        "\n"
        f"*Valid types:* {types_list}"
    )


def load_scraper_filter(path: str = "config/filters.toml") -> EventFilter | None:
    """Load scraper filter from a TOML config file.

    Returns ``None`` if the file does not exist or the ``[scraper]`` section
    is absent / defines no active filters.

    Raises on malformed TOML or invalid kind values.
    """
    config_path = Path(path)
    if not config_path.exists():
        logger.debug("No filter config at %s, scraper runs unfiltered", path)
        return None

    with config_path.open("rb") as f:
        data = tomllib.load(f)

    scraper = data.get("scraper", {})

    raw_kinds: list[str] = scraper.get("kinds", [])
    keyword: str = scraper.get("keyword", "")
    available_only: bool = scraper.get("available_only", False)
    raw_exclude: list[str] = scraper.get("exclude_names", [])

    raw_patterns: list[str] = scraper.get("exclude_patterns", [])
    compiled_patterns: list[re.Pattern[str]] = []
    for i, pat_str in enumerate(raw_patterns):
        try:
            compiled_patterns.append(re.compile(pat_str))
        except re.error as exc:
            raise ValueError(
                f"Invalid regex in {path} exclude_patterns[{i}]: {pat_str!r} -- {exc}"
            ) from exc

    # Validate kinds
    if raw_kinds:
        bad = [k for k in raw_kinds if k not in VALID_KINDS]
        if bad:
            raise ValueError(
                f"Invalid kind(s) in {path}: {', '.join(repr(b) for b in bad)}. "
                f"Valid kinds: {', '.join(sorted(VALID_KINDS))}"
            )

    kinds = tuple(raw_kinds) if raw_kinds else None
    kw = keyword.strip() or None
    stripped_names = [n.strip() for n in raw_exclude if n.strip()]
    exclude_names = tuple(stripped_names) if stripped_names else None
    exclude_pats = tuple(compiled_patterns) if compiled_patterns else None
    event_filter = EventFilter(
        kinds=kinds,
        keyword=kw,
        available_only=available_only,
        exclude_names=exclude_names,
        exclude_patterns=exclude_pats,
        exclude_companies=_company_terms(data),
    )

    if event_filter.is_empty:
        return None

    return event_filter


def _read_excluded_names(path: str) -> tuple[str, ...]:
    event_filter = load_scraper_filter(path)
    return event_filter.exclude_names if event_filter and event_filter.exclude_names else ()


def _write_excluded_names(path: str, terms: tuple[str, ...]) -> None:
    config_path = Path(path)
    text = config_path.read_text()
    rendered = "exclude_names = [\n" + "".join(
        f"    {json.dumps(term, ensure_ascii=False)},\n" for term in terms
    ) + "]"
    updated, count = re.subn(
        r"(?ms)^exclude_names = \[.*?^\]",
        rendered,
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError(f"Could not find scraper exclude_names in {path}")
    temporary = config_path.with_suffix(".tmp")
    temporary.write_text(updated)
    os.replace(temporary, config_path)


def add_event_exclusion(term: str, path: str = "config/filters.toml") -> bool:
    value = term.strip()
    terms = _read_excluded_names(path)
    if value.casefold() in {item.casefold() for item in terms}:
        return False
    _write_excluded_names(path, terms + (value,))
    return True


def remove_event_exclusion(term: str, path: str = "config/filters.toml") -> bool:
    value = term.strip()
    terms = _read_excluded_names(path)
    kept = tuple(item for item in terms if item.casefold() != value.casefold())
    if len(kept) == len(terms):
        return False
    _write_excluded_names(path, kept)
    return True


def list_event_exclusions(path: str = "config/filters.toml") -> tuple[str, ...]:
    return _read_excluded_names(path)


def load_offers_scraper_filter(path: str = "config/filters.toml") -> OfferFilter | None:
    """Load offers scraper filter from a TOML config file.

    Returns ``None`` if the file does not exist or the ``[offers_scraper]``
    section is absent / defines no active filters.

    Raises on malformed TOML or invalid values.
    """
    config_path = Path(path)
    if not config_path.exists():
        logger.debug("No filter config at %s, offers scraper runs unfiltered", path)
        return None

    with config_path.open("rb") as f:
        data = tomllib.load(f)

    section = data.get("offers_scraper", {})

    raw_types: list[str] = section.get("contract_types", [])
    target: str = section.get("target", "")
    city: str = section.get("city", "")
    country: str = section.get("country", "")
    keyword: str = section.get("keyword", "")
    raw_exclude: list[str] = section.get("exclude_titles", [])

    raw_patterns: list[str] = section.get("exclude_patterns", [])
    compiled_patterns: list[re.Pattern[str]] = []
    for i, pat_str in enumerate(raw_patterns):
        try:
            compiled_patterns.append(re.compile(pat_str))
        except re.error as exc:
            raise ValueError(
                f"Invalid regex in {path} offers_scraper.exclude_patterns[{i}]: "
                f"{pat_str!r} -- {exc}"
            ) from exc

    # Validate contract types
    if raw_types:
        bad = [t for t in raw_types if t not in VALID_CONTRACT_TYPES]
        if bad:
            raise ValueError(
                f"Invalid contract type(s) in {path}: {', '.join(repr(b) for b in bad)}. "
                f"Valid types: {', '.join(sorted(VALID_CONTRACT_TYPES))}"
            )

    # Validate target
    target_val = target.strip() or None
    if target_val and target_val not in VALID_TARGETS:
        raise ValueError(
            f"Invalid target in {path}: {target_val!r}. "
            f"Valid targets: {', '.join(sorted(VALID_TARGETS))}"
        )

    contract_types = tuple(raw_types) if raw_types else None
    kw = keyword.strip() or None
    city_val = city.strip() or None
    country_val = country.strip() or None
    stripped_titles = [t.strip() for t in raw_exclude if t.strip()]
    exclude_titles = tuple(stripped_titles) if stripped_titles else None
    exclude_pats = tuple(compiled_patterns) if compiled_patterns else None

    offer_filter = OfferFilter(
        contract_types=contract_types,
        target=target_val,
        city=city_val,
        country=country_val,
        keyword=kw,
        exclude_titles=exclude_titles,
        exclude_patterns=exclude_pats,
        exclude_companies=_company_terms(data),
    )

    if offer_filter.is_empty:
        return None

    return offer_filter
