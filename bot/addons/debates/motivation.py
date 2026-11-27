"""Context injection system for AI debates (v3) and bot-wide auto-motivation.

Supports CONTEXT_INJECTION_SYSTEM v3 document format:
  - 5 categories: A (responsibility), B (confidence), C (provocation),
                  D (honesty), E (Karpathy context engineering)
  - 10 phrases each (50 total), 3-line format (persona + context + action)
  - Adaptive category weights based on debate metrics
  - Variable interval: every 12-18 msgs random, min 8 min, max 25 min
  - FIFO-10: no repeat of last 10 used phrases

Also supports MOTIVATIONAL_INJECTION_SYSTEM v2 (4 categories, 40 phrases)
and legacy simple format (backward-compat).

Storage in addon state:
    addons.debates.motivation = {
        "doc": {
            "id": "mot_17xxxxx",
            "name": "...",
            "raw_content": "…full raw text…",
            "categories": {
                "A": [{"id": "A01", "text": "...", "source": "..."},...],
                "B": [...], "C": [...], "D": [...], "E": [...]
            },
            "timing": {...},
            "total_phrases": 50,
            "createdAt": "...",
        },
        "stats": {...},
    }
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import state as addon_state

_DOCS_DIR = Path(__file__).parent / "docs"
_ALL_CATS = ("A", "B", "C", "D", "E")


# ─── State helpers ────────────────────────────────────────────────────────────


def _data() -> dict[str, Any]:
    return addon_state.get("debates", "motivation", {}) or {}


def _save(d: dict[str, Any]) -> None:
    addon_state.set_("debates", "motivation", d)


# ─── Auto-load default ────────────────────────────────────────────────────────


def ensure_default() -> bool:
    """Load the default CONTEXT_INJECTION_SYSTEM v3 if no doc is set.

    Falls back to v2 if v3 not found. Call once at startup.
    Returns True if a default was loaded.
    """
    d = _data()
    doc = d.get("doc")
    if doc and isinstance(doc, dict) and doc.get("id"):
        return False  # already have a doc

    # Try v3 first, then v2
    for fname in ("CONTEXT_INJECTION_SYSTEM_v3.md",
                  "MOTIVATIONAL_INJECTION_SYSTEM_v2.md"):
        path = _DOCS_DIR / fname
        if path.exists():
            text = path.read_text(encoding="utf-8")
            categories, timing, total = parse_motivation_doc(text)
            if total > 0:
                name = _extract_title(text, fname.replace(".md", ""))
                set_doc(name, text, categories, timing, total)
                return True
    return False


def _extract_title(text: str, fallback: str = "Мотивация") -> str:
    """Extract title from first markdown heading."""
    for ln in text.split("\n"):
        ln = ln.strip()
        if ln.startswith("# "):
            return ln.lstrip("# ").strip()[:80]
    return fallback


# ─── Public getters ───────────────────────────────────────────────────────────


def get_doc() -> dict[str, Any] | None:
    doc = _data().get("doc")
    if doc and isinstance(doc, dict) and doc.get("id"):
        return doc
    return None


def get_stats() -> dict[str, Any]:
    return _data().get("stats") or _empty_stats()


def _empty_stats() -> dict[str, Any]:
    return {
        "total_sent": 0,
        "by_debater": {},
        "by_category": {k: 0 for k in _ALL_CATS},
        "last_phrase_id": "",
        "last_sent_at": 0,
        "last_injection_msg": 0,
        "next_threshold": random.randint(12, 18),
        "used_recently": [],
    }


# ─── Set / delete ─────────────────────────────────────────────────────────────


def set_doc(name: str, content: str,
            categories: dict[str, list[dict]],
            timing: dict[str, int] | None = None,
            total_phrases: int = 0) -> dict[str, Any]:
    """Save a parsed motivation/context-injection document. Resets stats."""
    if timing is None:
        timing = {
            "msg_interval_min": 12, "msg_interval_max": 18,
            "time_min_minutes": 8, "time_max_minutes": 25,
        }
    doc = {
        "id": f"mot_{int(time.time() * 1000)}",
        "name": name[:120].strip(),
        "raw_content": content,
        "categories": categories,
        "timing": timing,
        "total_phrases": total_phrases or sum(
            len(v) for v in categories.values()),
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _save({"doc": doc, "stats": _empty_stats()})
    return doc


def set_doc_legacy(name: str, content: str,
                   interval_minutes: int,
                   phrases: list[str]) -> dict[str, Any]:
    """Legacy: save from simple format (flat phrase list)."""
    cat_phrases = [
        {"id": f"L{i + 1:02d}", "text": p, "source": ""}
        for i, p in enumerate(phrases[:50])
    ]
    categories: dict[str, list[dict]] = {
        k: (cat_phrases if k == "A" else []) for k in _ALL_CATS
    }
    timing = {
        "msg_interval_min": max(1, interval_minutes),
        "msg_interval_max": max(1, interval_minutes),
        "time_min_minutes": 1, "time_max_minutes": 120,
    }
    return set_doc(name, content, categories, timing, len(cat_phrases))


def delete_doc() -> bool:
    if not get_doc():
        return False
    _save({})
    return True


# ─── Injection logic (debates) ────────────────────────────────────────────────


def should_inject(current_msg_count: int) -> bool:
    """Check whether an injection should happen now (debate context)."""
    doc = get_doc()
    if not doc:
        return False
    stats = get_stats()
    timing = doc.get("timing", {})

    msgs_since = current_msg_count - stats.get("last_injection_msg", 0)
    time_since = time.time() - stats.get("last_sent_at", 0)
    time_since_min = time_since / 60

    time_min = timing.get("time_min_minutes", 8)
    time_max = timing.get("time_max_minutes", 25)
    threshold = stats.get("next_threshold", 15)

    if time_since_min < time_min:
        return False
    if time_since_min > time_max:
        return True
    return msgs_since >= threshold


def select_phrase(context: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Select a phrase adaptively based on context.

    context keys: disagree_rate, phase, msg_count, last_rejection,
                  avg_msg_length, persona_name, remaining.
    """
    doc = get_doc()
    if not doc:
        return None
    ctx = context or {}
    cats = doc.get("categories", {})

    banks: dict[str, list[dict]] = {}
    for k in _ALL_CATS:
        items = cats.get(k, [])
        if items:
            banks[k] = items
    if not banks:
        return None

    weights = _adaptive_weights(ctx, list(banks.keys()))
    cat_keys = list(banks.keys())
    cat_weights = [weights.get(k, 1.0 / len(cat_keys)) for k in cat_keys]
    chosen_cat = random.choices(cat_keys, weights=cat_weights, k=1)[0]

    # Filter out recently used (FIFO-10)
    stats = get_stats()
    used = set(stats.get("used_recently", []))
    available = [p for p in banks[chosen_cat] if p["id"] not in used]

    if not available:
        for other in cat_keys:
            if other != chosen_cat:
                available = [p for p in banks[other]
                             if p["id"] not in used]
                if available:
                    chosen_cat = other
                    break

    if not available:
        stats["used_recently"] = []
        available = banks[chosen_cat]

    phrase = random.choice(available)
    text = phrase.get("text", "")
    persona = ctx.get("persona_name", "дебатёр")
    remaining = ctx.get("remaining", 300 - ctx.get("msg_count", 0))
    text = text.replace("{{persona}}", persona)
    text = text.replace("{{remaining}}", str(remaining))

    phase = ctx.get("phase", "P2")
    msg_count = ctx.get("msg_count", 0)
    formatted = (
        f'<context_injection phase="{phase}" msg="{msg_count}" '
        f'persona="{persona}">\n{text}\n</context_injection>'
    )

    return {
        "id": phrase["id"],
        "text": text,
        "category": chosen_cat,
        "formatted_text": formatted,
        "source": phrase.get("source", ""),
    }


