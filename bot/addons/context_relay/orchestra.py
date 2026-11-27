# «🎼 Оркестр» — per-role model slots for the TEMPORAL RELAY pipeline.
# Co-authored with CoCo
"""20 model slots, each configured like «Другое» (api_key / base_url / model)
plus a set of ROLES it plays in the «🧭 Контекст» pipeline.

The owner assigns each slot one or several relay roles. If a slot has several
roles it simply serves them in turn (the pipeline is sequential by phase, so
a slot waits for its phase, does that role, then can serve another later).
Roles are kept extensible so «дебатёр» and other-project roles can be added
later; for now they map to the relay pipeline.
"""

from __future__ import annotations

from .. import state as addon_state

ADDON = "orchestra"
MAX_SLOTS = 20

# Relay roles a slot can be assigned. (key, human label)
ROLES: list[tuple[str, str]] = [
    ("decomposer", "Декомпозитор"),
    ("coder", "Кодер"),
    ("comparator", "Компаратор"),
    ("refiner", "Рефайнер"),
    ("supervisor", "Супервайзер"),
    ("assembler", "Ассемблер"),
]
ROLE_KEYS = [k for k, _ in ROLES]
ROLE_LABEL = dict(ROLES)


def _all() -> dict:
    v = addon_state.get(ADDON, "slots", None)
    return dict(v) if isinstance(v, dict) else {}


def _save(d: dict) -> None:
    addon_state.set_(ADDON, "slots", d)


def get_slot(i: int) -> dict:
    return dict(_all().get(str(i), {}))


def set_slot_field(i: int, field: str, value) -> None:
    d = _all()
    slot = dict(d.get(str(i), {}))
    slot[field] = value
    d[str(i)] = slot
    _save(d)


def set_config(i: int, api_key: str, base_url: str, model: str) -> None:
    d = _all()
    slot = dict(d.get(str(i), {}))
    slot.update({
        "api_key": (api_key or "").strip(),
        "base_url": (base_url or "").strip(),
        "model": (model or "").strip(),
    })
    d[str(i)] = slot
    _save(d)


def clear_slot(i: int) -> None:
    d = _all()
    if str(i) in d:
        del d[str(i)]
        _save(d)


def is_configured(i: int) -> bool:
    s = get_slot(i)
    return bool(s.get("api_key") and s.get("model"))


def get_roles(i: int) -> list[str]:
    r = get_slot(i).get("roles")
    return [x for x in r if x in ROLE_KEYS] if isinstance(r, list) else []


def toggle_role(i: int, role: str) -> None:
    if role not in ROLE_KEYS:
        return
    roles = set(get_roles(i))
    roles.symmetric_difference_update({role})
    set_slot_field(i, "roles", [k for k in ROLE_KEYS if k in roles])


def slots_for_role(role: str) -> list[dict]:
    """Configured slots assigned ``role`` (in slot-index order)."""
    out = []
    d = _all()
    for i in range(MAX_SLOTS):
        s = d.get(str(i))
        if not s:
            continue
        if s.get("api_key") and s.get("model") and role in (s.get("roles") or []):
            out.append({**s, "idx": i})
    return out


def configured_count() -> int:
    return sum(1 for i in range(MAX_SLOTS) if is_configured(i))


def slot_label(i: int) -> str:
    s = get_slot(i)
    if not (s.get("api_key") and s.get("model")):
        return f"Слот {i + 1} — пусто"
    roles = get_roles(i)
    rlbl = ", ".join(ROLE_LABEL[r] for r in roles) if roles else "без роли"
    return f"Слот {i + 1} — {s.get('model')} [{rlbl}]"
