"""Tests for «🔧 Регенерация» — self-scan, the coder's fix output, the 3
deliverables (zip / json+instr / desc) and the wizard wiring."""

from __future__ import annotations

import json
import zipfile

import pytest


# ----------------------------------------------------------------- scanner

def test_repo_root_points_at_bot_package():
    from bot.addons.regen import scanner

    root = scanner.repo_root()
    assert (root / "bot" / "wizard.py").exists()


@pytest.mark.asyncio
async def test_scan_clean_repo_returns_report():
    from bot.addons.regen import scanner

    report = await scanner.scan(timeout=180)
    assert "counts" in report and "issues" in report
    assert report.get("fatal") is None
    # the project's own code is clean → no hard errors from our checks
    assert report["counts"].get("error", 0) == 0


def test_scan_worker_catches_undefined_name(tmp_path):
    """ruff F821 (undefined name) is the core «incomplete merge» footgun."""
    from pathlib import Path

    from bot.addons.regen import _scan_worker as w

    ruff = w._find_ruff()
    if not ruff:
        pytest.skip("ruff not available in this environment")
    bug_dir = Path(w.repo_root_guess()) if hasattr(w, "repo_root_guess") else None
    # write a throwaway bug module inside the repo, scan, then remove it
    from bot.addons.regen import scanner

    root = scanner.repo_root()
    bug = root / "bot" / "addons" / "_regen_pytest_bug"
    bug.mkdir(parents=True, exist_ok=True)
    (bug / "__init__.py").write_text("def f():\n    return definitely_undefined(1)\n")
    try:
        issues = w._check_ruff(root)
    finally:
        import shutil

        shutil.rmtree(bug, ignore_errors=True)
    hits = [i for i in issues if "_regen_pytest_bug" in i["where"]]
    assert hits, "scanner must flag the injected F821"
    assert hits[0]["kind"].startswith("lint/F821")


def test_read_snippet_returns_code():
    from bot.addons.regen import scanner

    snip = scanner.read_snippet("bot/wizard.py:1", context=5)
    assert "wizard" in snip.lower() or snip


# --------------------------------------------------------------- regen_brain

def test_extract_json_handles_fences_and_noise():
    from bot.addons.coder.regen_brain import _extract_json

    assert _extract_json('{"a": 1}')["a"] == 1
    assert _extract_json('```json\n{"b": 2}\n```')["b"] == 2
    assert _extract_json('prefix {"c": 3} suffix')["c"] == 3
    assert _extract_json("garbage") == {}


def test_fixresult_to_json_roundtrip():
    from bot.addons.coder.regen_brain import FixResult

    fix = FixResult(
        problem="p", root_cause="r", instructions="i",
        files={"bot/x.py": "X = 1\n"}, confidence="high", notes="n",
    )
    d = fix.to_json()
    assert d["files"]["bot/x.py"] == "X = 1\n"
    assert d["problem"] == "p" and d["confidence"] == "high"


# ----------------------------------------------------------------- outputs

def _demo_fix():
    from bot.addons.coder.regen_brain import FixResult

    return FixResult(
        problem="кнопка X: NameError ParseMode",
        root_cause="нет импорта ParseMode",
        instructions="добавь from aiogram.enums import ParseMode",
        files={"bot/addons/_regen_demo.py": "X = 1\ndef ok():\n    return X\n"},
        confidence="high",
        notes="демо",
    )


def test_build_description_is_plain_text():
    from bot.addons.regen import outputs

    desc = outputs.build_description(
        _demo_fix(), {"where": "bot/x.py:10", "kind": "lint/F821", "severity": "error"}
    )
    # _send_long escapes everything → desc must not rely on HTML tags
    assert "<b>" not in desc and "<code>" not in desc
    assert "NameError ParseMode" in desc


def test_build_json_file_contains_full_code(tmp_path):
    from bot.addons.regen import outputs

    path = outputs.build_json_file(_demo_fix(), out_dir=str(tmp_path))
    data = json.load(open(path))
    assert data["fix"]["files"]["bot/addons/_regen_demo.py"].startswith("X = 1")
    assert "how_to_use" in data


def test_build_fix_zip_applies_patch_and_excludes_junk(tmp_path):
    from bot.addons.regen import outputs, scanner

    zip_path, errors, _verify = outputs.build_fix_zip(
        _demo_fix(), scanner.repo_root(), out_dir=str(tmp_path)
    )
    assert errors == []
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
    assert "bot/addons/_regen_demo.py" in names
    assert "REGEN_FIX_NOTES.md" in names
    assert not any(".venv/" in n or "__pycache__" in n for n in names)