def record_sent(debater_id: str, phrase_result: dict[str, Any]) -> None:
    """Record that a phrase was sent."""
    d = _data()
    stats = d.get("stats") or _empty_stats()

    stats["total_sent"] = stats.get("total_sent", 0) + 1

    by_deb: dict = stats.get("by_debater") or {}
    by_deb[debater_id] = by_deb.get(debater_id, 0) + 1
    stats["by_debater"] = by_deb

    by_cat: dict = stats.get("by_category") or {k: 0 for k in _ALL_CATS}
    cat = phrase_result.get("category", "A")
    by_cat[cat] = by_cat.get(cat, 0) + 1
    stats["by_category"] = by_cat

    stats["last_phrase_id"] = phrase_result.get("id", "")
    stats["last_sent_at"] = time.time()

    # FIFO-10
    used: list = stats.get("used_recently", [])
    pid = phrase_result.get("id", "")
    if pid:
        used.append(pid)
    if len(used) > 10:
        used = used[-10:]
    stats["used_recently"] = used

    # New random threshold
    doc = get_doc()
    timing = (doc or {}).get("timing", {})
    lo = timing.get("msg_interval_min", 12)
    hi = timing.get("msg_interval_max", 18)
    stats["next_threshold"] = random.randint(lo, hi)
    stats["last_injection_msg"] = phrase_result.get("at_msg", 0)

    d["stats"] = stats
    _save(d)


