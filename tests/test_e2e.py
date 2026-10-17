"""End-to-end flow tests.

Simulates the full scraper cycle with mocked external APIs:
  42 API returns events → tier1 matching → registration → WhatsApp notification

Each test covers a complete user-facing scenario.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from whatsapp_bot.scraper import SeenEvents, _poll_and_post


class TestE2ETitleMatch:
    """Full flow: 42 API returns a Google event → title match
    → auto-register 2 users → DM each user with notification + .ics
    → post to group with annotation."""

    async def test_full_title_match_flow(
        self,
        seen_events_path,
        mock_evolution_client,
        mock_oauth_manager,
        user_tokens_store,
        tier1_entries,
        button_event_map,
    ):
        event = {
            "id": 500,
            "name": "Google Engineering Talk",
            "kind": "extern",
            "description": "<p>Come meet the Google Cloud team.</p>",
            "begin_at": "2026-06-15T14:00:00.000Z",
            "end_at": "2026-06-15T16:00:00.000Z",
            "location": "Amphi 42",
            "max_people": 80,
            "nbr_subscribers": 20,
        }

        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(return_value=([event], 1))

        seen = SeenEvents(seen_events_path)

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="bot-instance",
            campus_id=1,
            group_jid="120363xxx@g.us",
            first_run=False,
            button_event_map=button_event_map,
            tier1_entries=tier1_entries,
            oauth_manager=mock_oauth_manager,
            user_tokens=user_tokens_store,
        )

        # ------- Assertions -------

        # 1) Both users were registration-checked then registered
        assert mock_oauth_manager.is_registered.call_count == 2
        assert mock_oauth_manager.register_for_event.call_count == 2

        # 2) Each user gets a DM text notification
        user_dm_texts = [
            c
            for c in mock_evolution_client.send_text.call_args_list
            if c[0][1] in ("33612345678", "33698765432")
        ]
        assert len(user_dm_texts) == 2

        for dm_call in user_dm_texts:
            text = dm_call[0][2]
            assert "auto-registered" in text.lower()
            assert "community service" in text.lower()
            assert "Google" in text

        # 3) Each user gets a .ics calendar file
        user_dm_media = [
            c
            for c in mock_evolution_client.send_media.call_args_list
            if c[0][1] in ("33612345678", "33698765432")
        ]
        assert len(user_dm_media) == 2

        for media_call in user_dm_media:
            kwargs = media_call[1]
            assert kwargs["media_type"] == "document"
            assert kwargs["file_name"].endswith(".ics")
            # Verify the .ics content is valid base64
            ics_b64_uri = kwargs["url"]
            assert ics_b64_uri.startswith("data:text/calendar;base64,")
            b64_part = ics_b64_uri.split(",", 1)[1]
            decoded = base64.b64decode(b64_part).decode("utf-8")
            assert "BEGIN:VCALENDAR" in decoded
            assert "Google Engineering Talk" in decoded
            assert "TRIGGER:-PT30M" in decoded

        # 4) Group gets a message with auto-register annotation
        group_texts = [
            c
            for c in mock_evolution_client.send_text.call_args_list
            if c[0][1] == "120363xxx@g.us"
        ]
        assert len(group_texts) == 1
        group_msg = group_texts[0][0][2]
        assert "Auto-registered 2 user(s)" in group_msg
        assert "tier 1: Alphabet / Google" in group_msg

        # 5) No poll was sent (title match bypasses poll)
        mock_evolution_client.send_poll.assert_not_called()

        # 6) Event is marked as seen
        assert not seen.is_new(500)


class TestE2EDescriptionMatch:
    """Full flow: 42 API returns a hackathon mentioning Thales in description
    → description match → notify 2 users (NOT registered) → post to group."""

    async def test_full_description_match_flow(
        self,
        seen_events_path,
        mock_evolution_client,
        mock_oauth_manager,
        user_tokens_store,
        tier1_entries,
        button_event_map,
    ):
        event = {
            "id": 501,
            "name": "Cybersecurity Challenge 2026",
            "kind": "hackathon",
            "description": "A 48h hackathon on cybersecurity, co-organized with Thales.",
            "begin_at": "2026-07-01T09:00:00.000Z",
            "end_at": "2026-07-02T18:00:00.000Z",
            "location": "Innovation Lab",
            "max_people": 200,
            "nbr_subscribers": 45,
        }

        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(return_value=([event], 1))

        seen = SeenEvents(seen_events_path)

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="bot-instance",
            campus_id=1,
            group_jid="120363xxx@g.us",
            first_run=False,
            button_event_map=button_event_map,
            tier1_entries=tier1_entries,
            oauth_manager=mock_oauth_manager,
            user_tokens=user_tokens_store,
        )

        # 1) Nobody was registered
        mock_oauth_manager.register_for_event.assert_not_called()
        mock_oauth_manager.is_registered.assert_not_called()

        # 2) Both users got notification DMs
        user_dms = [
            c
            for c in mock_evolution_client.send_text.call_args_list
            if c[0][1] in ("33612345678", "33698765432")
        ]
        assert len(user_dms) == 2

        for dm_call in user_dms:
            text = dm_call[0][2]
            assert "NOT" in text
            assert "auto-registered" in text.lower()
            assert "Thales" in text
            assert "description" in text.lower()

        # 3) No .ics files sent (description match = no calendar invite)
        mock_evolution_client.send_media.assert_not_called()

        # 4) Group message has desc-match annotation
        group_texts = [
            c
            for c in mock_evolution_client.send_text.call_args_list
            if c[0][1] == "120363xxx@g.us"
        ]
        assert len(group_texts) == 1
        group_msg = group_texts[0][0][2]
        assert "Thales" in group_msg
        assert "not auto-registered" in group_msg.lower()

        # 5) No poll
        mock_evolution_client.send_poll.assert_not_called()

        # 6) Event marked seen
        assert not seen.is_new(501)


class TestE2ENormalEvent:
    """Full flow: Normal event with no tier1 match → poll + button tracking."""

    async def test_full_normal_event_flow(
        self,
        seen_events_path,
        mock_evolution_client,
        mock_oauth_manager,
        user_tokens_store,
        tier1_entries,
        button_event_map,
    ):
        event = {
            "id": 502,
            "name": "Peer Code Review Session",
            "kind": "event",
            "description": "Learn peer review techniques.",
            "begin_at": "2026-08-01T10:00:00.000Z",
            "end_at": "2026-08-01T11:30:00.000Z",
            "location": "Cluster 3",
            "max_people": 30,
            "nbr_subscribers": 5,
        }

        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(return_value=([event], 1))

        seen = SeenEvents(seen_events_path)

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="bot-instance",
            campus_id=1,
            group_jid="120363xxx@g.us",
            first_run=False,
            button_event_map=button_event_map,
            tier1_entries=tier1_entries,
            oauth_manager=mock_oauth_manager,
            user_tokens=user_tokens_store,
        )

        # 1) No registration or notification DMs
        mock_oauth_manager.register_for_event.assert_not_called()
        # Only group-level poll, no DMs to individual users
        user_dms = [
            c
            for c in mock_evolution_client.send_text.call_args_list
            if c[0][1] in ("33612345678", "33698765432")
        ]
        assert len(user_dms) == 0

        # 2) Poll sent to group
        mock_evolution_client.send_poll.assert_called_once()
        poll_kwargs = mock_evolution_client.send_poll.call_args[1]
        assert poll_kwargs["values"] == ["Register", "Unregister"]
        assert "120363xxx@g.us" in mock_evolution_client.send_poll.call_args[0]

        # 3) Button tracked for registration
        poll_msg_id = mock_evolution_client.send_poll.return_value["key"]["id"]
        assert button_event_map.get_event_id(poll_msg_id) == 502

        # 4) Event marked seen
        assert not seen.is_new(502)


class TestE2EMixedBatch:
    """Multiple events in one scraper cycle: one title match, one desc match,
    one normal, one filtered out."""

    async def test_mixed_batch_flow(
        self,
        seen_events_path,
        mock_evolution_client,
        mock_oauth_manager,
        user_tokens_store,
        tier1_entries,
        button_event_map,
    ):
        from whatsapp_bot.filters import EventFilter

        google_event = {
            "id": 600,
            "name": "Google AI Workshop",
            "kind": "workshop",
            "description": "Workshop on AI with Google Cloud.",
            "begin_at": "2026-09-01T10:00:00.000Z",
            "end_at": "2026-09-01T12:00:00.000Z",
            "location": "Room A",
            "max_people": 50,
            "nbr_subscribers": 10,
        }
        thales_event = {
            "id": 601,
            "name": "Defense Innovation Day",
            "kind": "extern",
            "description": "Presentations by Thales and Naval Group.",
            "begin_at": "2026-09-02T09:00:00.000Z",
            "end_at": "2026-09-02T17:00:00.000Z",
            "location": "Main Hall",
            "max_people": 300,
            "nbr_subscribers": 100,
        }
        normal_event = {
            "id": 602,
            "name": "Rush 01",
            "kind": "event",
            "description": "Weekend group project.",
            "begin_at": "2026-09-03T10:00:00.000Z",
            "end_at": "2026-09-04T18:00:00.000Z",
            "location": "Clusters",
            "max_people": None,
            "nbr_subscribers": 0,
        }
        filtered_event = {
            "id": 603,
            "name": "Yoga and Meditation",
            "kind": "event",
            "description": "Relax with yoga.",
            "begin_at": "2026-09-05T07:00:00.000Z",
            "end_at": "2026-09-05T08:00:00.000Z",
            "location": "Rooftop",
            "max_people": 20,
            "nbr_subscribers": 20,
        }

        all_events = [google_event, thales_event, normal_event, filtered_event]

        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(
            return_value=(all_events, len(all_events))
        )

        event_filter = EventFilter(exclude_names=("Yoga",))
        seen = SeenEvents(seen_events_path)

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="bot-instance",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            event_filter=event_filter,
            button_event_map=button_event_map,
            tier1_entries=tier1_entries,
            oauth_manager=mock_oauth_manager,
            user_tokens=user_tokens_store,
        )

        # Google event: title match → 2 users registered
        assert mock_oauth_manager.register_for_event.call_count == 2

        # Thales event: desc match → 2 users notified (but NOT registered)
        # Total user DMs: 2 (Google registration) + 2 (Google .ics) + 2 (Thales notify)
        user_dm_numbers = set()
        for c in mock_evolution_client.send_text.call_args_list:
            recipient = c[0][1]
            if recipient in ("33612345678", "33698765432"):
                user_dm_numbers.add(recipient)
        assert len(user_dm_numbers) == 2  # both users got DMs

        # Normal event: poll sent
        mock_evolution_client.send_poll.assert_called_once()

        # Filtered event: skipped entirely
        # All 4 events marked seen (even filtered ones go through mark_seen
        # after posting — but filtered ones never reach the posting loop)
        # Actually: filtered events are removed before the loop, so they're
        # NOT marked seen. Let's verify:
        assert not seen.is_new(600)  # Google - posted
        assert not seen.is_new(601)  # Thales - posted
        assert not seen.is_new(602)  # Normal - posted
        # Filtered event is NOT marked seen (it was removed before posting)
        # So it would appear again next cycle. This is current behavior.

        # Group messages: Google auto-reg text, Thales desc text
        # Normal event uses poll only (no separate text)
        group_texts = [
            c
            for c in mock_evolution_client.send_text.call_args_list
            if c[0][1] == "group@g.us"
        ]
        assert len(group_texts) == 2


class TestE2EFalsePositiveGuards:
    """Verify that known false positives do NOT trigger tier1 matching."""

    async def test_shell_scripting_no_match(
        self,
        seen_events_path,
        mock_evolution_client,
        mock_oauth_manager,
        user_tokens_store,
        tier1_entries,
        button_event_map,
    ):
        event = {
            "id": 700,
            "name": "Shell Scripting for Beginners",
            "kind": "workshop",
            "description": "Learn bash shell scripting basics.",
            "begin_at": "2026-10-01T14:00:00.000Z",
            "end_at": "2026-10-01T16:00:00.000Z",
            "location": "Lab 1",
            "max_people": 30,
            "nbr_subscribers": 5,
        }

        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(return_value=([event], 1))

        seen = SeenEvents(seen_events_path)

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="bot-instance",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            button_event_map=button_event_map,
            tier1_entries=tier1_entries,
            oauth_manager=mock_oauth_manager,
            user_tokens=user_tokens_store,
        )

        # No registration, no notification DMs
        mock_oauth_manager.register_for_event.assert_not_called()
        user_dms = [
            c
            for c in mock_evolution_client.send_text.call_args_list
            if c[0][1] in ("33612345678", "33698765432")
        ]
        assert len(user_dms) == 0

        # Should fall through to poll (normal event)
        mock_evolution_client.send_poll.assert_called_once()

    async def test_job_fair_no_match(
        self,
        seen_events_path,
        mock_evolution_client,
        mock_oauth_manager,
        user_tokens_store,
        tier1_entries,
        button_event_map,
    ):
        event = {
            "id": 701,
            "name": "Job Fair 2026",
            "kind": "event",
            "description": "Annual campus job fair with 50+ companies.",
            "begin_at": "2026-11-01T10:00:00.000Z",
            "end_at": "2026-11-01T18:00:00.000Z",
            "location": "Main Hall",
            "max_people": 500,
            "nbr_subscribers": 200,
        }

        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(return_value=([event], 1))

        seen = SeenEvents(seen_events_path)

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="bot-instance",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            button_event_map=button_event_map,
            tier1_entries=tier1_entries,
            oauth_manager=mock_oauth_manager,
            user_tokens=user_tokens_store,
        )

        mock_oauth_manager.register_for_event.assert_not_called()

    async def test_url_microsoft_no_desc_match(
        self,
        seen_events_path,
        mock_evolution_client,
        mock_oauth_manager,
        user_tokens_store,
        tier1_entries,
        button_event_map,
    ):
        """Microsoft in a URL should NOT trigger description match."""
        event = {
            "id": 702,
            "name": "Online Workshop",
            "kind": "workshop",
            "description": (
                "Join via https://teams.microsoft.com/l/abc123 for the session."
            ),
            "begin_at": "2026-10-15T14:00:00.000Z",
            "end_at": "2026-10-15T16:00:00.000Z",
            "location": "Online",
            "max_people": None,
            "nbr_subscribers": 0,
        }

        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(return_value=([event], 1))

        seen = SeenEvents(seen_events_path)

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="bot-instance",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            button_event_map=button_event_map,
            tier1_entries=tier1_entries,
            oauth_manager=mock_oauth_manager,
            user_tokens=user_tokens_store,
        )

        # No registration, no notification
        mock_oauth_manager.register_for_event.assert_not_called()
        user_dms = [
            c
            for c in mock_evolution_client.send_text.call_args_list
            if c[0][1] in ("33612345678", "33698765432")
        ]
        assert len(user_dms) == 0


class TestE2EExpiredTokenRefresh:
    """End-to-end: user has expired token → refresh → register → notify."""

    async def test_expired_token_refreshed_and_registered(
        self,
        seen_events_path,
        mock_evolution_client,
        mock_oauth_manager,
        tier1_entries,
        button_event_map,
        tmp_path,
    ):
        from whatsapp_bot.user_tokens import UserToken, UserTokenStore

        store = UserTokenStore(str(tmp_path / "tokens.json"))
        store.set(
            "33611111111",
            UserToken(
                access_token="expired-token",
                refresh_token="refresh-token-123",
                expires_at=time.time() - 3600,  # expired 1h ago
                user_id=42100,
            ),
        )

        event = {
            "id": 800,
            "name": "Microsoft Hackathon",
            "kind": "hackathon",
            "description": "Build with Azure.",
            "begin_at": "2026-12-01T09:00:00.000Z",
            "end_at": "2026-12-02T18:00:00.000Z",
            "location": "Innovation Hub",
            "max_people": 100,
            "nbr_subscribers": 30,
        }

        ft_client = AsyncMock()
        ft_client.get_campus_events = AsyncMock(return_value=([event], 1))

        seen = SeenEvents(seen_events_path)

        await _poll_and_post(
            ft_client,
            mock_evolution_client,
            seen=seen,
            instance="bot-instance",
            campus_id=1,
            group_jid="group@g.us",
            first_run=False,
            button_event_map=button_event_map,
            tier1_entries=tier1_entries,
            oauth_manager=mock_oauth_manager,
            user_tokens=store,
        )

        # Token should have been refreshed
        mock_oauth_manager.refresh_token.assert_called_once_with("refresh-token-123")

        # Then registered
        mock_oauth_manager.register_for_event.assert_called_once()

        # Notification sent
        user_dms = [
            c
            for c in mock_evolution_client.send_text.call_args_list
            if c[0][1] == "33611111111"
        ]
        assert len(user_dms) == 1
        assert "auto-registered" in user_dms[0][0][2].lower()
