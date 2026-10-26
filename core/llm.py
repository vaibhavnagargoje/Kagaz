"""
One function, `structured_call`, that sends text and files to the AI and gets
back JSON matching a schema.

Works with Google Gemini or Anthropic Claude, picked by AI_PROVIDER in .env.
The rest of the code never needs to know which one is used.
"""

import base64
import json
import logging
from dataclasses import dataclass
from functools import lru_cache

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 120


class LLMError(Exception):
    """The AI couldn't give an answer (no key, limit reached, outage, bad output)."""


@dataclass(frozen=True)
class File:
    """A document or image to show the AI."""

    data: bytes
    mime_type: str


def structured_call(*, system: str, parts: list[str | File], schema: dict) -> dict:
    """Send instructions plus text/files; return the JSON object the AI produced."""
    if settings.AI_PROVIDER == "gemini":
        text = _ask_gemini(system, parts, schema)
    elif settings.AI_PROVIDER == "claude":
        text = _ask_claude(system, parts, schema)
    else:
        raise ImproperlyConfigured(f"AI_PROVIDER must be 'gemini' or 'claude', not {settings.AI_PROVIDER!r}.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError("The AI returned invalid JSON.") from exc


# --- Google Gemini ----------------------------------------------------------


@lru_cache(maxsize=1)
def _gemini_client():
    from google import genai
    from google.genai import types

    return genai.Client(
        api_key=settings.GEMINI_API_KEY,
        http_options=types.HttpOptions(
            timeout=TIMEOUT_SECONDS * 1000,  # milliseconds
            # Gemini sometimes answers "503 busy" or "429 slow down": wait and try again.
            retry_options=types.HttpRetryOptions(
                attempts=4, initial_delay=2.0, max_delay=20.0, http_status_codes=[408, 429, 500, 502, 503, 504]
            ),
        ),
    )


def _ask_gemini(system: str, parts: list[str | File], schema: dict) -> str:
    import httpx
    from google.genai import errors, types

    if not settings.GEMINI_API_KEY:
        raise LLMError("GEMINI_API_KEY is not set in .env.")
    contents = [
        types.Part.from_bytes(data=part.data, mime_type=part.mime_type) if isinstance(part, File)
        else types.Part.from_text(text=part)
        for part in parts
    ]
    try:
        response = _gemini_client().models.generate_content(
            model=settings.AI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_json_schema=schema,
            ),
        )
    except errors.APIError as exc:
        if exc.code == 402:
            logger.error("Gemini credits are used up (402): top up at aistudio.google.com/billing")
            raise LLMError("Gemini credits are used up (402).") from exc
        logger.error("Gemini request failed: status=%s", exc.code)
        if exc.code == 429:
            raise LLMError("Gemini usage limit reached (free tier has a daily cap).") from exc
        raise LLMError(f"Gemini error {exc.code}.") from exc
    except httpx.HTTPError as exc:
        raise LLMError("Could not reach Gemini.") from exc

    if not response.text:
        raise LLMError("Gemini returned no answer (the content may have been blocked).")
    return response.text


# --- Anthropic Claude -------------------------------------------------------

# Models that accept server-side refusal fallbacks (`fallbacks="default"`).
CLAUDE_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
CLAUDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"
CLAUDE_EFFORT = "low"


@lru_cache(maxsize=1)
def _claude_client():
    import anthropic

    # With no key passed, the SDK falls back to ANTHROPIC_API_KEY from the environment.
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY or None, timeout=float(TIMEOUT_SECONDS))


def _claude_block(part: str | File) -> dict:
    if isinstance(part, str):
        return {"type": "text", "text": part}
    source = {"type": "base64", "media_type": part.mime_type, "data": base64.standard_b64encode(part.data).decode()}
    kind = "document" if part.mime_type == "application/pdf" else "image"
    return {"type": kind, "source": source}


def _ask_claude(system: str, parts: list[str | File], schema: dict) -> str:
    import anthropic

    model = settings.AI_MODEL
    request = dict(
        model=model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": [_claude_block(p) for p in parts]}],
        output_config={"effort": CLAUDE_EFFORT, "format": {"type": "json_schema", "schema": schema}},
    )
    client = _claude_client()
    try:
        if model in CLAUDE_FALLBACK_MODELS:
            response = client.beta.messages.create(betas=[CLAUDE_FALLBACK_BETA], fallbacks="default", **request)
        else:
            response = client.messages.create(**request)
    except anthropic.APIConnectionError as exc:
        raise LLMError("Could not reach Claude.") from exc
    except anthropic.APIStatusError as exc:
        logger.error("Claude request failed: status=%s request_id=%s", exc.status_code, exc.request_id)
        raise LLMError(f"Claude error {exc.status_code}.") from exc

    if response.stop_reason == "refusal":
        raise LLMError("Claude declined this request.")
    if response.stop_reason == "max_tokens":
        raise LLMError("Claude's answer was cut off.")
    text = next((block.text for block in response.content if block.type == "text"), None)
    if not text:
        raise LLMError("Claude returned no answer.")
    return text
