# Tests for ЧАСТЬ 18.1: sync category registry, exclusions, counters, config.
# Co-authored with CoCo
"""Приёмка 18.1 (unit-часть):

* каждая сущность → ровно одна категория; неопознанное → ``other``;
* реестр исключений (.frozen, vault, tmp/bak, sync-журнал) не попадает никуда;
* секрет-файлы резолвятся в ``secrets``, а не в ``media``/``other``;
* подсчёт записей/байтов по категориям работает;
* retention по умолчанию — все True; sync-state без секретов.
"""

from __future__ import annotations

import json

import pytest

from bot.addons.masterroom.sync import categories as cat
from bot.addons.masterroom.sync.categories import SyncCategory


# --- реестр исключений --------------------------------------------------

@pytest.mark.parametrize("rel", [
    ".frozen",
    "state.json.tmp",
    "state.json.bak",
    "sync_journal.db",
    "sync_journal.db-wal",
])
def test_excluded_files_resolve_to_none(rel):
    assert cat.category_for_file(rel) is None


def test_state_json_container_is_not_a_file_category():
    assert cat.category_for_file("state.json") is None


@pytest.mark.parametrize("dotted", [
    "_settings.addons.vault",
    "_settings.addons.vault.kill_mode",
    "_settings.addons.vault.iplogger_url",
])
def test_vault_namespace_excluded(dotted):
    assert cat.category_for_state_path(dotted) is None


# --- секрет-файлы > media/other ------------------------------------------

@pytest.mark.parametrize("rel", [
    "yt_cookies.txt",
    "session/ai_browser.png",
    "session/render/storage.json",
    "downloads/some_cookies.txt",   # даже внутри media-директории
])
def test_secret_files_always_secrets(rel):
    assert cat.category_for_file(rel) is SyncCategory.SECRETS


# --- state-секреты ---------------------------------------------------------

@pytest.mark.parametrize("dotted", [
    "_settings.keys.openrouter",
    "_settings.external_tools.apify",
    "_settings.media.slots.1.api_key",
    "_settings.addons.slack.token",           # двойной источник → state-секрет
    "_settings.addons.mailbox.password",
    "_settings.addons.masterroom.token",
    "_settings.addons.masterroom.render_gh_password",
    "_settings.addons.debates.admin_connect_secret",
])
def test_state_secrets(dotted):
    assert cat.category_for_state_path(dotted) is SyncCategory.SECRETS


# --- тематические категории -------------------------------------------------

def test_thematic_resolution():
    assert cat.category_for_state_path(
        "_settings.addons.masterroom.github_accounts"
    ) is SyncCategory.ACCOUNTS
    assert cat.category_for_state_path(
        "_settings.addons.stealth_fp.profiles"
    ) is SyncCategory.FINGERPRINTS
    assert cat.category_for_state_path("123456.history") is SyncCategory.HISTORY
    assert cat.category_for_state_path(
        "_settings.addons.masterroom.projects"
    ) is SyncCategory.HISTORY
    assert cat.category_for_state_path("_settings.model") is SyncCategory.MODULE_STATE
    assert cat.category_for_state_path(
        "_settings.addons.masterroom.defaults.stack"
    ) is SyncCategory.MODULE_STATE


def test_file_resolution():
    assert cat.category_for_file("storage/photo.jpg") is SyncCategory.MEDIA
    assert cat.category_for_file("downloads/video.mp4") is SyncCategory.MEDIA
    assert cat.category_for_file("jobs.db") is SyncCategory.HISTORY
    assert cat.category_for_file("projects/site/index.html") is SyncCategory.HISTORY
    assert cat.category_for_file("bot.log") is SyncCategory.HISTORY


# --- catch-all `other` -------------------------------------------------------

def test_unknown_state_key_falls_to_other():
    assert cat.category_for_state_path("brand_new_top_level") is SyncCategory.OTHER


def test_unknown_file_falls_to_other():
    assert cat.category_for_file("mystery.bin") is SyncCategory.OTHER


def test_every_entity_gets_exactly_one_category():
    # резолвер детерминирован и всегда возвращает категорию или None
    for dotted in ("_settings.keys.openai", "42.cwd", "weird.key"):
        result = cat.category_for_state_path(dotted)
        assert result is None or isinstance(result, SyncCategory)


# --- подсчёт записей / байтов ------------------------------------------------

def test_count_state_records_and_bytes():
    state = {
        "_settings": {
            "model": "some/model",
            "keys": {"openrouter": "sk-secret-value"},
            "addons": {
                "vault": {"kill_mode": "freeze"},   # исключение
                "slack": {"token": "xoxb-abc"},
            },
        },
        "777": {"cwd": None, "history": [{"role": "user", "content": "hi"}]},
    }
    counts = cat.count_state(state)
    assert counts[SyncCategory.SECRETS]["records"] == 2  # openrouter + slack
    assert counts[SyncCategory.MODULE_STATE]["records"] == 1  # model
    assert counts[SyncCategory.HISTORY]["records"] == 2  # cwd + history
    # исключение vault не учтено нигде
    total = sum(v["records"] for v in counts.values())
    assert total == 5
    assert counts[SyncCategory.SECRETS]["bytes"] >= len(
        json.dumps("sk-secret-value")
    )


def test_count_files(tmp_path):
    (tmp_path / "storage").mkdir()
    (tmp_path / "storage" / "a.jpg").write_bytes(b"x" * 10)
    (tmp_path / "yt_cookies.txt").write_text("cookie-data")
    (tmp_path / ".frozen").write_text("frozen_at=1")
    (tmp_path / "state.json.bak").write_text("{}")
    (tmp_path / "jobs.db").write_bytes(b"y" * 5)
    counts = cat.count_files(tmp_path)
    assert counts[SyncCategory.MEDIA] == {"records": 1, "bytes": 10}
    assert counts[SyncCategory.SECRETS]["records"] == 1
    assert counts[SyncCategory.HISTORY] == {"records": 1, "bytes": 5}
    # исключения не попали никуда
    total = sum(v["records"] for v in counts.values())
    assert total == 3


# --- sync-state (retention default all True, без секретов) -------------------

def test_sync_defaults_shape():
    from bot.addons.masterroom import state as mr_state

    d = mr_state._sync_defaults()  # noqa: SLF001
    assert d["db_status"] == "not_connected"
    assert d["last_sync_at"] is None
    assert d["paused"] is False
    assert d["last_error_public"] == ""
    # retention покрывает все 8 категорий, все True
    assert set(d["retention"]) == {c.value for c in SyncCategory}
    assert all(d["retention"].values())
    # никакие секреты в дефолтах не появляются
    payload = json.dumps(d)
    for forbidden in ("DATABASE_URL", "postgres://", "postgresql://"):
        assert forbidden not in payload


# --- config: валидация DATABASE_URL без подключения --------------------------

def test_validate_database_url():
    from bot.config import _validate_database_url

    assert _validate_database_url("") == ""
    assert _validate_database_url("not a url") == ""
    assert _validate_database_url("mysql://u:p@h/db") == ""
    good = "postgresql://user:pass@host.neon.tech:5432/mydb?sslmode=require"
    assert _validate_database_url(good) == good
    assert _validate_database_url("postgres://u:p@h/db") == "postgres://u:p@h/db"
