# Prompt-source fetcher: cascade markitdown→firecrawl→httpx→tavily per URL.
# Co-authored with CoCo
"""Best-effort fetcher that pulls visible text from prompt-source pages.

Kept deliberately small and forgiving: short timeouts, HTML stripped to
plain text, every error swallowed so one dead site never breaks the
flow. The extracted text is handed to the LLM, which mines real example
prompts out of it.
"""

from __future__ import annotations

import asyncio
import logging
import re

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n\s*\n+")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def _strip_html(html_text: str) -> str:
    html_text = _SCRIPT_RE.sub(" ", html_text)
    text = _TAG_RE.sub(" ", html_text)
    # Unescape a few common entities cheaply.
    for a, b in (("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"),
                 ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
        text = text.replace(a, b)
    text = _WS_RE.sub(" ", text)
    text = _BLANK_RE.sub("\n", text)
    return text.strip()


async def _via_markitdown(url: str, max_chars: int) -> str:
    try:
        from ...wizard import _run_markitdown
        ok, body = await _run_markitdown(url)
        if ok and body.strip():
            return body[:max_chars]
    except Exception:  # noqa: BLE001
        pass
    return ""


async def _via_firecrawl(url: str, max_chars: int) -> str:
    try:
        from ...external_tools import firecrawl_scrape
        body = await firecrawl_scrape(url)
        if body and not body.startswith("ERROR") and "requires" not in body[:40]:
            return body[:max_chars]
    except Exception:  # noqa: BLE001
        pass
    return ""


async def _via_httpx(url: str, *, timeout: float, max_chars: int) -> str:
    import httpx
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=_HEADERS
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return _strip_html(resp.text)[:max_chars]
    except Exception as exc:  # noqa: BLE001
        logger.info("prompt-source httpx fetch failed for %s: %s", url, exc)
        return ""


async def _via_tavily(url: str, max_chars: int) -> str:
    try:
        from ...external_tools import tavily_search
        body = await tavily_search(url, depth="basic", max_results=3)
        if body and not body.startswith("ERROR") and "не настроен" not in body.lower():
            return body[:max_chars]
    except Exception:  # noqa: BLE001
        pass
    return ""


async def _via_apify(url: str, keywords: str, max_chars: int) -> str:
    """Scrape a source site by keywords via a configured Apify actor.

    Actor id is read from ``prompt_sources.apify_actor`` in state; if unset
    or the call fails, returns '' so the cascade falls through.
    """
    if not keywords.strip():
        return ""
    try:
        from .. import state as addon_state
        actor = str(addon_state.get("prompt_sources", "apify_actor", "") or "").strip()
        if not actor:
            return ""
        from ...external_tools import apify_run_actor
        body = await apify_run_actor(actor, {
            "startUrls": [{"url": url}],
            "search": keywords,
            "query": keywords,
            "maxItems": 10,
        })
        if body and not str(body).startswith("ERROR"):
            return str(body)[:max_chars]
    except Exception:  # noqa: BLE001
        return ""
    return ""


async def _fetch_one(url: str, *, timeout: float, max_chars: int, keywords: str = "") -> str:
    """Cascade: apify(by keywords) → markitdown → firecrawl → httpx → tavily.

    Picks the first method that returns usable text, so JS-heavy galleries
    and PDFs are read properly instead of an empty HTML shell.
    """
    for fn in (
        lambda: _via_apify(url, keywords, max_chars),
        lambda: _via_markitdown(url, max_chars),
        lambda: _via_firecrawl(url, max_chars),
        lambda: _via_httpx(url, timeout=timeout, max_chars=max_chars),
        lambda: _via_tavily(url, max_chars),
    ):
        text = await fn()
        if text and text.strip():
            return text
    return ""


async def fetch_sources(
    urls: list[str],
    *,
    keywords: str = "",
    timeout: float = 6.0,
    max_chars_per_site: int = 4000,
    max_sites: int = 4,
) -> list[tuple[str, str]]:
    """Fetch up to ``max_sites`` URLs concurrently.

    ``keywords`` (optional) drives keyword-targeted scraping of each source
    site via the Apify actor when one is configured. Returns a list of
    ``(url, text)`` for sites that returned usable content.
    """
    picked = [u for u in urls if u][:max_sites]
    if not picked:
        return []
    results = await asyncio.gather(
        *(_fetch_one(u, timeout=timeout, max_chars=max_chars_per_site, keywords=keywords)
          for u in picked)
    )
    return [(u, t) for u, t in zip(picked, results) if t.strip()]
