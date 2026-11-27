# Anti-theft kill-switch: trap config, irreversible freeze, and code/secret wipe.
# Co-authored with CoCo
"""Owner-defined intrusion response for lilush.

When a foreign Telegram id touches the bot (the owner gates access by id),
the owner can arm a trap that, on intrusion, runs one of two modes:

* ``freeze``  — write an **irreversible** marker to the persistent ``/data``
  disk and drop every update for everyone (incl. the owner). Survives
  restarts; cannot be lifted from inside the bot (by design — the goal is
  «бот не оживёт»). Clearing it requires wiping the disk / redeploy.
* ``delete`` — wipe ``state.json`` (all secrets), ``/data`` and the on-disk
  ``bot/`` package, then ``os._exit(0)``.

All config lives under the ``vault`` namespace (separate keys), default OFF.
The single safeguard: it only ever fires for a FOREIGN id, never the owner.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_NS = "vault"

# Marker file lives on the persistent disk so freeze survives restarts.
_FREEZE_NAME = ".frozen"


def _state():
    from .addons import state as addon_state
    return addon_state


def _data_dir():
    from .config import DATA_DIR
    return DATA_DIR


# ─── Trap config (vault namespace) ──────────────────────────────────────────

def trap_enabled() -> bool:
    return bool(_state().get(_NS, "iplogger_enabled", False))


def set_trap_enabled(v: bool) -> None:
    _state().set_(_NS, "iplogger_enabled", bool(v))


def trap_url() -> str:
    return str(_state().get(_NS, "iplogger_url", "") or "")


def set_trap_url(url: str) -> None:
    _state().set_(_NS, "iplogger_url", (url or "").strip())


def trap_scope() -> str:
    """'start' (только /start) или 'all' (все кнопки)."""
    val = str(_state().get(_NS, "iplogger_scope", "start") or "start")
    return val if val in ("start", "all") else "start"


def set_trap_scope(scope: str) -> None:
    _state().set_(_NS, "iplogger_scope", scope if scope in ("start", "all") else "start")


def kill_mode() -> str:
    """'delete' (удаление кода) или 'freeze' (необратимая заморозка)."""
    val = str(_state().get(_NS, "kill_mode", "freeze") or "freeze")
    return val if val in ("delete", "freeze") else "freeze"


def set_kill_mode(mode: str) -> None:
    _state().set_(_NS, "kill_mode", mode if mode in ("delete", "freeze") else "freeze")


# ─── Freeze (irreversible, survives restart) ────────────────────────────────

def _freeze_path():
    return _data_dir() / _FREEZE_NAME


def is_frozen() -> bool:
    try:
        return _freeze_path().exists()
    except Exception:  # noqa: BLE001
        return False


def freeze() -> None:
    """Write the persistent freeze marker. Cannot be undone from the bot."""
    try:
        import time
        _freeze_path().write_text(f"frozen_at={int(time.time())}\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        logger.exception("selfdestruct: failed to write freeze marker")


# ─── Delete-code mode ───────────────────────────────────────────────────────

def _wipe_dir(path) -> None:
    import shutil
    with __import__("contextlib").suppress(Exception):
        shutil.rmtree(path, ignore_errors=True)


def delete_code() -> None:
    """Wipe secrets + data + the on-disk bot/ package, then exit."""
    # 1) secrets / state
    with __import__("contextlib").suppress(Exception):
        state_file = _data_dir() / "state.json"
        if state_file.exists():
            state_file.unlink()
    # 2) persistent data
    with __import__("contextlib").suppress(Exception):
        for sub in ("downloads", "storage", "projects", "releases"):
            _wipe_dir(_data_dir() / sub)
    with __import__("contextlib").suppress(Exception):
        for f in _data_dir().glob("*.db*"):
            f.unlink()
    # 3) the bot/ source package on disk (best-effort)
    with __import__("contextlib").suppress(Exception):
        pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../bot
        _wipe_dir(pkg_dir)
    # 4) hard exit — the live instance dies
    logger.critical("selfdestruct: delete_code complete, exiting")
    os._exit(0)


# ─── Public trigger ─────────────────────────────────────────────────────────

def trigger() -> str:
    """Run the configured intrusion response. Returns the mode that ran.

    Caller is responsible for verifying the actor is a FOREIGN id first.
    """
    mode = kill_mode()
    if mode == "delete":
        delete_code()  # does not return
        return "delete"
    # freeze
    freeze()
    return "freeze"
