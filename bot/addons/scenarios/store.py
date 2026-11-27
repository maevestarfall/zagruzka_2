"""Per-chat persistence of routes (ТЗ rev.7.1 §11).

Persist layer — :mod:`bot.addons.state` (per-chat ``chat_get``/``chat_set``):
- ``list``            -> list of route ids for the chat;
- ``item:<id>``       -> serialised :class:`Route` dict.

Secrets (§11): логины/пароли/почты **не хранятся** в открытом виде.
Секрет уходит в vault (``encrypt_data`` + ``push_entry``), а в ``Route``
остаётся только ``vault_ref`` — id записи сейфа.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from .. import state
from .models import Route

logger = logging.getLogger(__name__)

ADDON = "scenarios"

# Anything that looks like a secret in a "type" value.  We deliberately do
# NOT treat "{var}" placeholders as secrets — those are runtime variables.
_SECRET_HINTS = re.compile(r"парол|password|секрет|secret|token|ключ", re.I)


# ---- CRUD ----

def list_ids(chat_id: int) -> list[str]:
    return list(state.chat_get(ADDON, chat_id, "list", []) or [])


def load(chat_id: int, route_id: str) -> Route | None:
    raw = state.chat_get(ADDON, chat_id, f"item:{route_id}", None)
    if not raw:
        return None
    try:
        return Route.from_dict(raw)
    except Exception:  # noqa: BLE001
        logger.exception("scenarios: broken route %s", route_id)
        return None


def save(chat_id: int, route: Route) -> None:
    if not route.id:
        route.id = uuid.uuid4().hex[:10]
    # Never persist runtime vars (may contain emails/codes) — §11.
    d = route.to_dict()
    d["vars"] = {}
    state.chat_set(ADDON, chat_id, f"item:{route.id}", d)
    ids = list_ids(chat_id)
    if route.id not in ids:
        ids.append(route.id)
        state.chat_set(ADDON, chat_id, "list", ids)


def delete(chat_id: int, route_id: str) -> bool:
    ids = list_ids(chat_id)
    if route_id in ids:
        ids.remove(route_id)
        state.chat_set(ADDON, chat_id, "list", ids)
    existed = state.chat_get(ADDON, chat_id, f"item:{route_id}", None) is not None
    state.chat_set(ADDON, chat_id, f"item:{route_id}", None)
    return existed


def load_all(chat_id: int) -> list[Route]:
    routes: list[Route] = []
    for rid in list_ids(chat_id):
        r = load(chat_id, rid)
        if r is not None:
            routes.append(r)
    return routes


# ---- Secrets via vault (§11) ----

def stash_secret(title: str, secret_value: str) -> str | None:
    """Encrypt ``secret_value`` into the vault; return the vault entry id.

    В ``Route`` хранится только этот id (``Step.vault_ref``) — сам секрет
    в state сценариев не попадает (§11, критерий приёмки №9).
    """
    try:
        from ..vault.handlers import encrypt_data, push_entry
    except Exception:  # noqa: BLE001
        logger.warning("scenarios: vault unavailable, secret NOT stored")
        return None
    entry_id = f"vault_{int(time.time() * 1000)}"
    try:
        entry = {
            "id": entry_id,
            "title": f"Сценарий: {title}",
            "category": "credentials",
            "cipher_text": encrypt_data({"secret": secret_value}),
            "source": "scenarios",
            "source_info": "route secret",
            "is_working": None,
        }
        push_entry(entry)
        return entry_id
    except Exception:  # noqa: BLE001
        logger.exception("scenarios: failed to stash secret in vault")
        return None


def fetch_secret(vault_ref: str) -> str | None:
    """Resolve a ``vault_ref`` back to the plain secret at runtime."""
    try:
        from ..vault.handlers import _get_entries, decrypt_data
    except Exception:  # noqa: BLE001
        logger.warning("scenarios: vault unavailable")
        return None
    try:
        for e in _get_entries():
            if e.get("id") == vault_ref:
                return decrypt_data(e["cipher_text"]).get("secret")
    except Exception:  # noqa: BLE001
        logger.exception("scenarios: failed to fetch secret %s", vault_ref)
    return None


def sanitize_route_secrets(route: Route) -> Route:
    """Move plain secrets out of ``type`` values into the vault (§11).

    Any ``type`` step whose ``note``/``value`` looks like a password gets its
    value replaced by a ``vault_ref``.  ``{var}`` placeholders are left as is.
    """
    for stage in route.stages:
        for step in stage.steps:
            if step.op != "type":
                continue
            if step.vault_ref:
                # Секрет уже в сейфе — открытое значение из маршрута убираем.
                step.value = None
                continue
            val = str(step.value or "")
            if not val or val.startswith("{"):
                continue
            hint = f"{step.note or ''} {step.target or ''}"
            if _SECRET_HINTS.search(hint):
                ref = stash_secret(route.title, val)
                if ref:
                    step.vault_ref = ref
                    step.value = None
    return route


def resolve_step_secret(step_value: Any, vault_ref: str | None) -> Any:
    """Runtime helper: prefer the vault secret when the step carries a ref."""
    if vault_ref:
        secret = fetch_secret(vault_ref)
        if secret is not None:
            return secret
    return step_value
