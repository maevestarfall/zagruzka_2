"""Real, step-wise execution of a playbook plan.

This is what turns the «📚 Пошаговая инструкция» tree from a passive
text block into something the bot actually *runs*:

  • for each step of a node's plan (in order), it hands the step's
    prompt + tools + timing to the full agent loop (:func:`bot.agent.run_agent`,
    which has the bot's real tools), then
  • records the outcome (ok/fail + a short note) via
    :func:`bot.addons.playbook.store.record_step_outcome`.

Because ``record_step_outcome`` writes both the per-step ``ok``/``fail``
counters AND the shared ``exec_log``, Hermes finally gets real data to
learn from — the learning loop is no longer idling on empty stats.

Everything is best-effort and isolated per step: one failing step is
recorded as a failure (note included) but does NOT abort the rest of the
plan, so the bot still produces useful experience even on a bad run.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Awaitable, Callable

from . import store

logger = logging.getLogger(__name__)

# Markers that make us treat an otherwise-completed step as a failure.
# We can't peek inside every tool, but if the agent's own answer says it
# couldn't do the thing, that's a fail Hermes should learn from.
_FAIL_MARKERS = (
    "не удалось", "не получилось", "не вышло", "не смог", "не могу",
    "ошибка", "ошибк", "провал", "сбой", "отмен",
    "failed", "error", "cannot", "could not", "unable",
)

StepCallback = Callable[[int, int, dict, bool], Awaitable[None]]


def _snapshot_history(user_id: int) -> list[dict]:
    """Return a copy of the user's chat history (best-effort)."""
    try:
        from ...storage import storage

        return storage.get_history(user_id)  # already a list copy
    except Exception:  # noqa: BLE001
        return []


def _restore_history(user_id: int, snapshot: list[dict]) -> None:
    """Replace the user's chat history with ``snapshot`` so a plan run
    leaves the real conversation untouched (best-effort)."""
    try:
        from ...storage import storage

        storage._user(user_id)["history"] = list(snapshot)  # noqa: SLF001
        storage._save()  # noqa: SLF001
    except Exception:  # noqa: BLE001 — restoring must never crash the run
        logger.warning("playbook: failed to restore chat history after plan run")


def _looks_failed(text: str) -> bool:
    """Heuristic: empty output or an answer that admits failure → fail."""
    t = (text or "").strip().lower()
    if not t:
        return True
    return any(m in t for m in _FAIL_MARKERS)


async def _judge_step(path: str, step: dict, output: str) -> tuple[bool, str] | None:
    """Ask the brain whether the step *actually* succeeded.

    Returns ``(ok, reason)`` when the brain gives a verdict, or ``None``
    when no brain is configured / the judge is unavailable — in which
    case the caller falls back to the keyword heuristic. This is the
    "explicit self-assessment" layer: a real second opinion on top of the
    cheap heuristic, so Hermes' ok/fail data is trustworthy.
    """
    try:
        from .._llm import NoBrainError, chat_once
    except Exception:  # noqa: BLE001
        return None
    sys = (
        "Ты — строгий контролёр выполнения шагов сценария. Тебе дают описание "
        "шага и фактический результат работы агента. Ответь СТРОГО так:\n"
        "первая строка — одно слово: OK (шаг реально выполнен) или FAIL (нет);\n"
        "вторая строка — короткая причина (одна фраза)."
    )
    user = (
        f"Путь: {path}\n"
        f"Шаг: {step.get('name')}\n"
        f"Что требовалось: {step.get('prompt') or step.get('name')}\n"
        f"Инструменты: {', '.join(step.get('tools', []) or []) or '—'}\n\n"
        f"Результат агента:\n{(output or '')[:1500]}"
    )
    try:
        verdict = await chat_once(sys, user, max_tokens=60, temperature=0.0)
    except NoBrainError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("playbook step judge failed: %s", exc)
        return None
    if not verdict or not verdict.strip():
        return None
    lines = verdict.strip().splitlines()
    head = lines[0].strip().lower()
    reason = " ".join(lines[1:]).strip()[:160]
    if "fail" in head or "провал" in head or head in ("нет", "no"):
        return False, reason or "оценка: fail"
    if "ok" in head or "ок" in head or head in ("да", "yes", "success"):
        return True, reason or "оценка: ok"
    return None  # unparsable verdict → let the heuristic decide


