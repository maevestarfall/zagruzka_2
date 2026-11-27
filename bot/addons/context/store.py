# Context addon store: model context-test "clay", paging memory-DB, timer continuations.
# Co-authored with CoCo
"""Storage for «🧠 Контекст».

Three concerns:
* **clay** — measured context capacity per model+API («клеймо»): how much
  the model actually remembers. Saved in state (mirrored to Vault as a note).
* **paging memory-DB** — snapshot of a big ТЗ + files + progress on /data,
  keyed by task_id, so a heavy task can be done in context-sized chunks.
* **timers** — pending continuation prompts (task_id, due_ts, prompt) that a
  scheduler fires to resume the next chunk.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from .. import state as addon_state

logger = logging.getLogger(__name__)

ADDON = "context"


def _dir():
    from ...config import DATA_DIR
    d = DATA_DIR / "storage" / "context"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─── Brain slots ────────────────────────────────────────────────────────────

def list_brains() -> list[dict]:
    """Return [{slot, label, model}] for the two brain slots."""
    from ...storage import storage
    out = []
    active = storage.get_active_brain_slot()
    for slot in ("1", "2"):
        model = ""
        with __import__("contextlib").suppress(Exception):
            if slot == active:
                model = storage.get_model() or ""
        out.append({"slot": slot, "label": f"Мозг {slot}" + (" 🟢" if slot == active else ""),
                    "model": model})
    return out


def current_model_key() -> str:
    from ...storage import storage
    with __import__("contextlib").suppress(Exception):
        return (storage.get_model() or "unknown") + "@" + storage.get_provider()
    return "unknown"


# ─── Clay (measured capacity per model) ──────────────────────────────────────

def save_clay(model_key: str, data: dict) -> None:
    clay = addon_state.get(ADDON, "clay", {}) or {}
    clay[model_key] = {**data, "ts": int(time.time())}
    addon_state.set_(ADDON, "clay", clay)
    # Mirror to a human-readable file (import/export).
    with __import__("contextlib").suppress(Exception):
        (_dir() / "clay.json").write_text(
            json.dumps(clay, ensure_ascii=False, indent=2), encoding="utf-8")


def get_clay(model_key: str) -> dict | None:
    clay = addon_state.get(ADDON, "clay", {}) or {}
    return clay.get(model_key)


def all_clay() -> dict:
    return addon_state.get(ADDON, "clay", {}) or {}


def recommended_chunk_chars(model_key: str | None = None) -> int:
    """Recommended ТЗ chunk size (chars) from the clay, default 6000."""
    key = model_key or current_model_key()
    c = get_clay(key) or {}
    return int(c.get("chunk_chars") or 6000)


# ─── Paging memory-DB (per task) ─────────────────────────────────────────────

def new_task(tz_text: str, chunks: list[str]) -> str:
    tid = uuid.uuid4().hex[:8]
    rec = {
        "id": tid,
        "tz": tz_text,
        "chunks": chunks,
        "done": [],          # accumulated results per chunk
        "idx": 0,
        "created": int(time.time()),
    }
    (_dir() / f"task_{tid}.json").write_text(
        json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return tid


def load_task(tid: str) -> dict | None:
    p = _dir() / f"task_{tid}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def save_task(rec: dict) -> None:
    tid = rec.get("id")
    if tid:
        (_dir() / f"task_{tid}.json").write_text(
            json.dumps(rec, ensure_ascii=False), encoding="utf-8")


# ─── Timer continuations ─────────────────────────────────────────────────────

def add_timer(task_id: str, due_ts: int, prompt: str, chat_id: int) -> None:
    timers = addon_state.get(ADDON, "timers", []) or []
    timers.append({"task_id": task_id, "due": int(due_ts), "prompt": prompt,
                   "chat_id": int(chat_id)})
    addon_state.set_(ADDON, "timers", timers)


def due_timers(now: int | None = None) -> list[dict]:
    now = now or int(time.time())
    timers = addon_state.get(ADDON, "timers", []) or []
    return [t for t in timers if int(t.get("due", 0)) <= now]


def pop_timer(task_id: str, due: int) -> None:
    timers = addon_state.get(ADDON, "timers", []) or []
    kept = [t for t in timers if not (t.get("task_id") == task_id and int(t.get("due", 0)) == due)]
    addon_state.set_(ADDON, "timers", kept)
