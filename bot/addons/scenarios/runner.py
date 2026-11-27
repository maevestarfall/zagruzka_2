"""Route executor on top of ``open_browser_session`` (ТЗ rev.7.1 §7, §15).

Key facts honoured here (see §0):
- engine steps are tuples ``(op, val)``; ``type`` -> ``(text, target)`` (§0.3);
- ``copy`` results live in ``copied_texts`` as ``(selector, text)`` tuples,
  the sentinel «Элемент не найден или пуст» is never stored as a var (§0.1);
- ``pause`` value is SECONDS (§0.2);
- ``goto`` is executed by run_route itself via ``session.navigate`` /
  ``page.goto`` — it is NOT an engine op (§0.4);
- ``open_browser_session()`` takes no invented params — fingerprint/proxy/
  stealth resolve inside the engine (§0.5/§0.6);
- both tabs live in ONE session until ``finally`` (§15.1);
- ``copy`` + ``extract`` regex, retries with backoff and post-conditions,
  fresh-value polling with timeout, total budget + cancel (§15.2–15.6);
- engine ``progress_cb`` is reused for observability (§15.8).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Awaitable, Callable

from .models import COPY_NOT_FOUND, Route, Stage, Step, resolve_vars
from .store import resolve_step_secret

logger = logging.getLogger(__name__)

# §15.4 — retries with exponential backoff for critical steps.
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.5   # seconds; 1.5 -> 3 -> 6

# §15.5 / §15.6 — polling and budgets (seconds), configurable per call.
POLL_INTERVAL = 4.0
POLL_TIMEOUT = 120.0
ROUTE_TIMEOUT = 600.0


class RouteCancelled(Exception):
    """Raised when the user pressed «Отмена» (§15.6)."""


class RouteStepFailed(Exception):
    """Critical step failed after retries — stop the route (§15.4)."""

    def __init__(self, stage: Stage, step_no: int, step: Step, reason: str):
        self.stage = stage
        self.step_no = step_no
        self.step = step
        self.reason = reason
        super().__init__(
            f"Стадия {stage.index} «{stage.title}», шаг {step_no} "
            f"({step.op}): не выполнено — {reason}"
        )


def _to_engine_step(step: Step, vars_: dict) -> tuple[str, Any]:
    """Convert an addon Step into the engine tuple ``(op, val)`` (§0.3)."""
    if step.op == "type":
        raw = resolve_step_secret(step.value, step.vault_ref)  # §11
        text = resolve_vars(str(raw or ""), vars_)             # {email}/{code}
        return ("type", (text, step.target))                   # кортеж (текст, поле)
    if step.op in ("type_login", "type_password"):
        return (step.op, step.target)          # текст движок берёт из профиля
    if step.op == "pause":
        return ("pause", float(step.value))    # СЕКУНДЫ (§0.2)
    if step.op == "copy":
        return ("copy", step.target)           # save_as обрабатываем снаружи
    if step.op == "upload":
        return ("upload", step.value)
    if step.op == "click":
        return ("click", step.target)
    raise ValueError(f"goto не исполняется движком (см. §0.4): {step.op}")


def _extract_value(raw: str, extract: str | None) -> str | None:
    """§15.2: apply the ``extract`` regex; return None when no match.

    Guarantees that a whole-``body`` fallback dump never lands in a var.
    """
    if not raw or raw == COPY_NOT_FOUND:
        return None
    if not extract:
        return raw.strip() or None
    m = re.search(extract, raw)
    return m.group(0) if m else None


async def _copy_once(session, page, step: Step) -> str | None:
    """Run a single engine ``copy`` and return the extracted value or None."""
    r = await session.run_steps(page, [("copy", step.target)])
    copied = r.get("copied_texts") or []
    raw = copied[0][1] if copied else ""        # §0.1 — copied_texts[0][1]
    return _extract_value(raw, step.extract)


class _Cancel:
    """Cooperative cancellation token (§15.6)."""

    def __init__(self) -> None:
        self._flag = asyncio.Event()

    def cancel(self) -> None:
        self._flag.set()

    @property
    def cancelled(self) -> bool:
        return self._flag.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise RouteCancelled("Маршрут отменён пользователем.")


async def run_route(
    route: Route,
    *,
    on_stage: Callable[..., Awaitable[None]] | None = None,
    progress_cb: Callable[[int, int, str], Any] | None = None,
    cancel: "_Cancel | None" = None,
    route_timeout: float = ROUTE_TIMEOUT,
    poll_timeout: float = POLL_TIMEOUT,
    poll_interval: float = POLL_INTERVAL,
    screenshot_dir: str = "session",
) -> dict:
    """Execute a route over one shared browser session with two tabs.

    ``on_stage(phase, stage, screenshot=None)`` — демо-колбэк (§9):
    ``phase in ("start", "done")``; on "done" a stage screenshot path is
    supplied (§8).  ``progress_cb`` — engine step progress (§15.8).
    ``cancel`` — cancellation token from :func:`make_cancel_token` (§15.6).
    """
    cancel = cancel or _Cancel()
    deadline = time.monotonic() + route_timeout

    def _budget_check() -> None:
        cancel.check()
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Превышен общий лимит времени маршрута ({int(route_timeout)} с)."
            )

    from bot.workers.browser_clicker import open_browser_session

    session = await open_browser_session()  # §0.5 — БЕЗ выдуманных параметров
    try:
        # §15.1 — обе вкладки в ОДНОМ browser/context (общие cookies).
        pages = {"A": await session.new_page(), "B": await session.new_page()}
        await session.navigate(pages["A"], route.site_a)   # goto делает run_route (§0.4)
        if route.site_b:
            await session.navigate(pages["B"], route.site_b)

        if not session.supports_session_persist:
            # §15.7 — честно фиксируем ограничение nodriver в результате.
            logger.info("run_route: nodriver mode — session persist unavailable")

        # §15.5 п.1: «базовое» состояние поля кода фиксируется ДО действия
        # на Б, инициирующего отправку кода, — то есть сразу после открытия
        # вкладок, а не в момент начала поллинга (иначе быстро пришедший
        # код был бы ошибочно принят за «старое» значение).
        baselines: dict[int, str | None] = {}
        for stage in route.stages:
            for step in stage.steps:
                if step.op == "copy" and step.save_as and step.poll:
                    try:
                        baselines[id(step)] = await _copy_once(
                            session, pages[step.page], step
                        )
                    except Exception:  # noqa: BLE001
                        baselines[id(step)] = None

        results: list[int] = []
        for stage in route.stages:
            _budget_check()
            if on_stage:
                await on_stage("start", stage)

            for step_no, step in enumerate(stage.steps, 1):
                _budget_check()

                if step.op == "goto":                     # §0.4 — вне движка
                    await session.activate(pages[step.page])
                    await session.navigate(pages[step.page], str(step.target))
                    continue

                # §15.1(2) — активируем нужную вкладку перед шагом.
                await session.activate(pages[step.page])

                if step.op == "copy" and step.save_as and step.poll:
                    # §15.5 — только «свежее» значение, с таймаутом.
                    await _poll_fresh_copy(
                        session, pages[step.page], route, stage, step_no, step,
                        cancel=cancel, deadline=deadline,
                        poll_timeout=poll_timeout, poll_interval=poll_interval,
                        baseline=baselines.get(id(step)),
                    )
                    continue

                if step.op == "copy" and step.save_as:
                    await _copy_with_retries(
                        session, pages[step.page], route, stage, step_no, step,
                        cancel=cancel,
                    )
                    continue

                engine_step = _to_engine_step(step, route.vars)

                if step.op in ("click", "type"):
                    # §15.4 — ретраи с backoff и постусловием.
                    await _act_with_retries(
                        session, pages[step.page], engine_step,
                        stage, step_no, step, cancel=cancel,
                        progress_cb=progress_cb,
                    )
                else:
                    # pause / upload / type_login / type_password — один прогон.
                    r = await session.run_steps(
                        pages[step.page], [engine_step], progress_cb=progress_cb
                    )
                    if not r.get("ok"):
                        raise RouteStepFailed(
                            stage, step_no, step, str(r.get("error") or "ошибка движка")
                        )

            shot = None
            if on_stage:
                shot = await _shot(session, pages, stage, screenshot_dir)
                await on_stage("done", stage, screenshot=shot)
            results.append(stage.index)

        return {
            "ok": True,
            "stages_done": results,
            "vars": route.vars,
            "browser_type": session.browser_type,
            "session_persist": session.supports_session_persist,  # §15.7
        }
    except RouteCancelled as e:
        return {"ok": False, "error": str(e), "cancelled": True,
                "vars": route.vars}
    except RouteStepFailed as e:
        # §15.4 — явный стоп с указанием стадии/шага.
        return {"ok": False, "error": str(e),
                "stage": e.stage.index, "step": e.step_no, "vars": route.vars}
    except Exception as e:  # noqa: BLE001
        logger.exception("run_route failed")
        return {"ok": False, "error": str(e), "vars": route.vars}
    finally:
        await session.close()                              # §7 finally сохраняется


# ---- §15.4: retries with post-conditions ----

async def _act_with_retries(
    session, page, engine_step: tuple[str, Any],
    stage: Stage, step_no: int, step: Step, *,
    cancel: _Cancel, progress_cb=None,
) -> None:
    """click/type with up to RETRY_ATTEMPTS tries and exponential backoff."""
    last_err = "не удалось выполнить действие"
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        cancel.check()
        r = await session.run_steps(page, [engine_step], progress_cb=progress_cb)
        failed = r.get("failed_steps") or []
        if r.get("ok") and not failed:
            return  # постусловие §15.4: движок не зафиксировал промах
        if failed:
            last_err = failed[0][2]
        elif r.get("error"):
            last_err = str(r["error"])
        if attempt < RETRY_ATTEMPTS:
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    raise RouteStepFailed(stage, step_no, step, last_err)


async def _copy_with_retries(
    session, page, route: Route,
    stage: Stage, step_no: int, step: Step, *, cancel: _Cancel,
) -> None:
    """copy+save_as with retries; post-condition = non-empty extract match."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        cancel.check()
        value = await _copy_once(session, page, step)
        if value:                                     # §0.1 + §15.2
            route.vars[step.save_as] = value
            return
        if attempt < RETRY_ATTEMPTS:
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    raise RouteStepFailed(
        stage, step_no, step,
        "значение не найдено (селектор/шаблон не дал совпадения)",
    )


