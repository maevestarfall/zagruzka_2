"""Settings + last-scan cache for the «🔧 Регенерация» addon.

State lives in lilush ``state.json`` under ``addons.regen.*``.
"""

from __future__ import annotations

import time

from .. import state as addon_state

ADDON = "regen"

# Background analysis toggle + mode.
DEFAULT_BG = False
DEFAULT_MODE = "sim"  # "sim" (static, no side effects) | "real" (also self-ping)
DEFAULT_INTERVAL_MIN = 360  # 6h


def bg_enabled() -> bool:
    return bool(addon_state.get(ADDON, "bg_enabled", DEFAULT_BG))


def set_bg(enabled: bool) -> None:
    addon_state.set_(ADDON, "bg_enabled", bool(enabled))


def mode() -> str:
    m = addon_state.get(ADDON, "mode", DEFAULT_MODE)
    return m if m in ("sim", "real") else DEFAULT_MODE


def set_mode(m: str) -> None:
    addon_state.set_(ADDON, "mode", m if m in ("sim", "real") else DEFAULT_MODE)


def interval_min() -> int:
    try:
        return max(15, int(addon_state.get(ADDON, "interval_min", DEFAULT_INTERVAL_MIN)))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_MIN


def notify_chat_id() -> int | None:
    return addon_state.get(ADDON, "notify_chat_id", None)


def set_notify_chat_id(chat_id: int | None) -> None:
    addon_state.set_(ADDON, "notify_chat_id", chat_id)


def save_report(report: dict) -> None:
    """Cache the last scan report (issues + counts + ts)."""
    addon_state.set_(
        ADDON,
        "last_report",
        {
            "ts": int(time.time()),
            "counts": report.get("counts", {}),
            "issues": report.get("issues", [])[:40],
            "ruff_available": report.get("ruff_available", False),
            "fatal": report.get("fatal"),
        },
    )


def last_report() -> dict | None:
    return addon_state.get(ADDON, "last_report", None)


def issue_by_id(issue_id: str) -> dict | None:
    rep = last_report()
    if not rep:
        return None
    for it in rep.get("issues", []):
        if it.get("id") == issue_id:
            return it
    return None


def known_issue_keys() -> list[str]:
    return addon_state.get(ADDON, "known_keys", []) or []


def set_known_issue_keys(keys: list[str]) -> None:
    addon_state.set_(ADDON, "known_keys", keys[:200])


# --------------------------------------------------------------------------
# Live job status (for the dynamic 🟢/🟡 status button). The status is
# 🟡 «работаю» while the coder is solving a problem (asked manually OR a
# problem that arrived in the background and the user pressed Регенерация),
# and 🟢 «всё сделано» once it's solved — exactly as Mason asked.
# --------------------------------------------------------------------------
def job_active() -> bool:
    return bool(addon_state.get(ADDON, "job_active", False))


def job_label() -> str:
    return addon_state.get(ADDON, "job_label", "") or ""


def job_started_ts() -> int:
    try:
        return int(addon_state.get(ADDON, "job_started_ts", 0) or 0)
    except (TypeError, ValueError):
        return 0


def start_job(label: str = "") -> None:
    addon_state.set_(ADDON, "job_active", True)
    addon_state.set_(ADDON, "job_label", str(label or ""))
    addon_state.set_(ADDON, "job_started_ts", int(time.time()))


def finish_job() -> None:
    addon_state.set_(ADDON, "job_active", False)
    addon_state.set_(ADDON, "job_label", "")


# --------------------------------------------------------------------------
# Auto-output format: when set, a freshly written fix is delivered
# automatically in this format («авто») instead of waiting for the user to
# tap «исполнить». None → always ask.
# --------------------------------------------------------------------------
_VALID_OUTPUTS = ("zip", "json", "desc")


def auto_output() -> str | None:
    v = addon_state.get(ADDON, "auto_output", None)
    return v if v in _VALID_OUTPUTS else None


def set_auto_output(kind: str | None) -> None:
    addon_state.set_(ADDON, "auto_output", kind if kind in _VALID_OUTPUTS else None)


# --------------------------------------------------------------------------
# «Разрешить перенастроить через админку» — permission flag the user grants
# from the error screen so the bot may be reconfigured via the VISION panel.
# --------------------------------------------------------------------------
def admin_fix_allowed() -> bool:
    return bool(addon_state.get(ADDON, "admin_fix_allowed", False))


def set_admin_fix_allowed(allowed: bool) -> None:
    addon_state.set_(ADDON, "admin_fix_allowed", bool(allowed))


# --------------------------------------------------------------------------
# Last error the bot showed the user (captured from the global error handler
# and the agent-error replies). «🔧 Регенерация» on the error screen seeds
# the coder with this.
# --------------------------------------------------------------------------
def record_error(text: str, *, where: str = "") -> None:
    addon_state.set_(
        ADDON,
        "last_error",
        {"ts": int(time.time()), "text": (text or "")[:2000], "where": (where or "")[:200]},
    )


def last_error() -> dict | None:
    return addon_state.get(ADDON, "last_error", None)


def clear_error() -> None:
    addon_state.set_(ADDON, "last_error", None)
