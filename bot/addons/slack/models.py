"""Data models for the Slack connector addon.

Dependency-free dataclasses shared between the Slack client, the parsers and the
Telegram UI. ``SlackMessage`` is the normalised shape every message (top-level or
thread reply) is reduced to before rendering / filtering.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SlackFile:
    """One file attached to a Slack message."""

    id: str
    name: str
    title: str = ""
    mimetype: str = ""
    size: int = 0
    # Private URL — requires Bearer-token auth to download.
    url_private: str = ""
    # Convenience: external/permalink (not always authed-downloadable).
    permalink: str = ""

    @property
    def is_zip(self) -> bool:
        n = (self.name or "").lower()
        return n.endswith(".zip") or self.mimetype in {"application/zip", "application/x-zip-compressed"}


@dataclass
class SlackMessage:
    """A normalised Slack message (top-level post or a thread reply)."""

    ts: str                      # Slack message timestamp id ("1700000000.000100")
    channel_id: str
    author_id: str = ""
    author_name: str = ""
    is_me: bool = False          # True if written by the token owner ("я")
    text: str = ""
    # Wall-clock datetime (UTC, tz-aware) derived from ``ts``.
    datetime: object | None = None
    files: list[SlackFile] = field(default_factory=list)
    # Threading: if this is a reply, ``thread_ts`` points at the parent ts.
    thread_ts: str = ""
    is_thread_parent: bool = False
    reply_count: int = 0
    # Filled for parents: their normalised replies (excluding the parent itself).
    replies: list["SlackMessage"] = field(default_factory=list)

    @property
    def is_reply(self) -> bool:
        return bool(self.thread_ts) and self.thread_ts != self.ts


@dataclass
class SlackConversation:
    """A channel or DM the token can see."""

    id: str
    name: str          # human label ("#general", "DM: Alex", …)
    kind: str          # one of: public | private | im | mpim
    is_member: bool = True
