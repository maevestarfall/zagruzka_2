"""Regression test: issuance handlers import a single-card wrapper that
must exist in core (was an incomplete-merge bug — handlers imported
``simulate_verification`` while core only defined the ``_async`` form,
which disabled the whole CARD addon at startup).
"""

from __future__ import annotations

import asyncio


def test_core_exposes_single_card_wrapper():
    from bot.addons.issuance import core

    assert hasattr(core, "simulate_verification")


def test_handlers_module_imports():
    # Importing the handlers module must not raise (it imports the wrapper).
    import importlib

    mod = importlib.import_module("bot.addons.issuance.handlers")
    assert hasattr(mod, "build_issuance_router")


def test_simulate_verification_runs_on_test_card():
    from bot.addons.issuance.core import simulate_verification

    # 4242 4242 4242 4242 → Luhn-valid Stripe test card → LIVE.
    res = asyncio.run(simulate_verification("4242424242424242", "12", "123"))
    assert isinstance(res, dict)
    assert res.get("status") in {"LIVE", "DIE"}
