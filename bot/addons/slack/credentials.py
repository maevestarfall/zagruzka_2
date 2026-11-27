"""Slack credentials resolver.

Lookup order (mirrors mailbox/credentials.py):
1. Lilush state.json (token set via in-Telegram wizard) — most flexible.
2. Env vars ``SLACK_BOT_TOKEN`` / ``SLACK_TOKEN`` (Render dashboard).

The token is a Slack OAuth token (``xoxb-…`` bot token or ``xoxp-…`` user token).
It is stored alongside other addon secrets and never logged.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import state as addon_state
from .config import get_settings

_ADDON = "slack"


@dataclass(frozen=True)
class SlackCredentials:
    token: str


# Ключ записи в защищённом хранилище 18.4 = dotted-путь plaintext-источника.
_SECRET_STORE_KEY = "_settings.addons.slack.token"


def load_credentials() -> SlackCredentials | None:
    # 1) addon_state (wizard) — с 18.4 сперва пробуем зашифрованную копию
    #    (secret_store), с ОБЯЗАТЕЛЬНЫМ fallback на legacy plaintext: нет
    #    master key / записи / целостности → читаем как раньше. Slack не
    #    может перестать работать из-за миграции (18.4 §3 п.5).
    legacy = (addon_state.get(_ADDON, "token", "") or "").strip()
    token = legacy
    try:
        from ..masterroom.sync.secret_store import (  # noqa: PLC0415
            read_secret_with_fallback,
        )

        token = (read_secret_with_fallback(_SECRET_STORE_KEY, legacy) or "").strip()
    except Exception:  # noqa: BLE001 — защищённый слой не роняет коннектор
        token = legacy
    if token:
        return SlackCredentials(token=token)

    # 2) env vars
    settings = get_settings()
    if settings.slack_token:
        return SlackCredentials(token=settings.slack_token.strip())

    return None


def load_token() -> str | None:
    creds = load_credentials()
    return creds.token if creds else None


def save_token(token: str) -> None:
    # Legacy plaintext сохраняем как раньше (удаление — только 18.7),
    # плюс — при наличии master key — свежую зашифрованную копию, чтобы
    # secret_store не отставал от plaintext-источника.
    addon_state.set_(_ADDON, "token", token.strip())
    try:
        from ..masterroom.sync import secret_store  # noqa: PLC0415

        if secret_store.is_available():
            secret_store.put_secret(
                _SECRET_STORE_KEY, token.strip(), migrated_from="state",
            )
    except Exception:  # noqa: BLE001 — шифрование не роняет wizard
        pass


def clear_token() -> None:
    addon_state.delete(_ADDON, "token")
    try:
        from ..masterroom.sync import secret_store  # noqa: PLC0415

        secret_store.delete_secret(_SECRET_STORE_KEY)
    except Exception:  # noqa: BLE001
        pass


def is_configured() -> bool:
    return load_credentials() is not None


def looks_like_slack_token(value: str) -> bool:
    """Quick sanity check before hitting the API."""
    v = (value or "").strip()
    return v.startswith(("xoxb-", "xoxp-", "xoxe-", "xapp-"))


def mask_token(token: str) -> str:
    """Render a token for display without leaking it (e.g. ``xoxb-…abcd``)."""
    t = (token or "").strip()
    if not t:
        return "—"
    prefix = t.split("-", 1)[0] + "-" if "-" in t else ""
    tail = t[-4:] if len(t) >= 4 else "***"
    return f"{prefix}…{tail}"