# ─── Adaptive weights ────────────────────────────────────────────────────────


def _adaptive_weights(ctx: dict, available: list[str]) -> dict[str, float]:
    """Category weights based on debate metrics. 5 categories (v3)."""
    disagree = ctx.get("disagree_rate", 0.30)
    avg_len = ctx.get("avg_msg_length", 500)
    msg_count = ctx.get("msg_count", 0)
    last_rej = ctx.get("last_rejection", None)

    if disagree < 0.25:
        w = {"A": 0.20, "B": 0.05, "C": 0.45, "D": 0.15, "E": 0.15}
    elif avg_len < 300:
        w = {"A": 0.10, "B": 0.35, "C": 0.10, "D": 0.25, "E": 0.20}
    elif msg_count > 200:
        w = {"A": 0.30, "B": 0.10, "C": 0.10, "D": 0.25, "E": 0.25}
    elif last_rej in ("PERSONA_DRIFT", "TASK_DRIFT"):
        w = {"A": 0.10, "B": 0.25, "C": 0.10, "D": 0.35, "E": 0.20}
    elif last_rej in ("NO_ASSUMPTIONS", "NO_SIMPLICITY_CHECK"):
        w = {"A": 0.10, "B": 0.10, "C": 0.10, "D": 0.20, "E": 0.50}
    else:
        w = {k: 0.20 for k in _ALL_CATS}

    # Zero out missing, normalize
    for k in list(w.keys()):
        if k not in available:
            w[k] = 0.0
    total = sum(w.values())
    if total > 0:
        w = {k: v / total for k, v in w.items()}
    else:
        for k in available:
            w[k] = 1.0 / len(available)
    return w


# ─── Parsers ──────────────────────────────────────────────────────────────────


_TIMING_MSG_RE = re.compile(
    r"каждые\s+(\d+)\s*-\s*(\d+)\s+сообщен", re.IGNORECASE)
_TIMING_MIN_RE = re.compile(
    r"(?:не\s+чаще|минимальн[а-я]*\s+интервал)[^0-9]*(\d+)\s*мин",
    re.IGNORECASE)
_TIMING_MAX_RE = re.compile(
    r"(?:не\s+реже|максимальн[а-я]*\s+интервал)[^0-9]*(\d+)\s*мин",
    re.IGNORECASE)


def parse_motivation_doc(
    text: str,
) -> tuple[dict[str, list[dict]], dict[str, int], int]:
    """Parse CONTEXT_INJECTION_SYSTEM v3 / MOTIVATIONAL_INJECTION_SYSTEM v2.

    Returns (categories, timing, total_phrases).
    """
    categories: dict[str, list[dict]] = {k: [] for k in _ALL_CATS}

    # ── Timing ──
    timing = {
        "msg_interval_min": 12, "msg_interval_max": 18,
        "time_min_minutes": 8, "time_max_minutes": 25,
    }
    m = _TIMING_MSG_RE.search(text)
    if m:
        timing["msg_interval_min"] = int(m.group(1))
        timing["msg_interval_max"] = int(m.group(2))
    m = _TIMING_MIN_RE.search(text)
    if m:
        timing["time_min_minutes"] = int(m.group(1))
    m = _TIMING_MAX_RE.search(text)
    if m:
        timing["time_max_minutes"] = int(m.group(1))

    # ── Phrases: A01..E10 ──
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(r'^([A-E])(\d{2}):\s*"(.*)', line)
        if m:
            cat, num = m.group(1), m.group(2)
            phrase_id = f"{cat}{num}"
            phrase_lines = [m.group(3)]
            source = ""
            i += 1
            while i < len(lines):
                nline = lines[i].strip()
                src_m = re.match(r'\[Источник:\s*(.*?)]\s*$', nline)
                if src_m:
                    source = src_m.group(1).strip().rstrip("]")
                    i += 1
                    break
                if re.match(r'^[A-E]\d{2}:\s*"', nline):
                    break
                if nline == "```":
                    i += 1
                    break
                if nline.startswith("### ") or nline.startswith("## "):
                    break
                if nline.startswith("[Источник:"):
                    src_text = nline
                    while not src_text.rstrip().endswith("]"):
                        i += 1
                        if i >= len(lines):
                            break
                        src_text += " " + lines[i].strip()
                    src_m2 = re.match(r'\[Источник:\s*(.*)]', src_text)
                    if src_m2:
                        source = src_m2.group(1).strip()
                    i += 1
                    break
                phrase_lines.append(nline)
                i += 1

            raw = " ".join(phrase_lines).strip()
            if raw.endswith('"'):
                raw = raw[:-1].strip()
            raw = re.sub(r'\s+', ' ', raw).strip()
            if raw and len(raw) >= 20:
                categories[cat].append(
                    {"id": phrase_id, "text": raw, "source": source})
            continue
        i += 1

    total = sum(len(v) for v in categories.values())
    if total == 0:
        return _parse_legacy(text)
    return categories, timing, total


