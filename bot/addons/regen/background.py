"""Background self-analysis loop for «🔧 Регенерация».

Registered in ``bot/main.py`` next to the mailbox poller. Gated by the
``addons.regen.bg_enabled`` toggle: when OFF it just idles cheaply. When
ON it runs a scan every ``interval_min`` and notifies the owner about NEW
issues (issues unseen since last notify).

Modes:
* **sim**  — static scan only (no side effects). Safe default.
* **real** — scan + a real self-ping of the bot's HTTP health endpoint
  (a genuine «выход в мир» smoke check) when ``PUBLIC_URL`` is set.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from . import store
from .scanner import scan

logger = logging.getLogger(__name__)

__all__ = ["regen_bg_loop", "run_once"]

_IDLE_POLL_S = 60


def _issue_key(it: dict) -> str:
    return f"{it.get('kind')}|{it.get('where')}|{(it.get('message') or '')[:60]}"


async def _self_ping() -> str:
    """Real-mode smoke check: GET the public URL. Returns a short status."""
    import os

    base = (os.environ.get("PUBLIC_URL") or "").rstrip("/")
    if not base:
        return "real: PUBLIC_URL не задан — пропустил self-ping"
    try:
        import httpx

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(base + "/ping")
        return f"real: self-ping {base}/ping → HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return f"real: self-ping ошибка — {type(exc).__name__}: {exc}"


async def run_once(bot: Any, get_chat_id: Callable[[], Any]) -> dict:
    """One scan cycle. Notifies owner about new issues. Returns the report."""
    report = await scan()
    store.save_report(report)

    extra = ""
    if store.mode() == "real":
        extra = await _self_ping()

    issues = report.get("issues", [])
    keys = [_issue_key(it) for it in issues]
    known = set(store.known_issue_keys())
    new_issues = [it for it, k in zip(issues, keys) if k not in known]
    store.set_known_issue_keys(keys)

    chat_id = None
    try:
        chat_id = get_chat_id()
    except Exception:  # noqa: BLE001
        chat_id = None

    if chat_id and (new_issues or report.get("fatal")):
        counts = report.get("counts", {})
        head = (
            f"🔧 Регенерация (фон, {store.mode()}): "
            f"ошибок {counts.get('error', 0)}, предупреждений {counts.get('warn', 0)}."
        )
        if report.get("fatal"):
            head = f"🔧 Регенерация (фон): сканер упал — {report['fatal']}"
        lines = [head]
        if new_issues:
            lines.append(f"\nНовые проблемы ({len(new_issues)}):")
            for it in new_issues[:8]:
                lines.append(f"• [{it.get('severity')}] {it.get('where')} — {it.get('message')}")
            lines.append("\nОткрой «🔧 Регенерация» → «🔍 Сканировать», чтобы починить.")
        if extra:
            lines.append("\n" + extra)
        try:
            await bot.send_message(chat_id, "\n".join(lines))
        except Exception:  # noqa: BLE001
            logger.exception("regen bg notify failed")
    return report


async def regen_bg_loop(bot: Any, get_chat_id: Callable[[], Any]) -> None:
    """Long-running loop. Cheap when toggle is OFF."""
    logger.info("regen background loop started")
    waited = 0
    while True:
        try:
            if not store.bg_enabled():
                await asyncio.sleep(_IDLE_POLL_S)
                waited = 0
                continue
            await run_once(bot, get_chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("regen bg loop iteration failed")
        # sleep the configured interval, but wake periodically so toggle
        # changes take effect within a minute.
        target_s = store.interval_min() * 60
        waited = 0
        while waited < target_s:
            await asyncio.sleep(_IDLE_POLL_S)
            waited += _IDLE_POLL_S
            if not store.bg_enabled():
                break