def _compose_instruction(path: str, idx: int, total: int, step: dict) -> str:
    """Build the focused instruction the agent executes for one step."""
    tools = ", ".join(step.get("tools", []) or []) or "—"
    notes = (step.get("notes") or "").strip()
    extra = f"\nЗаметки/доработки Гермеса:\n{notes}\n" if notes else ""
    return (
        "[Пошаговая инструкция — выполнение плана]\n"
        f"Путь: {path}\n"
        f"Шаг {idx}/{total}: {step.get('name')}\n"
        f"Разрешённые инструменты: {tools}\n"
        f"Тайминг: {step.get('timing') or '—'}\n"
        f"{extra}\n"
        "Задача этого шага:\n"
        f"{step.get('prompt') or step.get('name')}\n\n"
        "Выполни ТОЛЬКО этот шаг, опираясь на указанные инструменты и тайминг. "
        "Не забегай вперёд к следующим шагам. Если шаг невыполним — "
        "коротко объясни, что именно помешало."
    )


async def run_plan(
    node_id: str,
    user_id: int,
    *,
    on_step: StepCallback | None = None,
    cancel_event: Any | None = None,
    isolate_history: bool = True,
) -> dict:
    """Run every step of ``node_id``'s plan in order, recording outcomes.

    Returns a summary dict::

        {"ok": bool, "reason": str, "path": str,
         "ran": int, "succeeded": int, "failed": int,
         "steps": [{"step": name, "ok": bool, "note": str}, ...]}

    ``ok`` is False only when there was nothing to run (no steps); a plan
    that ran but had failing steps still returns ``ok=True`` with the
    counts populated.

    When ``isolate_history`` is True (default) the user's chat history is
    snapshotted before the run and restored afterwards, so executing a
    plan does NOT pollute the real conversation — while still giving the
    steps continuity *between* themselves during the run.
    """
    steps = store.get_steps(node_id)
    path = store.path_titles(node_id)
    summary: dict = {
        "ok": False,
        "reason": "",
        "path": path,
        "ran": 0,
        "succeeded": 0,
        "failed": 0,
        "steps": [],
    }
    if not steps:
        summary["reason"] = "no_steps"
        return summary

    # Lazy module import (not the symbol) so tests can monkeypatch
    # ``bot.agent.run_agent`` and so we never create an import cycle at
    # load time (agent.py imports playbook lazily too).
    from ... import agent

    history_snapshot = _snapshot_history(user_id) if isolate_history else None
    total = len(steps)
    try:
        for idx, step in enumerate(steps, 1):
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                summary["reason"] = "cancelled"
                break

            instruction = _compose_instruction(path, idx, total, step)
            ok = False
            note = ""
            try:
                out = await agent.run_agent(
                    user_id, instruction, None, cancel_event=cancel_event
                )
                snippet = " ".join((out or "").split())[:160]
                verdict = await _judge_step(path, step, out)
                if verdict is not None:
                    ok, reason = verdict
                    note = (f"[оценка] {reason} · " if reason else "") + snippet
                else:
                    # No brain to judge → fall back to the keyword heuristic.
                    ok = not _looks_failed(out)
                    note = snippet
            except Exception as exc:  # noqa: BLE001 — one bad step ≠ whole plan
                ok = False
                note = f"исключение: {exc}"[:200]
                logger.warning("playbook step %s failed: %s", step.get("id"), exc)

            # THE point of this module: feed real outcomes back so Hermes
            # (and the per-step ✅/❌ stats + exec_log) stop being empty.
            store.record_step_outcome(node_id, step["id"], ok, note)

            summary["ran"] += 1
            summary["succeeded" if ok else "failed"] += 1
            summary["steps"].append({"step": step.get("name"), "ok": ok, "note": note})

            if on_step is not None:
                with contextlib.suppress(Exception):
                    await on_step(idx, total, step, ok)
    finally:
        if history_snapshot is not None:
            _restore_history(user_id, history_snapshot)

    summary["ok"] = True
    if not summary["reason"]:
        summary["reason"] = "done"
    return summary
