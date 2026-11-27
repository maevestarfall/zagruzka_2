# Live project map: fire-and-forget tracer (ТЗ «Живая карта проекта» v6.1).
# Co-authored with CoCo
"""Event tracer for the live project map.

Contract (раздел 5 ТЗ):

* Everything here is **fire-and-forget**: the bot never waits for the map.
  Every public function is wrapped in ``try/except Exception: pass`` —
  a broken tracer means a lost wave, never a broken handler.
* ``LIVEMAP_ENABLED=0`` (default) → all public functions are no-ops,
  checked ONCE at import; no queue, no structures are allocated.
* ``task_id`` flows through :mod:`contextvars` so nested ``@traced``
  functions discover the current task automatically (OTel model).
* ``@traced`` distinguishes FOUR kinds of functions (logfire model):
  sync, async, sync-generator, async-generator. For generators the span
  covers the WHOLE iteration, not just the time to the first ``yield``.
* ``task_text`` is masked **server-side, before it enters the buffer**
  (``LIVEMAP_TEXT_MODE=full|masked|off``).
* Error text is sanitised: token-looking substrings → ``***``.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import os
import re
import time
from typing import Any, Callable, Optional

# --- configuration (read once at import — kill switch contract) --------

LIVEMAP_ENABLED: bool = os.getenv("LIVEMAP_ENABLED", "0").strip() in ("1", "true", "yes")
TEXT_MODE: str = os.getenv("LIVEMAP_TEXT_MODE", "full").strip().lower()
if TEXT_MODE not in ("full", "masked", "off"):
    TEXT_MODE = "full"

# Max concurrent tasks the map tracks (лимит кардинальности, 4.4).
MAX_TASKS = 50
# Watchdog: no events for 30 min → force task_end reason="stale" (5.3).
STALE_AFTER_SEC = 30 * 60

# --- node categories (3.1) — one dict, unknown addons → neutral ---------

CATEGORIES: dict[str, str] = {
    # генерация
    "photo_chooser": "gen", "viddyn": "gen", "tts": "gen", "vision": "gen",
    "media_brain": "gen", "media_custom": "gen", "media_llm": "gen",
    "openreels": "gen", "image_edit": "gen", "video_gen": "gen",
    # инструменты
    "browser_panel": "tool", "coder": "tool", "clicker": "tool",
    "osint": "tool", "stealth": "tool", "stealth_browser": "tool",
    "slack_connector": "tool", "test": "tool", "debates": "tool",
    "tools": "tool", "web": "tool",
    # память/данные
    "memory": "data", "vault": "data", "context": "data",
    "promptbox": "data", "dump": "data", "inbox": "data",
    # инфраструктура
    "ram_guard": "infra", "heartbeat": "infra", "private_mode": "infra",
    "proxy_tunnel": "infra", "projectmap": "infra", "guide": "infra",
    "agent": "infra", "llm_router": "infra",
}


def category_of(node: str) -> str:
    return CATEGORIES.get(str(node or "").split(".")[0], "neutral")


# --- current task context ----------------------------------------------

_current_task: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "livemap_task", default=None
)


def current_task_id() -> Optional[str]:
    try:
        return _current_task.get()
    except Exception:  # noqa: BLE001
        return None


# --- helpers -------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{30,}")


def sanitize_error(exc: BaseException) -> str:
    """``TypeError: msg`` — max 200 chars, no traceback, secrets → ``***``."""
    try:
        text = f"{type(exc).__name__}: {exc}"[:200]
        return _TOKEN_RE.sub("***", text)
    except Exception:  # noqa: BLE001
        return "Error"


def mask_text(text: str) -> Optional[str]:
    """Apply ``LIVEMAP_TEXT_MODE`` BEFORE the text enters any buffer."""
    try:
        if TEXT_MODE == "off":
            return None
        t = str(text or "")
        if TEXT_MODE == "masked":
            return (t[:12] + "…") if len(t) > 12 else t
        return t[:80]
    except Exception:  # noqa: BLE001
        return None


# =========================================================================
# Disabled mode: pure no-ops, nothing is allocated.
# =========================================================================

if not LIVEMAP_ENABLED:

    def task_begin(task_id: str, chat_id: Any = None, text: str = "") -> None:  # noqa: ARG001
        return None

    def task_finish(task_id: Optional[str] = None, reason: str = "done") -> None:  # noqa: ARG001
        return None

    def task_error(exc: BaseException, node: str = "", task_id: Optional[str] = None) -> None:  # noqa: ARG001
        return None

    def emit(etype: str, task_id: Optional[str] = None, src: str = "", dst: str = "", **extra: Any) -> None:  # noqa: ARG001
        return None

    def traced(node: str) -> Callable:  # noqa: ARG001
        def deco(fn: Callable) -> Callable:
            return fn
        return deco

    def get_stats() -> dict:
        return {"enabled": False}

else:
    # =====================================================================
    # Enabled mode.
    # =====================================================================

    # НЕ «from . import live»: тот путь берёт закешированный атрибут
    # пакета и не регистрирует модуль в sys.modules после reload —
    # importlib.import_module делает и то и другое честно.
    import importlib as _importlib

    _live = _importlib.import_module(f"{__package__}.live")

    def _put(event: dict) -> None:
        """The single funnel: never raises, never blocks (5.1)."""
        try:
            _live.enqueue(event)
        except Exception:  # noqa: BLE001
            pass

    def task_begin(task_id: str, chat_id: Any = None, text: str = "") -> None:
        try:
            _current_task.set(task_id)
            ev: dict = {
                "v": 1,
                "type": "task_start",
                "task_id": task_id,
                "task_started_at": time.time(),
                "ts": time.time(),
            }
            masked = mask_text(text)
            if masked is not None:
                ev["task_text"] = masked
            _put(ev)
        except Exception:  # noqa: BLE001
            pass

    def task_finish(task_id: Optional[str] = None, reason: str = "done") -> None:
        try:
            tid = task_id or current_task_id()
            if not tid:
                return
            _put({"v": 1, "type": "task_end", "task_id": tid,
                  "reason": reason, "ts": time.time()})
            _current_task.set(None)
        except Exception:  # noqa: BLE001
            pass

    def task_error(exc: BaseException, node: str = "", task_id: Optional[str] = None) -> None:
        try:
            tid = task_id or current_task_id()
            if not tid:
                return
            _put({"v": 1, "type": "task_error", "task_id": tid,
                  "node": node, "error": sanitize_error(exc), "ts": time.time()})
            # An error CLOSES the task — no zombie waves (4.5 п.5).
            _put({"v": 1, "type": "task_end", "task_id": tid,
                  "reason": "error", "ts": time.time()})
            _current_task.set(None)
        except Exception:  # noqa: BLE001
            pass

    def emit(etype: str, task_id: Optional[str] = None, src: str = "", dst: str = "", **extra: Any) -> None:
        try:
            tid = task_id or current_task_id()
            ev: dict = {"v": 1, "type": etype, "ts": time.time()}
            if tid:
                ev["task_id"] = tid
            if src:
                ev["from"] = src
            if dst:
                ev["to"] = dst
            for k, v in extra.items():
                if k not in ev:
                    ev[k] = v
            _put(ev)
        except Exception:  # noqa: BLE001
            pass

    def get_stats() -> dict:
        try:
            return _live.get_stats()
        except Exception:  # noqa: BLE001
            return {"enabled": True}

    # --- @traced: four kinds of callables (logfire model, 5.1) ----------

    def _node_start(node: str) -> float:
        tid = current_task_id()
        _put({"v": 1, "type": "node_start", "node": node,
              **({"task_id": tid} if tid else {}), "ts": time.time()})
        return time.monotonic()

    def _node_end(node: str, t0: float, error: Optional[BaseException] = None) -> None:
        tid = current_task_id()
        ev: dict = {"v": 1, "type": "node_end", "node": node,
                    "duration_ms": max(0.0, (time.monotonic() - t0) * 1000.0),
                    "ts": time.time()}
        if tid:
            ev["task_id"] = tid
        if error is not None:
            ev["ok"] = False
        _put(ev)

    def traced(node: str) -> Callable:
        """Decorator: node_start/node_end + timings, isolation guaranteed.

        Durations use ``time.monotonic()`` (5.4). Generators keep the span
        open across the WHOLE iteration (5.1) — a naive decorator would
        close it on the first ``yield`` and silently lie about timings.
        """

        def deco(fn: Callable) -> Callable:
            if inspect.isasyncgenfunction(fn):
                @functools.wraps(fn)
                async def agen_wrapper(*args: Any, **kwargs: Any):
                    try:
                        t0 = _node_start(node)
                    except Exception:  # noqa: BLE001
                        t0 = time.monotonic()
                    err: Optional[BaseException] = None
                    try:
                        async for item in fn(*args, **kwargs):
                            yield item
                    except BaseException as exc:
                        err = exc
                        try:
                            task_error(exc, node=node)
                        except Exception:  # noqa: BLE001
                            pass
                        raise
                    finally:
                        try:
                            _node_end(node, t0, err)
                        except Exception:  # noqa: BLE001
                            pass
                return agen_wrapper

            if inspect.isgeneratorfunction(fn):
                @functools.wraps(fn)
                def gen_wrapper(*args: Any, **kwargs: Any):
                    try:
                        t0 = _node_start(node)
                    except Exception:  # noqa: BLE001
                        t0 = time.monotonic()
                    err: Optional[BaseException] = None
                    try:
                        yield from fn(*args, **kwargs)
                    except BaseException as exc:
                        err = exc
                        try:
                            task_error(exc, node=node)
                        except Exception:  # noqa: BLE001
                            pass
                        raise
                    finally:
                        try:
                            _node_end(node, t0, err)
                        except Exception:  # noqa: BLE001
                            pass
                return gen_wrapper

            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def async_wrapper(*args: Any, **kwargs: Any):
                    try:
                        t0 = _node_start(node)
                    except Exception:  # noqa: BLE001
                        t0 = time.monotonic()
                    err: Optional[BaseException] = None
                    try:
                        return await fn(*args, **kwargs)
                    except BaseException as exc:
                        err = exc
                        try:
                            task_error(exc, node=node)
                        except Exception:  # noqa: BLE001
                            pass
                        raise
                    finally:
                        try:
                            _node_end(node, t0, err)
                        except Exception:  # noqa: BLE001
                            pass
                return async_wrapper

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any):
                try:
                    t0 = _node_start(node)
                except Exception:  # noqa: BLE001
                    t0 = time.monotonic()
                err: Optional[BaseException] = None
                try:
                    return fn(*args, **kwargs)
                except BaseException as exc:
                    err = exc
                    try:
                        task_error(exc, node=node)
                    except Exception:  # noqa: BLE001
                        pass
                    raise
                finally:
                    try:
                        _node_end(node, t0, err)
                    except Exception:  # noqa: BLE001
                        pass
            return sync_wrapper

        return deco
