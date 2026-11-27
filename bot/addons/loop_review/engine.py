# Loop-review engine: review → fix → validate → repeat with a fresh reviewer.
# Co-authored with CoCo
"""The core loop (idea from github.com/di-sukharev/loop-review).

``run_loop`` takes code (or a task), and repeats:
  fresh independent reviewer → findings + score (1–10) → fixer applies them
until score >= threshold (9.5) or the reviewer reports no actionable
findings, or the pass cap is reached. Every reviewer is a fresh
``agent.oneshot_chat`` call (no shared context = independent review).
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from . import store

logger = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]


async def _noop(_m: str) -> None:
    return None


async def _agent(system: str, user: str, purpose: str, temperature: float = 0.2) -> str:
    from ...agent import oneshot_chat
    return await oneshot_chat(system, user, purpose=purpose, temperature=temperature)


_CODER_SYS = (
    "Ты — Кодер. На входе код ИЛИ задача. Верни ТОЛЬКО рабочий код (без "
    "пояснений вне кода). Если дан код — верни его же, поправив очевидное."
)

_REVIEWER_SYS = (
    "Ты — независимый ревьюер (свежий экземпляр, без истории). Оцени ТОЛЬКО "
    "присланный код: баги, регрессии, безопасность, целостность данных, "
    "поддерживаемость, нехватку тестов. Верни СТРОГО JSON: "
    '{\"score\": <1..10>, \"no_actionable\": <true|false>, '
    '\"findings\": [\"конкретная проблема с местом\", ...]}. '
    "score — общее качество; no_actionable=true, если нечего править."
)

_FIXER_SYS = (
    "Ты — Кодер-фиксер. Внеси ТОЧЕЧНЫЕ исправления по замечаниям ревьюера, "
    "не переписывая рабочее без нужды. Верни ТОЛЬКО итоговый код."
)


async def run_loop(
    target: str,
    *,
    progress: Progress | None = None,
    max_passes: int | None = None,
    threshold: float | None = None,
) -> dict:
    """Run the review→fix loop. Returns {code, score, passes, findings_left}."""
    say = progress or _noop
    passes_cap = max_passes or store.get_max_passes()
    thr = threshold if threshold is not None else store.get_threshold()

    # Initial code (works whether ``target`` is code or a task description).
    try:
        code = await _agent(_CODER_SYS, target, "loopreview-init", 0.3)
    except Exception as e:  # noqa: BLE001
        logger.warning("loop-review init failed: %s", e)
        code = target
    if not code.strip():
        code = target

    score = 0.0
    findings: list = []
    p = 0
    for p in range(1, passes_cap + 1):
        # Fresh independent reviewer each pass.
        raw = await _agent(_REVIEWER_SYS, f"КОД НА РЕВЬЮ:\n{code[:8000]}", "loopreview-review", 0.0)
        v = store.extract_json(raw) or {}
        try:
            score = float(v.get("score", 0))
        except Exception:  # noqa: BLE001
            score = 0.0
        findings = v.get("findings") or []
        no_act = bool(v.get("no_actionable", not findings))
        await say(f"🔁 Проход {p}: балл {score:.1f}/10, замечаний {len(findings)}")

        if score >= thr or no_act or not findings:
            break
        # Fixer applies the findings.
        fx = await _agent(
            _FIXER_SYS,
            f"КОД:\n{code[:8000]}\n\nЗАМЕЧАНИЯ:\n" + "\n".join(f"- {x}" for x in findings[:20]),
            "loopreview-fix", 0.2,
        )
        if fx.strip():
            code = fx

    return {"code": code, "score": score, "passes": p, "findings_left": findings}
