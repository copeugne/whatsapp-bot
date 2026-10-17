import re
from functools import lru_cache
from typing import Any, cast

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "BOT_", "env_file": ".env", "extra": "ignore"}

    @field_validator("instance_name")
    @classmethod
    def _validate_instance_name(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_-]+$", v):
            raise ValueError(
                f"instance_name must be alphanumeric/dash/underscore, got {v!r}"
            )
        return v

    evolution_api_url: str
    evolution_api_key: str
    webhook_host: str = "0.0.0.0"  # noqa: S104 - intentional authenticated container listener
    webhook_port: int = 8000
    instance_name: str = "default"

    # 42 API
    ft_client_id: str = ""
    ft_client_secret: str = ""
    ft_campus_id: int = 1

    # OAuth (user-facing authorization_code flow)
    ft_redirect_uri: str = "http://localhost:8000/oauth/callback"
    user_tokens_path: str = "data/user_tokens.json"
    button_events_path: str = "data/button_events.json"
    admin_whatsapp_number: str = ""

    # Scraper
    scraper_enabled: bool = False
    scraper_interval_seconds: int = 300
    whatsapp_group_jid: str = ""
    seen_events_path: str = "data/seen_events.json"

    # Offers scraper
    offers_scraper_enabled: bool = False
    offers_scraper_interval_seconds: int = 600
    seen_offers_path: str = "data/seen_offers.json"

    # Tier 1 auto-registration
    tier1_companies_path: str = "config/tier1_companies.txt"

    # Gemini API (optional — event description summarization)
    gemini_api_key: str = ""
    employer_model: str = "gemini-3.5-flash-lite"
    employer_cache_path: str = "data/employers.json"

    # Security
    webhook_secret: str = ""  # shared secret for webhook authentication
    token_encryption_key: str = ""  # Fernet key for encrypting user tokens at rest


@lru_cache
def get_settings() -> Settings:
    """Create the Settings singleton lazily on first access.

    This defers env-var / ``.env`` reading until the first call rather
    than at import time, which makes testing easier (no need to set env
    vars before importing any ``whatsapp_bot`` module).
    """
    return Settings()  # pyright: ignore[reportCallIssue] - required values come from env


class _SettingsProxy:
    """Proxy that forwards attribute access to the lazy singleton.

    Allows ``from whatsapp_bot.config import settings`` to keep working
    everywhere while deferring ``Settings()`` construction until first use.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(get_settings(), name)

    def __repr__(self) -> str:
        return repr(get_settings())


settings: Settings = cast(Settings, _SettingsProxy())
