"""Step-by-step executor for saved algorithms.

Each step is fed to the existing :func:`bot.agent.run_agent`, which
already owns the brain-failover and tool-dispatch logic. The executor
itself only takes care of:

* sending the user a progress line for each step,
* enforcing strict sequencing (step N+1 starts only after step N
  returned),
* short-circuiting on the first step that fails or returns ``ERROR:``
  from a tool (so the user notices early), and
* marking the slot's ``is_running`` flag so two simultaneous runs of
  the same slot (e.g. periodic + manual ``▶ Запустить``) don't
  overlap.

The executor is fully tool-set-agnostic — adding new tools to
``run_agent`` automatically makes them available to algorithms.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Awaitable, Callable

from . import state as algo_state

logger = logging.getLogger(__name__)

# ``status_fn`` is awaited with one short Russian line per step.
StatusFn = Callable[[str], Awaitable[None]]


def _hermes_note(event: str, *, outcome: str, lesson: str = "") -> None:
    """Feed the run outcome back into Hermes' experience (when enabled).

    This is the «оценивает удачные/неудачные пути» half of Hermes: every
    finished plan teaches it what worked and what didn't, which then flows
    back into the planner prompt via planner._hermes_addendum().
    """
    with contextlib.suppress(Exception):
        from .. import state as addon_state
        if not addon_state.get("algorithm", "hermes_enabled", False):
            return
        from ..hermes_experience import record
        record("general", event, outcome=outcome, lesson=lesson)


# Sentinel returned by ``run_agent`` when the agent hit its step budget
# without producing an answer — we treat that as a hard stop.
_AGENT_STEP_LIMIT_PREFIX = "(превышен лимит шагов агента"


def _step_failed(text: str) -> bool:
    """Heuristic: does ``text`` look like the step failed?

    The agent always returns SOMETHING, even on failure (it forwards
    tool-error strings into the final reply when there's no tool-less
    follow-up). We treat anything containing ``ERROR:`` or the
    "step limit" sentinel as a fail.
    """
    if not text:
        return True
    lowered = text.strip().lower()
    if "error:" in lowered:
        return True
    if text.startswith(_AGENT_STEP_LIMIT_PREFIX):
        return True
    return False


async def run_plan(
    chat_id: int,
    user_id: int,
    slot_index: int,
    *,
    status: StatusFn,
    plan_steps: list[str] | None = None,
) -> tuple[int, int, str]:
    """Execute the plan stored in ``slot_index`` (or ``plan_steps`` if
    provided).

    Args:
        chat_id: the chat the algorithm belongs to.
        user_id: the user-id passed to ``run_agent`` for history /
            project resolution.
        slot_index: 1..10.
        status: async callback for per-step progress messages.
        plan_steps: optional override. When ``None``, reads the plan
            from the slot. Used by «▶ Запустить» on an unsaved (just
            AI-generated) plan, so we can try the plan before saving.

    Returns:
        Tuple ``(steps_run, total_steps, last_reply)`` — useful for
        tests and for the post-run UI.
    """
    slot = algo_state.get_slot(chat_id, slot_index)
    steps = plan_steps if plan_steps is not None else (
        [ln.strip() for ln in slot.plan.splitlines() if ln.strip()]
    )
    total = len(steps)
    if total == 0:
        await status("Алгоритм пустой — нечего выполнять.")
        return 0, 0, ""

    # Single-flight guard — only meaningful for saved slots.
    if plan_steps is None:
        if slot.is_running:
            await status("Этот алгоритм уже выполняется — пропускаю.")
            return 0, total, ""
        slot.is_running = True
        algo_state.save_slot(chat_id, slot)

    last_reply = ""
    plan_label = (getattr(slot, "name", "") or "план").strip()[:60]
    try:
        from ...agent import run_agent

        for i, step in enumerate(steps, start=1):
            await status(f"Шаг {i}/{total}: {step}")
            try:
                reply = await run_agent(
                    user_id=user_id,
                    user_text=step,
                    cwd=None,
                    on_status=None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("step %d crashed: %s", i, step)
                await status(f"Шаг {i}/{total}: ❌ упал — {exc}")
                _hermes_note(
                    f"План «{plan_label}»: шаг {i}/{total} «{step[:80]}» упал с ошибкой",
                    outcome="fail",
                    lesson=f"Шаг «{step[:80]}» падает ({str(exc)[:80]}) — нужен другой путь.",
                )
                return i, total, str(exc)
            last_reply = reply or ""
            if _step_failed(last_reply):
                await status(f"Шаг {i}/{total}: ❌ — {last_reply[:200]}")
                _hermes_note(
                    f"План «{plan_label}»: шаг {i}/{total} «{step[:80]}» не сработал",
                    outcome="fail",
                    lesson=f"Подход «{step[:80]}» не дал результата — попробовать иначе.",
                )
                return i, total, last_reply
            # Mini-pause between steps so Telegram doesn't aggregate the
            # status updates into one long edit.
            await asyncio.sleep(0.05)
        await status(f"Готово: {total} из {total} шагов выполнены.")
        _hermes_note(
            f"План «{plan_label}» выполнен полностью ({total} шагов)",
            outcome="ok",
        )
        return total, total, last_reply
    finally:
        if plan_steps is None:
            slot = algo_state.get_slot(chat_id, slot_index)
            slot.is_running = False
            slot.last_run_at = time.time()
            with contextlib.suppress(Exception):
                algo_state.save_slot(chat_id, slot)
