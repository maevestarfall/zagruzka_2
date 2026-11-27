"""Slack connector addon — read channel/DM history (with thread replies) and
download attachments via Telegram.

Self-contained, mirrors the structure of :mod:`bot.addons.mailbox` but talks to
Slack's Web API (Slack has no IMAP) through the official ``slack_sdk`` library:

* ``config``      — lightweight settings (dataclass, env-backed).
* ``credentials`` — Slack OAuth token input / secure storage (addon_state).
* ``client``      — thin wrapper over ``slack_sdk.WebClient`` (history, thread
  replies, files, conversation list, user names) with rate-limit retry.
* ``parsers``     — normalise raw Slack messages into a single ``SlackMessage``
  shape plus markdown/CSV renderers.
* ``handlers``    — aiogram router: connect → pick channel → read/download →
  free-form prompt.

The token is stored in lilush's ``state.json`` (via ``addon_state``) exactly
like the mailbox password, and is never logged or echoed back into the chat.
"""

from __future__ import annotations
