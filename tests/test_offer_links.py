"""Student offer routes verified against a user-provided working Intra URL."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from whatsapp_bot.offers import _notify_tier1_offer, format_offer, offer_url

OFFER = {
    "id": 28517,
    "title": "Scrum Master Junior – Environnement SAFe (H/F)",
    "slug": "deliberately-not-used",
}
URL = "https://companies.intra.42.fr/en/offers/28517"


def test_confirmed_offer_uses_numeric_student_route():
    assert offer_url(OFFER) == URL
    rendered = format_offer(OFFER)
    assert URL in rendered
    assert "meta.intra" not in rendered
    assert OFFER["slug"] not in rendered


def test_missing_id_does_not_invent_slug_route():
    assert offer_url({"slug": "no-id"}) == ""
    assert "https://" not in format_offer({"slug": "no-id"})


@pytest.mark.parametrize("invalid_id", [True, -1, 0, "28517", "../../bad"])
def test_invalid_offer_id_fails_explicitly(invalid_id):
    with pytest.raises(ValueError, match="positive integer"):
        offer_url({"id": invalid_id})


async def test_private_tier1_notification_uses_same_student_url():
    users = MagicMock()
    users.items.return_value = [("12345", object())]
    client = AsyncMock()
    assert await _notify_tier1_offer(users, client, "default", OFFER, "Acme") == 1
    message = client.send_text.await_args.args[2]
    assert URL in message
    assert "meta.intra" not in message
    assert OFFER["slug"] not in message
