import asyncio
import hmac
import html
import logging
import stat
from contextlib import asynccontextmanager, suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from whatsapp_bot.config import settings
from whatsapp_bot.evolution import EvolutionClient
from whatsapp_bot.handlers import handle_webhook
from whatsapp_bot.security import RateLimiter, redact_phone

from .employers import EmployerResolver

_LOG_DIR = Path("logs")
_LOG_DIR.mkdir(exist_ok=True, mode=0o700)

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Console handler (already set up by basicConfig)
logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
)

# File handler with rotation: 5 MB per file, keep 100 backups
_LOG_PATH = _LOG_DIR / "bot.log"
_file_handler = RotatingFileHandler(
    _LOG_PATH,
    maxBytes=5 * 1024 * 1024,
    backupCount=100,
    encoding="utf-8",
)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
logging.getLogger().addHandler(_file_handler)
# Restrict log file permissions — logs contain redacted PII and operational data
if _LOG_PATH.exists():
    _LOG_PATH.chmod(0o600)

logger = logging.getLogger(__name__)

evolution_client = EvolutionClient(
    base_url=settings.evolution_api_url,
    api_key=settings.evolution_api_key,
)

# Module-level refs for the /oauth/callback endpoint
_oauth_manager = None
_user_tokens = None
_ft_client = None

# Rate limiters (in-memory, reset on restart)
_webhook_limiter = RateLimiter(max_requests=120, window_seconds=60)
_oauth_limiter = RateLimiter(max_requests=10, window_seconds=60)