def _parse_legacy(
    text: str,
) -> tuple[dict[str, list[dict]], dict[str, int], int]:
    """Fallback: simple 'Интервал: N мин / фраза1 / фраза2 ...' format."""
    lines = text.strip().split("\n")
    interval = 30
    phrases: list[dict] = []

    for line in lines:
        low = line.lower().strip()
        m = re.search(r"(\d+)\s*(мин|min|м\.)", low)
        if m:
            interval = int(m.group(1))
            break
        m = re.search(r"интервал[:\s]*(\d+)", low)
        if m:
            interval = int(m.group(1))
            break

    in_phrases = False
    idx = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        if any(kw in low for kw in [
            "интервал", "interval", "---", "каждые",
        ]):
            in_phrases = True
            continue
        if in_phrases or not any(kw in low for kw in [
            "мотивац", "документ", "название",
        ]):
            if len(stripped) >= 10:
                cleaned = re.sub(r"^[\d\.\)\-\*•]+\s*", "", stripped)
                if cleaned:
                    idx += 1
                    phrases.append(
                        {"id": f"L{idx:02d}", "text": cleaned, "source": ""})

    timing = {
        "msg_interval_min": max(1, interval),
        "msg_interval_max": max(1, interval),
        "time_min_minutes": 1, "time_max_minutes": 120,
    }
    cats: dict[str, list[dict]] = {
        k: (phrases[:50] if k == "A" else []) for k in _ALL_CATS
    }
    return cats, timing, len(phrases[:50])


# ─── UI helpers ───────────────────────────────────────────────────────────────


_CAT_NAMES = {
    "A": "Ответственность",
    "B": "Уверенность",
    "C": "Провокация",
    "D": "Честность",
    "E": "Karpathy контекст",
}
_CAT_EMOJI = {"A": "🔴", "B": "🟢", "C": "⚡", "D": "🔵", "E": "🧠"}


def format_doc_summary(doc: dict[str, Any]) -> str:
    """Short summary of the document for Telegram."""
    cats = doc.get("categories", {})
    timing = doc.get("timing", {})
    total = doc.get("total_phrases", 0)

    parts: list[str] = [f"📄 *{doc.get('name', 'Мотивация')}*"]
    parts.append(f"Всего фраз: {total}")
    for k in _ALL_CATS:
        items = cats.get(k, [])
        if items:
            parts.append(
                f"  {_CAT_EMOJI.get(k, '•')} {_CAT_NAMES.get(k, k)}: "
                f"{len(items)} фраз")

    lo = timing.get("msg_interval_min", 12)
    hi = timing.get("msg_interval_max", 18)
    tmin = timing.get("time_min_minutes", 8)
    tmax = timing.get("time_max_minutes", 25)
    parts.append(f"\n⏱ Каждые {lo}-{hi} сообщений")
    parts.append(f"   Мин {tmin} мин / Макс {tmax} мин")
    return "\n".join(parts)


def format_stats_summary() -> str:
    """Motivation stats for Telegram."""
    stats = get_stats()
    total = stats.get("total_sent", 0)
    if total == 0:
        return "📊 Ещё не отправлено ни одной инъекции."

    parts = ["📊 *Статистика мотивации*", f"Всего отправлено: {total}"]

    by_deb = stats.get("by_debater", {})
    if by_deb:
        parts.append("\nПо дебатёрам:")
        for d_id, cnt in by_deb.items():
            parts.append(f"  • {d_id}: {cnt}")

    by_cat = stats.get("by_category", {})
    if by_cat and any(v > 0 for v in by_cat.values()):
        parts.append("\nПо категориям:")
        for k in _ALL_CATS:
            cnt = by_cat.get(k, 0)
            if cnt > 0:
                parts.append(
                    f"  {_CAT_EMOJI.get(k, '•')} "
                    f"{_CAT_NAMES.get(k, k)}: {cnt}")

    last_id = stats.get("last_phrase_id", "")
    if last_id:
        parts.append(f"\nПоследняя: {last_id}")
    return "\n".join(parts)
