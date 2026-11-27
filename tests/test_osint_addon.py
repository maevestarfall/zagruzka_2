"""Tests for the OSINT addon — catalog integrity and router wiring."""

from __future__ import annotations


def test_catalog_has_categories_and_tools():
    from bot.addons.osint.catalog import CATALOG, CATEGORY_ORDER, SOURCE_URL

    assert SOURCE_URL.startswith("https://github.com/")
    assert len(CATEGORY_ORDER) >= 10
    total = 0
    for key in CATEGORY_ORDER:
        assert key in CATALOG, f"{key} missing from CATALOG"
        cat = CATALOG[key]
        assert cat["label"] and cat["emoji"]
        for name, url in cat["items"]:
            assert name and url.startswith(("http://", "https://"))
            total += 1
    assert total >= 50, f"expected a rich catalog, got {total} tools"


def test_router_registers_main_entry():
    from bot.addons.osint.handlers import build_osint_router

    router = build_osint_router()
    assert router.name == "osint_addon"
    # At least the home + ai + category handlers registered.
    assert len(router.callback_query.handlers) >= 4


def test_keyboards_build_without_error():
    from bot.addons.osint.handlers import _kb_category, _kb_home
    from bot.addons.osint.catalog import CATEGORY_ORDER

    home = _kb_home()
    assert home.inline_keyboard
    for key in CATEGORY_ORDER:
        kb = _kb_category(key)
        assert kb.inline_keyboard  # never empty (always has a back row)
