from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from whatsapp_bot.button_events import ButtonEventMap
from whatsapp_bot.filters import add_event_exclusion, list_event_exclusions
from whatsapp_bot.handlers import configure_oauth, handle_messages_upsert


def _configure(tmp_path: Path) -> Path:
    buttons = ButtonEventMap(str(tmp_path / "buttons.json"))
    path = tmp_path / "filters.toml"
    path.write_text('[scraper]\nexclude_names = [\n    "Afterwork",\n]\n')
    configure_oauth(AsyncMock(), AsyncMock(), buttons, str(path), "33633493864")
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/filter", "/f", "/F"])
async def test_admin_filter_command_adds_title_keyword(tmp_path: Path, command: str) -> None:
    path = _configure(tmp_path)
    client = AsyncMock()
    data = {
        "key": {
            "remoteJid": "group@g.us",
            "participantAlt": "33633493864@s.whatsapp.net",
        },
        "message": {"conversation": f"{command} BDE Flex"},
    }

    await handle_messages_upsert(data, client=client, instance="default")

    assert list_event_exclusions(str(path)) == ("Afterwork", "BDE Flex")
    client.send_text.assert_awaited_once_with(
        "default", "33633493864", "Event title filter added: BDE Flex",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/filter", "/f", "/uf", "/fs"])
async def test_non_admin_filter_command_is_ignored(tmp_path: Path, command: str) -> None:
    path = _configure(tmp_path)
    client = AsyncMock()
    data = {
        "key": {
            "remoteJid": "group@g.us",
            "participantAlt": "33600000000@s.whatsapp.net",
        },
        "message": {"conversation": f"{command} BDE Flex"},
    }

    await handle_messages_upsert(data, client=client, instance="default")

    client.send_text.assert_not_awaited()
    assert list_event_exclusions(str(path)) == ("Afterwork",)


@pytest.mark.asyncio
@pytest.mark.parametrize("listing,remove", [("/filters", "/unfilter"), ("/fs", "/uf"), ("/FS", "/UF")])
async def test_admin_can_list_and_remove_filters(tmp_path: Path, listing: str, remove: str) -> None:
    path = _configure(tmp_path)
    add_event_exclusion("BDE Flex", str(path))
    client = AsyncMock()
    key = {
        "remoteJid": "group@g.us",
        "participantAlt": "33633493864@s.whatsapp.net",
    }

    await handle_messages_upsert(
        {"key": key, "message": {"conversation": listing}},
        client=client,
        instance="default",
    )
    await handle_messages_upsert(
        {"key": key, "message": {"conversation": f"{remove} bde flex"}},
        client=client,
        instance="default",
    )

    assert list_event_exclusions(str(path)) == ("Afterwork",)
    assert client.send_text.await_count == 2
