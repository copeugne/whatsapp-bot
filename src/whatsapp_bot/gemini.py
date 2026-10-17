"""Gemini API client for event description summarization.

Uses Google's Gemini Flash to intelligently condense event descriptions
so they fit within WhatsApp's poll character limit while preserving all
important details (speakers, topics, logistics).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_MODEL = "gemini-3-flash-preview"

# The prompt deliberately avoids asking for a character count (LLMs can't
# count chars).  Instead it asks for *structured sections* that naturally
# fill the space, and a retry prompt asks the model to expand if too short.
_SYSTEM_PROMPT = """\
You are condensing an event description for a WhatsApp poll at 42 coding school.
The event title, date, location, and spots are ALREADY shown above your text — do NOT repeat them.

Your output MUST fit in {budget} characters.

Format (keep the same language as the input, usually French):
1. One sentence describing what the event is about and why it matters (do NOT repeat the event title)
2. Then a BLANK LINE
3. ALL speakers as "• Name — role at Company" — one per line
4. Logistics: dress code, what to bring, networking, etc. (only if mentioned in original)

Rules:
- Output ONLY the formatted text. No labels, no emojis.
- Include EVERY speaker — abbreviate their descriptions if needed to fit all of them.
- Do NOT include marketing fluff or generic encouragements.
- Do NOT add anything not in the original text.
- Do NOT use bold markers (*) — WhatsApp polls don't render them."""

_EXPAND_PROMPT = """\
Your summary is {actual_len} chars but you have {budget} chars available — you have {remaining} chars of unused space.
Expand your output to use the available space by:
- Using full (non-abbreviated) speaker titles and roles
- Adding more detail about what each speaker will present
- Including any logistics you omitted (dress code, what to bring, networking, etc.)
- Expanding the topic sentence with more detail from the original

Your previous output:
{previous}

Write an expanded version (same format, same language, max {budget} chars):"""


async def _call_gemini(
    client: httpx.AsyncClient,
    url: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.3,
) -> str | None:
    """Make a single Gemini API call and return the text response."""
    payload: dict[str, Any] = {
        "contents": messages,
        "generationConfig": {
            "maxOutputTokens": 2048,
            "temperature": temperature,
            # Gemini 2.5 Flash uses thinking by default, which eats
            # into the output token budget and leaves ~80 chars for
            # the actual text.  Disable it for this simple task.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    response = await client.post(url, json=payload)
    if not response.is_success:
        logger.warning(
            "Gemini API error %d: %s",
            response.status_code,
            response.text[:200],
        )
        return None

    body = response.json()
    candidates = body.get("candidates", [])
    if not candidates:
        logger.warning("Gemini returned no candidates: %s", body)
        return None

    return candidates[0]["content"]["parts"][0]["text"].strip()


def _trim_to_budget(text: str, budget: int) -> str:
    """Hard-trim text to *budget* chars at a clean boundary."""
    if len(text) <= budget:
        return text
    # Prefer trimming at a newline
    last_nl = text[:budget].rfind("\n")
    if last_nl > budget // 2:
        return text[:last_nl]
    # Else trim at a space
    last_sp = text[: budget - 3].rfind(" ")
    if last_sp > 0:
        return text[:last_sp] + "..."
    return text[: budget - 3] + "..."


async def summarize_description(
    api_key: str,
    description: str,
    budget: int,
) -> str | None:
    """Summarize an event description to fit within *budget* characters.

    Returns the summary string, or ``None`` on any failure (caller should
    fall back to truncation).
    """
    if not api_key or not description or budget < 50:
        return None

    expand_threshold = budget * 3 // 4  # expand if under 75% of budget

    # Keep credentials out of URLs, which httpx logs at INFO level.
    url = f"{_API_BASE}/models/{_MODEL}:generateContent"

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "parts": [
                {
                    "text": (
                        f"{_SYSTEM_PROMPT.format(budget=budget)}\n\n"
                        f"--- EVENT DESCRIPTION ---\n{description}\n---"
                    ),
                },
            ],
        },
    ]

    try:
        async with httpx.AsyncClient(
            timeout=120.0, headers={"x-goog-api-key": api_key},
        ) as client:
            text = await _call_gemini(client, url, messages)
            if text is None:
                return None

            # If the model left significant room, ask it to fill the space
            if len(text) < expand_threshold:
                remaining = budget - len(text)
                logger.info(
                    "Gemini summary under-filled (%d/%d, %d spare), "
                    "requesting expansion",
                    len(text),
                    budget,
                    remaining,
                )
                # Build multi-turn conversation for expansion
                messages.append(
                    {"role": "model", "parts": [{"text": text}]},
                )
                messages.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "text": _EXPAND_PROMPT.format(
                                    actual_len=len(text),
                                    budget=budget,
                                    remaining=remaining,
                                    previous=text,
                                ),
                            },
                        ],
                    },
                )
                expanded = await _call_gemini(
                    client, url, messages, temperature=0.5,
                )
                if expanded and len(expanded) > len(text):
                    text = expanded

            return _trim_to_budget(text, budget)

    except Exception:
        logger.exception("Gemini summarization failed")
        return None
