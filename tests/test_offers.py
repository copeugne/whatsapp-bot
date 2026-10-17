"""Tests for whatsapp_bot.offers — SeenOffers, formatting, filtering, scraper."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from whatsapp_bot.filters import (
    OfferFilter,
    format_offers_help,
    load_offers_scraper_filter,
    parse_offers_args,
)
from whatsapp_bot.offers import (
    SeenOffers,
    _find_tier1_offer_desc_match,
    _find_tier1_offer_match,
    _notify_tier1_offer,
    _poll_and_post_offers,
    format_offer,
    format_offer_short,
    get_upcoming_offers,
)

# ---------------------------------------------------------------------------
# Sample offers (matching real 42 API shape: full_address, stage, etc.)
# ---------------------------------------------------------------------------

GOOGLE_OFFER = {
    "id": 1001,
    "title": "Software Engineer Intern at Google",
    "little_description": "Join Google's engineering team for a 6-month internship.",
    "big_description": "<p>Work on large-scale systems.</p>",
    "salary": "2500€/month",
    "contract_type": "stage",
    "email": "recruiting@google.com",
    "full_address": "8 Rue de Londres, Paris, 75009, France",
    "valid_at": "2026-01-01T00:00:00.000Z",
    "invalid_at": "2026-06-30T23:59:59.000Z",
    "min_duration": 6,
    "max_duration": 6,
    "company_id": 42,
    "target": "student",
    "slug": "software-engineer-intern-google",
}

THALES_DESC_OFFER = {
    "id": 1002,
    "title": "Cybersecurity Analyst Internship",
    "little_description": "Work on cutting-edge cybersecurity at Thales Group.",
    "big_description": "<p>This internship with Thales covers defense and digital security.</p>",
    "salary": "",
    "contract_type": "stage",
    "email": "",
    "full_address": "Gennevilliers, France",
    "valid_at": "2026-02-01T00:00:00.000Z",
    "invalid_at": "2026-05-31T23:59:59.000Z",
    "min_duration": 4,
    "max_duration": 6,
    "company_id": 99,
    "target": "student",
    "slug": "cybersecurity-analyst-thales",
}

NORMAL_OFFER = {
    "id": 1003,
    "title": "Full Stack Developer at StartupXYZ",
    "little_description": "Join a growing startup building SaaS tools.",
    "big_description": "",
    "salary": "45K€/year",
    "contract_type": "cdi",
    "email": "jobs@startupxyz.com",
    "full_address": "Lyon, France",
    "valid_at": "2026-01-15T00:00:00.000Z",
    "invalid_at": "2026-03-15T23:59:59.000Z",
    "min_duration": None,
    "max_duration": None,
    "company_id": 200,
    "target": "student_and_alumni",
    "slug": "full-stack-developer-startupxyz",
}

MINIMAL_OFFER = {
    "id": 1004,
    "title": "Junior Developer",
    "little_description": "",
    "big_description": "",
    "salary": "",
    "contract_type": None,
    "full_address": "",
    "valid_at": None,
    "invalid_at": None,
    "min_duration": None,
    "max_duration": None,
    "company_id": None,
    "target": None,
    "slug": None,
}

FREELANCE_OFFER = {
    "id": 1005,
    "title": "DevOps Freelance Mission",
    "little_description": "Short-term DevOps consulting gig.",
    "big_description": "Help us migrate to Kubernetes.",
    "salary": "500€/day",
    "contract_type": "freelance",
    "full_address": "Remote",
    "valid_at": "2026-03-01T00:00:00.000Z",
    "invalid_at": "2026-04-01T23:59:59.000Z",
    "min_duration": 3,
    "max_duration": 6,
    "company_id": 300,
    "target": "student_and_alumni",
    "slug": "devops-freelance-mission",
}


# -----------------------------------------------------------------------
# SeenOffers
# -----------------------------------------------------------------------

class TestSeenOffers:
    def test_new_file_is_empty(self, tmp_path: Path):
        path = str(tmp_path / "seen.json")
        seen = SeenOffers(path)
        assert seen.is_empty

    def test_mark_seen(self, tmp_path: Path):
        path = str(tmp_path / "seen.json")
        seen = SeenOffers(path)
        seen.mark_seen(42)
        assert not seen.is_new(42)
        assert seen.is_new(99)

    def test_persistence(self, tmp_path: Path):
        path = str(tmp_path / "seen.json")
        seen = SeenOffers(path)
        seen.mark_seen(42)
        seen.mark_seen(99)

        # Reload from disk
        seen2 = SeenOffers(path)
        assert not seen2.is_new(42)
        assert not seen2.is_new(99)
        assert seen2.is_new(100)

    def test_mark_many_seen(self, tmp_path: Path):
        path = str(tmp_path / "seen.json")
        seen = SeenOffers(path)
        seen.mark_many_seen([1, 2, 3])
        assert not seen.is_new(1)
        assert not seen.is_new(2)
        assert not seen.is_new(3)
        assert seen.is_new(4)

    def test_corrupted_file_raises(self, tmp_path: Path):
        path = tmp_path / "seen.json"
        path.write_text("not json")
        with pytest.raises(RuntimeError, match="Corrupted"):
            SeenOffers(str(path))

    def test_wrong_type_raises(self, tmp_path: Path):
        path = tmp_path / "seen.json"
        path.write_text('{"not": "array"}')
        with pytest.raises(RuntimeError, match="expected JSON array"):
            SeenOffers(str(path))


# -----------------------------------------------------------------------
# format_offer_short
# -----------------------------------------------------------------------

class TestFormatOfferShort:
    def test_basic_format(self):
        result = format_offer_short(GOOGLE_OFFER)
        assert "*Software Engineer Intern at Google*" in result
        assert "Stage (Internship)" in result
        assert "8 Rue de Londres, Paris, 75009, France" in result
        assert "2500€/month" in result
        assert "Duration: 6 months" in result

    def test_cdi_contract(self):
        result = format_offer_short(NORMAL_OFFER)
        assert "CDI (Permanent)" in result
        assert "Lyon, France" in result
        assert "45K€/year" in result

    def test_minimal_offer(self):
        result = format_offer_short(MINIMAL_OFFER)
        assert "*Junior Developer*" in result
        assert "Unknown" in result  # contract type
        assert "Not specified" in result

    def test_validity_range(self):
        result = format_offer_short(GOOGLE_OFFER)
        assert "Jan 01, 2026" in result
        assert "Jun 30, 2026" in result

    def test_no_deadline(self):
        result = format_offer_short(MINIMAL_OFFER)
        assert "No deadline" in result

    def test_duration_range(self):
        result = format_offer_short(THALES_DESC_OFFER)
        assert "4-6 months" in result

    def test_freelance_contract(self):
        result = format_offer_short(FREELANCE_OFFER)
        assert "Freelance" in result
        assert "Remote" in result


# -----------------------------------------------------------------------
# format_offer
# -----------------------------------------------------------------------

class TestFormatOffer:
    def test_includes_description(self):
        result = format_offer(GOOGLE_OFFER)
        assert "Join Google's engineering team" in result

    def test_includes_link(self):
        result = format_offer(GOOGLE_OFFER)
        assert "https://companies.intra.42.fr/en/offers/1001" in result
        assert "meta.intra.42.fr/offers" not in result
        assert GOOGLE_OFFER["slug"] not in result

    def test_fallback_to_big_description(self):
        offer = {**MINIMAL_OFFER, "big_description": "<b>Some details here.</b>"}
        result = format_offer(offer)
        assert "Some details here." in result
        assert "<b>" not in result

    def test_link_uses_id_without_slug(self):
        result = format_offer(MINIMAL_OFFER)
        assert "https://companies.intra.42.fr/en/offers/1004" in result

    def test_long_description_truncated(self):
        offer = {**NORMAL_OFFER, "little_description": "A" * 1000}
        result = format_offer(offer)
        assert "..." in result
        # Should not exceed ~500 chars for description portion
        desc_part = result.split("\n\n")[1]  # Description is after header
        assert len(desc_part) <= 510


# -----------------------------------------------------------------------
# OfferFilter
# -----------------------------------------------------------------------

class TestOfferFilter:
    def test_empty_filter(self):
        f = OfferFilter()
        assert f.is_empty

    def test_contract_type_filter_not_empty(self):
        f = OfferFilter(contract_types=("stage",))
        assert not f.is_empty

    def test_to_api_params_contract_type(self):
        f = OfferFilter(contract_types=("stage", "cdi"))
        params = f.to_api_params()
        assert params["filter[contract_type]"] == "stage,cdi"

    def test_to_api_params_target(self):
        f = OfferFilter(target="student")
        params = f.to_api_params()
        assert params["filter[target]"] == "student"

    def test_to_api_params_city(self):
        f = OfferFilter(city="Paris")
        params = f.to_api_params()
        assert params["filter[city]"] == "Paris"

    def test_to_api_params_country(self):
        f = OfferFilter(country="France")
        params = f.to_api_params()
        assert params["filter[country]"] == "France"

    def test_to_api_params_combined(self):
        f = OfferFilter(contract_types=("stage",), target="student", city="Lyon")
        params = f.to_api_params()
        assert params["filter[contract_type]"] == "stage"
        assert params["filter[target]"] == "student"
        assert params["filter[city]"] == "Lyon"

    def test_to_api_params_empty(self):
        f = OfferFilter()
        assert f.to_api_params() == {}

    def test_keyword_matches_title(self):
        f = OfferFilter(keyword="Google")
        assert f.matches(GOOGLE_OFFER)

    def test_keyword_matches_little_description(self):
        f = OfferFilter(keyword="SaaS")
        assert f.matches(NORMAL_OFFER)

    def test_keyword_matches_big_description(self):
        f = OfferFilter(keyword="Kubernetes")
        assert f.matches(FREELANCE_OFFER)

    def test_keyword_rejects_no_match(self):
        f = OfferFilter(keyword="blockchain")
        reason = f.rejection_reason(NORMAL_OFFER)
        assert reason is not None
        assert "keyword" in reason

    def test_keyword_case_insensitive(self):
        f = OfferFilter(keyword="google")
        assert f.matches(GOOGLE_OFFER)

    def test_exclude_titles_rejects(self):
        f = OfferFilter(exclude_titles=("DevOps",))
        reason = f.rejection_reason(FREELANCE_OFFER)
        assert reason is not None
        assert "excluded term" in reason

    def test_exclude_titles_allows_non_match(self):
        f = OfferFilter(exclude_titles=("DevOps",))
        assert f.matches(NORMAL_OFFER)

    def test_exclude_patterns_rejects(self):
        import re
        f = OfferFilter(exclude_patterns=(re.compile(r"freelance"),))
        reason = f.rejection_reason(FREELANCE_OFFER)
        assert reason is not None
        assert "excluded pattern" in reason

    def test_exclude_patterns_allows_non_match(self):
        import re
        f = OfferFilter(exclude_patterns=(re.compile(r"freelance"),))
        assert f.matches(NORMAL_OFFER)

    def test_combined_filters(self):
        f = OfferFilter(contract_types=("cdi",), keyword="SaaS")
        # keyword matches, contract_type is API-side only (not checked client-side)
        assert f.matches(NORMAL_OFFER)
        # keyword doesn't match
        assert not f.matches(GOOGLE_OFFER)


# -----------------------------------------------------------------------
# parse_offers_args
# -----------------------------------------------------------------------

class TestParseOffersArgs:
    def test_empty_args(self):
        f = parse_offers_args("")
        assert f.is_empty

    def test_single_contract_type(self):
        f = parse_offers_args("stage")
        assert f.contract_types == ("stage",)
        assert f.keyword is None

    def test_multiple_contract_types(self):
        f = parse_offers_args("cdi cdd")
        assert f.contract_types == ("cdi", "cdd")

    def test_search_keyword(self):
        f = parse_offers_args("search python django")
        assert f.keyword == "python django"
        assert f.contract_types is None

    def test_contract_type_with_search(self):
        f = parse_offers_args("stage search machine learning")
        assert f.contract_types == ("stage",)
        assert f.keyword == "machine learning"

    def test_invalid_token_raises(self):
        with pytest.raises(ValueError, match="Unknown filter token"):
            parse_offers_args("invalid_type")

    def test_search_without_query_raises(self):
        with pytest.raises(ValueError, match="'search' requires a query"):
            parse_offers_args("search")

    def test_all_valid_types(self):
        for t in ("stage", "cdi", "cdd", "freelance"):
            f = parse_offers_args(t)
            assert f.contract_types == (t,)


# -----------------------------------------------------------------------
# format_offers_help
# -----------------------------------------------------------------------

class TestFormatOffersHelp:
    def test_contains_syntax(self):
        result = format_offers_help()
        assert "/offers" in result
        assert "search" in result
        assert "stage" in result
        assert "cdi" in result


# -----------------------------------------------------------------------
# load_offers_scraper_filter
# -----------------------------------------------------------------------

class TestLoadOffersScraperFilter:
    def test_missing_file_returns_none(self, tmp_path):
        result = load_offers_scraper_filter(str(tmp_path / "nope.toml"))
        assert result is None

    def test_empty_section_returns_none(self, tmp_path):
        p = tmp_path / "filters.toml"
        p.write_text("[offers_scraper]\ncontract_types = []\n")
        result = load_offers_scraper_filter(str(p))
        assert result is None

    def test_contract_types_loaded(self, tmp_path):
        p = tmp_path / "filters.toml"
        p.write_text('[offers_scraper]\ncontract_types = ["stage", "cdi"]\n')
        result = load_offers_scraper_filter(str(p))
        assert result is not None
        assert result.contract_types == ("stage", "cdi")

    def test_target_loaded(self, tmp_path):
        p = tmp_path / "filters.toml"
        p.write_text('[offers_scraper]\ntarget = "student"\n')
        result = load_offers_scraper_filter(str(p))
        assert result is not None
        assert result.target == "student"

    def test_invalid_contract_type_raises(self, tmp_path):
        p = tmp_path / "filters.toml"
        p.write_text('[offers_scraper]\ncontract_types = ["invalid"]\n')
        with pytest.raises(ValueError, match="Invalid contract type"):
            load_offers_scraper_filter(str(p))

    def test_invalid_target_raises(self, tmp_path):
        p = tmp_path / "filters.toml"
        p.write_text('[offers_scraper]\ntarget = "nobody"\n')
        with pytest.raises(ValueError, match="Invalid target"):
            load_offers_scraper_filter(str(p))

    def test_keyword_loaded(self, tmp_path):
        p = tmp_path / "filters.toml"
        p.write_text('[offers_scraper]\nkeyword = "python"\n')
        result = load_offers_scraper_filter(str(p))
        assert result is not None
        assert result.keyword == "python"

    def test_city_and_country_loaded(self, tmp_path):
        p = tmp_path / "filters.toml"
        p.write_text('[offers_scraper]\ncity = "Paris"\ncountry = "France"\n')
        result = load_offers_scraper_filter(str(p))
        assert result is not None
        assert result.city == "Paris"
        assert result.country == "France"

    def test_exclude_titles_loaded(self, tmp_path):
        p = tmp_path / "filters.toml"
        p.write_text('[offers_scraper]\nexclude_titles = ["spam", "fake"]\n')
        result = load_offers_scraper_filter(str(p))
        assert result is not None
        assert result.exclude_titles == ("spam", "fake")

    def test_exclude_patterns_loaded(self, tmp_path):
        p = tmp_path / "filters.toml"
        p.write_text('[offers_scraper]\nexclude_patterns = ["test\\\\d+"]\n')
        result = load_offers_scraper_filter(str(p))
        assert result is not None
        assert result.exclude_patterns is not None
        assert len(result.exclude_patterns) == 1

    def test_invalid_regex_raises(self, tmp_path):
        p = tmp_path / "filters.toml"
        p.write_text('[offers_scraper]\nexclude_patterns = ["[invalid"]\n')
        with pytest.raises(ValueError, match="Invalid regex"):
            load_offers_scraper_filter(str(p))

    def test_no_section_returns_none(self, tmp_path):
        p = tmp_path / "filters.toml"
        p.write_text("[scraper]\nkinds = []\n")
        result = load_offers_scraper_filter(str(p))
        assert result is None

    def test_full_config(self, tmp_path):
        p = tmp_path / "filters.toml"
        p.write_text(
            '[offers_scraper]\n'
            'contract_types = ["stage"]\n'
            'target = "student"\n'
            'city = "Paris"\n'
            'keyword = "python"\n'
            'exclude_titles = ["spam"]\n'
        )
        result = load_offers_scraper_filter(str(p))
        assert result is not None
        assert result.contract_types == ("stage",)
        assert result.target == "student"
        assert result.city == "Paris"
        assert result.keyword == "python"
        assert result.exclude_titles == ("spam",)


# -----------------------------------------------------------------------
# Tier 1 matching for offers
# -----------------------------------------------------------------------

class TestTier1OfferMatching:
    def test_title_match(self, tier1_entries):
        result = _find_tier1_offer_match(GOOGLE_OFFER, tier1_entries)
        assert result is not None
        assert "Google" in result

    def test_title_no_match(self, tier1_entries):
        result = _find_tier1_offer_match(NORMAL_OFFER, tier1_entries)
        assert result is None

    def test_desc_match(self, tier1_entries):
        result = _find_tier1_offer_desc_match(THALES_DESC_OFFER, tier1_entries)
        assert result is not None
        assert "Thales" in result

    def test_desc_no_match(self, tier1_entries):
        result = _find_tier1_offer_desc_match(NORMAL_OFFER, tier1_entries)
        assert result is None

    def test_empty_entries(self):
        result = _find_tier1_offer_match(GOOGLE_OFFER, [])
        assert result is None


# -----------------------------------------------------------------------
# _notify_tier1_offer
# -----------------------------------------------------------------------

class TestNotifyTier1Offer:
    async def test_notifies_all_users(
        self, mock_evolution_client, user_tokens_store
    ):
        notified = await _notify_tier1_offer(
            user_tokens_store,
            mock_evolution_client,
            "test",
            GOOGLE_OFFER,
            "Alphabet / Google",
            match_type="title",
        )
        assert notified == 2
        assert mock_evolution_client.send_text.call_count == 2

        # Check notification content
        first_call = mock_evolution_client.send_text.call_args_list[0]
        msg = first_call[0][2]
        assert "Tier 1 company offer detected" in msg
        assert "Google" in msg
        assert "title match" in msg

    async def test_desc_match_notification(
        self, mock_evolution_client, user_tokens_store
    ):
        notified = await _notify_tier1_offer(
            user_tokens_store,
            mock_evolution_client,
            "test",
            THALES_DESC_OFFER,
            "Thales",
            match_type="description",
        )
        assert notified == 2
        first_call = mock_evolution_client.send_text.call_args_list[0]
        msg = first_call[0][2]
        assert "description match" in msg

    async def test_no_users_returns_zero(self, mock_evolution_client, tmp_path):
        from whatsapp_bot.user_tokens import UserTokenStore

        empty_store = UserTokenStore(str(tmp_path / "empty_tokens.json"))
        notified = await _notify_tier1_offer(
            empty_store,
            mock_evolution_client,
            "test",
            GOOGLE_OFFER,
            "Google",
        )
        assert notified == 0
        mock_evolution_client.send_text.assert_not_called()


# -----------------------------------------------------------------------
# _poll_and_post_offers — core scraper flow
# -----------------------------------------------------------------------

class TestPollAndPostOffers:
    @pytest.fixture
    def seen_offers_path(self, tmp_path: Path) -> str:
        return str(tmp_path / "seen_offers.json")

    async def test_first_run_seeds_offers(
        self, seen_offers_path, mock_evolution_client
    ):
        """First run should seed all offers as seen, never post."""
        ft_client = AsyncMock()
        ft_client.get_offers = AsyncMock(
            return_value=([GOOGLE_OFFER, NORMAL_OFFER], 2)
        )

        seen = SeenOffers(seen_offers_path)
        assert seen.is_empty

        await _poll_and_post_offers(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=True,
        )

        assert not seen.is_new(GOOGLE_OFFER["id"])
        assert not seen.is_new(NORMAL_OFFER["id"])
        mock_evolution_client.send_text.assert_not_called()

    async def test_new_offer_posted_as_text(
        self, seen_offers_path, mock_evolution_client
    ):
        """New offers should be posted as plain text (no poll)."""
        ft_client = AsyncMock()
        ft_client.get_offers = AsyncMock(
            return_value=([NORMAL_OFFER], 1)
        )

        seen = SeenOffers(seen_offers_path)

        await _poll_and_post_offers(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
        )

        mock_evolution_client.send_text.assert_called_once()
        call_args = mock_evolution_client.send_text.call_args[0]
        assert call_args[1] == "group@g.us"
        assert "Full Stack Developer" in call_args[2]
        assert not seen.is_new(NORMAL_OFFER["id"])
        # No poll for offers
        mock_evolution_client.send_poll.assert_not_called()

    async def test_already_seen_offers_skipped(
        self, seen_offers_path, mock_evolution_client
    ):
        """Offers already seen should not be re-posted."""
        ft_client = AsyncMock()
        ft_client.get_offers = AsyncMock(
            return_value=([NORMAL_OFFER], 1)
        )

        seen = SeenOffers(seen_offers_path)
        seen.mark_seen(NORMAL_OFFER["id"])

        await _poll_and_post_offers(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
        )

        mock_evolution_client.send_text.assert_not_called()

    async def test_tier1_title_match_notifies_users(
        self,
        seen_offers_path,
        mock_evolution_client,
        user_tokens_store,
        tier1_entries,
    ):
        """Offers with tier1 title match should notify users (not auto-register)."""
        ft_client = AsyncMock()
        ft_client.get_offers = AsyncMock(
            return_value=([GOOGLE_OFFER], 1)
        )

        seen = SeenOffers(seen_offers_path)

        await _poll_and_post_offers(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            tier1_entries=tier1_entries,
            user_tokens=user_tokens_store,
        )

        # 2 user notifications + 1 group message = 3 calls
        assert mock_evolution_client.send_text.call_count == 3

        # Group message should contain tier1 annotation
        group_calls = [
            c
            for c in mock_evolution_client.send_text.call_args_list
            if c[0][1] == "group@g.us"
        ]
        assert len(group_calls) == 1
        group_msg = group_calls[0][0][2]
        assert "Tier 1 company" in group_msg
        assert "notified" in group_msg

    async def test_tier1_desc_match_notifies_users(
        self,
        seen_offers_path,
        mock_evolution_client,
        user_tokens_store,
        tier1_entries,
    ):
        """Offers with tier1 description match should notify users."""
        ft_client = AsyncMock()
        ft_client.get_offers = AsyncMock(
            return_value=([THALES_DESC_OFFER], 1)
        )

        seen = SeenOffers(seen_offers_path)

        await _poll_and_post_offers(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            tier1_entries=tier1_entries,
            user_tokens=user_tokens_store,
        )

        # 2 user notifications + 1 group message = 3 calls
        assert mock_evolution_client.send_text.call_count == 3

        group_calls = [
            c
            for c in mock_evolution_client.send_text.call_args_list
            if c[0][1] == "group@g.us"
        ]
        group_msg = group_calls[0][0][2]
        assert "description" in group_msg.lower()
        assert "Thales" in group_msg

    async def test_no_new_offers_does_nothing(
        self, seen_offers_path, mock_evolution_client
    ):
        """Empty API response means no messages sent."""
        ft_client = AsyncMock()
        ft_client.get_offers = AsyncMock(return_value=([], 0))

        seen = SeenOffers(seen_offers_path)

        await _poll_and_post_offers(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
        )

        mock_evolution_client.send_text.assert_not_called()

    async def test_title_match_prevents_description_match(
        self,
        seen_offers_path,
        mock_evolution_client,
        user_tokens_store,
        tier1_entries,
    ):
        """If title matches tier1, description matching should be skipped."""
        offer = {
            **GOOGLE_OFFER,
            "little_description": "Presented by Goldman Sachs.",
        }

        ft_client = AsyncMock()
        ft_client.get_offers = AsyncMock(return_value=([offer], 1))

        seen = SeenOffers(seen_offers_path)

        await _poll_and_post_offers(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            tier1_entries=tier1_entries,
            user_tokens=user_tokens_store,
        )

        group_calls = [
            c
            for c in mock_evolution_client.send_text.call_args_list
            if c[0][1] == "group@g.us"
        ]
        assert len(group_calls) == 1
        group_msg = group_calls[0][0][2]
        assert "Google" in group_msg or "Alphabet" in group_msg

    async def test_offer_filter_rejects_by_keyword(
        self, seen_offers_path, mock_evolution_client
    ):
        """Offers rejected by client-side filter should not be posted."""
        ft_client = AsyncMock()
        ft_client.get_offers = AsyncMock(
            return_value=([NORMAL_OFFER, FREELANCE_OFFER], 2)
        )

        seen = SeenOffers(seen_offers_path)
        # Only match offers with "devops" in title/description
        offer_filter = OfferFilter(keyword="devops")

        await _poll_and_post_offers(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            offer_filter=offer_filter,
        )

        # Only FREELANCE_OFFER matches "devops" keyword
        mock_evolution_client.send_text.assert_called_once()
        msg = mock_evolution_client.send_text.call_args[0][2]
        assert "DevOps" in msg

        # NORMAL_OFFER should be marked seen so it doesn't reappear
        assert not seen.is_new(NORMAL_OFFER["id"])
        assert not seen.is_new(FREELANCE_OFFER["id"])

    async def test_offer_filter_exclude_titles(
        self, seen_offers_path, mock_evolution_client
    ):
        """Offers with excluded titles should be skipped and marked seen."""
        ft_client = AsyncMock()
        ft_client.get_offers = AsyncMock(
            return_value=([NORMAL_OFFER, FREELANCE_OFFER], 2)
        )

        seen = SeenOffers(seen_offers_path)
        offer_filter = OfferFilter(exclude_titles=("DevOps",))

        await _poll_and_post_offers(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            offer_filter=offer_filter,
        )

        # Only NORMAL_OFFER should be posted (FREELANCE has "DevOps" in title)
        mock_evolution_client.send_text.assert_called_once()
        msg = mock_evolution_client.send_text.call_args[0][2]
        assert "Full Stack Developer" in msg

    async def test_offer_filter_passes_api_params(
        self, seen_offers_path, mock_evolution_client
    ):
        """API-side filter params should be included in the request."""
        ft_client = AsyncMock()
        ft_client.get_offers = AsyncMock(return_value=([], 0))

        seen = SeenOffers(seen_offers_path)
        offer_filter = OfferFilter(contract_types=("stage",), target="student")

        await _poll_and_post_offers(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            offer_filter=offer_filter,
        )

        # Check the params passed to get_offers
        call_kwargs = ft_client.get_offers.call_args[1]
        params = call_kwargs["extra_params"]
        assert params["filter[contract_type]"] == "stage"
        assert params["filter[target]"] == "student"
        assert params["filter[campus_id]"] == "1"
        assert params["filter[valid]"] == "true"


# -----------------------------------------------------------------------
# get_upcoming_offers (on-demand /offers command)
# -----------------------------------------------------------------------

class TestGetUpcomingOffers:
    async def test_returns_formatted_offers(self):
        ft_client = AsyncMock()
        ft_client.get_offers = AsyncMock(
            return_value=([GOOGLE_OFFER, NORMAL_OFFER], 2)
        )

        result = await get_upcoming_offers(ft_client, campus_id=1, limit=5)

        assert "Google" in result
        assert "StartupXYZ" in result
        assert "---" in result  # separator

    async def test_empty_result(self):
        ft_client = AsyncMock()
        ft_client.get_offers = AsyncMock(return_value=([], 0))

        result = await get_upcoming_offers(ft_client, campus_id=1)

        assert "No active offers" in result

    async def test_respects_limit(self):
        offers = [
            {**NORMAL_OFFER, "id": i + 1, "title": f"Offer {i}"}
            for i in range(10)
        ]
        ft_client = AsyncMock()
        ft_client.get_offers = AsyncMock(return_value=(offers, 10))

        result = await get_upcoming_offers(ft_client, campus_id=1, limit=3)

        assert result.count("---") == 2  # 3 offers = 2 separators

    async def test_with_contract_type_filter(self):
        """Filter should pass API params and apply client-side keyword."""
        ft_client = AsyncMock()
        ft_client.get_offers = AsyncMock(
            return_value=([GOOGLE_OFFER, NORMAL_OFFER], 2)
        )

        offer_filter = OfferFilter(keyword="Google")
        result = await get_upcoming_offers(
            ft_client, campus_id=1, offer_filter=offer_filter,
        )

        assert "Google" in result
        assert "StartupXYZ" not in result

    async def test_filter_no_match_message(self):
        ft_client = AsyncMock()
        ft_client.get_offers = AsyncMock(
            return_value=([NORMAL_OFFER], 1)
        )

        offer_filter = OfferFilter(keyword="nonexistent")
        result = await get_upcoming_offers(
            ft_client, campus_id=1, offer_filter=offer_filter,
        )

        assert "No offers match your filters" in result

    async def test_filter_sends_api_params(self):
        """Verify API params from filter are passed to get_offers."""
        ft_client = AsyncMock()
        ft_client.get_offers = AsyncMock(return_value=([], 0))

        offer_filter = OfferFilter(contract_types=("cdi",))
        await get_upcoming_offers(
            ft_client, campus_id=1, offer_filter=offer_filter,
        )

        call_kwargs = ft_client.get_offers.call_args[1]
        params = call_kwargs["extra_params"]
        assert params["filter[contract_type]"] == "cdi"
