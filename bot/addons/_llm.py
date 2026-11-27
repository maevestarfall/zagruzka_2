"""Tiny shared LLM helper for addons.

Reuses lilush's own chat brain credentials (the ``openrouter`` provider
slot + active model) so addons never need a second key. Mirrors the
private helpers in :mod:`bot.media_llm` but exposes a single public
``chat_once`` coroutine that every addon (osint / prompt_sources /
priority / hermes) can call.

Degrades gracefully: raises :class:`NoBrainError` when no key is
configured so callers can show a friendly "настрой мозг" message
instead of crashing.
"""

from __future__ import annotations

import logging

from ..config import APP_TITLE, DEFAULT_MODEL, HTTP_REFERER, OPENROUTER_BASE_URL
from ..storage import storage

logger = logging.getLogger(__name__)


class NoBrainError(RuntimeError):
    """Raised when no LLM key is configured anywhere."""


def _build_client():
    """Return ``(AsyncOpenAI, model_name)`` using lilush's own brain slot."""
    from openai import AsyncOpenAI  # local import — heavy module

    api_key = storage.get_provider_key("openrouter")
    if not api_key:
        raise NoBrainError(
            "LLM-мозг не настроен — открой /setup, выбери мозг и пришли "
            "API-ключ. Без него ИИ-функции недоступны."
        )
    provider = storage.get_provider()
    if provider == "custom":
        base_url = storage.get_base_url() or OPENROUTER_BASE_URL
    else:
        base_url = OPENROUTER_BASE_URL
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        default_headers={"HTTP-Referer": HTTP_REFERER, "X-Title": APP_TITLE},
    )
    model = storage.get_model() or DEFAULT_MODEL
    return client, model


async def chat_once(
    system: str,
    user: str,
    *,
    max_tokens: int = 800,
    temperature: float = 0.7,
) -> str:
    """Single non-streamed chat completion. Returns text (may be empty-safe).

    Raises :class:`NoBrainError` when the brain is unconfigured or the API
    errors out — callers decide how to surface it.
    """
    from openai._exceptions import APIError  # local import

    client, model = _build_client()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except APIError as exc:
        raise NoBrainError(f"LLM API ошибка: {exc}") from exc
    finally:
        try:
            await client.close()  # AsyncOpenAI uses close(), not aclose()
        except Exception:  # noqa: BLE001
            pass
    choices = getattr(resp, "choices", None)
    if not choices:
        raise NoBrainError("LLM вернул пустой ответ.")
    msg = choices[0].message
    return (getattr(msg, "content", None) or "").strip()
