"""Company blocklists follow the existing persistent admin-filter workflow."""
from unittest.mock import AsyncMock

import pytest

from whatsapp_bot import handlers
from whatsapp_bot.filters import (
    EventFilter,
    OfferFilter,
    add_company_exclusion,
    list_company_exclusions,
    load_offers_scraper_filter,
    load_scraper_filter,
    remove_company_exclusion,
)


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "filters.toml"
    path.write_text('[scraper]\nexclude_names = [\n    "Afterwork",\n]\n'
                    '[offers_scraper]\ncontract_types = ["stage"]\n')
    return path


def test_persistence_and_existing_settings(config):
    assert add_company_exclusion("Société Générale", str(config))
    assert not add_company_exclusion("societe generale", str(config))
    assert list_company_exclusions(str(config)) == ("Société Générale",)
    events = load_scraper_filter(str(config))
    offers = load_offers_scraper_filter(str(config))
    assert events is not None and offers is not None
    assert events.exclude_names == ("Afterwork",)
    assert offers.contract_types == ("stage",)
    assert not events.matches({"name": "Talk at Societe Generale"})
    assert not offers.matches({"company": {"name": "Société Générale"}})
    assert remove_company_exclusion("SOCIETE GENERALE", str(config))
    assert not remove_company_exclusion("Missing", str(config))
    assert list_company_exclusions(str(config)) == ()
    reloaded = load_scraper_filter(str(config))
    assert reloaded is not None
    assert reloaded.exclude_names == ("Afterwork",)


@pytest.mark.parametrize("term", ["", "   ", "***", "A" * 81])
def test_invalid_company_names_rejected(config, term):
    before = config.read_bytes()
    with pytest.raises(ValueError):
        add_company_exclusion(term, str(config))
    assert config.read_bytes() == before


@pytest.mark.parametrize("filter_type, field", [
    (EventFilter, "name"), (EventFilter, "description"),
    (OfferFilter, "title"), (OfferFilter, "little_description"),
    (OfferFilter, "big_description"),
])
def test_company_matching_is_normalized_literal_and_word_bounded(filter_type, field):
    active = filter_type(exclude_companies=("Meta", "Société Générale", "C++"))
    assert not active.is_empty
    assert not active.matches({field: "Meet META engineers"})
    assert not active.matches({field: "SOCIETE-GENERALE workshop"})
    assert active.matches({field: "Metaverse and metadata"})
    assert active.matches({field: "An unrelated event"})


def test_quoted_company_names_roundtrip_without_touching_other_sections(config):
    original = config.read_text()
    names = ('Acme ] Labs', 'Quoted "Brand"', r'Back\Slash')
    for name in names:
        assert add_company_exclusion(name, str(config))
    assert list_company_exclusions(str(config)) == names
    assert config.read_text().startswith(original)
    with config.open("a") as stream:
        stream.write('\n[extra]\nkeep = "unchanged"\n')
    assert remove_company_exclusion(names[0], str(config))
    assert list_company_exclusions(str(config)) == names[1:]
    assert '[extra]\nkeep = "unchanged"' in config.read_text()


def test_company_only_config_and_validation(tmp_path):
    path = tmp_path / "only.toml"
    path.write_text('[companies]\nexclude_names = ["Acme"]\n')
    events = load_scraper_filter(str(path))
    offers = load_offers_scraper_filter(str(path))
    assert events is not None and offers is not None
    assert not events.matches({"name": "Acme talk"})
    assert not offers.matches({"company": "Acme"})
    path.write_text('[companies]\nexclude_names = "Acme"\n')
    with pytest.raises(ValueError, match="array of strings"):
        load_scraper_filter(str(path))


@pytest.mark.parametrize("add,list_cmd,remove", [
    ("/filtercompany", "/filterscompany", "/unfiltercompany"),
    ("/fc", "/fcs", "/ufc"),
    ("/FC", "/FCS", "/UFC"),
])
async def test_admin_commands_add_list_remove_and_non_admin_denied(
    config, monkeypatch, add, list_cmd, remove,
):
    monkeypatch.setattr(handlers, "_filters_path", str(config))
    monkeypatch.setattr(handlers, "_admin_whatsapp_number", "12345")
    client = AsyncMock()

    async def command(text, sender="12345"):
        await handlers.handle_messages_upsert({
            "key": {"remoteJid": "group@g.us", "participantAlt": sender + "@s.whatsapp.net"},
            "message": {"conversation": text},
        }, client=client, instance="default")

    await command(f'{add} "Acme Group"', sender="99999")
    client.send_text.assert_not_awaited()
    await command(f'{add} "Acme Group"')
    assert list_company_exclusions(str(config)) == ("Acme Group",)
    client.send_text.assert_awaited_with("default", "12345", "Company filter added: Acme Group")
    await command(list_cmd)
    client.send_text.assert_awaited_with("default", "12345", "• Acme Group")
    await command(f"{remove} acme group")
    assert list_company_exclusions(str(config)) == ()
    await command(f"{add} ***")
    assert list_company_exclusions(str(config)) == ()


async def test_removed_companyfilters_alias_is_ignored(config, monkeypatch):
    monkeypatch.setattr(handlers, "_filters_path", str(config))
    monkeypatch.setattr(handlers, "_admin_whatsapp_number", "12345")
    client = AsyncMock()
    await handlers.handle_messages_upsert({
        "key": {"remoteJid": "12345@s.whatsapp.net"},
        "message": {"conversation": "/companyfilters"},
    }, client=client, instance="default")
    client.send_text.assert_not_awaited()


async def test_events_and_offers_queries_apply_company_exclusions(config):
    from whatsapp_bot.offers import get_upcoming_offers
    from whatsapp_bot.scraper import get_upcoming_events

    add_company_exclusion("Acme", str(config))
    ft = AsyncMock()
    ft.get_campus_events.return_value = ([{"id": 1, "name": "Acme talk"}], 1)
    ft.get_offers.return_value = ([{"id": 2, "title": "Acme engineer"}], 1)
    assert "No events match" in await get_upcoming_events(ft, 1, filters_path=str(config))
    assert "No offers match" in await get_upcoming_offers(ft, 1, filters_path=str(config))


async def test_scrapers_reload_and_exclude_before_notifications(config, tmp_path):
    from whatsapp_bot.offers import SeenOffers, _poll_and_post_offers
    from whatsapp_bot.scraper import SeenEvents, _poll_and_post

    ft = AsyncMock()
    client = AsyncMock()
    oauth = AsyncMock()
    users = AsyncMock()
    ft.get_campus_events.return_value = ([{"id": 11, "name": "Acme talk"}], 1)
    ft.get_offers.return_value = ([{"id": 12, "title": "Acme engineer"}], 1)
    events_seen = SeenEvents(str(tmp_path / "events.json"))
    offers_seen = SeenOffers(str(tmp_path / "offers.json"))
    # Change persistent configuration after startup, without rebuilding clients.
    add_company_exclusion("Acme", str(config))
    await _poll_and_post(ft, client, seen=events_seen, instance="default", campus_id=1,
                         group_jid="group@g.us", first_run=False, filters_path=str(config),
                         oauth_manager=oauth, user_tokens=users)
    await _poll_and_post_offers(ft, client, seen=offers_seen, instance="default", campus_id=1,
                               group_jid="group@g.us", first_run=False, filters_path=str(config),
                               user_tokens=users)
    client.send_text.assert_not_awaited()
    client.send_poll.assert_not_awaited()
    oauth.register_for_event.assert_not_awaited()
    assert not events_seen.is_new(11)
    assert not offers_seen.is_new(12)
