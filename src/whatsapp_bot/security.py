"""Security utilities: rate limiting, PII redaction."""

from __future__ import annotations

import time


class RateLimiter:
    """Simple in-memory sliding-window rate limiter.

    Tracks request timestamps per key (e.g. IP address) and rejects
    requests that exceed *max_requests* within *window_seconds*.

    Not shared across processes — suitable for a single-worker bot.
    """

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._requests: dict[str, list[float]] = {}

    def check(self, key: str) -> bool:
        """Return ``True`` if the request is allowed, ``False`` if rate-limited."""
        now = time.monotonic()
        cutoff = now - self._window

        timestamps = self._requests.get(key, [])
        # Prune old entries
        timestamps = [t for t in timestamps if t > cutoff]

        if len(timestamps) >= self._max:
            self._requests[key] = timestamps
            return False

        timestamps.append(now)
        self._requests[key] = timestamps

        # Periodic cleanup: remove keys with no recent activity
        if len(self._requests) > 1000:
            self._requests = {
                k: [t for t in v if t > cutoff]
                for k, v in self._requests.items()
                if any(t > cutoff for t in v)
            }

        return True


def redact_phone(phone: str) -> str:
    """Redact a phone number for logging: ``33633493864`` -> ``336***3864``.

    Keeps the first 3 and last 4 digits visible.  If the input is too
    short to redact meaningfully, returns ``***``.
    """
    if not phone or len(phone) < 7:
        return "***"
    return phone[:3] + "***" + phone[-4:]
