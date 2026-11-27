# Tests for ЧАСТЬ 18.4: crypto, secret_store, миграция plaintext, санитизация.
# Co-authored with CoCo
"""Приёмка 18.4:

* без master key: бот не падает, категория честно недоступна, no-op миграция;
* envelope: два шифрования одного значения → разные nonce и ciphertext;
* подмена/порча ciphertext → явная ошибка целостности (IntegrityError);
* обратная расшифровка совпадает с оригиналом (read-back);
* env-секреты (token_ref) НЕ мигрируются; plaintext-оригинал НЕ удаляется;
* повторный запуск миграции — no-op (без дублей и перешифровки);
* в state — ни plaintext-секретов (новых), ни ключей шифрования;
* fallback-чтение: коннекторы работают и с ключом, и без;
* санитизация ловит postgres://, AWS-ключи, Bearer-токены.
"""

from __future__ import annotations

import base64
import copy
import json
import os

import pytest

from bot.addons.masterroom.sync import crypto, migrate_secrets, secret_store
from bot.addons.masterroom.sync.crypto import (
    CryptoUnavailableError,
    IntegrityError,
)
from bot.addons.masterroom.sync.sanitize import REDACTED, sanitize_error_public

MASTER_KEY_B64 = base64.b64encode(b"\x07" * 32).decode("ascii")


@pytest.fixture()
def master_key(monkeypatch):
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, MASTER_KEY_B64)


@pytest.fixture()
def no_master_key(monkeypatch):
    monkeypatch.delenv(crypto.MASTER_KEY_ENV, raising=False)


@pytest.fixture()
def mem_state(monkeypatch):
    """In-memory подмена bot.addons.state — живой state.json не трогается."""
    import bot.addons.state as addon_state

    store: dict[tuple[str, str], object] = {}

    def _get(addon, key, default=None):
        return copy.deepcopy(store.get((addon, key), default))

    def _set(addon, key, value):
        store[(addon, key)] = copy.deepcopy(value)

    def _delete(addon, key):
        return store.pop((addon, key), None) is not None

    monkeypatch.setattr(addon_state, "get", _get)
    monkeypatch.setattr(addon_state, "set_", _set)
    monkeypatch.setattr(addon_state, "delete", _delete)
    return store


# ─── без master key: штатная деградация ──────────────────────────────────────


def test_unavailable_without_master_key(no_master_key):
    assert crypto.is_available() is False
    assert secret_store.is_available() is False
    assert crypto.MASTER_KEY_ENV in crypto.unavailable_reason()
    with pytest.raises(CryptoUnavailableError):
        crypto.encrypt("top-secret")


def test_invalid_master_key_is_unavailable(monkeypatch):
    monkeypatch.setenv(crypto.MASTER_KEY_ENV, "not-base64!!")
    assert crypto.is_available() is False
    monkeypatch.setenv(
        crypto.MASTER_KEY_ENV, base64.b64encode(b"short").decode("ascii"),
    )
    assert crypto.is_available() is False


def test_migration_noop_without_master_key(no_master_key, mem_state):
    report = migrate_secrets.migrate_plaintext_secrets(
        {"_settings": {"keys": {"openai": "sk-live-123"}}}
    )
    assert report.available is False
    assert report.reason
    assert not report.migrated and not report.failed


# ─── envelope encryption ─────────────────────────────────────────────────────


def test_roundtrip_and_unique_nonce_ciphertext(master_key):
    env1 = crypto.encrypt("same-value", aad="k1")
    env2 = crypto.encrypt("same-value", aad="k1")
    # приёмка: два шифрования одного значения → разные nonce и ciphertext
    assert env1["nonce"] != env2["nonce"]
    assert env1["key_nonce"] != env2["key_nonce"]
    assert env1["ct"] != env2["ct"]
    assert crypto.decrypt_text(env1) == "same-value"
    assert crypto.decrypt_text(env2) == "same-value"


def test_tampered_ciphertext_raises_integrity_error(master_key):
    env = crypto.encrypt("secret-token", aad="k")
    ct = bytearray(base64.b64decode(env["ct"]))
    ct[0] ^= 0xFF
    env["ct"] = base64.b64encode(bytes(ct)).decode("ascii")
    with pytest.raises(IntegrityError):
        crypto.decrypt(env)


def test_tampered_wrapped_key_raises_integrity_error(master_key):
    env = crypto.encrypt("secret-token")
    wk = bytearray(base64.b64decode(env["wrapped_key"]))
    wk[-1] ^= 0x01
    env["wrapped_key"] = base64.b64encode(bytes(wk)).decode("ascii")
    with pytest.raises(IntegrityError):
        crypto.decrypt(env)


def test_swapped_aad_raises_integrity_error(master_key):
    env = crypto.encrypt("secret", aad="entry-a")
    with pytest.raises(IntegrityError):
        crypto.decrypt(env, aad="entry-b")


def test_envelope_contains_no_plaintext_or_master_key(master_key):
    env = crypto.encrypt("hunter2-plaintext")
    blob = json.dumps(env)
    assert "hunter2-plaintext" not in blob
    assert MASTER_KEY_B64 not in blob


def test_argon2id_password_hash_never_plaintext(master_key):
    hashed = crypto.hash_password("S3cretPass!")
    assert hashed.startswith("$argon2id$")
    assert "S3cretPass!" not in hashed
    assert crypto.verify_password(hashed, "S3cretPass!") is True
    assert crypto.verify_password(hashed, "wrong") is False
    assert crypto.verify_password("garbage", "S3cretPass!") is False


