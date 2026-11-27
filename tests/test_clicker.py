"""Tests for the «🤖 Макрос действий (Кликер)» macro parser + recorder asset."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from bot.addons import clicker
from bot.addons.clicker import recorder


def test_parse_macro_basic():
    steps = clicker.parse_macro(
        "Клик: Запустить проверку\nПауза: 3\nСкопировать: Номер карты\nКлик: Очистить")
    assert steps == [
        {"op": "click", "label": "Запустить проверку"},
        {"op": "pause", "seconds": 3.0},
        {"op": "copy", "label": "Номер карты"},
        {"op": "click", "label": "Очистить"},
    ]


def test_parse_macro_synonyms_and_bare_lines():
    steps = clicker.parse_macro(
        "нажми Войти\nподожди 1.5\ncopy Баланс\nГотово")
    assert steps[0] == {"op": "click", "label": "Войти"}
    assert steps[1] == {"op": "pause", "seconds": 1.5}
    assert steps[2] == {"op": "copy", "label": "Баланс"}
    # bare line with no prefix → treated as a click target
    assert steps[3] == {"op": "click", "label": "Готово"}


def test_parse_macro_pause_clamped_and_empty():
    assert clicker.parse_macro("") == []
    big = clicker.parse_macro("Пауза: 9999")
    assert big[0]["seconds"] == 120.0  # clamped


def test_describe_step():
    assert "Клик" in clicker.describe_step({"op": "click", "label": "X"})
    assert "Пауза" in clicker.describe_step({"op": "pause", "seconds": 2})
    assert "Скопировать" in clicker.describe_step({"op": "copy", "label": "Y"})


def test_router_builds():
    r = clicker.build_clicker_router()
    assert r.name == "clicker"


def test_recorder_userscript_has_features():
    js = recorder.RECORDER_USERSCRIPT
    assert "==UserScript==" in js
    assert "F12" in js and "F9" in js          # hotkeys
    assert "Клик: " in js and "Пауза: " in js and "Скопировать: " in js  # bot format
    assert "GM_setClipboard" in js              # copy-for-bot
    assert "play" in js                          # playback


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_recorder_userscript_is_valid_js(tmp_path):
    p = tmp_path / "rec.user.js"
    p.write_text(recorder.RECORDER_USERSCRIPT, encoding="utf-8")
    res = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
    assert res.returncode == 0, f"userscript has a JS syntax error:\n{res.stderr}"
