import json
import logging
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

from whatsapp_bot.ft_client import API_BASE, TOKEN_URL, FtAPIError
from whatsapp_bot.security import redact_phone

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://api.intra.42.fr/oauth/authorize"


_PENDING_TTL = 3600  # 1 hour — pending flows older than this are purged


_PENDING_PATH_DEFAULT = "data/pending_oauth.json"


@dataclass
class PendingOAuth:
    """Record of a pending OAuth flow, persisted across restarts."""

    whatsapp_number: str
    event_id: int
    created_at: float = 0.0  # time.time() when the flow was created


class OAuthManager:
    """Manages per-user OAuth authorization_code flows against the 42 API."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        pending_path: str = _PENDING_PATH_DEFAULT,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("OAuth client_id and client_secret are required")
        if not redirect_uri:
            raise ValueError("OAuth redirect_uri is required")
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._client: httpx.AsyncClient | None = None
        self._pending: dict[str, PendingOAuth] = {}
        self._pending_path = Path(pending_path)
        self._load_pending()

    def _load_pending(self) -> None:
        """Load pending OAuth flows from disk and purge expired ones."""
        if not self._pending_path.exists():
            return
        text = self._pending_path.read_text().strip()
        if not text:
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Corrupted pending OAuth file %s, starting fresh", self._pending_path)
            return
        now = time.time()
        for state, entry in data.items():
            created_at = entry.get("created_at", 0.0)
            if now - created_at > _PENDING_TTL:
                continue  # expired, skip
            self._pending[state] = PendingOAuth(
                whatsapp_number=entry["whatsapp_number"],
                event_id=entry["event_id"],
                created_at=created_at,
            )
        if self._pending:
            logger.info("Loaded %d pending OAuth flows from %s", len(self._pending), self._pending_path)

    def _save_pending(self) -> None:
        """Persist pending OAuth flows to disk (atomic write)."""
        self._pending_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            state: {
                "whatsapp_number": p.whatsapp_number,
                "event_id": p.event_id,
                "created_at": p.created_at,
            }
            for state, p in self._pending.items()
        }
        tmp = self._pending_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        # M4: Restrict permissions — pending flows contain phone numbers
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        os.replace(tmp, self._pending_path)

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=30.0,
        )
        logger.info("OAuthManager started")

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._pending.clear()
        self._save_pending()
        logger.info("OAuthManager closed")

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("OAuthManager not started. Call start() first.")
        return self._client

    def _purge_expired_pending(self) -> None:
        """Remove pending OAuth flows older than TTL."""
        now = time.time()
        expired = [
            s for s, p in self._pending.items()
            if now - p.created_at > _PENDING_TTL
        ]
        for s in expired:
            del self._pending[s]
        if expired:
            logger.debug("Purged %d expired pending OAuth flows", len(expired))

    def create_auth_url(self, whatsapp_number: str, event_id: int) -> str:
        """Generate an OAuth authorization URL and store the pending state."""
        self._purge_expired_pending()
        state = secrets.token_urlsafe(32)
        self._pending[state] = PendingOAuth(
            whatsapp_number=whatsapp_number,
            event_id=event_id,
            created_at=time.time(),
        )
        self._save_pending()
        params = urlencode({
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": "public profile",
            "state": state,
        })
        url = f"{AUTHORIZE_URL}?{params}"
        logger.info(
            "Created auth URL for %s (event=%d)",
            redact_phone(whatsapp_number), event_id,
        )
        return url

    def pop_pending(self, state: str) -> PendingOAuth | None:
        """Retrieve and remove a pending OAuth flow by state token."""
        pending = self._pending.pop(state, None)
        if pending is not None:
            self._save_pending()
        return pending

    async def exchange_code(self, code: str) -> dict:
        """Exchange an authorization code for tokens.

        Returns the raw token response dict with at least
        ``access_token``, ``expires_in``, and optionally ``refresh_token``.
        """
        response = await self.client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": self._redirect_uri,
            },
        )
        if not response.is_success:
            raise FtAPIError(
                response.status_code,
                f"Code exchange failed: {response.text[:300]}",
            )
        return response.json()

    async def refresh_token(self, refresh_token_value: str) -> dict:
        """Use a refresh token to obtain new tokens.

        Returns the raw token response dict.
        """
        response = await self.client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token_value,
            },
        )
        if not response.is_success:
            raise FtAPIError(
                response.status_code,
                f"Token refresh failed: {response.text[:300]}",
            )
        return response.json()

    async def get_user_id(self, access_token: str) -> int:
        """Fetch the authenticated user's 42 ID via GET /v2/me."""
        response = await self.client.get(
            "/v2/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if not response.is_success:
            raise FtAPIError(
                response.status_code,
                f"GET /v2/me failed: {response.text[:300]}",
            )
        data = response.json()
        user_id = data.get("id")
        if user_id is None:
            raise FtAPIError(0, "GET /v2/me response missing 'id' field")
        return int(user_id)

    async def register_for_event(
        self, access_token: str, event_id: int, user_id: int
    ) -> dict:
        """Register the user for an event via POST /v2/events_users."""
        response = await self.client.post(
            "/v2/events_users",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "events_user": {
                    "event_id": event_id,
                    "user_id": user_id,
                },
            },
        )
        if not response.is_success:
            raise FtAPIError(
                response.status_code,
                f"Event registration failed: {response.text[:300]}",
            )
        return response.json()

    async def is_registered(
        self, access_token: str, event_id: int, user_id: int
    ) -> bool:
        """Check if the user is already registered for an event."""
        response = await self.client.get(
            "/v2/events_users",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "filter[event_id]": str(event_id),
                "filter[user_id]": str(user_id),
            },
        )
        if not response.is_success:
            raise FtAPIError(
                response.status_code,
                f"Registration check failed: {response.text[:300]}",
            )
        results = response.json()
        return len(results) > 0

    async def unregister_from_event(
        self, access_token: str, event_id: int, user_id: int
    ) -> bool:
        """Delete this user's registration for an event when it exists."""
        headers = {"Authorization": f"Bearer {access_token}"}
        response = await self.client.get(
            "/v2/events_users",
            headers=headers,
            params={
                "filter[event_id]": str(event_id),
                "filter[user_id]": str(user_id),
            },
        )
        if not response.is_success:
            raise FtAPIError(response.status_code, response.text[:300])
        registrations = response.json()
        if not registrations:
            return False
        registration_id = registrations[0].get("id")
        if registration_id is None:
            raise FtAPIError(0, "Event registration response missing id")
        response = await self.client.delete(
            f"/v2/events_users/{registration_id}", headers=headers,
        )
        if not response.is_success:
            raise FtAPIError(response.status_code, response.text[:300])
        return True

    @staticmethod
    def token_expires_at(expires_in: int) -> float:
        """Convert an ``expires_in`` seconds value to a wall-clock timestamp."""
        return time.time() + expires_in
