"""Shared fixtures for all tests."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure BOT_ env vars exist before any whatsapp_bot imports touch config.py
os.environ.setdefault("BOT_EVOLUTION_API_URL", "http://test:8080")
os.environ.setdefault("BOT_EVOLUTION_API_KEY", "test-key")


# ---------------------------------------------------------------------------
# Sample events used across multiple test files
# ---------------------------------------------------------------------------

GOOGLE_TITLE_EVENT = {
    "id": 100,
    "name": "Google Tech Talk",
    "kind": "extern",
    "description": "Join us for a tech talk with Google engineers.",
    "begin_at": "2026-03-10T14:00:00.000Z",
    "end_at": "2026-03-10T16:00:00.000Z",
    "location": "Amphi 1",
    "max_people": 100,
    "nbr_subscribers": 42,
}

MASTERCARD_TITLE_EVENT = {
    "id": 101,
    "name": "Master Card Workshop",
    "kind": "workshop",
    "description": "Fintech workshop with Master Card experts.",
    "begin_at": "2026-03-12T10:00:00.000Z",
    "end_at": "2026-03-12T12:00:00.000Z",
    "location": "Salle 42",
    "max_people": 50,
    "nbr_subscribers": 10,
}

THALES_DESC_EVENT = {
    "id": 102,
    "name": "Cybersecurity Hackathon",
    "kind": "hackathon",
    "description": "A cybersecurity hackathon sponsored by Thales and Marine Nationale.",
    "begin_at": "2026-04-01T09:00:00.000Z",
    "end_at": "2026-04-02T18:00:00.000Z",
    "location": "Innovation Lab",
    "max_people": 200,
    "nbr_subscribers": 75,
}

NORMAL_EVENT = {
    "id": 103,
    "name": "Peer-to-Peer Code Review",
    "kind": "event",
    "description": "A session on best practices for peer code review.",
    "begin_at": "2026-03-15T11:00:00.000Z",
    "end_at": "2026-03-15T12:00:00.000Z",
    "location": "Cluster 1",
    "max_people": 30,
    "nbr_subscribers": 5,
}

SHELL_FALSE_POSITIVE_EVENT = {
    "id": 104,
    "name": "Shell Scripting Workshop",
    "kind": "workshop",
    "description": "Learn bash and shell scripting from scratch.",
    "begin_at": "2026-03-20T14:00:00.000Z",
    "end_at": "2026-03-20T16:00:00.000Z",
    "location": "Cluster 2",
    "max_people": None,
    "nbr_subscribers": 0,
}

JOB_FAIR_FALSE_POSITIVE_EVENT = {
    "id": 105,
    "name": "Job Fair 2026",
    "kind": "event",
    "description": "Annual campus job fair.",
    "begin_at": "2026-05-01T10:00:00.000Z",
    "end_at": "2026-05-01T18:00:00.000Z",
    "location": "Main Hall",
    "max_people": 500,
    "nbr_subscribers": 200,
}

DESC_WITH_URL_EVENT = {
    "id": 106,
    "name": "Remote Collaboration Workshop",
    "kind": "workshop",
    "description": (
        "Join us at https://teams.microsoft.com/l/meetup-join/abc123 "
        "for a workshop on remote collaboration tools."
    ),
    "begin_at": "2026-03-25T14:00:00.000Z",
    "end_at": "2026-03-25T16:00:00.000Z",
    "location": "Online",
    "max_people": None,
    "nbr_subscribers": 0,
}

DESC_SLACK_NOISE_EVENT = {
    "id": 107,
    "name": "Team Coordination Meeting",
    "kind": "extern",
    "description": "Please join our Slack channel for updates on the project.",
    "begin_at": "2026-03-26T10:00:00.000Z",
    "end_at": "2026-03-26T11:00:00.000Z",
    "location": "Room B",
    "max_people": None,
    "nbr_subscribers": 0,
}

# Event with wrong kind for description matching
DESC_WRONG_KIND_EVENT = {
    "id": 108,
    "name": "Fun Day",
    "kind": "association",
    "description": "A fun day sponsored by Goldman Sachs.",
    "begin_at": "2026-04-10T10:00:00.000Z",
    "end_at": "2026-04-10T18:00:00.000Z",
    "location": "Courtyard",
    "max_people": None,
    "nbr_subscribers": 0,
}

FILTERED_EVENT = {
    "id": 109,
    "name": "Yoga Session",
    "kind": "event",
    "description": "Morning yoga for relaxation.",
    "begin_at": "2026-03-18T07:00:00.000Z",
    "end_at": "2026-03-18T08:00:00.000Z",
    "location": "Rooftop",
    "max_people": 20,
    "nbr_subscribers": 20,
}


@pytest.fixture
def tier1_companies_file(tmp_path: Path) -> Path:
    """Create a minimal tier1 companies file for testing."""
    p = tmp_path / "tier1_companies.txt"
    p.write_text(
        "# Big Tech\n"
        "Alphabet / Google\n"
        "Microsoft\n"
        "Amazon\n"
        "Meta Platforms\n"
        "\n"
        "# Finance\n"
        "Goldman Sachs\n"
        "Mastercard\n"
        "JPMorgan Chase\n"
        "\n"
        "# Defense\n"
        "Thales\n"
        "\n"
        "# Consulting\n"
        "McKinsey & Company\n"
        "\n"
        "# AI\n"
        "OpenAI\n"
        "Mistral AI\n"
        "\n"
        "# Telecom\n"
        "Orange\n"
    )
    return p


@pytest.fixture
def tier1_entries(tier1_companies_file: Path):
    """Load tier1 search entries from the test companies file."""
    from whatsapp_bot.tier1 import load_tier1_companies

    return load_tier1_companies(str(tier1_companies_file))


@pytest.fixture
def mock_evolution_client():
    """Create a mock EvolutionClient with all async methods mocked."""
    client = AsyncMock()
    client.send_text = AsyncMock(return_value={"key": {"id": "msg-001"}})
    client.send_media = AsyncMock(return_value={"key": {"id": "msg-002"}})
    client.send_poll = AsyncMock(
        return_value={"key": {"id": "poll-001"}}
    )
    return client


@pytest.fixture
def mock_oauth_manager():
    """Create a mock OAuthManager."""
    manager = AsyncMock()
    manager.is_registered = AsyncMock(return_value=False)
    manager.register_for_event = AsyncMock(return_value={"id": 1})
    manager.refresh_token = AsyncMock(
        return_value={
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expires_in": 7200,
        }
    )
    manager.token_expires_at = MagicMock(
        side_effect=lambda expires_in: time.time() + expires_in
    )
    return manager


@pytest.fixture
def user_tokens_store(tmp_path: Path):
    """Create a UserTokenStore with two test users."""
    from whatsapp_bot.user_tokens import UserToken, UserTokenStore

    path = tmp_path / "user_tokens.json"
    store = UserTokenStore(str(path))

    # User 1: valid token
    store.set(
        "33612345678",
        UserToken(
            access_token="token-user1",
            refresh_token="refresh-user1",
            expires_at=time.time() + 7200,
            user_id=42001,
        ),
    )

    # User 2: valid token
    store.set(
        "33698765432",
        UserToken(
            access_token="token-user2",
            refresh_token="refresh-user2",
            expires_at=time.time() + 7200,
            user_id=42002,
        ),
    )

    return store


@pytest.fixture
def button_event_map(tmp_path: Path):
    """Create a ButtonEventMap for testing."""
    from whatsapp_bot.button_events import ButtonEventMap

    return ButtonEventMap(str(tmp_path / "button_events.json"))


@pytest.fixture
def seen_events_path(tmp_path: Path) -> str:
    """Return a path for SeenEvents (file doesn't need to exist yet)."""
    return str(tmp_path / "seen_events.json")
