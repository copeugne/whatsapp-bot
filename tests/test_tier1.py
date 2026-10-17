"""Tests for whatsapp_bot.tier1 — company matching, ICS generation, notifications."""

from __future__ import annotations

import base64
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from whatsapp_bot.tier1 import (
    SearchEntry,
    _extract_search_terms,
    _normalize,
    _strip_urls,
    find_tier1_description_match,
    find_tier1_match,
    generate_ics,
    ics_to_base64,
    load_tier1_companies,
)


# -----------------------------------------------------------------------
# _normalize
# -----------------------------------------------------------------------

class TestNormalize:
    def test_lowercase(self):
        assert _normalize("Google") == "google"

    def test_accents_stripped(self):
        assert _normalize("Societe Generale") == "societe generale"
        assert _normalize("resume") == "resume"

    def test_non_alnum_collapsed(self):
        assert _normalize("McKinsey & Company") == "mckinsey company"
        assert _normalize("Block (Square/Cash App)") == "block square cash app"

    def test_leading_trailing_space(self):
        assert _normalize("  Google  ") == "google"

    def test_empty(self):
        assert _normalize("") == ""

    def test_accented_french(self):
        # e-acute should become e
        assert _normalize("Societe Generale") == "societe generale"


# -----------------------------------------------------------------------
# _strip_urls
# -----------------------------------------------------------------------

class TestStripUrls:
    def test_https_stripped(self):
        text = "Join at https://teams.microsoft.com/l/meetup and discuss"
        assert "microsoft" not in _strip_urls(text)
        assert "discuss" in _strip_urls(text)

    def test_http_stripped(self):
        text = "Visit http://meta.intra.42.fr/events/123"
        assert "meta" not in _strip_urls(text)

    def test_www_stripped(self):
        text = "Go to www.zoom.us/meeting/abc for the session"
        assert "zoom" not in _strip_urls(text)

    def test_no_urls_unchanged(self):
        text = "Goldman Sachs is presenting today"
        assert _strip_urls(text) == text

    def test_multiple_urls(self):
        text = "Links: https://slack.com/channel and https://github.com/repo end"
        result = _strip_urls(text)
        assert "slack" not in result
        assert "github" not in result
        assert "Links:" in result


# -----------------------------------------------------------------------
# _extract_search_terms
# -----------------------------------------------------------------------

class TestExtractSearchTerms:
    def test_simple_name(self):
        terms = _extract_search_terms("Google")
        assert terms == ["Google"]

    def test_slash_aliases(self):
        terms = _extract_search_terms("Alphabet / Google")
        assert "Alphabet" in terms
        assert "Google" in terms

    def test_parenthetical_aliases(self):
        terms = _extract_search_terms("Block (Square/Cash App)")
        assert "Square" in terms
        assert "Cash App" in terms
        assert "Block" in terms

    def test_combined_slash_and_parens(self):
        terms = _extract_search_terms("miHoYo / HoYoverse")
        assert "miHoYo" in terms
        assert "HoYoverse" in terms


# -----------------------------------------------------------------------
# load_tier1_companies
# -----------------------------------------------------------------------

class TestLoadTier1Companies:
    def test_loads_entries(self, tier1_companies_file: Path):
        entries = load_tier1_companies(str(tier1_companies_file))
        assert len(entries) > 0

    def test_noise_words_excluded(self, tier1_entries: list[SearchEntry]):
        terms = {e.term for e in tier1_entries}
        # "ai" is in _NOISE_WORDS — Mistral AI's "ai" token should be excluded
        assert "ai" not in terms

    def test_sorted_longest_first(self, tier1_entries: list[SearchEntry]):
        lengths = [len(e.term) for e in tier1_entries]
        assert lengths == sorted(lengths, reverse=True)

    def test_corporate_suffix_stripping(self, tier1_entries: list[SearchEntry]):
        terms = {e.term for e in tier1_entries}
        # "Meta Platforms" -> should also generate "meta" via suffix strip
        assert "meta platforms" in terms
        assert "meta" in terms
        # "McKinsey & Company" -> should also generate "mckinsey"
        assert "mckinsey company" in terms
        assert "mckinsey" in terms

    def test_spelling_variants(self, tier1_entries: list[SearchEntry]):
        terms = {e.term for e in tier1_entries}
        # Mastercard should also have "master card" variant
        assert "mastercard" in terms
        assert "master card" in terms

    def test_missing_file_returns_empty(self, tmp_path: Path):
        entries = load_tier1_companies(str(tmp_path / "nonexistent.txt"))
        assert entries == []

    def test_comments_and_blanks_skipped(self, tier1_companies_file: Path):
        entries = load_tier1_companies(str(tier1_companies_file))
        terms = {e.term for e in entries}
        # No comment lines should appear as terms
        assert "big tech" not in terms
        assert "finance" not in terms

    def test_google_deepmind_not_duplicate_google(self, tmp_path: Path):
        """If we have both Google and Google DeepMind, 'google' appears once."""
        p = tmp_path / "companies.txt"
        p.write_text("Alphabet / Google\nGoogle DeepMind\n")
        entries = load_tier1_companies(str(p))
        google_entries = [e for e in entries if e.term == "google"]
        assert len(google_entries) == 1


