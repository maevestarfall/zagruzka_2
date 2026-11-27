"""Parser of InsightFaceSwap replies (ТЗ SwapFace v2.0 §5.4–§5.5).

Pure functions over raw Discord message dicts (works for both push
events converted to dicts and REST polling results). No network here —
downloading is the caller's job; we only *classify* and *extract*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_LIMIT = "limit"

_LIMIT_MARKERS = (
    "daily limit", "no credits", "credit", "кредит", "лимит",
    "out of credits", "insufficient",
)
_ERROR_MARKERS = ("error", "failed", "ошибка", "invalid", "not found")

#: "N credits left" / "осталось N кредитов" / "credits: N"
_CREDITS_RE = re.compile(
    r"(?:осталось|remaining|left)\D{0,12}(\d{1,4})|(\d{1,4})\s*(?:credits?|кредит)",
    re.IGNORECASE,
)


@dataclass
class ParsedReply:
    status: str
    image_urls: list[str] = field(default_factory=list)       # link-button/full-res URLs
    attachment_refs: list[dict] = field(default_factory=list)  # {channel_id,message_id,attachment_id}
    credits_remaining: int | None = None
    error_text: str | None = None


def _iter_link_buttons(components: list) -> list[str]:
    """Collect URLs of link-buttons (type=2, style=5) — full-quality results."""
    urls: list[str] = []
    for row in components or []:
        for comp in (row.get("components") or []) if isinstance(row, dict) else []:
            if not isinstance(comp, dict):
                continue
            if comp.get("type") == 2 and comp.get("style") == 5 and comp.get("url"):
                urls.append(str(comp["url"]))
    return urls


def parse_credits(text: str) -> int | None:
    """Extract «remaining credits» from a bot message, if present."""
    for match in _CREDITS_RE.finditer(text or ""):
        for group in match.groups():
            if group is not None:
                try:
                    return int(group)
                except ValueError:
                    continue
    return None


def parse_reply(message: dict) -> ParsedReply:
    """Classify an InsightFaceSwap message (§5.4)."""
    content = str(message.get("content") or "")
    lowered = content.lower()
    attachments = message.get("attachments") or []
    components = message.get("components") or []
    link_urls = _iter_link_buttons(components)
    credits = parse_credits(content)

    has_results = bool(link_urls or attachments)

    if not has_results:
        if any(m in lowered for m in _LIMIT_MARKERS) and any(
            m in lowered for m in ("limit", "лимит", "no ", "insufficient", "исчерп", "законч")
        ):
            return ParsedReply(STATUS_LIMIT, credits_remaining=credits,
                               error_text=content or "Дневной лимит кредитов исчерпан.")
        if any(m in lowered for m in _ERROR_MARKERS):
            return ParsedReply(STATUS_ERROR, credits_remaining=credits,
                               error_text=content or "InsightFaceSwap вернул ошибку.")
        # placeholder «processing…» — ждём edit того же message.id
        return ParsedReply(STATUS_IN_PROGRESS, credits_remaining=credits)

    refs = []
    channel_id = message.get("channel_id")
    message_id = message.get("id")
    for att in attachments:
        if not isinstance(att, dict):
            continue
        refs.append({
            "channel_id": str(channel_id) if channel_id else None,
            "message_id": str(message_id) if message_id else None,
            "attachment_id": str(att.get("id")),
            "url": str(att.get("url") or ""),
            "filename": str(att.get("filename") or ""),
        })
    return ParsedReply(STATUS_DONE, image_urls=link_urls,
                       attachment_refs=refs, credits_remaining=credits)


def extract_image_url_from_html(html: str) -> str | None:
    """§5.4.3: a link-button URL may serve HTML instead of image bytes.

    Try ``og:image`` first, then the first ``<img src=…>``.
    """
    if not html:
        return None
    m = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            html, re.IGNORECASE,
        )
    if m:
        return m.group(1)
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
    return m.group(1) if m else None


# ── correlation (§5.5) ─────────────────────────────────────────────────────

def matches_interaction(message: dict, *, interaction_id: str | None = None,
                        nonce: str | None = None,
                        insight_app_id: str | None = None,
                        invoker_discord_id: str | None = None,
                        window_start: float | None = None) -> bool:
    """Correlate a channel message with OUR slash invocation (§5.5).

    Priority:
      1. ``interaction_metadata.id`` — current Discord field;
      2. deprecated ``interaction.id``;
      3. ``nonce`` echo;
      4. last resort (semi-manual mode): author == insight bot AND the
         interaction invoker is the owner AND timestamp is inside the
         waiting window.
    Never matches on «author only» — parallel/foreign swaps must not leak.
    """
    meta = message.get("interaction_metadata") or {}
    legacy = message.get("interaction") or {}

    if interaction_id:
        if str(meta.get("id") or "") == str(interaction_id):
            return True
        if str(legacy.get("id") or "") == str(interaction_id):
            return True
    if nonce and str(message.get("nonce") or "") == str(nonce):
        return True

    if insight_app_id and invoker_discord_id:
        author_id = str((message.get("author") or {}).get("id") or "")
        if author_id != str(insight_app_id):
            return False
        invoker = str((meta.get("user") or {}).get("id")
                      or (legacy.get("user") or {}).get("id") or "")
        if invoker != str(invoker_discord_id):
            return False
        if window_start is not None:
            ts = message.get("_timestamp")  # epoch float, set by watcher
            if ts is not None and float(ts) < float(window_start):
                return False
        return True
    return False
