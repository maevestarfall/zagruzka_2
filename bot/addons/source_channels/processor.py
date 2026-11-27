"""Process the captured buffer: classify every pending post, route
money/keys into the encrypted Vault, execute instructions, and build a
short report for the owner.

Used by both the manual «🔍 Проверить сейчас» button and the background
scheduler. Never raises — returns a human-readable report string.
"""

from __future__ import annotations

import logging
import time

from . import classify, store

logger = logging.getLogger(__name__)


def _save_to_vault(title: str, money_kind: str, text: str, source_title: str) -> None:
    """Append an encrypted entry into the existing Vault addon store."""
    try:
        from ..vault.handlers import encrypt_data, push_entry
    except Exception as exc:  # noqa: BLE001
        logger.warning("source_channels: vault unavailable: %s", exc)
        return

    if money_kind == "api_key":
        category = "api_key"
        secrets = {"provider": "из источника", "api_key": text.strip()}
    else:
        category = "card"
        secrets = {"raw": text.strip()}

    try:
        cipher_text = encrypt_data(secrets)
    except Exception as exc:  # noqa: BLE001
        logger.warning("source_channels: encrypt failed: %s", exc)
        return

    # push_entry also records the audit fingerprint in the vault log,
    # so every encryption that enters the safe is tracked in one place.
    push_entry({
        "id": f"vault_{int(time.time() * 1000)}",
        "title": title[:40] or "Из источника",
        "category": category,
        "cipher_text": cipher_text,
        "source": "bot",
        "source_info": f"канал-источник: {source_title}"[:80],
        "is_working": None,
    })


async def _execute_instruction(text: str) -> str:
    """Best-effort execution of an instruction via the bot's LLM brain.

    Returns a short result line. Real side-effecting automation stays
    owner-gated; here we produce the AI's action/answer and report it.
    """
    try:
        from .._llm import chat_once, NoBrainError
    except Exception:  # noqa: BLE001
        return "ИИ-мозг недоступен — инструкция сохранена для ручного запуска."
    try:
        out = await chat_once(
            "Ты — исполнитель инструкций бота. Выполни инструкцию владельца "
            "и верни краткий результат/план действий на русском (до 5 строк).",
            text[:1500],
            max_tokens=400,
            temperature=0.3,
        )
        return (out or "").strip()[:600] or "(пустой ответ ИИ)"
    except NoBrainError:
        return "ИИ-мозг не настроен — инструкция сохранена."
    except Exception as exc:  # noqa: BLE001
        logger.warning("source_channels: instruction exec failed: %s", exc)
        return f"Ошибка исполнения: {exc}"


async def _ingest_sites() -> int:
    """Fetch text from every website source and add it to the buffer.

    Reuses the existing prompt-source fetcher (markitdown→firecrawl→httpx→
    tavily). Best-effort: a dead site never breaks the check.
    """
    sites = store.get_site_sources()
    if not sites:
        return 0
    try:
        from ..prompt_sources.fetch import fetch_sources
    except Exception as exc:  # noqa: BLE001
        logger.warning("source_channels: site fetcher unavailable: %s", exc)
        return 0
    urls = [s.get("url", "") for s in sites if s.get("url")]
    by_url = {s.get("url"): s for s in sites}
    added = 0
    try:
        results = await fetch_sources(urls, max_sites=len(urls) or 1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("source_channels: fetch_sources failed: %s", exc)
        return 0
    for url, text in results:
        if not text.strip():
            continue
        src = by_url.get(url, {})
        store.add_to_buffer({
            "source_id": src.get("id"),
            "source_title": src.get("title") or url,
            "chat_id": None,
            "url": url,
            "kind": "site",
            "text": text[:4000],
            "caption": "",
        })
        added += 1
    return added


async def process_pending() -> dict:
    """Classify & route every pending buffer item.

    First pulls fresh content from website sources, then processes the
    whole buffer. Returns a dict with counts and a report string.
    """
    await _ingest_sites()
    buf = store.get_buffer()
    pending = [i for i in buf if not i.get("processed")]
    result = {
        "processed": 0,
        "counts": {c: 0 for c in store.ALL_CATEGORIES},
        "instructions": [],
        "vaulted": 0,
        "report": "",
    }
    if not pending:
        result["report"] = "📭 Новых сообщений в источниках нет."
        return result

    for item in pending:
        text = item.get("text") or item.get("caption") or ""
        has_media = item.get("kind") in ("photo", "video", "animation", "document")
        try:
            verdict = await classify.classify(text, has_media=has_media)
        except Exception as exc:  # noqa: BLE001
            logger.warning("classify crashed: %s", exc)
            verdict = {"category": store.CAT_CHECK, "is_instruction": False,
                       "summary": text[:80], "money_kind": ""}

        cat = verdict["category"]
        item["category"] = cat
        item["summary"] = verdict.get("summary", "")
        item["processed"] = True
        item["processed_ts"] = int(time.time())
        result["counts"][cat] += 1
        result["processed"] += 1

        if cat == store.CAT_MONEY:
            _save_to_vault(
                title=verdict.get("summary", "") or "Из источника",
                money_kind=verdict.get("money_kind", "") or "card",
                text=text,
                source_title=item.get("source_title", "?"),
            )
            item["vaulted"] = True
            result["vaulted"] += 1

        if verdict.get("is_instruction") and text.strip():
            out = await _execute_instruction(text)
            item["instruction_result"] = out
            result["instructions"].append({"text": text[:80], "result": out})

    store.save_buffer(buf)
    result["report"] = _build_report(result)
    return result


def _build_report(result: dict) -> str:
    c = result["counts"]
    lines = [
        "🔌 <b>Проверка каналов-источников завершена</b>",
        f"Обработано сообщений: <b>{result['processed']}</b>",
        "",
        f"{store.CATEGORY_LABEL[store.CAT_CHECK]}: <b>{c[store.CAT_CHECK]}</b>",
        f"{store.CATEGORY_LABEL[store.CAT_MEDIA]}: <b>{c[store.CAT_MEDIA]}</b>",
        f"{store.CATEGORY_LABEL[store.CAT_CODE]}: <b>{c[store.CAT_CODE]}</b>",
        f"{store.CATEGORY_LABEL[store.CAT_MONEY]}: <b>{c[store.CAT_MONEY]}</b>"
        + (f" → 🔒 в сейф: {result['vaulted']}" if result["vaulted"] else ""),
    ]
    if result["instructions"]:
        lines.append("")
        lines.append("📋 <b>Инструкции — ИИ-разбор (требуют подтверждения):</b>")
        for ins in result["instructions"][:5]:
            lines.append(f"• <i>{_esc(ins['text'])}</i>\n  → {_esc(ins['result'][:200])}")
    return "\n".join(lines)


def _esc(s: str) -> str:
    import html

    return html.escape(s or "")
