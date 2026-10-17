import logging
from typing import TYPE_CHECKING

from whatsapp_bot.security import RateLimiter, redact_phone

if TYPE_CHECKING:
    from whatsapp_bot.button_events import ButtonEventMap
    from whatsapp_bot.employers import EmployerResolver
    from whatsapp_bot.evolution import EvolutionClient
    from whatsapp_bot.ft_client import FtAPIError, FtClient
    from whatsapp_bot.oauth import OAuthManager
    from whatsapp_bot.user_tokens import UserTokenStore

logger = logging.getLogger(__name__)

_JOIN_WORDS: frozenset[str] = frozenset({
    "+", "+1", "in", "join", "yes", "ok", "register", "/register",
})


def _is_join_trigger(text: str) -> bool:
    """Return True if *text* looks like a registration intent."""
    lower = text.lower()
    if lower in _JOIN_WORDS:
        return True
    # Single emoji — no ASCII letters/digits, at most a few characters
    return bool(len(text) <= 10 and text and not any(c.isascii() and c.isalnum() for c in text))

# Module-level state injected by main.py via configure_ft() / configure_oauth()
_ft_client: "FtClient | None" = None
_ft_campus_id: int = 1
_employer_resolver: "EmployerResolver | None" = None

_oauth_manager: "OAuthManager | None" = None
_user_tokens: "UserTokenStore | None" = None
_button_events: "ButtonEventMap | None" = None
_filters_path = "config/filters.toml"
_admin_whatsapp_number = ""

# Per-sender rate limiters to prevent abuse from individual WhatsApp users
_events_cmd_limiter = RateLimiter(max_requests=5, window_seconds=60)
_offers_cmd_limiter = RateLimiter(max_requests=5, window_seconds=60)
_registration_limiter = RateLimiter(max_requests=10, window_seconds=60)


def configure_ft(ft_client: "FtClient", campus_id: int, *, employer_resolver: "EmployerResolver | None" = None) -> None:
    """Inject the 42 API client so handlers can serve /events."""
    global _ft_client, _ft_campus_id, _employer_resolver
    _ft_client = ft_client
    _ft_campus_id = campus_id
    _employer_resolver = employer_resolver
    logger.info("Handlers configured with 42 API client (campus %d)", campus_id)


def configure_oauth(
    oauth_manager: "OAuthManager",
    user_tokens: "UserTokenStore",
    button_events: "ButtonEventMap",
    filters_path: str,
    admin_whatsapp_number: str,
) -> None:
    """Inject OAuth components so handlers can process event registration."""
    global _oauth_manager, _user_tokens, _button_events, _filters_path, _admin_whatsapp_number
    _oauth_manager = oauth_manager
    _user_tokens = user_tokens
    _button_events = button_events
    _filters_path = filters_path
    _admin_whatsapp_number = admin_whatsapp_number
    logger.info("Handlers configured with OAuth manager")


def _sender_number(key: dict, remote_jid: str) -> str:
    sender_jid = (
        key.get("participantAlt") or key.get("participant", "")
        if remote_jid.endswith("@g.us")
        else remote_jid
    )
    return sender_jid.split("@")[0]


def _is_filter_admin(sender_number: str) -> bool:
    return bool(_admin_whatsapp_number) and sender_number == _admin_whatsapp_number


def _is_waitlist_registration(event: dict | None) -> bool:
    if event is None or not event.get("waitlist"):
        return False
    maximum = event.get("max_people")
    return maximum is not None and event.get("nbr_subscribers", 0) >= maximum


async def handle_webhook(
    event: str, data: dict, *, client: "EvolutionClient", instance: str
) -> None:
    """Dispatch a webhook event to the appropriate handler."""
    handler = _EVENT_HANDLERS.get(event)
    if handler is None:
        logger.debug("Unhandled event: %s", event)
        return
    await handler(data, client=client, instance=instance)


def _extract_stanza_id(data: dict) -> str | None:
    """Extract the quoted message's ID from the webhook data.

    Evolution API places ``contextInfo`` at the top level of ``data``,
    as a sibling of ``key`` and ``message``.
    """
    return data.get("contextInfo", {}).get("stanzaId")


