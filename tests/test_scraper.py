"""Tests for whatsapp_bot.scraper — SeenEvents, format_event, _poll_and_post."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from whatsapp_bot.scraper import SeenEvents, _poll_and_post, format_event


# -----------------------------------------------------------------------
# SeenEvents
# -----------------------------------------------------------------------

class TestSeenEvents:
    def test_new_file_is_empty(self, tmp_path: Path):
        path = str(tmp_path / "seen.json")
        seen = SeenEvents(path)
        assert seen.is_empty

    def test_mark_seen(self, tmp_path: Path):
        path = str(tmp_path / "seen.json")
        seen = SeenEvents(path)
        seen.mark_seen(42)
        assert not seen.is_new(42)
        assert seen.is_new(99)

    def test_persistence(self, tmp_path: Path):
        path = str(tmp_path / "seen.json")
        seen = SeenEvents(path)
        seen.mark_seen(42)
        seen.mark_seen(99)

        # Reload from disk
        seen2 = SeenEvents(path)
        assert not seen2.is_new(42)
        assert not seen2.is_new(99)
        assert seen2.is_new(100)

    def test_mark_many_seen(self, tmp_path: Path):
        path = str(tmp_path / "seen.json")
        seen = SeenEvents(path)
        seen.mark_many_seen([1, 2, 3])
        assert not seen.is_new(1)
        assert not seen.is_new(2)
        assert not seen.is_new(3)
        assert seen.is_new(4)

    def test_corrupted_file_raises(self, tmp_path: Path):
        path = tmp_path / "seen.json"
        path.write_text("not json")
        with pytest.raises(RuntimeError, match="Corrupted"):
            SeenEvents(str(path))

    def test_wrong_type_raises(self, tmp_path: Path):
        path = tmp_path / "seen.json"
        path.write_text('{"not": "array"}')
        with pytest.raises(RuntimeError, match="expected JSON array"):
            SeenEvents(str(path))


# -----------------------------------------------------------------------
# format_event
# -----------------------------------------------------------------------

class TestFormatEvent:
    def test_basic_format(self):
        from tests.conftest import NORMAL_EVENT

        result = format_event(NORMAL_EVENT)
        assert "*Peer-to-Peer Code Review*" in result
        assert "Cluster 1" in result
        assert "5/30" in result

    def test_unlimited_spots(self):
        from tests.conftest import SHELL_FALSE_POSITIVE_EVENT

        result = format_event(SHELL_FALSE_POSITIVE_EVENT)
        assert "Unlimited" in result

    def test_html_stripped(self):
        event = {
            "id": 1,
            "name": "Test",
            "kind": "event",
            "description": "<p>Hello <b>world</b></p>",
            "begin_at": "2026-01-01T10:00:00.000Z",
            "end_at": "2026-01-01T11:00:00.000Z",
            "location": "A",
            "max_people": None,
            "nbr_subscribers": 0,
        }
        result = format_event(event)
        assert "<p>" not in result
        assert "Hello world" in result

    def test_long_description_not_truncated(self):
        event = {
            "id": 1,
            "name": "Test",
            "kind": "event",
            "description": "A" * 1000,
            "begin_at": "2026-01-01T10:00:00.000Z",
            "end_at": "2026-01-01T11:00:00.000Z",
            "location": "A",
            "max_people": None,
            "nbr_subscribers": 0,
        }
        result = format_event(event)
        assert "A" * 1000 in result

    def test_tbd_for_missing_dates(self):
        event = {"id": 1, "name": "X", "kind": "event"}
        result = format_event(event)
        assert "TBD" in result


# -----------------------------------------------------------------------
# _poll_and_post — core scraper flow
# -----------------------------------------------------------------------

class TestPollAndPost:
    """Tests for _poll_and_post with mocked dependencies."""

    async def test_first_run_seeds_events(
        self, seen_events_path, mock_evolution_client
    ):
        """First run should seed all events as seen, never post."""
        from tests.conftest import GOOGLE_TITLE_EVENT, NORMAL_EVENT

        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(
            return_value=([GOOGLE_TITLE_EVENT, NORMAL_EVENT], 2)
        )

        seen = SeenEvents(seen_events_path)
        assert seen.is_empty

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=True,
        )

        # Events should be marked seen
        assert not seen.is_new(GOOGLE_TITLE_EVENT["id"])
        assert not seen.is_new(NORMAL_EVENT["id"])
        # Nothing should be posted
        mock_evolution_client.send_text.assert_not_called()
        mock_evolution_client.send_poll.assert_not_called()

    async def test_normal_event_sends_poll(
        self, seen_events_path, mock_evolution_client, button_event_map
    ):
        """Normal events (no tier1 match) should be sent as polls."""
        from tests.conftest import NORMAL_EVENT

        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(
            return_value=([NORMAL_EVENT], 1)
        )

        seen = SeenEvents(seen_events_path)

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            button_event_map=button_event_map,
        )

        mock_evolution_client.send_poll.assert_called_once()
        call_kwargs = mock_evolution_client.send_poll.call_args[1]
        assert call_kwargs["values"] == ["Register", "Unregister"]
        assert not seen.is_new(NORMAL_EVENT["id"])

    async def test_normal_event_sends_text_without_buttons(
        self, seen_events_path, mock_evolution_client
    ):
        """Without button_event_map, normal events are sent as plain text."""
        from tests.conftest import NORMAL_EVENT

        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(
            return_value=([NORMAL_EVENT], 1)
        )

        seen = SeenEvents(seen_events_path)

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            button_event_map=None,
        )

        mock_evolution_client.send_text.assert_called_once()
        mock_evolution_client.send_poll.assert_not_called()

    async def test_title_match_triggers_auto_register(
        self,
        seen_events_path,
        mock_evolution_client,
        mock_oauth_manager,
        user_tokens_store,
        tier1_entries,
        button_event_map,
    ):
        """Events with a tier1 title match should auto-register all users."""
        from tests.conftest import GOOGLE_TITLE_EVENT

        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(
            return_value=([GOOGLE_TITLE_EVENT], 1)
        )

        seen = SeenEvents(seen_events_path)

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            button_event_map=button_event_map,
            tier1_entries=tier1_entries,
            oauth_manager=mock_oauth_manager,
            user_tokens=user_tokens_store,
        )

        # auto_register_all_users should have been called (registers 2 users)
        assert mock_oauth_manager.register_for_event.call_count == 2

        # Group message should contain auto-register annotation
        group_calls = [
            c
            for c in mock_evolution_client.send_text.call_args_list
            if c[0][1] == "group@g.us"
        ]
        assert len(group_calls) == 1
        group_msg = group_calls[0][0][2]
        assert "Auto-registered" in group_msg
        assert "tier 1" in group_msg.lower()

        # Poll should NOT be sent for tier1 events
        mock_evolution_client.send_poll.assert_not_called()

    async def test_description_match_triggers_notify_only(
        self,
        seen_events_path,
        mock_evolution_client,
        mock_oauth_manager,
        user_tokens_store,
        tier1_entries,
        button_event_map,
    ):
        """Events with a tier1 description match should notify, not register."""
        from tests.conftest import THALES_DESC_EVENT

        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(
            return_value=([THALES_DESC_EVENT], 1)
        )

        seen = SeenEvents(seen_events_path)

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            button_event_map=button_event_map,
            tier1_entries=tier1_entries,
            oauth_manager=mock_oauth_manager,
            user_tokens=user_tokens_store,
        )

        # Should NOT auto-register
        mock_oauth_manager.register_for_event.assert_not_called()

        # Group message should contain desc-match annotation
        group_calls = [
            c
            for c in mock_evolution_client.send_text.call_args_list
            if c[0][1] == "group@g.us"
        ]
        assert len(group_calls) == 1
        group_msg = group_calls[0][0][2]
        assert "not auto-registered" in group_msg.lower()
        assert "Thales" in group_msg

    async def test_already_seen_events_skipped(
        self, seen_events_path, mock_evolution_client
    ):
        """Events that have already been seen should not be posted again."""
        from tests.conftest import NORMAL_EVENT

        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(
            return_value=([NORMAL_EVENT], 1)
        )

        seen = SeenEvents(seen_events_path)
        seen.mark_seen(NORMAL_EVENT["id"])  # Pre-mark as seen

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
        )

        mock_evolution_client.send_text.assert_not_called()
        mock_evolution_client.send_poll.assert_not_called()

    async def test_filtered_events_skipped(
        self, seen_events_path, mock_evolution_client
    ):
        """Events rejected by EventFilter should not be posted."""
        from whatsapp_bot.filters import EventFilter

        from tests.conftest import FILTERED_EVENT

        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(
            return_value=([FILTERED_EVENT], 1)
        )

        event_filter = EventFilter(exclude_names=("Yoga",))
        seen = SeenEvents(seen_events_path)

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            event_filter=event_filter,
        )

        mock_evolution_client.send_text.assert_not_called()

    async def test_title_match_prevents_description_match(
        self,
        seen_events_path,
        mock_evolution_client,
        mock_oauth_manager,
        user_tokens_store,
        tier1_entries,
    ):
        """If title matches, description matching should be skipped."""
        # Event where BOTH title and description contain company names
        event = {
            "id": 200,
            "name": "Google Tech Summit",  # Title match
            "kind": "extern",
            "description": "Presented by Goldman Sachs.",  # Would also desc-match
            "begin_at": "2026-06-01T10:00:00.000Z",
            "end_at": "2026-06-01T12:00:00.000Z",
            "location": "Main Hall",
            "max_people": 100,
            "nbr_subscribers": 0,
        }

        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(return_value=([event], 1))

        seen = SeenEvents(seen_events_path)

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            tier1_entries=tier1_entries,
            oauth_manager=mock_oauth_manager,
            user_tokens=user_tokens_store,
        )

        # Title match path: auto_register_all_users should fire
        assert mock_oauth_manager.register_for_event.call_count == 2

        # Group message should say "Auto-registered", not "not auto-registered"
        group_calls = [
            c
            for c in mock_evolution_client.send_text.call_args_list
            if c[0][1] == "group@g.us"
        ]
        assert len(group_calls) == 1
        group_msg = group_calls[0][0][2]
        assert "Auto-registered" in group_msg

    async def test_no_new_events_does_nothing(
        self, seen_events_path, mock_evolution_client
    ):
        """No new events means no messages sent."""
        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(return_value=([], 0))

        seen = SeenEvents(seen_events_path)

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="test",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
        )

        mock_evolution_client.send_text.assert_not_called()
        mock_evolution_client.send_poll.assert_not_called()
