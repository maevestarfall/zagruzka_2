"""Slack-addon settings (lightweight, no Pydantic).

Plain ``dataclasses`` so the addon does not pull extra deps into lilush's
dependency tree. Values come from env vars; runtime overrides (token set via the
Telegram wizard) live in lilush's ``state.json`` via :mod:`bot.addons.state`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ...config import DATA_DIR


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip()
    return v if v else default


# Built-in default prompt prefilled into «Вставить промт».
DEFAULT_PROMPT = "вынуть переписку таблицей и скачать все вложения"


# OAuth scopes the connector needs. Shown to the user in the connect hint.
REQUIRED_SCOPES: tuple[str, ...] = (
    "channels:history",   # read public channel messages
    "groups:history",     # read private channel messages
    "im:history",         # read direct messages
    "mpim:history",       # read group direct messages
    "users:read",         # resolve author names instead of IDs
    "files:read",         # download file attachments
)


@dataclass(frozen=True)
class Settings:
    # Token resolved at call time via credentials.py; mirrored here for env path.
    slack_token: str | None
    # Pagination: messages pulled per Slack API page (Slack caps ~1000; 200 is safe).
    history_page_size: int
    # Hard safety cap on number of pages walked per channel (None-ish = a lot).
    max_history_pages: int
    # Where downloaded attachments are stored: <storage_root>/<channel>/<date>/.
    storage_root: str
    # Network timeout (seconds) for Slack API + file downloads.
    request_timeout: float
    # Default channel id to preselect (optional).
    default_channel: str | None


def get_settings() -> Settings:
    data_dir: Path = DATA_DIR
    return Settings(
        slack_token=_env("SLACK_BOT_TOKEN") or _env("SLACK_TOKEN"),
        history_page_size=int(_env("SLACK_HISTORY_PAGE_SIZE", "200") or "200"),
        max_history_pages=int(_env("SLACK_MAX_HISTORY_PAGES", "200") or "200"),
        storage_root=str(data_dir / "slack"),
        request_timeout=float(_env("SLACK_REQUEST_TIMEOUT", "30.0") or "30.0"),
        default_channel=_env("SLACK_DEFAULT_CHANNEL"),
    )
