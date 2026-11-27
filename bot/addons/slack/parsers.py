"""Normalisation + rendering helpers for the Slack connector.

Turns a list of :class:`SlackMessage` (top-level + thread replies) into:
* a human-readable Markdown dump with explicit thread nesting,
* a CSV table (author, time, text, relation, attachments),
* short Telegram cards,
and parses a free-form user prompt into an action intent.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

from .models import SlackFile, SlackMessage


# ---- time / text helpers -------------------------------------------------


def _fmt_dt(msg: SlackMessage) -> str:
    if msg.datetime is None:
        return msg.ts
    try:
        return msg.datetime.strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return str(msg.datetime)


def _clean_text(text: str) -> str:
    """Light cleanup of Slack mrkdwn: keep it readable, drop noisy markup."""
    if not text:
        return ""
    t = text
    # <@U123> mentions and <#C123|name> channel refs -> readable-ish
    t = re.sub(r"<#[A-Z0-9]+\|([^>]+)>", r"#\1", t)
    t = re.sub(r"<@([A-Z0-9]+)>", r"@\1", t)
    # <http://url|label> -> label (url)
    t = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", t)
    t = re.sub(r"<(https?://[^>]+)>", r"\1", t)
    t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return t.strip()


def _files_label(files: list[SlackFile]) -> str:
    if not files:
        return ""
    return "; ".join(f"{f.name} ({f.size} B)" if f.size else f.name for f in files)


# ---- collectors ----------------------------------------------------------


def iter_all_messages(messages: list[SlackMessage]):
    """Yield (message, is_reply, parent_ts) over top-level msgs and their replies."""
    for m in messages:
        yield m, False, ""
        for r in m.replies:
            yield r, True, m.ts


def collect_files(messages: list[SlackMessage], only_zip: bool = False) -> list[tuple[SlackMessage, SlackFile]]:
    """All (message, file) pairs across the conversation, incl. thread replies."""
    out: list[tuple[SlackMessage, SlackFile]] = []
    for m, _is_reply, _parent in iter_all_messages(messages):
        for f in m.files:
            if only_zip and not f.is_zip:
                continue
            out.append((m, f))
    return out


# ---- renderers -----------------------------------------------------------


def render_markdown(channel_label: str, messages: list[SlackMessage]) -> str:
    """Readable dump with explicit thread nesting (parent vs reply)."""
    lines: list[str] = [f"# Переписка Slack — {channel_label}", ""]
    total = sum(1 + len(m.replies) for m in messages)
    lines.append(f"_Всего сообщений: {total} (верхнеуровневых: {len(messages)})_")
    lines.append("")
    for m in messages:
        author = m.author_name or m.author_id or "—"
        who = "🟢 Я" if m.is_me else f"🔵 {author}"
        lines.append(f"### {who} · {_fmt_dt(m)}")
        body = _clean_text(m.text) or "_(без текста)_"
        lines.append(body)
        if m.files:
            for f in m.files:
                tag = "📦" if f.is_zip else "📎"
                lines.append(f"- {tag} **{f.name}** — {f.url_private or f.permalink}")
        if m.replies:
            lines.append(f"\n  _↳ ответов в треде: {len(m.replies)}_")
            for r in m.replies:
                rauthor = r.author_name or r.author_id or "—"
                rwho = "Я" if r.is_me else rauthor
                rbody = _clean_text(r.text) or "_(без текста)_"
                lines.append(f"  - **↳ {rwho}** · {_fmt_dt(r)}: {rbody}")
                for f in r.files:
                    tag = "📦" if f.is_zip else "📎"
                    lines.append(f"    - {tag} {f.name} — {f.url_private or f.permalink}")
        lines.append("")
    return "\n".join(lines)


def render_csv(messages: list[SlackMessage]) -> str:
    """CSV table: author, time, text, relation (основное/ответ), attachments."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Автор", "Направление", "Время", "Текст", "Тип", "К сообщению (ts)", "Вложения", "Имена файлов"])
    for m, is_reply, parent in iter_all_messages(messages):
        names = "; ".join(f.name for f in m.files)
        w.writerow([
            m.author_name or m.author_id or "",
            "я писал" if m.is_me else "мне писали",
            _fmt_dt(m),
            _clean_text(m.text),
            "ответ" if is_reply else "основное",
            parent if is_reply else m.ts,
            _files_label(m.files),
            names,
        ])
    return buf.getvalue()


