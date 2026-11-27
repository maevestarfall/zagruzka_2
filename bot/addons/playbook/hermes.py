"""Hermes — works the playbook steps in the background.

When «Гермес дорабатывает» is ON, a background loop periodically:
  • reads execution outcomes (per-step ok/fail + exec log),
  • logs lessons into :mod:`bot.addons.hermes_experience`,
  • for steps that fail more than they succeed, asks the LLM brain for an
    improved prompt and appends it to the step's notes (keeping the
    original — «не удаляя остальное»).

Everything is best-effort: no brain / no data → it just idles. Manual
«проработать сейчас» calls :func:`refine_once` directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Callable

from . import store

logger = logging.getLogger(__name__)


async def _improve_step(path: str, step: dict) -> str:
    """Ask the brain for a better prompt for a failing step. Returns text."""
    try:
        from .._llm import chat_once, NoBrainError
    except Exception:  # noqa: BLE001
        return ""
    sys = (
        "Ты — Гермес, наставник по инструкциям. Тебе дают шаг сценария, "
        "который чаще проваливается. Предложи УЛУЧШЕННЫЙ промт/настройку "
        "для этого шага: коротко, по делу, на русском (до 4 строк). "
        "Не выдумывай инструментов — опирайся на указанные."
    )
    user = (
        f"Путь: {path}\n"
        f"Шаг: {step.get('name')}\n"
        f"Инструменты: {', '.join(step.get('tools', []))}\n"
        f"Текущий промт: {step.get('prompt', '')}\n"
        f"Статистика: ok={step.get('ok', 0)}, fail={step.get('fail', 0)}\n"
        f"Заметки: {step.get('notes', '')}"
    )
    try:
        out = await chat_once(sys, user, max_tokens=220, temperature=0.4)
        return (out or "").strip()[:600]
    except NoBrainError:
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("playbook hermes improve failed: %s", exc)
        return ""


async def refine_once() -> dict:
    """One refinement pass over all steps. Returns a summary dict."""
    from .. import hermes_experience as hx

    nodes = store._nodes()  # noqa: SLF001 — internal read is fine here
    reviewed = 0
    improved = 0
    notes: list[str] = []

    for nid in list(nodes.keys()):
        steps = store.get_steps(nid)
        if not steps:
            continue
        path = store.path_titles(nid)
        for step in steps:
            reviewed += 1
            ok = int(step.get("ok", 0))
            fail = int(step.get("fail", 0))
            # Only spend a brain call on steps that actually struggle.
            if fail >= 1 and fail >= ok:
                suggestion = await _improve_step(path, step)
                if suggestion:
                    merged = (step.get("notes", "") +
                              f"\n[Гермес] {suggestion}").strip()[:1000]
                    store.update_step(nid, step["id"], notes=merged)
                    improved += 1
                    notes.append(f"{path} · {step.get('name')}")
                    hx.record(
                        "general",
                        f"playbook улучшен шаг «{step.get('name')}» ({path})",
                        outcome="improved",
                        lesson=suggestion[:300],
                    )

    return {"reviewed": reviewed, "improved": improved, "notes": notes}


def _build_report(summary: dict) -> str:
    if summary["improved"] == 0:
        return (
            "🧠 <b>Гермес проработал инструкцию</b>\n"
            f"Просмотрено шагов: {summary['reviewed']}\n"
            "Доработок не потребовалось — пока всё работает."
        )
    lines = [
        "🧠 <b>Гермес доработал инструкцию</b>",
        f"Просмотрено шагов: {summary['reviewed']} · улучшено: {summary['improved']}",
        "",
    ]
    for n in summary["notes"][:8]:
        lines.append(f"• {n}")
    return "\n".join(lines)


async def hermes_loop(bot: Any, get_chat_id: Callable[[], Any]) -> None:
    """Background loop. Idles cheaply when the toggle is OFF."""
    logger.info("playbook Hermes loop started")
    while True:
        try:
            if store.hermes_auto():
                summary = await refine_once()
                if summary.get("improved"):
                    chat_id = None
                    with contextlib.suppress(Exception):
                        chat_id = get_chat_id()
                    if chat_id:
                        with contextlib.suppress(Exception):
                            await bot.send_message(chat_id, _build_report(summary))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("playbook Hermes tick failed — continuing")
        # Wake periodically so the toggle/interval changes take effect fast.
        target = store.hermes_interval_min() * 60
        slept = 0
        while slept < target:
            await asyncio.sleep(30)
            slept += 30
            if not store.hermes_auto():
                break