# ---- §15.5: fresh-value polling for OTP codes ----

async def _poll_fresh_copy(
    session, page, route: Route,
    stage: Stage, step_no: int, step: Step, *,
    cancel: _Cancel, deadline: float,
    poll_timeout: float, poll_interval: float,
    baseline: str | None = None,
) -> None:
    """Poll ``copy``+``extract`` until a NEW value appears (anti-OTP-race).

    ``baseline`` — состояние поля кода, снятое run_route ДО действия на Б,
    инициирующего отправку (§15.5 п.1): пусто или старый код с прошлого
    запуска.  Принимается только значение, отличное от baseline.  По
    таймауту — явный стоп маршрута (§15.4), а не старый/пустой код.
    """
    poll_deadline = min(time.monotonic() + poll_timeout, deadline)
    while True:
        cancel.check()
        value = await _copy_once(session, page, step)
        if value and value != baseline:               # только «свежее» значение
            route.vars[step.save_as] = value
            return
        if time.monotonic() > poll_deadline:
            raise RouteStepFailed(
                stage, step_no, step,
                f"код не появился за {int(poll_timeout)} с (таймаут поллинга)",
            )
        await asyncio.sleep(poll_interval)


# ---- §8: stage screenshots ----

async def _shot(session, pages: dict, stage: Stage, screenshot_dir: str) -> str | None:
    """Screenshot the tab the stage last worked on (§8)."""
    page_label = stage.steps[-1].page if stage.steps else "A"
    page = pages.get(page_label) or pages["A"]
    path = f"{screenshot_dir}/scn_stage_{stage.index}_{int(time.time())}.png"
    try:
        return await session.screenshot(page, path)
    except Exception:  # noqa: BLE001
        logger.warning("scenarios: stage %s screenshot failed", stage.index)
        return None


def make_cancel_token() -> _Cancel:
    """Factory for the cancellation token used by handlers (§15.6)."""
    return _Cancel()