# ─── защищённое хранилище ────────────────────────────────────────────────────


def test_secret_store_roundtrip_only_ciphertext_in_state(master_key, mem_state):
    secret_store.put_secret("_settings.keys.openai", "sk-live-abc")
    assert secret_store.has_secret("_settings.keys.openai")
    assert secret_store.get_secret("_settings.keys.openai") == "sk-live-abc"
    # в state — только ciphertext + метаданные, без plaintext и без ключей
    blob = json.dumps(mem_state[("masterroom", "secret_store")])
    assert "sk-live-abc" not in blob
    assert MASTER_KEY_B64 not in blob
    assert secret_store.delete_secret("_settings.keys.openai") is True
    assert secret_store.get_secret("_settings.keys.openai") is None


def test_fallback_reader(master_key, mem_state, monkeypatch):
    # записи нет → legacy plaintext
    assert secret_store.read_secret_with_fallback("k", "legacy") == "legacy"
    # запись есть → защищённое значение
    secret_store.put_secret("k", "encrypted-value")
    assert secret_store.read_secret_with_fallback("k", "legacy") == "encrypted-value"
    # нет master key → честный откат на legacy, без исключений
    monkeypatch.delenv(crypto.MASTER_KEY_ENV, raising=False)
    assert secret_store.read_secret_with_fallback("k", "legacy") == "legacy"


def test_file_payload_roundtrip_and_tamper(master_key):
    data = b"# Netscape HTTP Cookie File\nyt\tTRUE\t/\tSSID\tabc123\n"
    payload = secret_store.encrypt_file_payload(data, rel_path="yt_cookies.txt")
    assert b"abc123" not in payload  # в S3 уходит только ciphertext
    assert secret_store.decrypt_file_payload(
        payload, rel_path="yt_cookies.txt",
    ) == data
    with pytest.raises(IntegrityError):
        secret_store.decrypt_file_payload(payload, rel_path="other.txt")


# ─── миграция legacy plaintext ───────────────────────────────────────────────


def _fake_state() -> dict:
    return {
        "_settings": {
            "keys": {"openai": "sk-live-openai"},
            "addons": {
                "slack": {"token": "xoxb-slack-token", "enabled": True},
                "masterroom": {
                    "token": "mr-link-token",
                    "github_accounts": [
                        # основной путь: token_ref (env) — НЕ мигрируется
                        {"id": "gh1", "owner": "me", "token_ref": "GITHUB_TOKEN"},
                        # legacy plaintext token — мигрируется по id
                        {"id": "gh2", "owner": "me", "token": "ghp_plain"},
                    ],
                },
            },
        },
    }


def test_migration_migrates_verifies_and_keeps_plaintext(master_key, mem_state):
    state = _fake_state()
    before = copy.deepcopy(state)
    report = migrate_secrets.migrate_plaintext_secrets(state)
    assert report.ok, report.failed
    assert "_settings.addons.slack.token" in report.migrated
    assert "_settings.keys.openai" in report.migrated
    assert (
        "_settings.addons.masterroom.github_accounts.gh2.token"
        in report.migrated
    )
    # env-ref (token_ref) НЕ мигрирован
    assert not any("gh1" in k or "token_ref" in k for k in report.migrated)
    # обратная расшифровка совпадает с оригиналом
    assert secret_store.get_secret(
        "_settings.addons.slack.token") == "xoxb-slack-token"
    # plaintext-оригинал НЕ удалён (удаление — только 18.7)
    assert state == before


def test_migration_is_idempotent(master_key, mem_state):
    state = _fake_state()
    first = migrate_secrets.migrate_plaintext_secrets(state)
    assert first.migrated
    second = migrate_secrets.migrate_plaintext_secrets(state)
    # повторный запуск — no-op: без дублей и без перешифровки
    assert not second.migrated and not second.updated and not second.failed
    assert set(second.skipped) == set(first.migrated)
    keys = secret_store.list_secret_keys()
    assert len(keys) == len(set(keys)) == len(first.migrated)


def test_migration_reencrypts_changed_plaintext(master_key, mem_state):
    state = _fake_state()
    migrate_secrets.migrate_plaintext_secrets(state)
    state["_settings"]["addons"]["slack"]["token"] = "xoxb-rotated"
    report = migrate_secrets.migrate_plaintext_secrets(state)
    assert "_settings.addons.slack.token" in report.updated
    assert secret_store.get_secret(
        "_settings.addons.slack.token") == "xoxb-rotated"


def test_migration_never_touches_own_bucket(master_key, mem_state):
    state = _fake_state()
    migrate_secrets.migrate_plaintext_secrets(state)
    state["_settings"]["addons"]["masterroom"]["secret_store"] = (
        mem_state[("masterroom", "secret_store")]
    )
    report = migrate_secrets.migrate_plaintext_secrets(state)
    assert not any("secret_store" in k for k in
                   report.migrated + report.updated + report.failed)


# ─── санитизация (18.4 §4: postgres://, AWS-ключи, Bearer) ──────────────────


def test_sanitize_postgres_connection_string():
    out = sanitize_error_public(
        "fail: postgres://admin:Sup3r@db.host:5432/app?sslmode=require"
    )
    assert "postgres://" not in out
    assert "Sup3r" not in out
    assert REDACTED in out


def test_sanitize_aws_keys():
    out = sanitize_error_public(
        "S3 denied: AccessKeyId=AKIAIOSFODNN7EXAMPLE "
        "secret=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    )
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "wJalrXUtnFEMI" not in out


def test_sanitize_bearer_token():
    out = sanitize_error_public(
        "401: Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc"
    )
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in out
