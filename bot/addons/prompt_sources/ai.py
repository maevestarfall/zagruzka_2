# Prompt help: keyword extraction + keyword-targeted source search → 4 prompt options.
# Co-authored with CoCo
"""Turn a user's plain-text idea into 4 prompt options sourced from sites.

Output contract (what the photo/video flow consumes):

    {
        "variants": [v1, v2, v3, v4],   # always 4 ready-to-use prompts
        "kinds":    ["site", "site", "site", "hybrid"],
        "note":     "...",              # short status line for the user
    }

The first three are pulled as close to verbatim as possible from the
fetched source material and ranked by popularity/relevance; the fourth is
a hybrid that adapts the best of the three to the user's exact request.
Falls back gracefully to LLM-only generation when no source text is
available (e.g. all sites blocked the fetch).
"""

from __future__ import annotations

import json
import logging
import re

from .._llm import NoBrainError, chat_once
from .fetch import fetch_sources
from .store import get_sources

logger = logging.getLogger(__name__)


def _parse_json_block(text: str) -> dict | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        # Try to locate the first {...} span.
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
    return None


def _coerce_variants(data: dict) -> tuple[list[str], list[str]]:
    raw = data.get("variants") or []
    variants: list[str] = []
    kinds: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            v = str(item.get("prompt") or item.get("text") or "").strip()
            k = str(item.get("kind") or "site").strip().lower()
        else:
            v = str(item).strip()
            k = "site"
        if v:
            variants.append(v)
            kinds.append("hybrid" if k.startswith("hyb") else "site")
    return variants[:4], kinds[:4]


async def _describe_keywords(user_text: str, kind: str) -> str:
    """Ask the LLM to describe the desired prompt and return search keywords.

    Returns a short comma-separated keyword string used to search the
    configured prompt-source sites; '' on any failure (caller falls back
    to the raw user text).
    """
    if not user_text.strip():
        return ""
    try:
        system = (
            "Ты помогаешь искать готовые промпты на сайтах-галереях. По запросу "
            "пользователя опиши, какой это промпт, и выдай 4-8 КЛЮЧЕВЫХ ФРАЗ "
            "(англ., через запятую) для поиска похожих промптов на сайтах. "
            "Только ключевые фразы, без пояснений."
        )
        raw = await chat_once(system, f"Запрос: «{user_text}» (тип: {kind})",
                              max_tokens=120, temperature=0.4)
        return (raw or "").strip().replace("\n", " ")[:300]
    except Exception:  # noqa: BLE001
        return ""


async def prompt_help(keywords: str, *, kind: str) -> dict:
    """Build 4 prompt options for ``keywords`` from configured sources."""
    keywords = (keywords or "").strip()
    sources = get_sources(kind)
    # Step 1: let the LLM describe the prompt and pull search keywords, then
    # search the configured sources BY those keywords (apify/firecrawl/…).
    search_keys = await _describe_keywords(keywords, kind) or keywords
    fetched = await fetch_sources(sources, keywords=search_keys) if sources else []

    kind_desc = {
        "photo": "генерации/редактирования изображения (img2img)",
        "video": "анимации кадра в видеоролик (img2video)",
    }.get(kind, "генерации картинки")

    if fetched:
        corpus_parts = []
        for url, text in fetched:
            corpus_parts.append(f"=== Источник: {url} ===\n{text}")
        corpus = "\n\n".join(corpus_parts)[:14000]
        note_src = ", ".join(u.split("//", 1)[-1].split("/")[0] for u, _ in fetched)
        system = (
            "Ты — помощник по промптам для AI-генерации " + kind_desc + ". "
            "Тебе дают сырой текст со страниц сайтов-галерей готовых промптов "
            "и запрос пользователя. Найди в этом тексте реальные примеры "
            "промптов, максимально подходящие по смыслу к запросу, и отбери "
            "3 самых удачных/популярных — ВЕРБАТИМ (как есть, без переписывания, "
            "только убрав мусор/обрезав до промпта). Затем сделай 4-й — "
            "ГИБРИД: возьми лучшее из этих трёх и адаптируй именно под запрос "
            "пользователя.\n"
            "Верни СТРОГО JSON: {\"variants\":[{\"kind\":\"site\",\"prompt\":\"...\"},"
            "{\"kind\":\"site\",\"prompt\":\"...\"},{\"kind\":\"site\",\"prompt\":\"...\"},"
            "{\"kind\":\"hybrid\",\"prompt\":\"...\"}]}. Промпты — на английском "
            "(как принято для диффузионных моделей). Если в тексте нет "
            "подходящих готовых промптов — собери близкие по стилю варианты "
            "сам и всё равно пометь 4-й как hybrid."
        )
        user_msg = (
            f"Запрос пользователя: «{keywords}»\n\n"
            f"Материал со страниц-источников:\n{corpus}"
        )
        note = f"Источники: {note_src}"
    else:
        system = (
            "Ты — помощник по промптам для AI-генерации " + kind_desc + ". "
            "Дай 4 варианта промпта по запросу пользователя. Первые 3 — в "
            "стиле популярных промптов из галерей (PromptHero/Lexica и т.п.), "
            "4-й — гибрид, максимально заточенный под запрос пользователя.\n"
            "Верни СТРОГО JSON: {\"variants\":[{\"kind\":\"site\",\"prompt\":\"...\"},"
            "{\"kind\":\"site\",\"prompt\":\"...\"},{\"kind\":\"site\",\"prompt\":\"...\"},"
            "{\"kind\":\"hybrid\",\"prompt\":\"...\"}]}. Промпты — на английском."
        )
        user_msg = f"Запрос пользователя: «{keywords}»"
        note = (
            "Источники не открылись — варианты собраны ИИ в стиле галерей."
            if sources
            else "Источники не настроены — варианты собраны ИИ."
        )

    raw = await chat_once(system, user_msg, max_tokens=900, temperature=0.6)
    data = _parse_json_block(raw)
    variants: list[str] = []
    kinds: list[str] = []
    if data:
        variants, kinds = _coerce_variants(data)
    if not variants:
        # Last-resort: split lines.
        lines = [
            re.sub(r'^[-*•\d).\s"]+', "", ln).strip().strip('"')
            for ln in raw.splitlines()
            if ln.strip()
        ]
        variants = [ln for ln in lines if ln][:4]
        kinds = ["site"] * len(variants)
    if not variants:
        raise NoBrainError("Не удалось получить варианты от ИИ.")
    # Ensure last is labelled hybrid when we have a full set.
    while len(kinds) < len(variants):
        kinds.append("site")
    if len(variants) >= 4:
        kinds[3] = "hybrid"
    return {"variants": variants[:4], "kinds": kinds[:4], "note": note}