# -----------------------------------------------------------------------
# find_tier1_match (title matching)
# -----------------------------------------------------------------------

class TestFindTier1Match:
    def test_exact_match(self, tier1_entries):
        assert find_tier1_match("Google Tech Talk", tier1_entries) is not None

    def test_case_insensitive(self, tier1_entries):
        assert find_tier1_match("GOOGLE Workshop", tier1_entries) is not None

    def test_accent_insensitive(self, tier1_entries):
        # Accented characters should not block matches
        result = find_tier1_match("Goldman Sachs Presentation", tier1_entries)
        assert result is not None

    def test_no_match(self, tier1_entries):
        assert find_tier1_match("Yoga Session", tier1_entries) is None

    def test_empty_title(self, tier1_entries):
        assert find_tier1_match("", tier1_entries) is None

    def test_none_title(self, tier1_entries):
        # event.get("name") could return None; function handles gracefully
        assert find_tier1_match(None, tier1_entries) is None  # type: ignore[arg-type]

    def test_empty_entries(self):
        assert find_tier1_match("Google Tech Talk", []) is None

    def test_returns_original_company_name(self, tier1_entries):
        result = find_tier1_match("Google Tech Talk", tier1_entries)
        assert result == "Alphabet / Google"

    def test_spelling_variant_mastercard(self, tier1_entries):
        """'Master Card' in title should match via spelling variant."""
        result = find_tier1_match("Master Card Workshop", tier1_entries)
        assert result is not None
        assert "Mastercard" in result

    def test_suffix_stripped_meta(self, tier1_entries):
        """'Meta' alone (from 'Meta Platforms' suffix strip) matches."""
        result = find_tier1_match("Meta Engineering Day", tier1_entries)
        assert result == "Meta Platforms"

    def test_suffix_stripped_mckinsey(self, tier1_entries):
        """'McKinsey' alone (from 'McKinsey & Company') matches."""
        result = find_tier1_match("McKinsey Case Study", tier1_entries)
        assert result is not None
        assert "McKinsey" in result

    # --- False positive guards ---

    def test_shell_is_noise(self, tier1_entries):
        """'Shell Scripting' should NOT match Shell oil company."""
        assert find_tier1_match("Shell Scripting Workshop", tier1_entries) is None

    def test_fair_is_noise(self, tier1_entries):
        """'Job Fair' should NOT match Meta AI FAIR."""
        assert find_tier1_match("Job Fair 2026", tier1_entries) is None

    def test_coach_is_noise(self, tier1_entries):
        """'AI Coach' should NOT match Coach luxury brand."""
        assert find_tier1_match("AI Coach Session", tier1_entries) is None

    def test_mars_is_noise(self, tier1_entries):
        """French month 'Mars' should NOT match Mars Inc."""
        assert find_tier1_match("Events de Mars", tier1_entries) is None

    def test_strategy_is_noise(self, tier1_entries):
        """'Strategy' should NOT match (Strategy& normalizes to 'strategy')."""
        assert find_tier1_match("Business Strategy Workshop", tier1_entries) is None

    def test_word_boundary_prevents_partial(self, tier1_entries):
        """Partial word matches should not trigger."""
        # "Amazonia" should NOT match "Amazon"
        assert find_tier1_match("Amazonia Documentary", tier1_entries) is None


# -----------------------------------------------------------------------
# find_tier1_description_match
# -----------------------------------------------------------------------

