# Tests for ЧАСТЬ 18.3: PostgreSQL/S3 adapters, migrations, safe labels.
# Co-authored with CoCo
"""Приёмка 18.3 (офлайн-часть, без реальной БД/S3):

* ленивые импорты: без ``DATABASE_URL`` asyncpg НЕ загружается при
  импорте модулей sync (бюджет памяти Render 512MB);
* ``db_label`` — только host + имя БД, без user/password/query;
* ``check_connection`` без ``DATABASE_URL`` — честный ``not ok``,
  безопасный текст, никаких исключений;
* ``classify_error`` не содержит URL/учёток;
* SigV4-подпись совпадает с официальным тест-вектором AWS,
  secret key не попадает в возвращаемые заголовки;
* миграции идемпотентны (повтор не выполняет DDL и не дублирует
  версию), все таблицы — ТОЛЬКО в схеме ``masterroom_sync``;
* ``sync_outbox`` — канонический контракт 18.3 §3 (все поля, CHECK
  по статусам, индекс воркера).
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timezone

import pytest

from bot.addons.masterroom.sync import db
from bot.addons.masterroom.sync import migrations as mig
from bot.addons.masterroom.sync import objects
from bot.addons.masterroom.sync.objects import sign_v4


# --- ленивые импорты и деградация без DATABASE_URL --------------------------


def test_asyncpg_not_imported_without_database_url(monkeypatch):
    """Модули sync уже импортированы наверху файла — asyncpg при этом
    не должен был загрузиться (ленивый импорт внутри connect())."""
    monkeypatch.setattr("bot.config.DATABASE_URL", "", raising=False)
    assert db.is_configured() is False
    assert "asyncpg" not in sys.modules


def test_s3_not_configured_without_env(monkeypatch):
    for name in ("SYNC_S3_ENDPOINT", "SYNC_S3_BUCKET",
                 "SYNC_S3_ACCESS_KEY_ID", "SYNC_S3_SECRET_ACCESS_KEY"):
        monkeypatch.setattr(f"bot.config.{name}", "", raising=False)
    assert objects.is_configured() is False


async def test_check_connection_without_url_is_honest(monkeypatch):
    monkeypatch.setattr("bot.config.DATABASE_URL", "", raising=False)
    result = await db.check_connection()
    assert result["ok"] is False
    assert "DATABASE_URL" in result["error_public"]
    assert result["schema_version"] == 0


# --- безопасная метка назначения ---------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("postgresql://user:pass@ep-x.neon.tech/mydb?sslmode=require",
     "ep-x.neon.tech/mydb"),
    ("postgres://admin:Sup3r@rc1a.mdb.yandexcloud.net:6432/prod?sslmode=require",
     "rc1a.mdb.yandexcloud.net/prod"),
    ("postgresql://u@host", "host"),
    ("", ""),
])
def test_db_label_host_and_dbname_only(url, expected):
    assert db.db_label(url) == expected


def test_db_label_never_leaks_credentials():
    lbl = db.db_label("postgresql://alice:hunter2@host:5432/db?sslmode=require")
    for secret in ("alice", "hunter2", "5432", "sslmode"):
        assert secret not in lbl


# --- классификация ошибок ----------------------------------------------------


def test_classify_error_timeout_and_ssl():
    assert "таймаут" in db.classify_error(asyncio.TimeoutError())
    assert "SSL" in db.classify_error(RuntimeError("certificate verify failed"))
    assert "сеть" in db.classify_error(ConnectionError("refused"))


def test_classify_error_never_echoes_message():
    secret = "postgresql://u:PlainPass@host/db"
    out = db.classify_error(RuntimeError(secret))
    assert "PlainPass" not in out
    assert "postgresql://" not in out


def test_s3_classify_error_safe():
    assert "таймаут" in objects.classify_error(asyncio.TimeoutError())
    out = objects.classify_error(RuntimeError("AKIA secret at endpoint"))
    assert "AKIA" not in out


# --- SigV4 --------------------------------------------------------------------


def test_sigv4_matches_aws_official_test_vector():
    """GET examplebucket/test.txt — пример из документации AWS SigV4."""
    hdrs = sign_v4(
        "GET", "https://examplebucket.s3.amazonaws.com/test.txt",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        headers={"range": "bytes=0-9"},
        payload_sha256=("e3b0c44298fc1c149afbf4c8996fb924"
                        "27ae41e4649b934ca495991b7852b855"),
        now=datetime(2013, 5, 24, 0, 0, 0, tzinfo=timezone.utc),
    )
    expected = "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    assert expected in hdrs["Authorization"]


def test_sigv4_secret_key_not_in_headers():
    hdrs = sign_v4(
        "PUT", "https://s3.example.com/bucket/key",
        access_key="AK", secret_key="TOP-SECRET-KEY", region="ru-central1",
    )
    assert "TOP-SECRET-KEY" not in str(hdrs)


# --- миграции ------------------------------------------------------------------


class _StubTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _StubConn:
    """Мини-стенд asyncpg-соединения для проверки идемпотентности."""

    def __init__(self):
        self.executed: list[str] = []
        self.version = 0
        self.version_table = False

    async def execute(self, q, *args):
        self.executed.append(q)
        if "sync_schema_version" in q and q.strip().upper().startswith("CREATE"):
            self.version_table = True
        if "INSERT INTO" in q and "sync_schema_version" in q:
            self.version = max(self.version, args[0])

    async def fetchval(self, q, *args):
        if "to_regclass" in q:
            return "x" if self.version_table else None
        if "MAX(version)" in q:
            return self.version
        return None

    def transaction(self):
        return _StubTx()


async def test_migrations_idempotent():
    conn = _StubConn()
    v1 = await mig.apply_migrations(conn)
    n_first = len(conn.executed)
    v2 = await mig.apply_migrations(conn)
    second_run = conn.executed[n_first:]
    assert v1 == v2 == mig.latest_version() == 1
    # повторный прогон: ни одного DDL таблиц данных, ни одной записи версии
    assert not any("sync_outbox" in q for q in second_run)
    assert not any("INSERT INTO" in q for q in second_run)


def test_all_tables_only_in_masterroom_sync_schema():
    sql_all = " ".join(s for _, _, stmts in mig.MIGRATIONS for s in stmts)
    tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\S+)", sql_all)
    assert tables, "миграция v1 должна создавать таблицы"
    assert all(t.startswith(f"{mig.SCHEMA}.") for t in tables), tables
    assert "public." not in sql_all


def test_sync_outbox_canonical_contract():
    """Канонический inline-контракт 18.3 §3: поля, CHECK, индекс воркера."""
    sql_all = " ".join(s for _, _, stmts in mig.MIGRATIONS for s in stmts)
    m = re.search(
        r"CREATE TABLE IF NOT EXISTS masterroom_sync\.sync_outbox\s*\((.*?)\)\s",
        sql_all, re.S,
    )
    assert m, "нет DDL sync_outbox"
    ddl = m.group(1)
    for col in ("operation_id", "entity_type", "entity_id", "revision",
                "checksum", "status", "attempt_count", "created_at",
                "updated_at"):
        assert col in ddl, f"нет колонки {col}"
    assert "PRIMARY KEY" in ddl  # operation_id уникален
    for st in ("pending", "delivering", "confirmed", "failed"):
        assert st in ddl
    assert "sync_outbox (status, updated_at)" in sql_all  # индекс воркера
