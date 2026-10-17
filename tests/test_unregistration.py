from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from whatsapp_bot.button_events import ButtonEventMap
from whatsapp_bot.handlers import configure_oauth, handle_messages_upsert
from whatsapp_bot.handlers import _is_waitlist_registration
from whatsapp_bot.oauth import OAuthManager
from whatsapp_bot.user_tokens import UserToken, UserTokenStore


@pytest.mark.asyncio
async def test_oauth_unregisters_matching_event_registration(tmp_path: Path) -> None:
    manager = OAuthManager("client", "secret", "https://example.test/callback", str(tmp_path / "pending.json"))
    registration_response = MagicMock(is_success=True)
    registration_response.json.return_value = [{"id": 99}]
    delete_response = MagicMock(is_success=True)
    http_client = MagicMock()
    http_client.get = AsyncMock(return_value=registration_response)
    http_client.delete = AsyncMock(return_value=delete_response)
    manager._client = http_client

    removed = await manager.unregister_from_event("token", 42, 7)

    assert removed is True
    http_client.delete.assert_awaited_once_with(
        "/v2/events_users/99", headers={"Authorization": "Bearer token"},
    )


@pytest.mark.asyncio
async def test_poll_unregister_uses_sender_linked_account(tmp_path: Path) -> None:
    filters_path = tmp_path / "filters.toml"
    filters_path.write_text("[scraper]\nexclude_names = []\n")
    buttons = ButtonEventMap(str(tmp_path / "buttons.json"))
    buttons.track("event-message", 42)
    tokens = UserTokenStore(str(tmp_path / "tokens.json"))
    tokens.set("33633493864", UserToken("token", "refresh", 9_999_999_999, 7))
    oauth = MagicMock()
    oauth.unregister_from_event = AsyncMock(return_value=True)
    configure_oauth(oauth, tokens, buttons, str(filters_path), "33633493864")
    client = AsyncMock()
    data = {
        "key": {
            "remoteJid": "group@g.us",
            "participantAlt": "33633493864@s.whatsapp.net",
        },
        "message": {
            "pollUpdateMessage": {
                "pollCreationMessageKey": {"id": "event-message"},
                "vote": {"selectedOptions": ["Unregister"]},
            },
        },
    }

    await handle_messages_upsert(data, client=client, instance="default")

    oauth.unregister_from_event.assert_awaited_once_with("token", 42, 7)
    client.send_text.assert_awaited_once_with(
        "default", "33633493864", "✅ Your 42 event registration or waitlist entry was cancelled.",
    )


def test_detects_full_event_with_active_waitlist() -> None:
    assert _is_waitlist_registration(
        {"waitlist": {"id": 1}, "max_people": 4, "nbr_subscribers": 4}
    )
    assert not _is_waitlist_registration(
        {"waitlist": {"id": 1}, "max_people": 4, "nbr_subscribers": 3}
    )