async def handle_messages_upsert(
    data: dict, *, client: "EvolutionClient", instance: str
) -> None:
    """Process incoming messages."""
    key = data.get("key", {})
    from_me = key.get("fromMe", False)
    if from_me:
        return

    remote_jid = key.get("remoteJid", "")
    if not remote_jid:
        logger.warning("Message missing remoteJid in webhook data")
        return

    if remote_jid == "status@broadcast":
        return

    message = data.get("message", {})

    # --- Poll vote detection ---
    poll_update = message.get("pollUpdateMessage")
    if poll_update and _button_events is not None and _oauth_manager is not None:
        poll_key = poll_update.get("pollCreationMessageKey", {})
        poll_msg_id = poll_key.get("id")

        if poll_msg_id:
            event_id = _button_events.get_event_id(poll_msg_id)
            if event_id is not None:
                selected = poll_update.get("vote", {}).get("selectedOptions", [])
                sender_number = _sender_number(key, remote_jid)
                if "Register" in selected:

                    if not sender_number:
                        logger.warning("Poll vote with no sender JID")
                    elif not _registration_limiter.check(sender_number):
                        logger.warning(
                            "Registration rate limit hit for %s",
                            redact_phone(sender_number),
                        )
                    else:
                        logger.info(
                            "Poll registration vote from %s for event %d",
                            redact_phone(sender_number), event_id,
                        )
                        try:
                            await _handle_registration(client, instance, sender_number, event_id)
                        except Exception:
                            logger.exception(
                                "Unhandled error in poll registration for %s (event %d)",
                                redact_phone(sender_number), event_id,
                            )
                elif "Unregister" in selected:
                    if not sender_number:
                        logger.warning("Poll unregistration with no sender JID")
                    else:
                        try:
                            await _handle_unregistration(
                                client, instance, sender_number, event_id,
                            )
                        except Exception:
                            logger.exception(
                                "Unhandled error unregistering %s from event %d",
                                redact_phone(sender_number), event_id,
                            )
                            await client.send_text(
                                instance, sender_number,
                                "Could not cancel your registration. Please try again later.",
                            )
                else:
                    logger.debug("Poll vote was not 'Register': %s", selected)
        return  # Poll updates have no text to process further

    text = (
        message.get("conversation")
        or message.get("extendedTextMessage", {}).get("text")
    )

    if not text:
        logger.info("Non-text message from %s, skipping", remote_jid)
        return

    logger.info("Message from %s", redact_phone(remote_jid.split("@")[0]))

    # Use full JID for groups, strip suffix for individual chats
    if remote_jid.endswith("@g.us"):
        number = remote_jid
    else:
        number = remote_jid.split("@")[0]

    # --- Registration trigger (reply to an event message) ---
    stripped = text.strip()
    command, _, argument = stripped.partition(" ")
    filter_aliases: dict[str, str] = {
        "/f": "/filter",
        "/uf": "/unfilter",
        "/fs": "/filters",
        "/fc": "/filtercompany",
        "/ufc": "/unfiltercompany",
        "/fcs": "/filterscompany",
    }
    normalized_command = str(command).lower()
    command = filter_aliases.get(normalized_command, normalized_command)
    if command.lower() in {"/filtercompany", "/unfiltercompany", "/filterscompany"}:
        sender_number = _sender_number(key, remote_jid)
        if not _is_filter_admin(sender_number):
            logger.warning("Unauthorized company filter command from %s", redact_phone(sender_number))
            return
        from whatsapp_bot.filters import (
            add_company_exclusion,
            list_company_exclusions,
            remove_company_exclusion,
        )
        term = argument.strip().strip('"').strip()
        if command.lower() == "/filterscompany":
            listing = "\n".join(f"• {item}" for item in list_company_exclusions(_filters_path))
            reply = listing or "No company filters."
        elif command.lower() == "/filtercompany":
            try:
                added = add_company_exclusion(term, _filters_path)
            except ValueError as exc:
                reply = str(exc)
            else:
                reply = f"Company filter added: {term}" if added else f"Filter already exists: {term}"
        elif not term or len(term) > 80:
            reply = "Use a company name up to 80 characters."
        else:
            removed = remove_company_exclusion(term, _filters_path)
            reply = f"Company filter removed: {term}" if removed else f"No matching filter: {term}"
        await client.send_text(instance, sender_number, reply)
        return

    if command.lower() in {"/filter", "/unfilter", "/filters"}:
        sender_number = _sender_number(key, remote_jid)
        if not _is_filter_admin(sender_number):
            logger.warning("Unauthorized filter command from %s", redact_phone(sender_number))
            return
        from whatsapp_bot.filters import (
            add_event_exclusion,
            list_event_exclusions,
            remove_event_exclusion,
        )

        term = argument.strip().strip('"').strip()
        if command.lower() == "/filters":
            listing = "\n".join(f"• {item}" for item in list_event_exclusions(_filters_path))
            await client.send_text(instance, sender_number, listing or "No event filters.")
        elif not term or len(term) > 80:
            await client.send_text(instance, sender_number, "Use a title keyword up to 80 characters.")
        elif command.lower() == "/filter":
            added = add_event_exclusion(term, _filters_path)
            reply = f"Event title filter added: {term}" if added else f"Filter already exists: {term}"
            await client.send_text(instance, sender_number, reply)
        else:
            removed = remove_event_exclusion(term, _filters_path)
            reply = f"Event title filter removed: {term}" if removed else f"No matching filter: {term}"
            await client.send_text(instance, sender_number, reply)
        return

    if _is_join_trigger(stripped) and _oauth_manager is not None:
        stanza_id = _extract_stanza_id(data)
        if stanza_id and _button_events is not None:
            event_id = _button_events.get_event_id(stanza_id)
            if event_id is not None:
                # In groups, use participantAlt (phone@s.whatsapp.net) over participant (@lid)
                if remote_jid.endswith("@g.us"):
                    sender_jid = key.get("participantAlt") or key.get("participant", "")
                else:
                    sender_jid = remote_jid
                sender_number = sender_jid.split("@")[0]

                if not sender_number:
                    logger.warning("Registration trigger with no sender JID")
                    return

                if not _registration_limiter.check(sender_number):
                    logger.warning(
                        "Registration rate limit hit for %s",
                        redact_phone(sender_number),
                    )
                    return

                try:
                    await _handle_registration(client, instance, sender_number, event_id)
                except Exception:
                    logger.exception(
                        "Unhandled error in reply registration for %s (event %d)",
                        redact_phone(sender_number), event_id,
                    )
                return

    # Command: /events [args]
    if stripped.lower().startswith("/events"):
        # Per-sender rate limit to prevent 42 API exhaustion
        if not _events_cmd_limiter.check(number):
            await client.send_text(
                instance, number, "Please wait a moment before using /events again.",
            )
            return

        if _ft_client is None:
            await client.send_text(
                instance, number, "42 API is not configured."
            )
            return

        from whatsapp_bot.filters import format_help, parse_events_args
        from whatsapp_bot.scraper import get_upcoming_events

        args_str = stripped[len("/events"):].strip()

        if args_str.lower() == "help":
            reply = format_help()
        else:
            try:
                event_filter = parse_events_args(args_str) if args_str else None
            except ValueError as e:
                reply = str(e)
            else:
                try:
                    reply = await get_upcoming_events(
                        _ft_client, _ft_campus_id, event_filter=event_filter,
                        filters_path=_filters_path,
                    )
                except Exception:
                    logger.exception("Failed to fetch events for /events command")
                    reply = "Failed to fetch events. Please try again later."

        await client.send_text(instance, number, reply)
        logger.info("Replied to /events from %s", redact_phone(remote_jid.split("@")[0]))
        return

    # Command: /offers [args]
    if stripped.lower().startswith("/offers"):
        if not _offers_cmd_limiter.check(number):
            await client.send_text(
                instance, number, "Please wait a moment before using /offers again.",
            )
            return

        if _ft_client is None:
            await client.send_text(
                instance, number, "42 API is not configured."
            )
            return

        from whatsapp_bot.filters import format_offers_help, parse_offers_args
        from whatsapp_bot.offers import get_upcoming_offers

        args_str = stripped[len("/offers"):].strip()

        if args_str.lower() == "help":
            reply = format_offers_help()
        else:
            try:
                offer_filter = parse_offers_args(args_str) if args_str else None
            except ValueError as e:
                reply = str(e)
            else:
                try:
                    reply = await get_upcoming_offers(
                        _ft_client, _ft_campus_id, offer_filter=offer_filter,
                        filters_path=_filters_path, employer_resolver=_employer_resolver,
                    )
                except Exception:
                    logger.exception("Failed to fetch offers for /offers command")
                    reply = "Failed to fetch offers. Please try again later."

        await client.send_text(instance, number, reply)
        logger.info("Replied to /offers from %s", redact_phone(remote_jid.split("@")[0]))
        return

    # Unrecognized message — log and ignore (no echo in production)
    logger.debug("Unhandled message from %s: %.50s", remote_jid, text)


