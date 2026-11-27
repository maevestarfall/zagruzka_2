# Research helper for regen: routes through Analyzer PRO with a Tavily fallback.
# Co-authored with CoCo
"""«Узнать как решают люди» — research helper for Регенерация.

Mason's architecture: the coder gathers information **through Analyzer
PRO** (the Analyzer-Boss server, which has many baked-in search methods),
not through a single raw web query. We therefore:

1. If an Analyzer PRO server is connected (addon 🔬), submit the problem
   as an analysis task, poll until done, and return its structured report.
2. If no server is configured / it errors / times out, fall back to the
   bot's own Tavily web search so research never blocks the fix.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

__all__ = ["research_problem"]

# Bounded so regen never hangs waiting on a long analysis.
_PRO_POLL_INTERVAL = 3.0
_PRO_MAX_WAIT = 90.0
_PRO_BUDGET_TOKENS = 60_000


def _query_from_problem(problem: str) -> str:
    q = " ".join((problem or "").split())
    for marker in ("Error:", "Error", "error", "NameError", "ModuleNotFoundError"):
        idx = q.find(marker)
        if idx != -1:
            return q[idx : idx + 160]
    return q[:160]


async def _research_via_analyzer_pro(problem: str) -> str:
    """Try Analyzer PRO; return digest text or '' if unavailable/failed."""
    try:
        from ..analyzer.handlers import _active_server, _fetch, _fetch_with_retry
    except Exception:  # noqa: BLE001
        return ""
    svc = _active_server()
    if not svc:
        return ""
    task_prompt = (
        "Найди, как разработчики решают эту проблему в Python/aiogram-боте, "
        "и предложи рабочее решение. Проблема:\n\n" + problem.strip()
    )
    try:
        code, body = await _fetch_with_retry(
            svc, "POST", "/user/analysis-request",
            json_body={
                "text": task_prompt,
                "budget_tokens": _PRO_BUDGET_TOKENS,
                "priority": "high",
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("regen: analyzer submit failed: %s", exc)
        return ""
    if code != 200 or not isinstance(body, dict) or not body.get("task_id"):
        return ""
    task_id = str(body["task_id"])

    # Poll until terminal or timeout.
    waited = 0.0
    status = (body.get("status") or "queued").lower()
    while status not in ("completed", "failed", "cancelled") and waited < _PRO_MAX_WAIT:
        await asyncio.sleep(_PRO_POLL_INTERVAL)
        waited += _PRO_POLL_INTERVAL
        try:
            scode, sbody = await _fetch(svc, "GET", "/user/status", params={"task_id": task_id})
        except Exception:  # noqa: BLE001
            break
        if scode == 200 and isinstance(sbody, dict):
            status = (sbody.get("status") or status).lower()
    if status != "completed":
        return ""
    try:
        rcode, rbody = await _fetch_with_retry(svc, "GET", f"/sessions/{task_id}/result")
    except Exception:  # noqa: BLE001
        return ""
    if rcode != 200:
        return ""
    if isinstance(rbody, dict):
        text = rbody.get("report_md") or rbody.get("summary") or rbody.get("report") or ""
    else:
        text = str(rbody)
    return (text or "").strip()[:4000]


async def _research_via_tavily(problem: str, *, max_results: int) -> str:
    query = _query_from_problem(problem)
    if not query.strip():
        return ""
    try:
        from ...external_tools import tavily_search

        text = await tavily_search(
            f"python aiogram telegram bot {query} fix solution",
            depth="basic",
            max_results=max_results,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("regen research (tavily) failed: %s", exc)
        return ""
    if not text or text.startswith("ERROR") or "не настроен" in text.lower():
        return ""
    return text[:2500]


async def research_problem(problem: str, *, max_results: int = 5) -> str:
    """Return a research digest. Analyzer PRO first, Tavily as fallback."""
    if not (problem or "").strip():
        return ""
    # 1) Prefer Analyzer PRO (its baked-in search methods).
    with_pro = ""
    try:
        with_pro = await _research_via_analyzer_pro(problem)
    except Exception as exc:  # noqa: BLE001
        logger.warning("regen: analyzer pro path errored: %s", exc)
    if with_pro:
        return "[Analyzer PRO]\n" + with_pro
    # 2) Fallback: plain Tavily web search.
    tav = await _research_via_tavily(problem, max_results=max_results)
    if tav:
        return "[Tavily]\n" + tav
    return ""
