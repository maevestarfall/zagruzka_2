"""Centralised "who is allowed to do what" helpers.

The bot has three access modes (see ``Storage.get_access_mode``):

* ``private`` — only the owner + explicit co-owners can use the bot at
  all and change settings (this is the original lilush behaviour).
* ``public`` — anyone can USE the bot's features (chat, photo flows,
  Helpzavr, pretty-text, mailbox, media-generation). Settings —
  API keys, models, statistics, persona switching — stay owner-only.
* ``full_public`` — anyone can use AND change settings (no gating).

``can_use`` is the broad gate; ``can_admin`` is the strict gate. UI
code chooses which one to call depending on whether the action is a
feature or a settings change.

Co-owners (see :func:`Storage.add_co_owner`) are tracked in storage and
always count as admins regardless of the current access mode.
"""

from __future__ import annotations


def _storage():
    """Resolve the current :class:`Storage` singleton lazily.

    Tests sometimes pop ``bot.storage`` from ``sys.modules`` and
    re-import it to get a fresh data directory. Calling :func:`_storage`
    every time means we always read the most-recent module-level
    singleton instead of caching a stale reference from import time.
    """
    from .storage import storage

    return storage


def can_use(user_id: int | None) -> bool:
    """``True`` when ``user_id`` is allowed to use the bot's features."""
    return _storage().can_use_bot(user_id)


def can_admin(user_id: int | None) -> bool:
    """``True`` when ``user_id`` is allowed to change bot settings.

    Used to gate API-key entry, model selection, URL changes, persona
    switching, token statistics, the access-mode picker itself, and the
    co-owner list.
    """
    return _storage().can_admin_bot(user_id)


def is_owner(user_id: int | None) -> bool:
    """``True`` only for the single original owner (not co-owners).

    Used by the ``/start`` ownership-claim flow, where co-owners must
    not be able to "re-claim" the slot. Most other callers want
    :func:`can_admin` instead.
    """
    if user_id is None:
        return False
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    return _storage().get_owner_id() == uid


def guest_can_use(user_id: int | None, feature_key: str) -> bool:
    """Whether ``user_id`` may USE feature ``feature_key``.

    Owners/co-owners always may. When the fine-grained «приват режим» layer
    is OFF this just defers to the broad :func:`can_use` gate. When it is ON,
    a guest needs both the per-feature USE flag (green ✅) and, if any payment
    method is configured, a positive paid balance.
    """
    st = _storage()
    if st.is_owner_or_co_owner(user_id):
        return True
    if not st.pm_is_enabled():
        return can_use(user_id)
    if not st.pm_can_guest_use(feature_key):
        return False
    # If the owner set up paid access, the guest must have credit left.
    if st.pm_enabled_methods():
        return st.pm_has_paid_access(user_id)
    return True


def guest_can_see(user_id: int | None, feature_key: str) -> bool:
    """Whether ``user_id`` may SEE feature ``feature_key`` in menus.

    Owners/co-owners always may. With the fine-grained layer OFF, visibility
    follows :func:`can_use`. With it ON, a guest sees a feature only when its
    per-feature SEE flag (green ✅) is on.
    """
    st = _storage()
    if st.is_owner_or_co_owner(user_id):
        return True
    if not st.pm_is_enabled():
        return can_use(user_id)
    return st.pm_can_guest_see(feature_key)