class TestFindTier1DescriptionMatch:
    def test_matches_company_in_description(self, tier1_entries):
        event = {
            "kind": "hackathon",
            "description": "A hackathon sponsored by Thales.",
        }
        result = find_tier1_description_match(event, tier1_entries)
        assert result == "Thales"

    def test_wrong_kind_rejects(self, tier1_entries):
        """'association' kind should not trigger description matching."""
        event = {
            "kind": "association",
            "description": "Goldman Sachs is sponsoring the event.",
        }
        assert find_tier1_description_match(event, tier1_entries) is None

    def test_desc_noise_blocks_slack(self, tier1_entries):
        """'Slack' in description should be filtered by _DESC_NOISE."""
        event = {
            "kind": "extern",
            "description": "Join the Slack channel for updates.",
        }
        assert find_tier1_description_match(event, tier1_entries) is None

    def test_desc_noise_blocks_google(self, tier1_entries):
        """'Google' in description should be filtered by _DESC_NOISE."""
        event = {
            "kind": "workshop",
            "description": "Use Google to search for resources.",
        }
        assert find_tier1_description_match(event, tier1_entries) is None

    def test_desc_noise_blocks_meta(self, tier1_entries):
        """'meta' in description should be filtered (meta.intra.42.fr)."""
        event = {
            "kind": "extern",
            "description": "Check out meta for more info on the project.",
        }
        assert find_tier1_description_match(event, tier1_entries) is None

    def test_desc_noise_blocks_orange(self, tier1_entries):
        """'orange' in description should be filtered (common word)."""
        event = {
            "kind": "extern",
            "description": "The walls are painted orange.",
        }
        assert find_tier1_description_match(event, tier1_entries) is None

    def test_url_stripping_prevents_false_positive(self, tier1_entries):
        """Microsoft in URL should not trigger a match."""
        event = {
            "kind": "workshop",
            "description": "Join at https://teams.microsoft.com/abc for the session.",
        }
        assert find_tier1_description_match(event, tier1_entries) is None

    def test_empty_description(self, tier1_entries):
        event = {"kind": "extern", "description": ""}
        assert find_tier1_description_match(event, tier1_entries) is None

    def test_no_description_key(self, tier1_entries):
        event = {"kind": "extern"}
        assert find_tier1_description_match(event, tier1_entries) is None

    def test_valid_kind_extern(self, tier1_entries):
        event = {
            "kind": "extern",
            "description": "A presentation by Goldman Sachs engineers.",
        }
        result = find_tier1_description_match(event, tier1_entries)
        assert result is not None

    def test_valid_kind_conference(self, tier1_entries):
        event = {
            "kind": "conference",
            "description": "Conference with JPMorgan Chase representatives.",
        }
        result = find_tier1_description_match(event, tier1_entries)
        assert result is not None

    def test_valid_kind_workshop(self, tier1_entries):
        event = {
            "kind": "workshop",
            "description": "Thales cybersecurity training workshop.",
        }
        result = find_tier1_description_match(event, tier1_entries)
        assert result is not None

    def test_valid_kind_hackathon(self, tier1_entries):
        event = {
            "kind": "hackathon",
            "description": "Hackathon challenge by Thales.",
        }
        result = find_tier1_description_match(event, tier1_entries)
        assert result is not None

    def test_valid_kind_challenge(self, tier1_entries):
        event = {
            "kind": "challenge",
            "description": "Challenge organized by OpenAI.",
        }
        result = find_tier1_description_match(event, tier1_entries)
        assert result is not None


# -----------------------------------------------------------------------
# generate_ics
# -----------------------------------------------------------------------