async def _handle_registration(
    client: "EvolutionClient",
    instance: str,
    sender_number: str,
    event_id: int,
) -> None:
    """Handle the full registration flow for a user and event.

    1. Look up stored token
    2. If no token → send OAuth link
    3. If expired → try refresh, fall back to re-auth
    4. If valid → check already registered, then register
    5. DM result to sender
    """
    if _oauth_manager is None or _user_tokens is None:
        raise RuntimeError("_handle_registration called but OAuth not configured")

    from whatsapp_bot.ft_client import FtAPIError
    from whatsapp_bot.user_tokens import UserToken

    token = _user_tokens.get(sender_number)

    # --- No token: send OAuth link ---
    if token is None:
        auth_url = _oauth_manager.create_auth_url(sender_number, event_id)
        await client.send_text(
            instance, sender_number,
            f"To register for this event, link your 42 account first:\n{auth_url}",
        )
        logger.info("Sent OAuth link to %s for event %d", redact_phone(sender_number), event_id)
        return

    # --- Expired token: try refresh ---
    if token.is_expired:
        if token.refresh_token:
            try:
                token_data = await _oauth_manager.refresh_token(token.refresh_token)
                token = UserToken(
                    access_token=token_data["access_token"],
                    refresh_token=token_data.get("refresh_token"),
                    expires_at=_oauth_manager.token_expires_at(
                        token_data.get("expires_in", 7200)
                    ),
                    user_id=token.user_id,
                )
                _user_tokens.set(sender_number, token)
                logger.info("Refreshed token for %s", redact_phone(sender_number))
            except FtAPIError:
                logger.exception("Token refresh failed for %s", redact_phone(sender_number))
                _user_tokens.delete(sender_number)
                auth_url = _oauth_manager.create_auth_url(sender_number, event_id)
                await client.send_text(
                    instance, sender_number,
                    f"Your 42 session expired. Please re-link your account:\n{auth_url}",
                )
                return
        else:
            _user_tokens.delete(sender_number)
            auth_url = _oauth_manager.create_auth_url(sender_number, event_id)
            await client.send_text(
                instance, sender_number,
                f"Your 42 session expired. Please re-link your account:\n{auth_url}",
            )
            return

    # --- Valid token: register ---
    try:
        already = await _oauth_manager.is_registered(
            token.access_token, event_id, token.user_id
        )
        if already:
            await client.send_text(
                instance, sender_number,
                "You already have a registration or waitlist entry for this event.",
            )
            logger.info(
                "User %s (42 id=%d) already registered for event %d",
                redact_phone(sender_number), token.user_id, event_id,
            )
            return

        await _oauth_manager.register_for_event(
            token.access_token, event_id, token.user_id
        )
        event = None
        if _ft_client is not None:
            try:
                event = await _ft_client.get_event(event_id)
            except FtAPIError:
                logger.warning("Could not check waitlist status for event %d", event_id)
        if _is_waitlist_registration(event):
            reply = "🕒 The event is full; you were added to its 42 waitlist."
        else:
            reply = "✅ You've been registered for this event on 42 Intra!"
        await client.send_text(
            instance, sender_number, reply,
        )
        logger.info(
            "Registered user %s (42 id=%d) for event %d",
            redact_phone(sender_number), token.user_id, event_id,
        )
    except FtAPIError as exc:
        logger.warning(
            "Registration failed for %s (event %d): %s",
            redact_phone(sender_number), event_id, exc.detail[:200],
        )
        await _handle_registration_error(
            client, instance, sender_number, event_id, exc,
        )