def test_build_fix_zip_flags_compile_error(tmp_path):
    from bot.addons.coder.regen_brain import FixResult
    from bot.addons.regen import outputs, scanner

    bad = FixResult(
        problem="p", root_cause="r", instructions="i",
        files={"bot/addons/_regen_bad.py": "def broken(:\n    pass\n"},
    )
    _zip, errors, _verify = outputs.build_fix_zip(bad, scanner.repo_root(), out_dir=str(tmp_path))
    assert errors and "_regen_bad.py" in errors[0]


# ------------------------------------------------------------------- store

def test_store_toggles_persist(tmp_path, monkeypatch):
    from bot.addons import state as addon_state
    from bot.addons.regen import store

    # isolate state file
    monkeypatch.setattr(addon_state, "_PATH", tmp_path / "state.json", raising=False)
    assert store.bg_enabled() in (True, False)
    store.set_mode("real")
    assert store.mode() == "real"
    store.set_mode("nonsense")
    assert store.mode() == "sim"
    assert store.interval_min() >= 15


# -------------------------------------------------------------------- wiring

def test_wizard_has_regen_menu_button_and_handlers():
    import bot.wizard as wz

    assert hasattr(wz, "cb_regen")
    assert hasattr(wz, "regen_capture_problem")
    assert hasattr(wz, "_regen_run_fix")
    assert hasattr(wz, "RegenStates")
    kb = wz._kb_main_after_claim(123)
    flat = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "main:regen" in flat


def test_main_registers_regen_loop():
    import inspect

    import bot.main as m

    src = inspect.getsource(m)
    assert "regen_bg_loop" in src and "notify_chat_id" in src


# ------------------------------------------------ new features (06-15 batch 2)

def test_store_job_status_and_auto_output(tmp_path, monkeypatch):
    from bot.addons import state as addon_state
    from bot.addons.regen import store

    monkeypatch.setattr(addon_state, "_PATH", tmp_path / "state.json", raising=False)
    # job status (drives the dynamic 🟢/🟡 button)
    assert store.job_active() is False
    store.start_job("чиню кнопку")
    assert store.job_active() is True
    assert store.job_label() == "чиню кнопку"
    store.finish_job()
    assert store.job_active() is False
    # auto-output format
    assert store.auto_output() is None
    store.set_auto_output("zip")
    assert store.auto_output() == "zip"
    store.set_auto_output("garbage")
    assert store.auto_output() is None
    # admin-fix permission + last error
    store.set_admin_fix_allowed(True)
    assert store.admin_fix_allowed() is True
    store.record_error("boom", where="agent")
    assert store.last_error()["text"] == "boom"
    store.clear_error()
    assert store.last_error() is None


def test_regen_error_keyboard_has_two_buttons():
    import bot.wizard as wz

    kb = wz.regen_error_keyboard()
    flat = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "regen:fromerr" in flat
    assert "regen:adminfix" in flat


def test_kb_regen_has_dynamic_status_button(tmp_path, monkeypatch):
    from bot.addons import state as addon_state
    from bot.addons.regen import store
    import bot.wizard as wz

    monkeypatch.setattr(addon_state, "_PATH", tmp_path / "state.json", raising=False)
    store.finish_job()
    flat = [b.text for row in wz._kb_regen(1).inline_keyboard for b in row]
    assert any("🟢" in t and "сделано" in t for t in flat)
    store.start_job("x")
    flat = [b.text for row in wz._kb_regen(1).inline_keyboard for b in row]
    assert any("🟡" in t for t in flat)


def test_kb_regen_outputs_marks_auto_and_uses_submenu(tmp_path, monkeypatch):
    from bot.addons import state as addon_state
    from bot.addons.regen import store
    import bot.wizard as wz

    monkeypatch.setattr(addon_state, "_PATH", tmp_path / "state.json", raising=False)
    store.set_auto_output("zip")
    cbs = [b.callback_data for row in wz._kb_regen_outputs("iss1").inline_keyboard for b in row]
    # formats now open a submenu, not deliver directly
    assert "regen:outmenu:zip:iss1" in cbs
    txt = [b.text for row in wz._kb_regen_outputs("iss1").inline_keyboard for b in row]
    assert any("АВТО" in t for t in txt)
    # per-format submenu has авто / исполнить / вернуться
    sub = wz._kb_regen_output_one("zip", "iss1")
    scbs = [b.callback_data for row in sub.inline_keyboard for b in row]
    assert "regen:out:zip:iss1" in scbs  # исполнить
    assert "regen:setauto:zip:iss1" in scbs  # авто
    assert "regen:outback:iss1" in scbs  # вернуться


def test_main_error_handler_registered():
    import inspect
    import bot.main as m

    src = inspect.getsource(m)
    assert "dp.errors" in src and "regen_error_keyboard" in src


def test_agent_error_sites_attach_regen_keyboard():
    import inspect
    import bot.handlers as h
    import bot.groq_handlers as g

    assert "regen_error_keyboard" in inspect.getsource(h)
    assert "regen_error_keyboard" in inspect.getsource(g)