class TestGenerateIcs:
    def test_basic_ics(self):
        event = {
            "id": 42,
            "name": "Google Tech Talk",
            "description": "A talk by Google engineers.",
            "begin_at": "2026-03-10T14:00:00.000Z",
            "end_at": "2026-03-10T16:00:00.000Z",
            "location": "Amphi 1",
        }
        ics = generate_ics(event, "Alphabet / Google")
        assert "BEGIN:VCALENDAR" in ics
        assert "END:VCALENDAR" in ics
        assert "BEGIN:VEVENT" in ics
        assert "END:VEVENT" in ics
        assert "Google Tech Talk" in ics
        assert "Amphi 1" in ics
        assert "DTSTART:20260310T140000Z" in ics
        assert "DTEND:20260310T160000Z" in ics
        assert "Tier 1: Alphabet / Google" in ics

    def test_valarm_present(self):
        event = {
            "id": 1,
            "name": "Event",
            "begin_at": "2026-01-01T10:00:00.000Z",
        }
        ics = generate_ics(event, "Test")
        assert "BEGIN:VALARM" in ics
        assert "TRIGGER:-PT30M" in ics

    def test_no_start_time_returns_empty(self):
        event = {"id": 1, "name": "Event", "begin_at": ""}
        assert generate_ics(event, "Test") == ""

    def test_missing_start_time_returns_empty(self):
        event = {"id": 1, "name": "Event"}
        assert generate_ics(event, "Test") == ""

    def test_uid_contains_event_id(self):
        event = {"id": 999, "name": "Event", "begin_at": "2026-01-01T10:00:00.000Z"}
        ics = generate_ics(event, "Test")
        assert "UID:42-event-999@42paris" in ics

    def test_html_stripped_from_description(self):
        event = {
            "id": 1,
            "name": "Event",
            "begin_at": "2026-01-01T10:00:00.000Z",
            "description": "<p>Hello <b>world</b></p>",
        }
        ics = generate_ics(event, "Test")
        assert "<p>" not in ics
        assert "<b>" not in ics
        assert "Hello world" in ics

    def test_crlf_line_endings(self):
        event = {"id": 1, "name": "Event", "begin_at": "2026-01-01T10:00:00.000Z"}
        ics = generate_ics(event, "Test")
        # iCalendar requires CRLF
        assert "\r\n" in ics

    def test_no_end_time_omits_dtend(self):
        event = {"id": 1, "name": "Event", "begin_at": "2026-01-01T10:00:00.000Z"}
        ics = generate_ics(event, "Test")
        assert "DTEND" not in ics


# -----------------------------------------------------------------------
# ics_to_base64
# -----------------------------------------------------------------------

class TestIcsToBase64:
    def test_returns_data_uri(self):
        result = ics_to_base64("BEGIN:VCALENDAR\r\nEND:VCALENDAR")
        assert result.startswith("data:text/calendar;base64,")

    def test_decodable(self):
        original = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR"
        encoded = ics_to_base64(original)
        b64_part = encoded.split(",", 1)[1]
        decoded = base64.b64decode(b64_part).decode("utf-8")
        assert decoded == original


# -----------------------------------------------------------------------
# auto_register_all_users (async)
# -----------------------------------------------------------------------