async def _handle_unregistration(
    client: "EvolutionClient",
    instance: str,
    sender_number: str,
    event_id: int,
) -> None:
    if _oauth_manager is None or _user_tokens is None:
        raise RuntimeError("_handle_unregistration called but OAuth not configured")

    from whatsapp_bot.ft_client import FtAPIError
    from whatsapp_bot.user_tokens import UserToken

    token = _user_tokens.get(sender_number)
    if token is None:
        await client.send_text(
            instance, sender_number,
            "Link your 42 account with Register before using /unregister.",
        )
        return
    if token.is_expired and token.refresh_token:
        try:
            token_data = await _oauth_manager.refresh_token(token.refresh_token)
            token = UserToken(
                access_token=token_data["access_token"],
                refresh_token=token_data.get("refresh_token"),
                expires_at=_oauth_manager.token_expires_at(
                    token_data.get("expires_in", 7200)
                ),
                user_id=token.user_id,
            )
            _user_tokens.set(sender_number, token)
        except FtAPIError:
            _user_tokens.delete(sender_number)
            await client.send_text(
                instance, sender_number,
                "Your 42 session expired. Tap Register to link it again, then tap Unregister.",
            )
            return
    elif token.is_expired:
        _user_tokens.delete(sender_number)
        await client.send_text(
            instance, sender_number,
            "Your 42 session expired. Tap Register to link it again, then tap Unregister.",
        )
        return

    try:
        removed = await _oauth_manager.unregister_from_event(
            token.access_token, event_id, token.user_id,
        )
    except FtAPIError:
        _user_tokens.delete(sender_number)
        await client.send_text(
            instance, sender_number,
            "Your 42 session is no longer valid. Tap Register to link it again, then tap Unregister.",
        )
        return
    if removed:
        await client.send_text(
            instance, sender_number, "✅ Your 42 event registration or waitlist entry was cancelled.",
        )
        logger.info(
            "Unregistered user %s (42 id=%d) from event %d",
            redact_phone(sender_number), token.user_id, event_id,
        )
    else:
        await client.send_text(
            instance, sender_number, "You are not registered for this event.",
        )


