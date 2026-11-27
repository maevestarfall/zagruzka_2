"""Banned-phrase enforcement (§7 of PRAVILO_DEBAT_v3).

All matches are case-insensitive substring (NOT regex word-boundary), so that
"production-grade" matches "production-grade architecture" and
"productiongrade" alike. We accept some false-positive risk in favor of strict
enforcement; debaters can rephrase.
"""

from __future__ import annotations

BANNED_PHRASES: tuple[str, ...] = (
    "production-grade",
    "production grade",
    "robust",
    "robustness",
    "scalable",
    "scalability",
    "elegant",
    "elegance",
    "should consider",
    "may want to",
    "could potentially",
    "best practices",
    "industry standard",
    "industry-standard",
    "reasonable defaults",
    "sane defaults",
    "user-friendly",
    "state-of-the-art",
    "cutting-edge",
    "cutting edge",
    "seamless",
    "seamlessly",
    "enterprise-ready",
    "enterprise ready",
    "well-architected",
    "clean architecture",
    "rock solid",
    "rock-solid",
    "battle-tested",
    "battle tested",
)


def find_banned(text: str) -> list[dict]:
    """Return list of {rule, detail, offset} for every banned-phrase hit."""
    if not text:
        return []
    lower = text.lower()
    hits: list[dict] = []
    for phrase in BANNED_PHRASES:
        off = 0
        while True:
            idx = lower.find(phrase, off)
            if idx < 0:
                break
            hits.append({"rule": "banned_phrase", "detail": phrase, "offset": idx})
            off = idx + len(phrase)
    return hits


# Prose-density heuristic for cluster-propose / final-propose (§7 second half).
# We look for presence of "structural anchors" — bullet lists, tables, code
# fences, env-style ALL_CAPS_VARS, function signatures. A message with zero
# anchors is too prose-heavy.
import re

_ANCHOR_PATTERNS = (
    re.compile(r"\n[ \t]*[-*][ \t]+\S"),          # bullet list
    re.compile(r"\n[ \t]*\d+\.[ \t]+\S"),          # numbered list
    re.compile(r"\n[ \t]*\|[^\n]+\|"),             # markdown table row
    re.compile(r"```"),                             # code fence
    re.compile(r"\b[A-Z][A-Z0-9_]{3,}\b"),         # ALL_CAPS env var or const
    re.compile(r"\bdef\s+\w+\s*\("),               # python def
    re.compile(r"\b(class|interface)\s+\w+"),      # class/interface
    re.compile(r"`[^`\n]{2,}`"),                   # inline code
    re.compile(r"\bSELECT\b|\bCREATE TABLE\b|\bINSERT INTO\b", re.IGNORECASE),
)


def prose_density_ok(text: str, min_anchors: int = 3) -> tuple[bool, int]:
    """Return (ok, anchor_count). For cluster/final-propose only."""
    if not text:
        return False, 0
    count = 0
    for pat in _ANCHOR_PATTERNS:
        count += len(pat.findall(text))
    return count >= min_anchors, count
