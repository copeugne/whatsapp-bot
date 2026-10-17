#!/usr/bin/env python3
"""Live verification of all security hardening measures.

Starts the bot, then probes every security control to confirm it works.
Run with: uv run python3 scripts/verify_security.py

Exit code 0 = all checks passed, non-zero = failures found.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_URL = "http://127.0.0.1:8000"
WEBHOOK_SECRET = os.environ.get("BOT_WEBHOOK_SECRET", "")
ENCRYPTION_KEY = os.environ.get("BOT_TOKEN_ENCRYPTION_KEY", "")
INSTANCE_NAME = os.environ.get("BOT_INSTANCE_NAME", "default")

# If not in env, try to read from .env file
if not WEBHOOK_SECRET:
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("BOT_WEBHOOK_SECRET="):
                WEBHOOK_SECRET = line.split("=", 1)[1]
            elif line.startswith("BOT_TOKEN_ENCRYPTION_KEY="):
                ENCRYPTION_KEY = line.split("=", 1)[1]
            elif line.startswith("BOT_INSTANCE_NAME="):
                INSTANCE_NAME = line.split("=", 1)[1]

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

passed = 0
failed = 0
results: list[tuple[str, str, bool, str]] = []  # (id, name, ok, detail)


def check(check_id: str, name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    results.append((check_id, name, ok, detail))
    if ok:
        passed += 1
        print(f"  \033[32mPASS\033[0m  {check_id}: {name}")
    else:
        failed += 1
        print(f"  \033[31mFAIL\033[0m  {check_id}: {name}")
    if detail:
        print(f"         {detail}")


def section(title: str) -> None:
    print(f"\n\033[1m{'─' * 60}\033[0m")
    print(f"\033[1m  {title}\033[0m")
    print(f"\033[1m{'─' * 60}\033[0m")


# ---------------------------------------------------------------------------
# Pre-flight: make sure the bot is running
# ---------------------------------------------------------------------------

def wait_for_bot(timeout: int = 10) -> bool:
    """Wait for the bot to respond to /health."""
    for _ in range(timeout * 10):
        try:
            r = httpx.get(f"{BOT_URL}/health", timeout=1)
            if r.status_code == 200:
                return True
        except httpx.ConnectError:
            pass
        time.sleep(0.1)
    return False


def main() -> int:
    print("\n\033[1m  SECURITY VERIFICATION SUITE\033[0m")
    print(f"  Bot URL: {BOT_URL}")
    print(f"  Webhook secret configured: {'yes' if WEBHOOK_SECRET else 'NO'}")
    print(f"  Encryption key configured: {'yes' if ENCRYPTION_KEY else 'NO'}")

    if not wait_for_bot():
        print("\n\033[31mERROR: Bot is not running at %s\033[0m" % BOT_URL)
        print("Start it with: uv run bot &")
        return 1

    # ═════════════════════════════════════════════════════════════════════
    section("C1: Webhook Authentication")
    # ═════════════════════════════════════════════════════════════════════

    payload = json.dumps({
        "event": "messages.upsert",
        "instance": INSTANCE_NAME,
        "data": {"key": {"fromMe": True}, "message": {}},
    })
    headers = {"Content-Type": "application/json"}

    # 1a. No token → should be 401
    r = httpx.post(f"{BOT_URL}/webhook", content=payload, headers=headers)
    check("C1a", "Webhook WITHOUT token → 401",
          r.status_code == 401,
          f"Got {r.status_code}: {r.text[:100]}")

    # 1b. Wrong token → should be 401
    r = httpx.post(f"{BOT_URL}/webhook?token=wrong-token",
                   content=payload, headers=headers)
    check("C1b", "Webhook with WRONG token → 401",
          r.status_code == 401,
          f"Got {r.status_code}")

    # 1c. Correct token → should be 200
    r = httpx.post(f"{BOT_URL}/webhook?token={WEBHOOK_SECRET}",
                   content=payload, headers=headers)
    check("C1c", "Webhook with CORRECT token → 200",
          r.status_code == 200,
          f"Got {r.status_code}: {r.text[:100]}")

    # ═════════════════════════════════════════════════════════════════════
    section("C2: Token Encryption at Rest")
    # ═════════════════════════════════════════════════════════════════════

    tokens_path = Path("data/user_tokens.json")
    if tokens_path.exists():
        raw = tokens_path.read_text().strip()
        is_encrypted = raw.startswith("ENC::")
        check("C2a", "Token file is encrypted (ENC:: prefix)",
              is_encrypted,
              f"File starts with: {raw[:30]!r}...")

        # Verify no plaintext tokens visible
        has_plaintext_token = "access_token" in raw and not raw.startswith("ENC::")
        check("C2b", "No plaintext tokens in file",
              not has_plaintext_token,
              "Plaintext tokens found!" if has_plaintext_token else "OK")
    else:
        check("C2a", "Token file exists", False, "data/user_tokens.json not found")
        check("C2b", "No plaintext tokens", True, "No file to check")

    # ═════════════════════════════════════════════════════════════════════
    section("C3: .env File Permissions")
    # ═════════════════════════════════════════════════════════════════════

    env_path = Path(".env")
    if env_path.exists():
        mode = stat.S_IMODE(env_path.stat().st_mode)
        check("C3", ".env permissions are 0600 (owner-only)",
              mode == 0o600,
              f"Current mode: {mode:04o}")
    else:
        check("C3", ".env exists", False, ".env not found")

    # ═════════════════════════════════════════════════════════════════════
    section("M5: Error Messages Don't Leak API Details")
    # (Run BEFORE M1 rate-limit test so the OAuth endpoint isn't exhausted)
    # ═════════════════════════════════════════════════════════════════════

    # Send an invalid OAuth callback — the error should be generic
    r = httpx.get(f"{BOT_URL}/oauth/callback?code=invalid&state=invalid",
                  follow_redirects=False, timeout=5)
    # State won't match → "Invalid or expired link" (not an API error)
    body_text = r.text
    check("M5a", "Invalid OAuth state → no API error leaked",
          "Invalid or expired link" in body_text,
          f"Got: {body_text[:100]}")

    # Check that no raw API text patterns appear
    leak_patterns = ["response.text", "HTTP/1.1", "Bearer ", "access_token"]
    has_leak = any(p in body_text for p in leak_patterns)
    check("M5b", "No raw API details in error response",
          not has_leak,
          "Leak detected!" if has_leak else "Clean")

    # ═════════════════════════════════════════════════════════════════════
    section("M1: Rate Limiting")
    # ═════════════════════════════════════════════════════════════════════

    # OAuth callback rate limit: 10 req/min
    # Send 11 requests rapidly — the 11th should be 429
    rate_limited = False
    for i in range(15):
        r = httpx.get(f"{BOT_URL}/oauth/callback?code=test&state=invalid",
                      follow_redirects=False, timeout=5)
        if r.status_code == 429:
            rate_limited = True
            break

    check("M1", "OAuth callback rate-limits after burst",
          rate_limited,
          f"Hit 429 after {i + 1} requests" if rate_limited else
          f"Sent 15 requests without 429 (got {r.status_code})")

    # ═════════════════════════════════════════════════════════════════════
    section("M2: Instance Name Validation")
    # ═════════════════════════════════════════════════════════════════════

    spoofed_payload = json.dumps({
        "event": "messages.upsert",
        "instance": "attacker-instance",
        "data": {"key": {"fromMe": True}, "message": {}},
    })
    r = httpx.post(f"{BOT_URL}/webhook?token={WEBHOOK_SECRET}",
                   content=spoofed_payload, headers=headers)
    body = r.json()
    check("M2", "Webhook rejects wrong instance name",
          body.get("status") == "ignored",
          f"Response: {body}")

    # ═════════════════════════════════════════════════════════════════════
    section("M3: PII Redaction in Logs")
    # ═════════════════════════════════════════════════════════════════════

    from whatsapp_bot.security import redact_phone

    test_cases = [
        ("33633493864", "336***3864"),
        ("14155552671", "141***2671"),
        ("12345", "***"),
        ("", "***"),
    ]
    all_redact_ok = True
    for phone, expected in test_cases:
        result = redact_phone(phone)
        if result != expected:
            all_redact_ok = False
            check("M3", f"redact_phone({phone!r})", False,
                  f"Expected {expected!r}, got {result!r}")

    if all_redact_ok:
        check("M3", "Phone number redaction works correctly", True,
              f"All {len(test_cases)} test cases passed")

    # ═════════════════════════════════════════════════════════════════════
    section("M4: pending_oauth.json Permissions")
    # ═════════════════════════════════════════════════════════════════════

    pending_path = Path("data/pending_oauth.json")
    if pending_path.exists():
        mode = stat.S_IMODE(pending_path.stat().st_mode)
        check("M4", "pending_oauth.json permissions are 0600",
              mode == 0o600,
              f"Current mode: {mode:04o}")
    else:
        check("M4", "pending_oauth.json permissions",
              True, "File doesn't exist yet (created on first OAuth flow)")

    # ═════════════════════════════════════════════════════════════════════
    section("M6: Instance Name Format Validation")
    # ═════════════════════════════════════════════════════════════════════

    from pydantic import ValidationError
    from whatsapp_bot.config import Settings

    # Valid names should work
    valid_ok = True
    for name in ["default", "my-bot", "bot_v2"]:
        try:
            Settings(evolution_api_url="http://x", evolution_api_key="k",
                     instance_name=name)
        except Exception:
            valid_ok = False

    check("M6a", "Valid instance names accepted",
          valid_ok, "default, my-bot, bot_v2")

    # Path traversal should be rejected
    traversal_blocked = False
    try:
        Settings(evolution_api_url="http://x", evolution_api_key="k",
                 instance_name="../../admin")
    except (ValidationError, ValueError):
        traversal_blocked = True

    check("M6b", "Path traversal in instance name rejected",
          traversal_blocked, "../../admin → rejected")

    # ═════════════════════════════════════════════════════════════════════
    section("M7: send_media URL Scheme Validation")
    # ═════════════════════════════════════════════════════════════════════

    from whatsapp_bot.evolution import EvolutionClient

    client = EvolutionClient(base_url="http://test:8080", api_key="key")

    # Test that http:// is rejected
    import asyncio

    async def test_ssrf():
        client._client = httpx.AsyncClient()  # mock client to avoid "not started"
        try:
            await client.send_media("default", "123", "image",
                                    "http://169.254.169.254/metadata")
            return False  # Should have raised
        except ValueError as e:
            return "data: or https://" in str(e)
        finally:
            await client._client.aclose()

    ssrf_blocked = asyncio.run(test_ssrf())
    check("M7", "send_media rejects http:// URLs (SSRF protection)",
          ssrf_blocked, "http://169.254.169.254/... → ValueError")

    # ═════════════════════════════════════════════════════════════════════
    section("H2: User-facing Error Messages Are Generic")
    # ═════════════════════════════════════════════════════════════════════

    # The registration error handler should not include exc.detail
    import inspect
    from whatsapp_bot import handlers

    source = inspect.getsource(handlers._handle_registration_error)
    check("H2", "Registration error handler sends generic message",
          "exc.detail" not in source.split("send_text")[1] if "send_text" in source else False,
          "No exc.detail passed to send_text")

    # ═════════════════════════════════════════════════════════════════════
    section("Token File Security")
    # ═════════════════════════════════════════════════════════════════════

    if tokens_path.exists():
        mode = stat.S_IMODE(tokens_path.stat().st_mode)
        check("TF", "user_tokens.json permissions are 0600",
              mode == 0o600,
              f"Current mode: {mode:04o}")
    else:
        check("TF", "user_tokens.json permissions", True, "No file yet")

    # ═════════════════════════════════════════════════════════════════════
    section("NEW: Env File Separation")
    # ═════════════════════════════════════════════════════════════════════

    evo_env = Path("evolution.env")
    if evo_env.exists():
        evo_content = evo_env.read_text()
        # evolution.env should NOT contain bot secrets
        bot_secrets_leaked = any(s in evo_content for s in [
            "BOT_FT_CLIENT_SECRET", "BOT_TOKEN_ENCRYPTION_KEY",
            "BOT_GEMINI_API_KEY", "BOT_FT_CLIENT_ID",
        ])
        check("ENV1", "evolution.env has NO bot secrets",
              not bot_secrets_leaked,
              "Bot secrets found in evolution.env!" if bot_secrets_leaked else "Clean")

        evo_mode = stat.S_IMODE(evo_env.stat().st_mode)
        check("ENV2", "evolution.env permissions are 0600",
              evo_mode == 0o600,
              f"Current mode: {evo_mode:04o}")
    else:
        check("ENV1", "evolution.env exists", False, "evolution.env not found")
        check("ENV2", "evolution.env permissions", False, "File missing")

    # docker-compose should reference evolution.env, not .env
    dc_path = Path("docker-compose.yml")
    if dc_path.exists():
        dc_content = dc_path.read_text()
        uses_evo_env = "evolution.env" in dc_content
        uses_dot_env = "- .env" in dc_content or "env_file:\n      - .env" in dc_content
        check("ENV3", "docker-compose uses evolution.env (not .env)",
              uses_evo_env and not uses_dot_env,
              "evolution.env in compose" if uses_evo_env else "Still using .env!")

    # ═════════════════════════════════════════════════════════════════════
    section("NEW: Log File Permissions")
    # ═════════════════════════════════════════════════════════════════════

    log_path = Path("logs/bot.log")
    if log_path.exists():
        log_mode = stat.S_IMODE(log_path.stat().st_mode)
        check("LOG", "bot.log permissions are 0600",
              log_mode == 0o600,
              f"Current mode: {log_mode:04o}")
    else:
        check("LOG", "bot.log permissions", True, "No log file yet")

    # ═════════════════════════════════════════════════════════════════════
    section("NEW: Request Body Size Limit")
    # ═════════════════════════════════════════════════════════════════════

    # Send an oversized payload — should be rejected
    huge_payload = '{"x": "' + "A" * 2_000_000 + '"}'
    r = httpx.post(
        f"{BOT_URL}/webhook?token={WEBHOOK_SECRET}",
        content=huge_payload,
        headers={"Content-Type": "application/json",
                 "Content-Length": str(len(huge_payload))},
        timeout=10,
    )
    check("BODY", "Oversized webhook payload rejected (413)",
          r.status_code == 413,
          f"Got {r.status_code}")

    # ═════════════════════════════════════════════════════════════════════
    section("NEW: Per-Sender Rate Limiting")
    # ═════════════════════════════════════════════════════════════════════

    from whatsapp_bot.handlers import _events_cmd_limiter, _registration_limiter

    check("PSLIM1", "Per-sender /events rate limiter exists",
          _events_cmd_limiter._max == 5 and _events_cmd_limiter._window == 60,
          f"Config: {_events_cmd_limiter._max} req / {_events_cmd_limiter._window}s")

    check("PSLIM2", "Per-sender registration rate limiter exists",
          _registration_limiter._max == 10 and _registration_limiter._window == 60,
          f"Config: {_registration_limiter._max} req / {_registration_limiter._window}s")

    # ═════════════════════════════════════════════════════════════════════
    section("NEW: Redis Authentication")
    # ═════════════════════════════════════════════════════════════════════

    if dc_path.exists():
        dc_content = dc_path.read_text()
        has_requirepass = "--requirepass" in dc_content
        check("REDIS", "Redis has --requirepass in docker-compose",
              has_requirepass,
              "Authentication enabled" if has_requirepass else "NO password!")

    # ═════════════════════════════════════════════════════════════════════
    # Summary
    # ═════════════════════════════════════════════════════════════════════
    print(f"\n\033[1m{'═' * 60}\033[0m")
    print(f"\033[1m  RESULTS: {passed} passed, {failed} failed\033[0m")
    print(f"\033[1m{'═' * 60}\033[0m")

    if failed == 0:
        print("\n\033[32m  ✓ All security controls verified and effective.\033[0m\n")
    else:
        print(f"\n\033[31m  ✗ {failed} check(s) failed — review above.\033[0m\n")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
