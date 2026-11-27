# «🎓 Обучалка» — ученик Qwen: внешний OpenAI-совместимый endpoint, stateless.
"""Proxy to the external Qwen student.

The student model does NOT live on the bot host (14B needs a GPU box).
Instead the teacher points ``qwen_base_url`` at any OpenAI-compatible
server (Ollama ``http://host:11434/v1``, vLLM, etc.).

Every run is STATELESS — «выключение перед ячейкой»: no chat history is
ever sent. The prompt contains only:
* the vessel macro map (the «каркас»),
* the leaf task,
* optional extra context, hard-capped by the leaf's ``context_budget``.

So the student meets each leaf as if freshly booted, which is exactly the
training discipline the vessel is built around.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from . import store

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=180)

_SYSTEM_TEMPLATE = (
    "Ты — ученик. Отвечай строго по заданию, кратко и по делу.\n"
    "Каркас, в рамках которого ты работаешь:\n{macro}"
)


def build_prompt(leaf: dict, macro_map: str, extra_context: str = "") -> list[dict]:
    """Compose the stateless message list for one leaf run."""
    budget = int(leaf.get("context_budget") or store.DEFAULT_CONTEXT_BUDGET)
    context = (extra_context or "").strip()[:budget]
    user = leaf.get("task") or leaf.get("title") or ""
    if context:
        user = f"{user}\n\nКонтекст (усечён до бюджета листика):\n{context}"
    return [
        {"role": "system", "content": _SYSTEM_TEMPLATE.format(macro=macro_map[:budget])},
        {"role": "user", "content": user},
    ]


def is_configured() -> bool:
    return bool(store.get_qwen_base_url())


async def ping() -> dict[str, Any]:
    """Cheap reachability check of the configured endpoint."""
    base = store.get_qwen_base_url()
    if not base:
        return {"ok": False, "error": "not_configured"}
    url = base.rstrip("/") + "/models"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(url) as resp:
                return {"ok": resp.status < 500, "status": resp.status}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


async def run_leaf(leaf_id: int, extra_context: str = "") -> dict[str, Any]:
    """Run ONE leaf through the student and record the attempt.

    Returns ``{ok, answer, attempt_id}`` or ``{ok: False, error}``.
    """
    base = store.get_qwen_base_url()
    if not base:
        return {"ok": False, "error": "ученик не подключён: задай qwen_base_url"}
    leaf = store.get_leaf(leaf_id)
    if leaf is None:
        return {"ok": False, "error": "leaf_not_found"}
    vessel = store.get_vessel(leaf["vessel_id"]) or {}
    messages = build_prompt(leaf, vessel.get("macro_map", ""), extra_context)
    payload = {
        "model": store.get_qwen_model(),
        "messages": messages,
        "temperature": 0.3,
        "stream": False,
    }
    url = base.rstrip("/") + "/chat/completions"
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.post(url, json=payload) as resp:
                if resp.status >= 400:
                    body = (await resp.text())[:300]
                    return {"ok": False, "error": f"student HTTP {resp.status}: {body}"}
                data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.exception("obuchalka: student run failed")
        return {"ok": False, "error": str(exc)[:200]}
    try:
        answer = data["choices"][0]["message"]["content"]
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "student returned malformed response"}
    prompt_text = "\n---\n".join(m["content"] for m in messages)
    attempt_id = store.add_attempt(leaf_id, prompt_text, answer)
    store.journal_add("teacher", "student_run", f"leaf#{leaf_id} — попытка #{attempt_id}")
    return {"ok": True, "answer": answer, "attempt_id": attempt_id}
