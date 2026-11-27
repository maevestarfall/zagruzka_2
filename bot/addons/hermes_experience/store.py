"""Experience-entry storage + document builder for Hermes."""

from __future__ import annotations

import time

from .. import state as addon_state

ADDON = "hermes_experience"
_KEY = "entries"
_MAX_ENTRIES = 500

# Human labels for the features Hermes accumulates experience about.
FEATURE_LABELS = {
    "prompt_sources": "Источники промптов (фото/видео)",
    "osint": "Исследование и поиск человека (OSINT)",
    "media": "Генерация фото/видео",
    "debates": "Дебаты",
    "general": "Общее",
}


def _entries() -> list[dict]:
    val = addon_state.get(ADDON, _KEY, None)
    return list(val) if isinstance(val, list) else []


def record(
    feature: str,
    event: str,
    *,
    outcome: str = "",
    lesson: str = "",
) -> None:
    """Append one experience entry. Silent on any failure (best-effort).

    ``feature``  — bucket key (see FEATURE_LABELS).
    ``event``    — what happened, in plain words ("источники lexica.art не
                   ответили").
    ``outcome``  — "ok" / "fail" / free text.
    ``lesson``   — recommendation drawn from the event.
    """
    try:
        entries = _entries()
        entries.append(
            {
                "ts": int(time.time()),
                "feature": feature or "general",
                "event": (event or "").strip()[:500],
                "outcome": (outcome or "").strip()[:120],
                "lesson": (lesson or "").strip()[:500],
            }
        )
        if len(entries) > _MAX_ENTRIES:
            entries = entries[-_MAX_ENTRIES:]
        addon_state.set_(ADDON, _KEY, entries)
    except Exception:  # noqa: BLE001 — logging experience must never crash a flow
        pass


def list_entries(feature: str | None = None) -> list[dict]:
    entries = _entries()
    if feature:
        entries = [e for e in entries if e.get("feature") == feature]
    return entries


def count() -> int:
    return len(_entries())


def clear() -> int:
    n = count()
    addon_state.set_(ADDON, _KEY, [])
    return n


def _fmt_ts(ts: int) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))
    except Exception:  # noqa: BLE001
        return str(ts)


def build_document() -> str:
    """Compile the accumulated experience into a Markdown instruction doc.

    Pure, deterministic, offline — groups raw events by feature so the
    resulting document is provably based ONLY on what the bot lived
    through, not on anything fetched from the web.
    """
    entries = _entries()
    header = (
        "# Инструкции Гермеса (на основе опыта)\n\n"
        f"Сгенерировано: {_fmt_ts(int(time.time()))} UTC\n"
        f"Всего записей опыта: {len(entries)}\n\n"
        "> Этот документ собран ИСКЛЮЧИТЕЛЬНО из реального опыта работы "
        "бота (что произошло), а не из прочитанного в интернете. "
        "Каждая рекомендация привязана к конкретному событию.\n"
    )
    if not entries:
        tail = ""
        try:
            from ..instructions import important as _imp
            n = _imp.directive()
            if n.strip():
                tail = ("\n\n## ❗ВАЖНЫЕ ВЫВОДЫ ВЛАДЕЛЬЦА (обязательно учитывать)\n"
                        "Сверяйся с ними и ПРЕДЛАГАЙ связки перед правками.\n" + n)
        except Exception:  # noqa: BLE001
            pass
        return header + "\n_Опыта пока не накоплено. Возвращайся позже._\n" + tail

    # Group by feature, preserving FEATURE_LABELS order then extras.
    buckets: dict[str, list[dict]] = {}
    for e in entries:
        buckets.setdefault(e.get("feature", "general"), []).append(e)

    ordered = [k for k in FEATURE_LABELS if k in buckets]
    ordered += [k for k in buckets if k not in FEATURE_LABELS]

    parts = [header]
    for feat in ordered:
        label = FEATURE_LABELS.get(feat, feat)
        items = buckets[feat]
        parts.append(f"\n## {label}\n")
        # Distil: aggregate identical lessons, keep latest examples.
        lessons: dict[str, int] = {}
        for e in items:
            les = e.get("lesson") or ""
            if les:
                lessons[les] = lessons.get(les, 0) + 1
        if lessons:
            parts.append("**Выводы (чем чаще — тем выше):**\n")
            for les, cnt in sorted(lessons.items(), key=lambda kv: -kv[1]):
                parts.append(f"- {les} _(подтверждено {cnt}×)_")
            parts.append("")
        parts.append("**Журнал событий:**\n")
        for e in items[-25:]:
            oc = f" — `{e['outcome']}`" if e.get("outcome") else ""
            parts.append(f"- [{_fmt_ts(e.get('ts', 0))}]{oc} {e.get('event', '')}")
        parts.append("")
    # «❗Важно» — выводы владельца. Гермес обязан их учитывать при поиске
    # проблем и ПРЕДЛАГАТЬ, как связать механизмы, ПРЕЖДЕ чем что-то менять.
    try:
        from ..instructions import important as _imp
        notes = _imp.directive()
        if notes.strip():
            parts.append(
                "\n## ❗ВАЖНЫЕ ВЫВОДЫ ВЛАДЕЛЬЦА (обязательно учитывать)\n"
                "Гермес: при поиске проблем сверяйся с этими выводами и "
                "СНАЧАЛА ПРЕДЛАГАЙ, как связать механизмы в проекте, и только "
                "после одобрения — правь. Не меняй ничего молча.\n"
            )
            parts.append(notes)
    except Exception:  # noqa: BLE001 — never break the doc
        pass
    return "\n".join(parts)
