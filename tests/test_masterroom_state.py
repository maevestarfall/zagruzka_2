"""State-layer tests for the «🛠 Master Room» addon.

Covers:

* ``reserve_defaults`` seeds full namespace + is idempotent
* Token rotate / disable round-trip
* Multi-server CRUD + active-server switching
* Multi-GitHub-account CRUD + active-account switching
* Targets (render / oracle / custom) — Brains-pattern Set URL, add /
  remove custom, switch active
* Projects CRUD + status update
* Defaults set + clamp to allowed values

All tests use ``fresh_mr_state`` to wipe the namespace between cases so
state never leaks; piggybacks on the same ``storage._settings()`` dict
the addon uses in production.
"""

from __future__ import annotations

import pytest

from bot.addons.masterroom import state as mr_state
from bot.storage import storage


@pytest.fixture
def fresh_mr_state():
    settings = storage._settings()  # noqa: SLF001
    settings.setdefault("addons", {}).pop("masterroom", None)
    storage._save()  # noqa: SLF001
    yield
    settings = storage._settings()  # noqa: SLF001
    settings.setdefault("addons", {}).pop("masterroom", None)
    storage._save()  # noqa: SLF001


def test_reserve_defaults_seeds_namespace(fresh_mr_state):
    out = mr_state.reserve_defaults()
    assert out == {"enabled": False, "token": "", "rotated_at": 0}
    # Re-running must be a no-op (does NOT regenerate token).
    out2 = mr_state.reserve_defaults()
    assert out2 == out
    assert mr_state.get_servers() == []
    assert mr_state.get_github_accounts() == []
    assert mr_state.get_projects() == []
    assert mr_state.get_active_target_id() == mr_state.DEFAULT_TARGET_ID


def test_generate_link_rotates_and_enables(fresh_mr_state):
    assert mr_state.get_token() == ""
    assert mr_state.is_enabled() is False
    t1, link1 = mr_state.generate_link(base_url="https://bot.example")
    assert t1
    assert mr_state.is_enabled() is True
    assert link1 == f"https://bot.example/masterroom/{t1}/"
    t2, _ = mr_state.generate_link(base_url="https://bot.example")
    assert t2 != t1


def test_disable_link_blocks_but_keeps_token(fresh_mr_state):
    token, _ = mr_state.generate_link(base_url="https://x")
    assert mr_state.disable_link() is True
    assert mr_state.is_enabled() is False
    assert mr_state.get_token() == token


def test_servers_crud_with_active_switching(fresh_mr_state):
    s1 = mr_state.add_server("prod", "https://coder.example/", "tok-1")
    # url stripped of trailing slash
    assert s1["url"] == "https://coder.example"
    # First add auto-activates
    assert mr_state.get_active_server_id() == s1["id"]
    assert mr_state.get_active_server() == s1
    s2 = mr_state.add_server("staging", "https://coder-staging.example", "tok-2")
    # Adding a second leaves the first active
    assert mr_state.get_active_server_id() == s1["id"]
    # Switch active
    mr_state.set_active_server_id(s2["id"])
    assert mr_state.get_active_server() == s2
    # Remove active → falls back to remaining
    assert mr_state.remove_server(s2["id"]) is True
    assert mr_state.get_active_server_id() == s1["id"]
    # Remove last → no active
    assert mr_state.remove_server(s1["id"]) is True
    assert mr_state.get_active_server_id() is None
    # Removing unknown returns False
    assert mr_state.remove_server("nope") is False


def test_github_accounts_crud_with_active_switching(fresh_mr_state):
    a1 = mr_state.add_github_account("main", "chloe", "GITHUB_TOKEN", "private")
    assert mr_state.get_active_github_id() == a1["id"]
    a2 = mr_state.add_github_account("alt", "chloe-alt", "GITHUB_TOKEN_ALT", "public")
    assert a2["default_visibility"] == "public"
    assert mr_state.get_active_github_id() == a1["id"]
    mr_state.set_active_github_id(a2["id"])
    assert mr_state.get_active_github_account() == a2
    assert mr_state.get_github_account(a1["id"]) == a1
    assert mr_state.remove_github_account(a2["id"]) is True
    assert mr_state.get_active_github_id() == a1["id"]
    assert mr_state.remove_github_account("nope") is False


def test_invalid_visibility_defaults_to_private(fresh_mr_state):
    a = mr_state.add_github_account("x", "u", "ENV", "bogus")
    assert a["default_visibility"] == "private"


def test_targets_set_url_and_switch_active(fresh_mr_state):
    mr_state.reserve_defaults()
    assert mr_state.set_target_url("render", "https://my.onrender.com") is True
    tgt = mr_state.get_target("render")
    assert tgt["url_template"] == "https://my.onrender.com"
    # Bogus preset id rejected
    assert mr_state.set_target_url("bogus", "https://x") is False
    # Switch active
    mr_state.set_active_target_id("oracle")
    assert mr_state.get_active_target()["id"] == "oracle"


def test_custom_targets_crud(fresh_mr_state):
    c = mr_state.add_custom_target("vercel", "https://v.example/{slug}")
    assert c["id"].startswith("custom:")
    listed = mr_state.list_custom_targets()
    assert len(listed) == 1 and listed[0]["id"] == c["id"]
    # Switch active to custom, remove → fallback to default
    mr_state.set_active_target_id(c["id"])
    raw_id = c["id"].split(":", 1)[1]
    assert mr_state.remove_custom_target(raw_id) is True
    assert mr_state.get_active_target_id() == mr_state.DEFAULT_TARGET_ID
    assert mr_state.list_custom_targets() == []
    assert mr_state.remove_custom_target("nope") is False


def test_projects_crud(fresh_mr_state):
    p = mr_state.add_project({"title": "demo", "stack": "static"})
    assert p["id"] and p["status"] == "queued"
    # Newest first
    p2 = mr_state.add_project({"title": "demo2", "stack": "react"})
    listing = mr_state.get_projects()
    assert listing[0]["id"] == p2["id"]
    # Update
    updated = mr_state.update_project(p["id"], status="done",
                                       preview_url="https://x")
    assert updated["status"] == "done"
    assert mr_state.get_project(p["id"])["preview_url"] == "https://x"
    # Update of unknown
    assert mr_state.update_project("nope", status="done") is None
    # Remove
    assert mr_state.remove_project(p["id"]) is True
    assert mr_state.get_project(p["id"]) is None
    assert mr_state.remove_project("nope") is False


def test_defaults_clamp_to_allowed_values(fresh_mr_state):
    mr_state.reserve_defaults()
    d = mr_state.set_defaults(stack="react", budget=500_000, target_id="oracle")
    assert d == {"stack": "react", "budget": 500_000, "target_id": "oracle"}
    # Bogus values silently ignored — last good value stays
    d2 = mr_state.set_defaults(stack="bogus", budget=42, target_id=None)
    assert d2["stack"] == "react"
    assert d2["budget"] == 500_000
    assert d2["target_id"] == "oracle"


# ─── ZIP project export toggle ────────────────────────────────────────
def test_zip_export_disabled_by_default(fresh_mr_state):
    assert mr_state.get_zip_export_enabled() is False


def test_zip_export_set_and_toggle(fresh_mr_state):
    mr_state.set_zip_export_enabled(True)
    assert mr_state.get_zip_export_enabled() is True
    # toggle flips and returns the new value
    assert mr_state.toggle_zip_export_enabled() is False
    assert mr_state.get_zip_export_enabled() is False
    assert mr_state.toggle_zip_export_enabled() is True
    assert mr_state.get_zip_export_enabled() is True
