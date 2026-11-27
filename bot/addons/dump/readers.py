# Dump readers: extract text from mixed content incl. .mhtml/.htm web archives.
# Co-authored with CoCo
"""Read whatever the user threw into the bin, reusing the bot's existing
capabilities (markitdown, Groq Whisper/Vision, Firecrawl/Apify, OCR).

The whole point of «Скинуть весь мусор»: the user combines functions the
bot already has so it can actually *read* mixed content — a video gets
screenshotted / markdowned / scraped, a PDF gets converted, a link gets
fetched, a voice gets transcribed — and everything is merged into one
context the LLM answers from.

Every reader is best-effort and never raises: on failure it returns a
short human note so the rest of the batch still goes through.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from typing import Any

logger = logging.getLogger(__name__)

# Keep each extracted blob bounded so a huge transcript can't blow the
# LLM context window when many items are combined.
_PER_ITEM_LIMIT = 12000


def _cut(text: str, limit: int = _PER_ITEM_LIMIT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[обрезано, всего {len(text)} символов]"


async def _download_tmp(bot: Any, file_id: str, suffix: str) -> str:
    """Download a Telegram file into a temp path (reuses groq helper)."""
    from ...groq_handlers import _download_to_tempfile

    return await _download_to_tempfile(bot, file_id, suffix or "")


async def _markitdown(arg: str) -> tuple[bool, str]:
    """Run markitdown on a URL *or* local path (CLI accepts both)."""
    from ...wizard import _run_markitdown

    return await _run_markitdown(arg)


async def _read_url(item: dict[str, Any]) -> str:
    url = (item.get("text") or "").strip()
    if not url:
        return ""
    # 1) markitdown (handles YouTube/Vimeo transcripts, PDFs, articles…)
    with contextlib.suppress(Exception):
        ok, body = await _markitdown(url)
        if ok and body.strip():
            return _cut(f"Источник {url}:\n{body}")
    # 2) Firecrawl scrape → markdown
    with contextlib.suppress(Exception):
        from ...external_tools import firecrawl_scrape

        body = await firecrawl_scrape(url)
        if body and not body.startswith("ERROR") and "requires" not in body[:40]:
            return _cut(body)
    # 3) Tavily search on the URL as a last resort
    with contextlib.suppress(Exception):
        from ...external_tools import tavily_search

        body = await tavily_search(url)
        if body and not body.startswith("ERROR"):
            return _cut(body)
    return f"Ссылка (не удалось прочитать автоматически): {url}"


async def _read_document(bot: Any, item: dict[str, Any]) -> str:
    name = item.get("file_name") or "файл"
    fid = item.get("file_id")
    suffix = item.get("suffix") or os.path.splitext(name)[1] or ""
    if not fid:
        return f"Документ: {name}"
    # .mhtml / .htm / .html — markitdown часто не вытягивает MHTML-архив,
    # поэтому читаем их сами (stdlib): MIME-распаковка + снятие тегов.
    low = (suffix or "").lower()
    if low in (".mhtml", ".mht", ".htm", ".html"):
        path = None
        try:
            path = await _download_tmp(bot, fid, suffix)
            with open(path, "rb") as _raw_fh:
                raw = _raw_fh.read()
            if low in (".mhtml", ".mht"):
                text = _mhtml_to_text(raw)
            else:
                text = _html_to_text(raw.decode("utf-8", "ignore"))
            if text.strip():
                return _cut(f"Документ {name} (HTML→текст):\n{text}")
            # фоллбэк на markitdown, если своё не сработало
            ok, body = await _markitdown(path)
            if ok and body.strip():
                return _cut(f"Документ {name}:\n{body}")
            return f"Документ {name}: HTML без читаемого текста."
        except Exception as exc:  # noqa: BLE001
            logger.exception("dump: html/mhtml read failed")
            return f"Документ {name}: ошибка чтения HTML ({exc})."
        finally:
            if path:
                with contextlib.suppress(Exception):
                    os.unlink(path)
    path = None
    try:
        path = await _download_tmp(bot, fid, suffix)
        ok, body = await _markitdown(path)
        if ok and body.strip():
            return _cut(f"Документ {name}:\n{body}")
        return f"Документ {name}: не удалось извлечь текст ({body[:200]})."
    except Exception as exc:  # noqa: BLE001
        logger.exception("dump: document read failed")
        return f"Документ {name}: ошибка чтения ({exc})."
    finally:
        if path:
            with contextlib.suppress(Exception):
                os.unlink(path)


def _html_to_text(html_str: str) -> str:
    """Strip tags/scripts/styles from HTML → plain text (stdlib only)."""
    import re
    from html import unescape
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", html_str)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</(p|div|tr|li|h[1-6])>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = unescape(s)
    # collapse whitespace, keep line breaks
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _mhtml_to_text(raw: bytes) -> str:
    """Unpack an MHTML web-archive (MIME) and extract its HTML part → text."""
    import email
    msg = email.message_from_bytes(raw)
    html_parts: list[str] = []
    text_parts: list[str] = []
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        if ctype == "text/html":
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            html_parts.append(payload.decode(charset, "ignore"))
        elif ctype == "text/plain":
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text_parts.append(payload.decode(charset, "ignore"))
    if html_parts:
        return _html_to_text("\n".join(html_parts))
    if text_parts:
        return "\n".join(text_parts)
    # not a multipart MHTML — try as raw html
    return _html_to_text(raw.decode("utf-8", "ignore"))


async def _read_photo(bot: Any, item: dict[str, Any]) -> str:
    fid = item.get("file_id")
    caption = (item.get("text") or "").strip()
    if not fid:
        return ""
    instruction = caption or (
        "Опиши подробно, что на изображении, и извлеки весь видимый текст."
    )
    # 1) Groq Vision (if a vision-capable Groq slot is configured)
    with contextlib.suppress(Exception):
        import io

        from ...groq_helpers import describe_image_with_groq, should_use_groq_override

        if should_use_groq_override():
            buf = io.BytesIO()
            await bot.download(fid, destination=buf)
            answer = await describe_image_with_groq(buf.getvalue(), instruction)
            if answer and answer.strip():
                return _cut(f"Фото: {answer}")
    # 2) Local OCR fallback (pytesseract)
    path = None
    try:
        path = await _download_tmp(bot, fid, ".jpg")
        text = _ocr(path)
        if text:
            return _cut(f"Фото (распознанный текст):\n{text}")
    except Exception:  # noqa: BLE001
        logger.debug("dump: photo OCR fallback failed", exc_info=True)
    finally:
        if path:
            with contextlib.suppress(Exception):
                os.unlink(path)
    return "Фото: добавлено (не удалось прочитать содержимое автоматически)."


def _ocr(path: str) -> str:
    try:
        import pytesseract
        from PIL import Image

        return (pytesseract.image_to_string(Image.open(path)) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


async def _read_audio(bot: Any, item: dict[str, Any]) -> str:
    fid = item.get("file_id")
    suffix = item.get("suffix") or ".ogg"
    if not fid:
        return ""
    path = None
    try:
        path = await _download_tmp(bot, fid, suffix)
        from ...groq_helpers import transcribe_audio_with_groq

        transcript = await transcribe_audio_with_groq(path)
        if transcript and transcript.strip():
            return _cut(f"Аудио (расшифровка):\n{transcript}")
        return "Аудио: добавлено (пустая расшифровка)."
    except Exception as exc:  # noqa: BLE001
        logger.exception("dump: audio read failed")
        return f"Аудио: не удалось расшифровать ({exc})."
    finally:
        if path:
            with contextlib.suppress(Exception):
                os.unlink(path)


def _grab_frame(video_path: str) -> str | None:
    """Grab a middle keyframe to a temp JPEG (best-effort, needs cv2)."""
    try:
        import cv2  # type: ignore

        cap = cv2.VideoCapture(video_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, total // 2)
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return None
        fd, out = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        cv2.imwrite(out, frame)
        return out
    except Exception:  # noqa: BLE001
        return None


async def _read_video(bot: Any, item: dict[str, Any]) -> str:
    """Read a video by combining functions: markitdown → keyframe+Vision/OCR.

    Per the user's spec: a thrown video should be screenshotted, markdowned
    or scraped so the bot can actually read it.
    """
    name = item.get("file_name") or "видео"
    fid = item.get("file_id")
    caption = (item.get("text") or "").strip()
    if not fid:
        return f"Видео: {name}"
    path = None
    parts: list[str] = []
    if caption:
        parts.append(f"Подпись: {caption}")
    try:
        path = await _download_tmp(bot, fid, item.get("suffix") or ".mp4")
        # 1) markitdown on the file (metadata / any embedded text/transcript)
        with contextlib.suppress(Exception):
            ok, body = await _markitdown(path)
            if ok and body.strip() and len(body.strip()) > 30:
                parts.append(f"Markitdown:\n{body}")
        # 2) keyframe screenshot → Vision / OCR
        frame = _grab_frame(path)
        if frame:
            shot = await _vision_or_ocr_local(bot, frame, caption)
            with contextlib.suppress(Exception):
                os.unlink(frame)
            if shot:
                parts.append(f"Кадр из видео: {shot}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("dump: video read failed")
        parts.append(f"(ошибка чтения видео: {exc})")
    finally:
        if path:
            with contextlib.suppress(Exception):
                os.unlink(path)
    if not parts:
        return (
            f"Видео {name}: добавлено. Не удалось прочитать автоматически — "
            "можно подключить Apify-актор для скрейпинга видео-ссылок."
        )
    return _cut(f"Видео {name}:\n" + "\n".join(parts))


async def _vision_or_ocr_local(bot: Any, image_path: str, caption: str) -> str:
    instruction = (
        "Это кадр из видео. Опиши, что происходит, и извлеки весь видимый текст."
    )
    with contextlib.suppress(Exception):
        from ...groq_helpers import describe_image_with_groq, should_use_groq_override

        if should_use_groq_override():
            with open(image_path, "rb") as fh:
                data = fh.read()
            answer = await describe_image_with_groq(data, instruction)
            if answer and answer.strip():
                return answer.strip()
    text = _ocr(image_path)
    return text or ""


async def read_item(bot: Any, item: dict[str, Any]) -> str:
    """Read a single bin item into text, dispatching by kind. Never raises."""
    from .store import (
        KIND_ANIMATION,
        KIND_AUDIO,
        KIND_DOCUMENT,
        KIND_PHOTO,
        KIND_TEXT,
        KIND_URL,
        KIND_VIDEO,
        KIND_VOICE,
    )

    kind = item.get("kind", KIND_TEXT)
    try:
        if kind == KIND_TEXT:
            return _cut(item.get("text") or "")
        if kind == KIND_URL:
            return await _read_url(item)
        if kind == KIND_DOCUMENT:
            return await _read_document(bot, item)
        if kind == KIND_PHOTO:
            return await _read_photo(bot, item)
        if kind in (KIND_VIDEO, KIND_ANIMATION):
            return await _read_video(bot, item)
        if kind in (KIND_AUDIO, KIND_VOICE):
            return await _read_audio(bot, item)
        return _cut(item.get("text") or str(item))
    except Exception as exc:  # noqa: BLE001
        logger.exception("dump: read_item failed for kind=%s", kind)
        return f"[{kind}] не удалось прочитать: {exc}"


async def read_all(bot: Any, items: list[dict[str, Any]]) -> str:
    """Read every item and assemble a single labelled context block."""
    chunks: list[str] = []
    for i, item in enumerate(items, 1):
        body = await read_item(bot, item)
        if body and body.strip():
            chunks.append(f"--- материал #{i} ---\n{body.strip()}")
    return "\n\n".join(chunks)
