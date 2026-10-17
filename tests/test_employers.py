"""Semantic extraction, cache boundaries and real offer-consumer behavior (no live sends)."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from whatsapp_bot import employers
from whatsapp_bot.employers import (
    EmployerResolutionError,
    EmployerResolver,
    clean_offer,
    match_tier1_employer,
    passages,
    validate,
)
from whatsapp_bot.offers import SeenOffers, _poll_and_post_offers, get_upcoming_offers
from whatsapp_bot.tier1 import load_tier1_companies

AD = {"id": 28388, "title": "Cursor and Azure engineer", "little_description": "", "big_description": "<p>Galadrim recrute pour sa propre équipe.</p>\n\nOutils : Cursor, Microsoft Azure."}
WIRE = {"hiring_company": "Galadrim", "relationship": "direct_employer", "evidence_ids": [1]}
UNKNOWN = {"hiring_company": None, "relationship": "unknown", "evidence_ids": []}


def generation(wire, finish="STOP"):
    return {"candidates": [{"finishReason": finish, "content": {"parts": [{"text": json.dumps(wire)}]}}]}


@pytest.fixture
async def factory(tmp_path):
    opened = []

    def make(wire=None, *, status=200, path=None, model=employers.MODEL):
        requests = []
        state = {"status": status, "body": generation(WIRE if wire is None else wire)}

        async def handler(request):
            requests.append(request)
            return httpx.Response(state["status"], json=state["body"])

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        resolver = EmployerResolver("test-key", cache_path=str(path or tmp_path / f"cache-{len(opened)}.json"), client=client, min_interval=0, model=model)
        opened.append(resolver)
        return resolver, requests, state

    yield make
    for resolver in opened:
        await resolver.close()


@pytest.fixture
def roster():
    return load_tier1_companies("config/tier1_companies.txt")


def test_visible_source_and_verbatim_evidence():
    source = clean_offer(AD)
    assert source["big_description"].startswith("Galadrim")
    result = validate(WIRE, source)
    assert result["evidence"] == ["Galadrim recrute pour sa propre équipe."]
    assert result["status"] == "supported"
    assert len(passages(source)) == 3


@pytest.mark.parametrize("update", [
    {"evidence_ids": [999]}, {"evidence_ids": [True]}, {"evidence_ids": [1, 1]},
    {"hiring_company": "Fabricated Company"}, {"hiring_company": "Galadrim\nMicrosoft"},
    {"hiring_company": None}, {"relationship": "unknown"}, {"status": "supported"},
    {"evidence_ids": "1"},
])
def test_invalid_contract_is_rejected(update):
    with pytest.raises(ValueError):
        validate({**WIRE, **update}, clean_offer(AD))


def test_null_unnamed_client_derives_unknown():
    result = validate({**UNKNOWN, "relationship": "unnamed_client", "evidence_ids": [2]}, clean_offer(AD))
    assert result["status"] == "unknown"
    assert result["hiring_company"] is None


def test_provenance_is_not_keyword_relationship_inference():
    # This would be semantically WRONG. Provenance checks deliberately do not pretend otherwise.
    # The semantic model is evaluated against reference labels, not lexical hiring rules.
    result = validate({"hiring_company": "Microsoft", "relationship": "direct_employer", "evidence_ids": [2]}, clean_offer(AD))
    assert result["hiring_company"] == "Microsoft"


async def test_request_contract_header_and_citations(factory):
    resolver, requests, _ = factory()
    result = await resolver.resolve(AD)
    assert result["hiring_company"] == "Galadrim"
    request = requests[0]
    assert request.headers["x-goog-api-key"] == "test-key"
    assert "test-key" not in str(request.url)
    payload = json.loads(request.content)
    assert payload["systemInstruction"]["parts"][0]["text"] == employers.PROMPT
    assert payload["generationConfig"]["responseSchema"] == employers.SCHEMA
    supplied = json.loads(payload["contents"][0]["parts"][0]["text"])
    assert supplied == {"passages": passages(clean_offer(AD))}
    assert "Tier 1 roster" not in payload["contents"][0]["parts"][0]["text"]


async def test_persistent_cache_reloads_and_avoids_api(factory, tmp_path):
    path = tmp_path / "cache.json"
    first, calls, _ = factory(path=path)
    result = await first.resolve(AD)
    again, later_calls, _ = factory(path=path)
    assert await again.resolve(AD) == result
    assert len(calls) == 1 and not later_calls
    assert path.stat().st_mode & 0o777 == 0o600
    assert "test-key" not in path.read_text()


async def test_concurrent_consumers_share_one_request(factory):
    resolver, calls, _ = factory()
    results = await asyncio.gather(resolver.resolve(AD), resolver.resolve(AD))
    assert results[0] == results[1]
    assert len(calls) == 1


async def test_changed_text_and_model_invalidate_cache(factory, tmp_path):
    path = tmp_path / "cache.json"
    resolver, calls, state = factory(path=path)
    await resolver.resolve(AD)
    # Adding a paragraph shifts the correct source citation; simulate a fresh model response.
    state["body"] = generation({**WIRE, "evidence_ids": [2]})
    await resolver.resolve({**AD, "little_description": "Updated role"})
    assert len(calls) == 2
    changed, calls2, _ = factory(path=path, model="another-model")
    await changed.resolve(AD)
    assert len(calls2) == 1


async def test_prompt_changes_invalidate_cache(factory, monkeypatch):
    resolver, calls, _ = factory()
    await resolver.resolve(AD)
    monkeypatch.setattr(employers, "PROMPT", employers.PROMPT + "\nNew version")
    await resolver.resolve(AD)
    assert len(calls) == 2


@pytest.mark.parametrize("wire,age", [(WIRE, 604801), (UNKNOWN, 3601)])
async def test_expired_cache_reextracts(factory, wire, age):
    resolver, calls, _ = factory(wire)
    await resolver.resolve(AD)
    for record in resolver._cache.values():
        record["time"] -= age
    await resolver.resolve(AD)
    assert len(calls) == 2


@pytest.mark.parametrize("status", [403, 404, 429, 503])
async def test_http_failure_not_cached_or_keyword_fallback(factory, status):
    resolver, calls, _ = factory(status=status)
    with pytest.raises(EmployerResolutionError, match=f"HTTP {status}"):
        await resolver.resolve(AD)
    assert not resolver.path.exists()
    if status == 429:
        with pytest.raises(EmployerResolutionError, match="rate limited"):
            await resolver.resolve(AD)
        assert len(calls) == 1


@pytest.mark.parametrize("body", [
    {}, generation(WIRE, "MAX_TOKENS"), generation({**WIRE, "evidence_ids": [999]}),
    {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "not JSON"}]}}]},
])
async def test_malformed_generation_not_cached(factory, body):
    resolver, _, state = factory()
    state["body"] = body
    with pytest.raises(EmployerResolutionError, match="schema/source-citation"):
        await resolver.resolve(AD)
    assert not resolver.path.exists()


def test_bad_cache_and_model_fail_descriptively(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not-json")
    with pytest.raises(ValueError, match="Invalid employer cache"):
        EmployerResolver("test-key", cache_path=str(path))
    with pytest.raises(ValueError, match="model ID"):
        EmployerResolver("test-key", cache_path=str(path), model="../secret")


@pytest.mark.parametrize("surface", ["BCG X", "BCG X Delivery", "BCG X Delivery Casablanca"])
def test_reviewed_exact_aliases_map_after_extraction(roster, surface):
    result = {"status": "supported", "hiring_company": surface}
    assert match_tier1_employer(result, roster) == "Boston Consulting Group (BCG)"
    # Disabling Tier 1 must also disable alias lookup, not block normal posting.
    assert match_tier1_employer(result, [], aliases_path="missing-disabled-aliases.toml") is None
    assert match_tier1_employer({**result, "hiring_company": surface + " unrelated startup"}, roster) is None
    assert match_tier1_employer({**result, "status": "unknown"}, roster) is None


async def poll(resolver, offer, tmp_path, roster, users=None, seen=None):
    ft, evolution = AsyncMock(), AsyncMock()
    ft.get_offers.return_value = ([offer], 1)
    seen = seen or SeenOffers(str(tmp_path / "seen.json"))
    await _poll_and_post_offers(ft, evolution, seen=seen, instance="test", campus_id=1,
        group_jid="group@g.us", first_run=False, tier1_entries=roster,
        user_tokens=users, employer_resolver=resolver)
    return ft, evolution, seen


async def test_tools_do_not_trigger_private_alerts(factory, roster, tmp_path, monkeypatch):
    resolver, calls, _ = factory()
    users = MagicMock()
    ft, evolution, seen = await poll(resolver, AD, tmp_path, roster, users)
    evolution.send_text.assert_awaited_once()
    message = evolution.send_text.await_args.args[2]
    assert "Hiring company: Galadrim" in message
    assert "Tier 1 company:" not in message
    users.items.assert_not_called()
    assert not seen.is_new(AD["id"])
    # Actual on-demand consumer reuses polling's persisted extraction, not a second model call.
    reply = await get_upcoming_offers(ft, 1, employer_resolver=resolver)
    assert "Hiring company: Galadrim" in reply
    assert len(calls) == 1


async def test_verified_tier1_employer_notifies_not_registers(factory, roster, tmp_path, user_tokens_store):
    offer = {"id": 27454, "title": "Engineer at BCG X Delivery Casablanca"}
    resolver, _, _ = factory({"hiring_company": "BCG X Delivery Casablanca", "relationship": "direct_employer", "evidence_ids": [0]})
    _, evolution, seen = await poll(resolver, offer, tmp_path, roster, user_tokens_store)
    assert evolution.send_text.await_count == 3
    group = [c.args[2] for c in evolution.send_text.await_args_list if c.args[1] == "group@g.us"][0]
    assert "Tier 1 company: Boston Consulting Group (BCG)" in group
    assert "Hiring company: BCG X Delivery Casablanca" in group
    assert not seen.is_new(27454)
    evolution.send_poll.assert_not_awaited()


async def test_unknown_employer_never_falls_back_to_tier1_title(factory, roster, tmp_path):
    resolver, _, _ = factory({**UNKNOWN, "relationship": "unnamed_client"})
    users = MagicMock()
    _, evolution, seen = await poll(resolver, {**AD, "title": "Google software engineer"}, tmp_path, roster, users)
    evolution.send_text.assert_awaited_once()
    assert "Hiring company: Unnamed client" in evolution.send_text.await_args.args[2]
    users.items.assert_not_called()
    assert not seen.is_new(AD["id"])


async def test_failed_extraction_stays_unseen_then_retries(factory, roster, tmp_path):
    resolver, _, state = factory(status=503)
    _, evolution, seen = await poll(resolver, AD, tmp_path, roster)
    evolution.send_text.assert_not_awaited()
    assert seen.is_new(AD["id"])
    state["status"] = 200
    _, retried, seen = await poll(resolver, AD, tmp_path, roster, seen=seen)
    retried.send_text.assert_awaited_once()
    assert not seen.is_new(AD["id"])


async def test_on_demand_failure_is_explicit(factory):
    resolver, _, _ = factory(status=503)
    ft = AsyncMock()
    ft.get_offers.return_value = ([AD], 1)
    reply = await get_upcoming_offers(ft, 1, employer_resolver=resolver)
    assert "Hiring company: Unavailable" in reply
    assert "Tier 1" not in reply


async def test_first_run_seeding_does_not_invoke_extractor(factory, tmp_path):
    resolver, calls, _ = factory()
    ft, evolution = AsyncMock(), AsyncMock()
    ft.get_offers.return_value = ([AD], 1)
    await _poll_and_post_offers(ft, evolution, seen=SeenOffers(str(tmp_path / "seed.json")), instance="test", campus_id=1, group_jid="group@g.us", first_run=True, employer_resolver=resolver)
    assert not calls
    evolution.send_text.assert_not_awaited()


async def test_lifespan_shares_resolver_with_command_and_scraper(factory, tmp_path, monkeypatch):
    from whatsapp_bot import ft_client, handlers, main, oauth, offers
    from whatsapp_bot.config import Settings

    resolver, requests, _ = factory()
    constructor = MagicMock(return_value=resolver)
    monkeypatch.setattr(main, "EmployerResolver", constructor)
    api = AsyncMock()
    api.get_offers.return_value = ([AD], 1)
    monkeypatch.setattr(ft_client, "FtClient", MagicMock(return_value=api))
    oauth_api = AsyncMock()
    monkeypatch.setattr(oauth, "OAuthManager", MagicMock(return_value=oauth_api))
    evolution = AsyncMock()
    monkeypatch.setattr(main, "evolution_client", evolution)
    background = AsyncMock()
    monkeypatch.setattr(offers, "run_offers_scraper", background)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    settings = Settings(evolution_api_url="http://test:8080", evolution_api_key="test-key",
        ft_client_id="test-client", ft_client_secret="test-secret", gemini_api_key="test-gemini",  # noqa: S106 - inert fixture credentials, never sent
        user_tokens_path=str(tmp_path / "tokens.json"), button_events_path=str(tmp_path / "buttons.json"),
        employer_cache_path=str(tmp_path / "employers.json"), offers_scraper_enabled=True,
        whatsapp_group_jid="group@g.us", scraper_enabled=False)
    monkeypatch.setattr(main, "settings", settings)
    for attr in ("_ft_client", "_ft_campus_id", "_employer_resolver", "_oauth_manager", "_user_tokens", "_button_events", "_filters_path", "_admin_whatsapp_number"):
        monkeypatch.setattr(handlers, attr, getattr(handlers, attr))
    monkeypatch.setattr(main, "_ft_client", main._ft_client)
    async with main.lifespan(main.app):
        assert handlers._employer_resolver is resolver
        await asyncio.sleep(0)  # Let the mocked background coroutine start.
        assert background.await_args is not None
        assert background.await_args.kwargs["employer_resolver"] is resolver
        await handlers.handle_messages_upsert(
            {"key": {"remoteJid": "99119911@s.whatsapp.net"}, "message": {"conversation": "/offers"}},
            client=evolution, instance="test")
        assert "Hiring company: Galadrim" in evolution.send_text.await_args.args[2]
        assert len(requests) == 1
    assert resolver._http.is_closed
    constructor.assert_called_once_with("test-gemini", model=settings.employer_model, cache_path=settings.employer_cache_path)
    api.close.assert_awaited_once()
    oauth_api.close.assert_awaited_once()
    evolution.close.assert_awaited_once()