def render_telegram_cards(messages: list[SlackMessage], limit: int = 10) -> list[str]:
    """Short HTML cards for inline display (first ``limit`` top-level msgs)."""
    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    cards: list[str] = []
    for m in messages[:limit]:
        author = esc(m.author_name or m.author_id or "—")
        who = "🟢 Я" if m.is_me else f"🔵 {author}"
        body = esc(_clean_text(m.text))[:1500]
        parts = [f"<b>{who}</b> · <i>{_fmt_dt(m)}</i>", body or "<i>(без текста)</i>"]
        if m.files:
            parts.append("📎 " + esc(_files_label(m.files)))
        if m.replies:
            parts.append(f"<i>↳ ответов: {len(m.replies)}</i>")
            for r in m.replies[:5]:
                rauthor = esc(r.author_name or r.author_id or "—")
                rbody = esc(_clean_text(r.text))[:400]
                parts.append(f"   ↳ <b>{rauthor}</b>: {rbody}")
        cards.append("\n".join(parts))
    return cards


# ---- filtering -----------------------------------------------------------


def filter_messages(
    messages: list[SlackMessage],
    *,
    author: str | None = None,
    keyword: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[SlackMessage]:
    """Filter top-level messages (replies stay attached to surviving parents)."""
    def match(m: SlackMessage) -> bool:
        if author:
            a = (m.author_name or m.author_id or "").lower()
            if author.lower() not in a:
                return False
        if keyword and keyword.lower() not in (m.text or "").lower():
            return False
        if m.datetime is not None:
            day = m.datetime.strftime("%Y-%m-%d")
            if date_from and day < date_from:
                return False
            if date_to and day > date_to:
                return False
        return True

    return [m for m in messages if match(m)]


# ---- prompt intent parsing ----------------------------------------------


@dataclass
class PromptIntent:
    want_table: bool = False        # выгрузить переписку таблицей/разбором
    want_download: bool = False     # скачать вложения
    only_zip: bool = False          # только zip-файлы
    want_code_map: bool = False     # сопоставить функции с кодом (promt-разбор)
    raw: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.want_table or self.want_download or self.want_code_map)


_TABLE_WORDS = ("таблиц", "разбор", "вынут", "выгруз", "выгрузк", "переписк", "сообщени", "table", "dump")
_DOWNLOAD_WORDS = ("скач", "download", "вложен", "файл", "хранилищ", "сохрани", "выгрузи файл")
_ZIP_WORDS = ("zip", "зип", "архив")
_CODE_WORDS = ("код", "функци", "сопоставл", "анализ кода", "code")


def parse_prompt(text: str) -> PromptIntent:
    """Heuristic intent parser for the free-form «Вставить промт» field."""
    t = (text or "").lower()
    intent = PromptIntent(raw=text or "")
    if any(w in t for w in _TABLE_WORDS):
        intent.want_table = True
    has_zip_word = any(w in t for w in _ZIP_WORDS)
    if any(w in t for w in _DOWNLOAD_WORDS) or has_zip_word:
        intent.want_download = True
    if has_zip_word:
        intent.only_zip = True
    # "only-zip" only makes sense if the user didn't ask for all files/attachments
    if intent.only_zip and ("все файл" in t or "все вложен" in t or "вложения" in t):
        intent.only_zip = False
    if any(w in t for w in _CODE_WORDS):
        intent.want_code_map = True
        intent.want_table = True  # code-map implies a table output
    return intent
