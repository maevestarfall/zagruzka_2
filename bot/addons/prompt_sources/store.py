# Per-kind (photo/video) prompt-source list storage with seeded defaults.
# Co-authored with CoCo
"""Per-kind prompt-source list storage (photo / video).

Stored under ``_settings.addons.prompt_sources.sources_<kind>`` via the
shared addon-state helper, so it rides along in lilush's single
``state.json`` (no extra files for Render Free to lose).
"""

from __future__ import annotations

from .. import state as addon_state

ADDON = "prompt_sources"
KINDS = ("photo", "video")

# Seeded defaults — well-known galleries of ready-made prompts. Users can
# add/remove freely; these only apply until the user touches the list.
_DEFAULTS: dict[str, list[str]] = {
    "photo": [
        "https://www.bananaprompts.xyz",
        "https://lexica.art",
        "https://prompthero.com/midjourney-prompts",
        "https://openart.ai/discovery",
        "https://civitai.com/images",
    ],
    "video": [
        "https://sjinn.ai/seedance-prompts",
        "https://prompthero.com/sora-prompts",
        "https://www.viggle.ai",
        "https://klingai.com",
        "https://lib.kalodata.com",
    ],
}


def _key(kind: str) -> str:
    return f"sources_{kind}"


def get_sources(kind: str) -> list[str]:
    """Return the current source URLs for ``kind`` (defaults if unset)."""
    if kind not in KINDS:
        return []
    val = addon_state.get(ADDON, _key(kind), None)
    if val is None:
        return list(_DEFAULTS.get(kind, []))
    return list(val)


def _normalize(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def add_source(kind: str, url: str) -> tuple[bool, str]:
    """Add a URL. Returns (ok, message)."""
    if kind not in KINDS:
        return False, "Неизвестный тип."
    norm = _normalize(url)
    if not norm or "." not in norm.split("//", 1)[-1]:
        return False, "Это не похоже на ссылку."
    current = get_sources(kind)
    if norm in current:
        return False, "Такой источник уже есть."
    current.append(norm)
    addon_state.set_(ADDON, _key(kind), current)
    return True, norm


def remove_source(kind: str, index: int) -> bool:
    """Remove the source at ``index``. Returns True on success."""
    current = get_sources(kind)
    if 0 <= index < len(current):
        current.pop(index)
        addon_state.set_(ADDON, _key(kind), current)
        return True
    return False


def reset_sources(kind: str) -> None:
    """Restore the seeded defaults for ``kind``."""
    addon_state.set_(ADDON, _key(kind), list(_DEFAULTS.get(kind, [])))
