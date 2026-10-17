"""Tier 1 names must not turn an employer's tools into company involvement."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from whatsapp_bot.tier1 import (
    find_tier1_description_match,
    find_tier1_match,
    find_tier1_text_match,
    load_tier1_companies,
)


@pytest.fixture
def companies():
    return load_tier1_companies(str(Path(__file__).parents[1] / "config/tier1_companies.txt"))


GALADRIM = {
    "id": 28388,
    "title": "Product & Software Engineer",
    "little_description": "Rejoins nos 90+ ingés et crée des produits web, mobile et IA sur mesure.",
    "big_description": (
        "Nous travaillons aujourd’hui notamment avec :\n"
        "* Outils d’IA : Claude Code, Codex, Cursor\n"
        "* Backend &amp; infra : Node.js (Adonis, Nest.js), PostgreSQL, AWS\n"
        "* Frontend : JavaScript / TypeScript, React, React Native"
    ),
}


@pytest.mark.parametrize("text", [
    "Cursor", "Cursor workshop", "Formation sur Cursor",
    "Nous utilisons Cursor au quotidien.",
    "Outils d'IA : Claude Code, Codex, Cursor",
    "Our tech stack: OpenAI, Microsoft, Oracle.",
    "Experience with OpenAI required.",
    "Introduction to OpenAI", "Un atelier sur Microsoft",
    "Look at Cursor", "Look at OpenAI",
    "Backend: NestJS, PostgreSQL. Deployed on Vercel.",
    "Databases: MongoDB, Snowflake, Oracle.",
    "We orchestrate the best technologies: Anthropic, OpenAI, Manus.",
    "Frontend mockups Figma. SQL Microsoft SQL Server.",
    "Skills: databases and Material UI, Ant Design, Bootstrap.",
    '<a href="https://cursor.com">our tools</a>',
    'https://anysphere.inc/careers',
])
def test_tools_topics_and_links_are_not_company_involvement(companies, text):
    assert find_tier1_match(text, companies) is None
    assert find_tier1_text_match(text, companies, description=True) is None
    assert find_tier1_description_match({"kind": "workshop", "description": text}, companies) is None


@pytest.mark.parametrize("text,expected", [
    ("Software Engineer at Anysphere", "Anysphere (Cursor)"),
    ("Anysphere recrute", "Anysphere (Cursor)"),
    ("Engineers from Cursor are presenting", "Anysphere (Cursor)"),
    ("Rencontre avec les ingénieurs de Cursor", "Anysphere (Cursor)"),
    ("Cursor is hiring", "Anysphere (Cursor)"),
    ("Un poste chez Cursor", "Anysphere (Cursor)"),
    ("Hackathon sponsored by Thales", "Thales"),
    ("A presentation by Goldman Sachs engineers", "Goldman Sachs"),
    ("Challenge organized by OpenAI", "OpenAI"),
])
def test_actual_company_and_organization_context_still_match(companies, text, expected):
    assert find_tier1_match(text, companies) == expected
    assert find_tier1_text_match(text, companies, description=True) == expected
    assert find_tier1_description_match({"kind": "extern", "description": text}, companies) == expected


def test_employer_context_must_belong_to_the_matched_name(companies):
    text = "Nous sommes ingénieurs chez Galadrim et utilisons Cursor."
    assert find_tier1_text_match(text, companies, description=True) is None


def test_real_company_can_match_after_an_unrelated_tool_mention(companies):
    text = "We use Cursor. This hackathon is sponsored by Thales."
    assert find_tier1_text_match(text, companies, description=True) == "Thales"


def test_html_entities_and_formatting_keep_genuine_context(companies):
    text = "Rencontrez les ing&eacute;nieurs de <strong>Cursor</strong>."
    assert find_tier1_text_match(text, companies, description=True) == "Anysphere (Cursor)"


async def test_false_tool_offer_posts_normally_without_private_tier1_alerts(companies, tmp_path):
    from whatsapp_bot.offers import SeenOffers, _poll_and_post_offers

    ft = AsyncMock()
    ft.get_offers.return_value = ([GALADRIM], 1)
    client = AsyncMock()
    users = MagicMock()
    seen = SeenOffers(str(tmp_path / "seen.json"))
    await _poll_and_post_offers(
        ft, client, seen=seen, instance="default", campus_id=1,
        group_jid="group@g.us", first_run=False,
        tier1_entries=companies, user_tokens=users,
    )
    client.send_text.assert_awaited_once()
    assert client.send_text.await_args.args[1] == "group@g.us"
    assert "Tier 1 company" not in client.send_text.await_args.args[2]
    users.items.assert_not_called()
    assert not seen.is_new(28388)


async def test_tool_event_does_not_auto_register_or_send_private_alerts(companies, tmp_path):
    from whatsapp_bot.scraper import SeenEvents, _poll_and_post

    event = {"id": 1, "name": "IA workshop", "kind": "workshop", "description": GALADRIM["big_description"]}
    ft = AsyncMock()
    ft.get_campus_events.return_value = ([event], 1)
    client = AsyncMock()
    oauth = AsyncMock()
    users = MagicMock()
    await _poll_and_post(
        ft, client, seen=SeenEvents(str(tmp_path / "events.json")),
        instance="default", campus_id=1, group_jid="group@g.us", first_run=False,
        tier1_entries=companies, oauth_manager=oauth, user_tokens=users,
    )
    oauth.register_for_event.assert_not_awaited()
    users.items.assert_not_called()
    client.send_text.assert_awaited_once()
    assert "Tier 1 company" not in client.send_text.await_args.args[2]


def test_partial_words_do_not_match(companies):
    assert find_tier1_match("Cursorial metadata", companies) is None