class TestAutoRegisterAllUsers:
    async def test_registers_all_users(
        self, mock_oauth_manager, user_tokens_store, mock_evolution_client
    ):
        from whatsapp_bot.tier1 import auto_register_all_users

        from tests.conftest import GOOGLE_TITLE_EVENT

        count = await auto_register_all_users(
            mock_oauth_manager,
            user_tokens_store,
            mock_evolution_client,
            "test-instance",
            GOOGLE_TITLE_EVENT,
            "Alphabet / Google",
        )

        assert count == 2
        # Each user gets: 1 text notification + 1 .ics document
        assert mock_evolution_client.send_text.call_count == 2
        assert mock_evolution_client.send_media.call_count == 2
        # Verify registration was called for each user
        assert mock_oauth_manager.register_for_event.call_count == 2

    async def test_skips_already_registered(
        self, mock_oauth_manager, user_tokens_store, mock_evolution_client
    ):
        from whatsapp_bot.tier1 import auto_register_all_users

        from tests.conftest import GOOGLE_TITLE_EVENT

        mock_oauth_manager.is_registered = AsyncMock(return_value=True)

        count = await auto_register_all_users(
            mock_oauth_manager,
            user_tokens_store,
            mock_evolution_client,
            "test-instance",
            GOOGLE_TITLE_EVENT,
            "Alphabet / Google",
        )

        assert count == 0
        mock_oauth_manager.register_for_event.assert_not_called()

    async def test_refreshes_expired_tokens(
        self, mock_oauth_manager, mock_evolution_client, tmp_path
    ):
        from whatsapp_bot.tier1 import auto_register_all_users
        from whatsapp_bot.user_tokens import UserToken, UserTokenStore

        from tests.conftest import GOOGLE_TITLE_EVENT

        store = UserTokenStore(str(tmp_path / "tokens.json"))
        store.set(
            "33600000000",
            UserToken(
                access_token="expired",
                refresh_token="refresh-me",
                expires_at=time.time() - 100,  # expired
                user_id=42099,
            ),
        )

        count = await auto_register_all_users(
            mock_oauth_manager,
            store,
            mock_evolution_client,
            "test-instance",
            GOOGLE_TITLE_EVENT,
            "Alphabet / Google",
        )

        assert count == 1
        mock_oauth_manager.refresh_token.assert_called_once_with("refresh-me")

    async def test_no_users_returns_zero(
        self, mock_oauth_manager, mock_evolution_client, tmp_path
    ):
        from whatsapp_bot.tier1 import auto_register_all_users
        from whatsapp_bot.user_tokens import UserTokenStore

        from tests.conftest import GOOGLE_TITLE_EVENT

        empty_store = UserTokenStore(str(tmp_path / "empty.json"))

        count = await auto_register_all_users(
            mock_oauth_manager,
            empty_store,
            mock_evolution_client,
            "test-instance",
            GOOGLE_TITLE_EVENT,
            "Alphabet / Google",
        )

        assert count == 0

    async def test_notification_contains_community_service_warning(
        self, mock_oauth_manager, user_tokens_store, mock_evolution_client
    ):
        from whatsapp_bot.tier1 import auto_register_all_users

        from tests.conftest import GOOGLE_TITLE_EVENT

        await auto_register_all_users(
            mock_oauth_manager,
            user_tokens_store,
            mock_evolution_client,
            "test-instance",
            GOOGLE_TITLE_EVENT,
            "Alphabet / Google",
        )

        # Get the first send_text call's message body
        text_arg = mock_evolution_client.send_text.call_args_list[0][0][2]
        assert "community service" in text_arg.lower()
        assert "auto-registered" in text_arg.lower()

    async def test_ics_sent_as_document(
        self, mock_oauth_manager, user_tokens_store, mock_evolution_client
    ):
        from whatsapp_bot.tier1 import auto_register_all_users

        from tests.conftest import GOOGLE_TITLE_EVENT

        await auto_register_all_users(
            mock_oauth_manager,
            user_tokens_store,
            mock_evolution_client,
            "test-instance",
            GOOGLE_TITLE_EVENT,
            "Alphabet / Google",
        )

        # Check send_media called with document type and .ics filename
        call_kwargs = mock_evolution_client.send_media.call_args_list[0]
        args = call_kwargs[0]
        kwargs = call_kwargs[1]
        assert kwargs.get("media_type") or args[2] == "document"
        file_name = kwargs.get("file_name", "")
        assert file_name.endswith(".ics")


# -----------------------------------------------------------------------
# notify_description_match (async)
# -----------------------------------------------------------------------

class TestNotifyDescriptionMatch:
    async def test_notifies_all_users(
        self, user_tokens_store, mock_evolution_client
    ):
        from whatsapp_bot.tier1 import notify_description_match

        from tests.conftest import THALES_DESC_EVENT

        notified = await notify_description_match(
            user_tokens_store,
            mock_evolution_client,
            "test-instance",
            THALES_DESC_EVENT,
            "Thales",
        )

        assert notified == 2
        assert mock_evolution_client.send_text.call_count == 2

    async def test_notification_says_not_auto_registered(
        self, user_tokens_store, mock_evolution_client
    ):
        from whatsapp_bot.tier1 import notify_description_match

        from tests.conftest import THALES_DESC_EVENT

        await notify_description_match(
            user_tokens_store,
            mock_evolution_client,
            "test-instance",
            THALES_DESC_EVENT,
            "Thales",
        )

        text = mock_evolution_client.send_text.call_args_list[0][0][2]
        assert "NOT" in text
        assert "auto-registered" in text.lower() or "not been auto-registered" in text.lower()

    async def test_notification_contains_company_name(
        self, user_tokens_store, mock_evolution_client
    ):
        from whatsapp_bot.tier1 import notify_description_match

        from tests.conftest import THALES_DESC_EVENT

        await notify_description_match(
            user_tokens_store,
            mock_evolution_client,
            "test-instance",
            THALES_DESC_EVENT,
            "Thales",
        )

        text = mock_evolution_client.send_text.call_args_list[0][0][2]
        assert "Thales" in text

    async def test_no_users_returns_zero(
        self, mock_evolution_client, tmp_path
    ):
        from whatsapp_bot.tier1 import notify_description_match
        from whatsapp_bot.user_tokens import UserTokenStore

        from tests.conftest import THALES_DESC_EVENT

        empty = UserTokenStore(str(tmp_path / "empty.json"))

        notified = await notify_description_match(
            empty,
            mock_evolution_client,
            "test-instance",
            THALES_DESC_EVENT,
            "Thales",
        )
        assert notified == 0
