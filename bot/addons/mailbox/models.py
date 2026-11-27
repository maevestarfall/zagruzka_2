"""Data models for the mailbox (freelance-task) addon.

These dataclasses are the shared contract between the IMAP client, the
site-specific parsers (``parsers/``) and the addon UI. Kept dependency-free
(stdlib ``dataclasses`` only) so importing them never drags in heavy deps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class FreelanceTask:
    """A single freelance task parsed out of a notification email."""

    uid: str
    source: str
    received_at: Any | None = None
    from_address: str = ""
    subject: str = ""
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    # Human-readable price string as it appeared in the email (e.g. "до 5 000 ₽").
    price: Optional[str] = None
    # Numeric amount extracted from the price, if any.
    price_amount: Optional[float] = None
    currency: Optional[str] = None
    created_label: Optional[str] = None
    apply_link: Optional[str] = None
    raw_text_excerpt: str = ""


@dataclass
class KnownSource:
    """Describes a freelance source a parser knows how to handle."""

    id: str
    display_name: str
    sender_match: list[str] = field(default_factory=list)
    notes: str = ""
