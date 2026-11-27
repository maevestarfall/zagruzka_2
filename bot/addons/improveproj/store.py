# «➕ Улучшить проект из Важного» — Гермес предлагает и (по кнопке) чинит.
# Co-authored with CoCo
"""State for the «➕ Улучшить проект из Важного» feature.

Гермес читает «❗Важно» (backlog новых функций) + карту проекта, исследует по
инструкции и ПРЕДЛАГАЕТ («было так → станет так → плюсы»). По кнопке «Улучшить»
берёт проект и правит через Кодер. Настройки:
* ``auto``      — при ВКЛ применяет сразу, не спрашивая.
* ``schedule``  — {what: errors|instruction, freq: off|daily|weekly, time: "HH:MM", last_run}.
* ``last_proposal`` — текст последнего предложения (для шага «Улучшить»).
"""

from __future__ import annotations

import time

from .. import state as addon_state

ADDON = "improveproj"

_WHAT = ("errors", "instruction")
_FREQ = ("off", "daily", "weekly")


def get_auto() -> bool:
    return bool(addon_state.get(ADDON, "auto", False))


def set_auto(val: bool) -> None:
    addon_state.set_(ADDON, "auto", bool(val))


def get_last_proposal() -> str:
    return str(addon_state.get(ADDON, "last_proposal", "") or "")


def set_last_proposal(text: str) -> None:
    addon_state.set_(ADDON, "last_proposal", (text or "")[:8000])


def get_schedule() -> dict:
    v = addon_state.get(ADDON, "schedule", None)
    if not isinstance(v, dict):
        v = {}
    return {
        "what": v.get("what", "errors") if v.get("what") in _WHAT else "errors",
        "freq": v.get("freq", "off") if v.get("freq") in _FREQ else "off",
        "time": str(v.get("time", "09:00")),
        "last_run": str(v.get("last_run", "")),
    }


def set_schedule(**kw) -> dict:
    s = get_schedule()
    s.update({k: v for k, v in kw.items() if k in ("what", "freq", "time", "last_run")})
    addon_state.set_(ADDON, "schedule", s)
    return s


def due() -> bool:
    """True if the scheduled improvement should run now (daily/weekly)."""
    s = get_schedule()
    if s["freq"] == "off":
        return False
    now = time.localtime()
    hhmm = time.strftime("%H:%M", now)
    today = time.strftime("%Y-%m-%d", now)
    if hhmm < s["time"]:
        return False
    last = s["last_run"]
    if s["freq"] == "daily":
        return last != today
    # weekly: run if never run or >= 7 days since last_run
    if not last:
        return True
    try:
        last_t = time.mktime(time.strptime(last, "%Y-%m-%d"))
        return (time.time() - last_t) >= 7 * 86400
    except Exception:  # noqa: BLE001
        return True


def mark_ran() -> None:
    set_schedule(last_run=time.strftime("%Y-%m-%d", time.localtime()))


# --- версия проекта (старт 1.201, бамп на каждое улучшение) ------------

def get_version() -> str:
    return str(addon_state.get(ADDON, "version", "1.202") or "1.202")


def bump_version() -> str:
    cur = get_version()
    try:
        major, minor = cur.split(".", 1)
        new = f"{major}.{int(minor) + 1}"
    except Exception:  # noqa: BLE001
        new = "1.203"
    addon_state.set_(ADDON, "version", new)
    return new


# --- решение от Анализатора + «дополнение» (scope) владельца -----------

def get_last_solution() -> str:
    return str(addon_state.get(ADDON, "last_solution", "") or "")


def set_last_solution(text: str) -> None:
    addon_state.set_(ADDON, "last_solution", (text or "")[:8000])


def get_refinement() -> str:
    return str(addon_state.get(ADDON, "refinement", "") or "")


def set_refinement(text: str) -> None:
    addon_state.set_(ADDON, "refinement", (text or "")[:2000])


# --- хранилище: последний zip проекта (база для кодера) ----------------

def _storage_dir():
    from ...config import DATA_DIR
    d = DATA_DIR / "storage"
    d.mkdir(parents=True, exist_ok=True)
    return d


def latest_storage_zip():
    """Path новейшего .zip в хранилище (DATA_DIR/storage) или None."""
    try:
        zips = sorted(_storage_dir().glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        return zips[0] if zips else None
    except Exception:  # noqa: BLE001
        return None