def _check_startup_security() -> None:
    """Log warnings for insecure configuration at startup."""
    # C3: Check .env file permissions
    env_path = Path(".env")
    if env_path.exists():
        mode = env_path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            logger.warning(
                "SECURITY: .env file is readable by group/others (mode %04o). "
                "Run: chmod 600 .env",
                stat.S_IMODE(mode),
            )

    # H1: Warn about 0.0.0.0 without webhook auth
    if settings.webhook_host == "0.0.0.0" and not settings.webhook_secret:  # noqa: S104 (security check, not a bind)
        logger.warning(
            "SECURITY: Webhook bound to 0.0.0.0 without authentication. "
            "Set BOT_WEBHOOK_SECRET to secure the webhook endpoint."
        )

    # C1: Confirm webhook auth status
    if settings.webhook_secret:
        logger.info("Webhook authentication enabled")
    else:
        logger.warning(
            "SECURITY: BOT_WEBHOOK_SECRET is not set — webhook endpoint is unauthenticated"
        )

    # H3: HTTPS redirect URI check
    if settings.ft_redirect_uri:
        parsed = urlparse(settings.ft_redirect_uri)
        if parsed.scheme == "http" and parsed.hostname not in (
            "localhost",
            "127.0.0.1",
            "host.docker.internal",
        ):
            logger.warning(
                "SECURITY: OAuth redirect URI uses HTTP on non-localhost host (%s). "
                "Use HTTPS in production to protect authorization codes.",
                parsed.hostname,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _oauth_manager, _user_tokens, _ft_client

    _check_startup_security()

    # --- Evolution API ---
    await evolution_client.start()
    logger.info("Evolution API client started: %s", settings.evolution_api_url)
    logger.info("Instance: %s", settings.instance_name)

    # --- 42 API ---
    ft_client = None
    scraper_task = None
    offers_scraper_task = None
    employer_resolver = None
    oauth_manager = None
    has_ft_credentials = settings.ft_client_id and settings.ft_client_secret

    if has_ft_credentials:
        from whatsapp_bot.ft_client import FtClient

        ft_client = FtClient(settings.ft_client_id, settings.ft_client_secret)
        await ft_client.start()
        _ft_client = ft_client
        logger.info("42 API client started")

        # Enable /events in handlers
        from whatsapp_bot.handlers import configure_ft

        if settings.gemini_api_key:
            employer_resolver = EmployerResolver(
                settings.gemini_api_key, model=settings.employer_model,
                cache_path=settings.employer_cache_path,
            )
        else:
            logger.warning("Employer extractor not configured: offers remain unverified, Tier 1 offer alerts disabled")
        configure_ft(ft_client, settings.ft_campus_id, employer_resolver=employer_resolver)

        # --- OAuth + Registration ---
        from whatsapp_bot.button_events import ButtonEventMap
        from whatsapp_bot.filters import load_scraper_filter
        from whatsapp_bot.handlers import configure_oauth
        from whatsapp_bot.oauth import OAuthManager
        from whatsapp_bot.user_tokens import UserTokenStore

        oauth_manager = OAuthManager(
            settings.ft_client_id,
            settings.ft_client_secret,
            settings.ft_redirect_uri,
        )
        await oauth_manager.start()

        user_tokens = UserTokenStore(
            settings.user_tokens_path,
            encryption_key=settings.token_encryption_key,
        )
        button_events = ButtonEventMap(settings.button_events_path)
        scraper_filter = load_scraper_filter()

        configure_oauth(
            oauth_manager,
            user_tokens,
            button_events,
            "config/filters.toml",
            settings.admin_whatsapp_number,
        )

        # Expose to /oauth/callback endpoint
        _oauth_manager = oauth_manager
        _user_tokens = user_tokens

        logger.info("OAuth manager started (redirect=%s)", settings.ft_redirect_uri)

        # --- Tier 1 companies (shared by events + offers scrapers) ---
        from whatsapp_bot.tier1 import load_tier1_companies

        tier1_entries = load_tier1_companies(settings.tier1_companies_path)

        # --- Events Scraper ---
        if settings.scraper_enabled:
            if not settings.whatsapp_group_jid:
                logger.warning(
                    "Scraper enabled but BOT_WHATSAPP_GROUP_JID is not set — scraper will NOT start"
                )
            else:
                from whatsapp_bot.scraper import run_scraper

                if scraper_filter is not None:
                    logger.info("Scraper filter: %s", scraper_filter)

                scraper_task = asyncio.create_task(
                    run_scraper(
                        ft_client,
                        evolution_client,
                        instance=settings.instance_name,
                        campus_id=settings.ft_campus_id,
                        group_jid=settings.whatsapp_group_jid,
                        interval_seconds=settings.scraper_interval_seconds,
                        seen_events_path=settings.seen_events_path,
                        event_filter=scraper_filter,
                        button_event_map=button_events,
                        filters_path="config/filters.toml",
                        tier1_entries=tier1_entries or None,
                        oauth_manager=oauth_manager,
                        user_tokens=user_tokens,
                        gemini_api_key=settings.gemini_api_key,
                    )
                )
                logger.info("Events scraper task started")
        else:
            logger.info("Events scraper disabled (BOT_SCRAPER_ENABLED=false)")

        # --- Offers Scraper ---
        if settings.offers_scraper_enabled:
            if not settings.whatsapp_group_jid:
                logger.warning(
                    "Offers scraper enabled but BOT_WHATSAPP_GROUP_JID is not set — offers scraper will NOT start"
                )
            else:
                from whatsapp_bot.filters import load_offers_scraper_filter
                from whatsapp_bot.offers import run_offers_scraper

                offers_filter = load_offers_scraper_filter()
                if offers_filter is not None:
                    logger.info("Offers scraper filter: %s", offers_filter)

                offers_scraper_task = asyncio.create_task(
                    run_offers_scraper(
                        ft_client,
                        evolution_client,
                        instance=settings.instance_name,
                        campus_id=settings.ft_campus_id,
                        group_jid=settings.whatsapp_group_jid,
                        interval_seconds=settings.offers_scraper_interval_seconds,
                        seen_offers_path=settings.seen_offers_path,
                        employer_resolver=employer_resolver,
                        filters_path="config/filters.toml",
                        offer_filter=offers_filter,
                        tier1_entries=tier1_entries or None,
                        user_tokens=user_tokens,
                    )
                )
                logger.info("Offers scraper task started")
        else:
            logger.info("Offers scraper disabled (BOT_OFFERS_SCRAPER_ENABLED=false)")
    else:
        logger.info("42 API not configured (no credentials), /events and scraper disabled")

    yield

    # --- Shutdown ---
    _oauth_manager = None
    _user_tokens = None

    if scraper_task is not None:
        scraper_task.cancel()
        with suppress(asyncio.CancelledError):
            await scraper_task
        logger.info("Events scraper task cancelled")

    if offers_scraper_task is not None:
        offers_scraper_task.cancel()
        with suppress(asyncio.CancelledError):
            await offers_scraper_task
        logger.info("Offers scraper task cancelled")

    if oauth_manager is not None:
        await oauth_manager.close()
        logger.info("OAuth manager closed")

    if employer_resolver is not None:
        await employer_resolver.close()

    if ft_client is not None:
        await ft_client.close()
        logger.info("42 API client closed")

    await evolution_client.close()
    logger.info("Evolution API client closed")


app = FastAPI(title="WhatsApp Bot", lifespan=lifespan)


_MAX_WEBHOOK_BODY = 1_048_576  # 1 MB — Evolution API payloads are typically <50 KB


@app.post("/webhook", response_model=None)
async def webhook(request: Request):
    """Receive webhook events from Evolution API."""
    # Request body size check — prevent memory exhaustion from oversized payloads
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_body_size = int(content_length)
        except ValueError:
            return JSONResponse({"error": "invalid content length"}, status_code=400)
        if declared_body_size > _MAX_WEBHOOK_BODY:
            return JSONResponse({"error": "payload too large"}, status_code=413)

    # C1: Authenticate webhook requests via shared secret
    if settings.webhook_secret:
        token = request.query_params.get("token", "")
        if not hmac.compare_digest(token, settings.webhook_secret):
            # M1: Rate-limit unauthenticated requests by IP to slow brute-force
            client_ip = request.client.host if request.client else "unknown"
            if not _webhook_limiter.check(client_ip):
                return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
            logger.warning(
                "Webhook request with invalid/missing token from %s",
                request.client.host if request.client else "unknown",
            )
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # Authenticated requests skip IP rate limiting — all Evolution API
        # webhooks share a single source IP (Docker gateway), so IP-based
        # limiting would block ALL users when one spams.
    else:
        # No secret configured — fall back to IP-based rate limiting
        client_ip = request.client.host if request.client else "unknown"
        if not _webhook_limiter.check(client_ip):
            logger.warning("Webhook rate limit exceeded for %s", client_ip)
            return JSONResponse({"error": "rate limit exceeded"}, status_code=429)

    body: dict[str, Any] = await request.json()

    event: str = body.get("event", "")
    data: dict = body.get("data", {})
    instance_name: str = body.get("instance", "")

    # M2: Validate instance name matches configuration
    if instance_name != settings.instance_name:
        logger.debug("Ignoring webhook from unknown instance: %s", instance_name)
        return {"status": "ignored"}

    logger.info("Webhook event: %s from instance: %s", event, instance_name)

    await handle_webhook(
        event,
        data,
        client=evolution_client,
        instance=settings.instance_name,
    )

    return {"status": "ok"}


async def _friendly_registration_error(event_id: int) -> str:
    """Return a human-readable error message for a failed registration."""
    from whatsapp_bot.ft_client import FtAPIError

    if _ft_client is not None:
        try:
            event = await _ft_client.get_event(event_id)
            max_people = event.get("max_people")
            nbr_subscribers = event.get("nbr_subscribers", 0)
            name = event.get("name", "This event")
            if max_people and nbr_subscribers >= max_people:
                return (
                    f"⚠️ {name} is full ({nbr_subscribers}/{max_people}). "
                    f"No waitlist is available for this event."
                )
        except FtAPIError:
            pass

    return "❌ Registration failed. Please try again from WhatsApp."


@app.get("/oauth/callback")
async def oauth_callback(request: Request, code: str, state: str) -> HTMLResponse:
    """Handle the OAuth redirect from 42 Intra."""
    # M1: Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    if not _oauth_limiter.check(client_ip):
        return HTMLResponse(
            "<h1>Too many requests</h1><p>Please wait a moment and try again.</p>",
            status_code=429,
        )

    if _oauth_manager is None or _user_tokens is None:
        return HTMLResponse(
            "<h1>OAuth not configured</h1><p>The bot is not set up for 42 authentication.</p>",
            status_code=500,
        )

    from whatsapp_bot.ft_client import FtAPIError
    from whatsapp_bot.user_tokens import UserToken

    # 1. Validate state
    pending = _oauth_manager.pop_pending(state)
    if pending is None:
        return HTMLResponse(
            "<h1>Invalid or expired link</h1>"
            "<p>This authorization link is no longer valid. "
            "Please tap the button again in WhatsApp to get a new link.</p>",
            status_code=400,
        )

    # 2. Exchange code for token
    try:
        token_data = await _oauth_manager.exchange_code(code)
    except FtAPIError:
        logger.exception(
            "OAuth code exchange failed for %s", redact_phone(pending.whatsapp_number)
        )
        # H2: Don't expose raw API error details to users
        return HTMLResponse(
            "<h1>Authorization failed</h1>"
            "<p>Could not complete authorization. Please try again from WhatsApp.</p>",
            status_code=400,
        )

    # 3. Get user_id via /v2/me
    access_token = token_data["access_token"]
    try:
        user_id = await _oauth_manager.get_user_id(access_token)
    except FtAPIError:
        logger.exception(
            "Failed to get 42 user_id for %s", redact_phone(pending.whatsapp_number)
        )
        # H2: Don't expose raw API error details to users
        return HTMLResponse(
            "<h1>Failed to identify your 42 account</h1>"
            "<p>Could not retrieve your 42 profile. Please try again.</p>",
            status_code=400,
        )

    # 4. Store token
    token = UserToken(
        access_token=access_token,
        refresh_token=token_data.get("refresh_token"),
        expires_at=_oauth_manager.token_expires_at(
            token_data.get("expires_in", 7200)
        ),
        user_id=user_id,
    )
    _user_tokens.set(pending.whatsapp_number, token)

    # 5. Auto-register for the pending event
    reg_ok = False
    registration_msg = ""
    try:
        already = await _oauth_manager.is_registered(
            access_token, pending.event_id, user_id
        )
        if already:
            reg_ok = True
            registration_msg = "You were already registered for this event."
        else:
            await _oauth_manager.register_for_event(
                access_token, pending.event_id, user_id
            )
            reg_ok = True
            registration_msg = "You've been registered for this event on 42 Intra!"
    except FtAPIError as exc:
        logger.warning(
            "Auto-registration failed for %s (event %d): %s",
            redact_phone(pending.whatsapp_number), pending.event_id, exc.detail[:200],
        )
        registration_msg = await _friendly_registration_error(pending.event_id)

    # 6. DM confirmation to user via WhatsApp
    if reg_ok:
        wa_msg = f"✅ 42 account linked! {registration_msg}"
    else:
        wa_msg = f"42 account linked!\n{registration_msg}"
    try:
        await evolution_client.send_text(
            settings.instance_name,
            pending.whatsapp_number,
            wa_msg,
        )
    except Exception:
        logger.exception(
            "Failed to send WhatsApp confirmation to %s",
            redact_phone(pending.whatsapp_number),
        )

    # 7. Return HTML success page
    icon = "✅" if reg_ok else "⚠️"
    return HTMLResponse(
        "<html><body style='font-family: sans-serif; text-align: center; padding: 50px;'>"
        "<h1>Account linked!</h1>"
        f"<p>{icon} {html.escape(registration_msg)}</p>"
        "<p>You can close this tab and return to WhatsApp.</p>"
        "</body></html>"
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}


def run() -> None:
    uvicorn.run(
        "whatsapp_bot.main:app",
        host=settings.webhook_host,
        port=settings.webhook_port,
        log_level="warning",
    )
