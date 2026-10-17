"""Regression for credentials leaking through HTTP request URLs."""
import logging

import httpx

from whatsapp_bot import gemini


async def test_api_key_is_header_not_logged_url(monkeypatch, caplog):
    real_client = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["x-goog-api-key"] == "private-test-credential"
        assert "key" not in request.url.params
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "A" * 90}]}}],
        })

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(gemini.httpx, "AsyncClient", client_factory)
    with caplog.at_level(logging.INFO, logger="httpx"):
        result = await gemini.summarize_description(
            "private-test-credential", "An event description", 100,
        )
    assert result == "A" * 90
    assert len(requests) == 1
    assert "private-test-credential" not in caplog.text
