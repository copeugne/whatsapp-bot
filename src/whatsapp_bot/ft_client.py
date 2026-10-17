import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.intra.42.fr/oauth/token"
API_BASE = "https://api.intra.42.fr"

_MAX_RETRIES = 10
_INITIAL_RETRY_DELAY_SECONDS = 0.5
_RETRYABLE_STATUSES = frozenset({429, 502, 503})


class FtAPIError(Exception):
    """Raised when a 42 API request fails."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"42 API error {status_code}: {detail}")


class FtClient:
    """Async HTTP client for 42 Intra API (OAuth2 client credentials)."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        if not client_id or not client_secret:
            raise ValueError("42 API client_id and client_secret are required")
        self._client_id = client_id
        self._client_secret = client_secret
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=30.0,
        )
        # Eagerly fetch token so bad credentials fail fast
        await self._ensure_token()
        logger.info("42 API client started, token acquired")

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._token = None
        self._token_expires_at = 0.0

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("FtClient not started. Call start() first.")
        return self._client

    async def _ensure_token(self) -> None:
        """Fetch or refresh the OAuth2 token if expired or about to expire.

        Uses ``time.monotonic()`` (not ``time.time()``) because this token
        is never persisted — monotonic is immune to wall-clock adjustments
        (NTP, DST, manual changes).  Contrast with :class:`UserToken` which
        uses ``time.time()`` because it IS persisted to disk and must
        survive process restarts.
        """
        # Refresh 30 seconds before actual expiry
        if self._token and time.monotonic() < (self._token_expires_at - 30):
            return

        response = await self.client.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        if not response.is_success:
            raise FtAPIError(
                response.status_code,
                f"Token request failed: {response.text[:300]}",
            )

        body = response.json()
        self._token = body["access_token"]
        expires_in = body.get("expires_in", 7200)
        self._token_expires_at = time.monotonic() + expires_in
        logger.info("42 API token acquired, expires in %ds", expires_in)

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated API request with retry on transient errors.

        Returns raw Response so callers can access headers (e.g. X-Total).
        Retries on network errors and HTTP 429/502/503 with exponential backoff.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            await self._ensure_token()
            try:
                response = await self.client.request(
                    method,
                    path,
                    headers={"Authorization": f"Bearer {self._token}"},
                    **kwargs,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    wait = _INITIAL_RETRY_DELAY_SECONDS * (2 ** attempt)
                    logger.warning(
                        "42 API %s %s network error: %s, retry %d/%d in %.1fs",
                        method, path, exc, attempt + 1, _MAX_RETRIES, wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise FtAPIError(
                    0, f"Network error after {_MAX_RETRIES} retries: {exc}"
                ) from exc

            if response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                wait = _INITIAL_RETRY_DELAY_SECONDS * (2 ** attempt)
                logger.warning(
                    "42 API %s %s returned %d, retry %d/%d in %.1fs",
                    method, path, response.status_code,
                    attempt + 1, _MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                continue

            if not response.is_success:
                raise FtAPIError(response.status_code, response.text[:500])
            return response

        # Should never reach here, but satisfy type checker
        raise FtAPIError(0, "Retry loop exhausted")  # pragma: no cover

    async def get_event(self, event_id: int) -> dict:
        """Fetch a single event by ID."""
        response = await self._request("GET", f"/v2/events/{event_id}")
        return response.json()

    async def get_offer(self, offer_id: int) -> dict:
        """Fetch a single offer by ID."""
        response = await self._request("GET", f"/v2/offers/{offer_id}")
        return response.json()

    async def get_offers(
        self,
        *,
        page_size: int = 100,
        page_number: int = 1,
        extra_params: dict[str, str] | None = None,
    ) -> tuple[list[dict], int]:
        """Fetch offers from the 42 API.

        Returns (offers_list, total_count).
        *extra_params* are merged into the query string
        (e.g. ``filter[campus_id]``, ``filter[valid]``).
        """
        params: dict[str, str | int] = {
            "page[size]": page_size,
            "page[number]": page_number,
        }
        if extra_params:
            params.update(extra_params)
        response = await self._request("GET", "/v2/offers", params=params)
        total = int(response.headers.get("X-Total", "0"))
        offers: list[dict] = response.json()
        return offers, total

    async def get_campus_events(
        self,
        campus_id: int,
        *,
        page_size: int = 100,
        page_number: int = 1,
        extra_params: dict[str, str] | None = None,
    ) -> tuple[list[dict], int]:
        """Fetch future events for a campus.

        Returns (events_list, total_count).
        *extra_params* are merged into the query string (e.g. ``filter[kind]``).
        """
        params: dict[str, str | int] = {
            "filter[future]": "true",
            "sort": "begin_at",
            "page[size]": page_size,
            "page[number]": page_number,
        }
        if extra_params:
            params.update(extra_params)
        response = await self._request(
            "GET",
            f"/v2/campus/{campus_id}/events",
            params=params,
        )
        total = int(response.headers.get("X-Total", "0"))
        events: list[dict] = response.json()
        return events, total
