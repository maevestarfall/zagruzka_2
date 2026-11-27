# Algo-slots store: up to 10 preset slots with images on /data and params in state.
# Co-authored with CoCo
"""Storage for the «🧩 Алгоритм» preset slots.

Each slot is a reusable photo→video recipe:
* ``ref_photo``     — reference image, shown at run start (bytes on /data)
* ``photo_prompt``  — prompt for img2img
* ``preview_photo`` — gallery thumbnail «как смотрится видео» (bytes on /data)
* ``video_prompt``  — prompt for img2video
* ``params``        — {photo:{model,format,quality}, video:{quality,format,model}}

Metadata (prompts, params, names, which images exist) lives in state.json
under the ``algo_slots`` addon namespace; the image bytes live on the
persistent ``/data`` disk so they survive restarts.
"""

from __future__ import annotations

import logging
import uuid

from .. import state as addon_state

logger = logging.getLogger(__name__)

ADDON = "algo_slots"
MAX_SLOTS = 10
GALLERY_MAX = 6


def _slots_dir():
    from ...config import DATA_DIR
    d = DATA_DIR / "storage" / "algo_slots"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─── Slot list ──────────────────────────────────────────────────────────────

def get_slots() -> list[dict]:
    raw = addon_state.get(ADDON, "slots", []) or []
    return [s for s in raw if isinstance(s, dict)]


def _save_slots(slots: list[dict]) -> None:
    addon_state.set_(ADDON, "slots", slots)


def get_slot(slot_id: str) -> dict | None:
    for s in get_slots():
        if s.get("id") == slot_id:
            return s
    return None


def add_slot(name: str = "") -> dict | None:
    slots = get_slots()
    if len(slots) >= MAX_SLOTS:
        return None
    sid = uuid.uuid4().hex[:8]
    slot = {
        "id": sid,
        "name": name.strip() or f"Слот {len(slots) + 1}",
        "photo_prompt": "",
        "video_prompt": "",
        "has_ref": False,
        "has_preview": False,
        "params": {
            "photo": {"model": "", "format": "", "quality": ""},
            "video": {"model": "", "format": "", "quality": ""},
        },
    }
    slots.append(slot)
    _save_slots(slots)
    return slot


def update_slot(slot_id: str, **patch) -> dict | None:
    slots = get_slots()
    for s in slots:
        if s.get("id") == slot_id:
            s.update(patch)
            _save_slots(slots)
            return s
    return None


def set_param(slot_id: str, media: str, key: str, value: str) -> None:
    """media: 'photo'|'video'; key: model|format|quality."""
    slots = get_slots()
    for s in slots:
        if s.get("id") == slot_id:
            params = s.setdefault("params", {})
            params.setdefault(media, {})[key] = value
            _save_slots(slots)
            return


def remove_slot(slot_id: str) -> bool:
    slots = get_slots()
    kept = [s for s in slots if s.get("id") != slot_id]
    if len(kept) == len(slots):
        return False
    _save_slots(kept)
    # wipe images
    import contextlib
    for tag in ("ref", "preview"):
        with contextlib.suppress(Exception):
            (_slots_dir() / f"{slot_id}_{tag}.jpg").unlink()
    return True


# ─── Image bytes on /data ────────────────────────────────────────────────────

def _img_path(slot_id: str, tag: str):
    return _slots_dir() / f"{slot_id}_{tag}.jpg"


def save_image(slot_id: str, tag: str, data: bytes) -> None:
    """tag: 'ref' | 'preview' | 'last_result'. Persist bytes + flag in slot."""
    _img_path(slot_id, tag).write_bytes(data)
    if tag == "ref":
        update_slot(slot_id, has_ref=True)
    elif tag == "preview":
        update_slot(slot_id, has_preview=True)


def load_image(slot_id: str, tag: str) -> bytes | None:
    p = _img_path(slot_id, tag)
    if not p.exists():
        return None
    try:
        return p.read_bytes()
    except Exception:  # noqa: BLE001
        return None