async def _handle_registration_error(
    client: "EvolutionClient",
    instance: str,
    sender_number: str,
    event_id: int,
    exc: "FtAPIError",
) -> None:
    """Send a helpful DM when registration fails.

    Fetches the event to check if it's full (the most common failure)
    and provides a clear message instead of a raw API error.
    """
    from whatsapp_bot.ft_client import FtAPIError

    # Try to fetch event details to give a better error message
    event: dict | None = None
    if _ft_client is not None:
        try:
            event = await _ft_client.get_event(event_id)
        except FtAPIError:
            logger.debug("Could not fetch event %d for error context", event_id)

    if event is not None:
        max_people = event.get("max_people")
        nbr_subscribers = event.get("nbr_subscribers", 0)
        event_name = event.get("name", "this event")

        if max_people and nbr_subscribers >= max_people:
            await client.send_text(
                instance, sender_number,
                f"⚠️ *{event_name}* is full ({nbr_subscribers}/{max_people}).\n"
                f"No waitlist is available for this event.",
            )
            return

    # H2: Generic fallback — don't expose raw API errors to users
    await client.send_text(
        instance, sender_number,
        "❌ Registration failed. Please try again later.",
    )


async def handle_connection_update(
    data: dict, *, client: "EvolutionClient", instance: str
) -> None:
    """Log connection state changes."""
    state = data.get("state")
    status_reason = data.get("statusReason")
    logger.info(
        "Connection update [%s]: state=%s statusReason=%s",
        instance,
        state,
        status_reason,
    )


async def handle_qrcode_update(
    data: dict, *, client: "EvolutionClient", instance: str
) -> None:
    """Log QR code updates with pairing code."""
    qrcode_base64 = data.get("qrcode", {}).get("base64")
    pairing_code = data.get("pairingCode")

    if pairing_code:
        logger.info(
            "Pairing code for [%s]: %s", instance, pairing_code
        )
        print(f"\n{'='*50}")
        print(f"  PAIRING CODE: {pairing_code}")
        print(f"{'='*50}\n")
    elif qrcode_base64:
        logger.info("QR code updated for [%s]", instance)
    else:
        logger.debug("QR code event with no data: %s", data)


_EVENT_HANDLERS = {
    "messages.upsert": handle_messages_upsert,
    "connection.update": handle_connection_update,
    "qrcode.updated": handle_qrcode_update,
}
