"""Light handler-layer tests for «🛠 Master Room».

Aiogram handlers are hard to exercise without a full Bot harness, so
these tests focus on the pure functions inside :mod:`handlers` that
build keyboards and render screens — no FSM dispatch, no message
sending. Keeps coverage meaningful without booting telegram.

Covers:

* ``_render_home`` mentions all four counters and label strings
* ``_render_servers`` empty / non-empty branches
* ``_render_github`` empty / non-empty branches
* ``_render_projects`` empty / non-empty branches
* ``_kb_home`` returns the eight-button submenu in two-column layout
* ``_kb_stack_picker`` lists every stack and includes Назад
* ``_kb_budget_picker`` lists every budget choice
* ``build_masterroom_router`` returns a Router named ``masterroom`` and
  picks up every callback prefix we expect to handle
"""

from __future__ import annotations

import pytest

from bot.addons.masterroom import handlers, state as mr_state
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


def test_render_home_empty(fresh_mr_state):
    body = handlers._render_home()
    assert "Master Room" in body
    assert "Кодер:" in body
    assert "не задан" in body
    assert "Подключи кодер-сервер" in body


def test_render_home_with_state(fresh_mr_state):
    mr_state.add_server("prod", "https://c", "tok")
    mr_state.add_github_account("main", "chloe", "GH_TOK", "private")
    body = handlers._render_home()
    assert "prod" in body
    assert "main" in body
    # no nag once we have a server
    assert "Подключи кодер-сервер" not in body


def test_render_servers_empty(fresh_mr_state):
    body = handlers._render_servers()
    assert "Список пуст" in body


def test_render_servers_non_empty(fresh_mr_state):
    mr_state.add_server("alpha", "https://alpha", "t")
    body = handlers._render_servers()
    assert "alpha" in body
    assert "https://alpha" in body
    assert "🟢" in body  # auto-active marker


def test_render_github_empty(fresh_mr_state):
    body = handlers._render_github()
    assert "Список пуст" in body


def test_render_github_non_empty(fresh_mr_state):
    mr_state.add_github_account("main", "chloe", "GH_TOK", "public")
    body = handlers._render_github()
    assert "chloe" in body
    assert "GH_TOK" in body
    assert "public" in body


def test_render_projects_empty(fresh_mr_state):
    body = handlers._render_projects()
    assert "Пусто" in body


def test_render_projects_non_empty(fresh_mr_state):
    mr_state.add_project({"title": "demo", "stack": "react", "status": "coding"})
    body = handlers._render_projects()
    assert "demo" in body
    assert "react" in body


def test_kb_home_has_twelve_buttons():
    kb = handlers._kb_home()
    flat = [b for row in kb.inline_keyboard for b in row]
    # 11 main (incl. 🔐 pdsync, ЧАСТЬ 18.2) + 1 back
    assert len(flat) == 12
    callbacks = {b.callback_data for b in flat}
    expected = {
        "mr:status", "mr:servers", "mr:new", "mr:projects",
        "mr:github", "mr:targets", "mr:settings", "mr:storage",
        "mr:pdsync", "mr:render", "mr:help", "mr:back",
    }
    assert callbacks == expected


def test_kb_settings_has_zip_toggle_and_download(fresh_mr_state):
    kb = handlers._kb_settings()
    flat = [b for row in kb.inline_keyboard for b in row]
    callbacks = {b.callback_data for b in flat}
    assert "mr:zip:toggle" in callbacks
    assert "mr:zip:download" in callbacks


def test_kb_settings_zip_toggle_reflects_state(fresh_mr_state):
    mr_state.set_zip_export_enabled(False)
    off = [b for row in handlers._kb_settings().inline_keyboard for b in row]
    off_btn = next(b for b in off if b.callback_data == "mr:zip:toggle")
    assert "ВЫКЛ" in off_btn.text
    mr_state.set_zip_export_enabled(True)
    on = [b for row in handlers._kb_settings().inline_keyboard for b in row]
    on_btn = next(b for b in on if b.callback_data == "mr:zip:toggle")
    assert "ВКЛ" in on_btn.text


def test_kb_stack_picker_includes_all_choices():
    kb = handlers._kb_stack_picker("mr:fsm:stack")
    flat = [b for row in kb.inline_keyboard for b in row]
    # 7 stacks + 1 back
    assert len(flat) == 8
    for choice in mr_state.STACK_CHOICES:
        assert any(b.callback_data == f"mr:fsm:stack:{choice}" for b in flat)


def test_kb_budget_picker_includes_all_choices():
    kb = handlers._kb_budget_picker("mr:fsm:budget")
    flat = [b for row in kb.inline_keyboard for b in row]
    # 4 budgets + 1 back
    assert len(flat) == 5
    for choice in mr_state.BUDGET_CHOICES:
        assert any(b.callback_data == f"mr:fsm:budget:{choice}" for b in flat)


def test_build_router_returns_named_router():
    r = handlers.build_masterroom_router()
    assert r.name == "masterroom"
    # Router has callback + message observers populated
    assert r.callback_query.handlers, "no callback handlers registered"
    assert r.message.handlers, "no message handlers registered"


def test_kb_project_detail_cancellable_when_in_progress():
    p = {"id": "p1", "task_id": "t", "status": "coding",
         "preview_url": "https://x"}
    kb = handlers._kb_project_detail(p)
    flat = [b for row in kb.inline_keyboard for b in row]
    assert any(b.callback_data == "mr:proj:cancel:p1" for b in flat)
    # preview URL becomes URL-button (no callback)
    assert any(b.url == "https://x" for b in flat)


def test_kb_project_detail_no_cancel_when_done():
    p = {"id": "p1", "task_id": "t", "status": "done"}
    kb = handlers._kb_project_detail(p)
    flat = [b for row in kb.inline_keyboard for b in row]
    assert all(b.callback_data != "mr:proj:cancel:p1" for b in flat)
