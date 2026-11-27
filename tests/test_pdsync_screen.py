# Tests for ЧАСТЬ 18.2: pdsync screen, strict owner gate, write-time sanitiser.
# Co-authored with CoCo
"""Приёмка 18.2:

* КАЖДЫЙ callback ``mr:pdsync*`` (прямой, в обход меню) отвергается для
  не-owner — включая co-owner и любого пользователя в ``full_public``
  (ловит подмену строгого ``is_owner`` на ``can_admin``);
* без ``DATABASE_URL`` — статус «не подключена», все 8 категорий на экране;
* ``last_error_public`` санитизируется НА ЗАПИСИ: connection string /
  токены не попадают в state и на экран;
* «Пауза» — не заглушка (реально переключает ``paused``); остальные
  четыре кнопки — честные заглушки, не изображающие успех;
* префикс ``mr:pdsync`` не пересекается с ``mr:storage*``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.addons.masterroom import handlers as mr_handlers
from bot.addons.masterroom import state as mr_state
from bot.addons.masterroom.sync import handlers as pd
from bot.addons.masterroom.sync.sanitize import sanitize_error_public
from bot.storage import storage

OWNER = 111
CO_OWNER = 222
STRANGER = 333

ALL_CALLBACKS = (
    "mr:pdsync",
    "mr:pdsync:check",
    "mr:pdsync:preview",
    "mr:pdsync:all",
    "mr:pdsync:all:confirm",
    "mr:pdsync:pause",
    "mr:pdsync:retry",
    "mr:pdsync:conflicts",
    "mr:pdsync:cfl:1",
    "mr:pdsync:cfl:1:local",
    "mr:pdsync:cfl:1:remote",
    # 18.7: retention-переключатели и удаление — owner-only на сервере
    "mr:pdsync:ret",
    "mr:pdsync:ret:t:articles",
    "mr:pdsync:ret:purge:articles",
    "mr:pdsync:ret:purge:articles:confirm",
)

# С 18.3 «Проверить подключение» — реальная кнопка; с 18.5 реальны
# «Предпросмотр» (dry-run) с «Синхронизировать всё» (bootstrap); с 18.6
# реальны «Повторить ошибки» и карточки конфликтов — заглушек не осталось.
STUB_CALLBACKS: tuple[str, ...] = ()


class FakeQuery:
    """Минимальный стенд CallbackQuery: data + from_user + answer-лог."""

    def __init__(self, data: str, uid: int | None):
        self.data = data
        self.from_user = SimpleNamespace(id=uid) if uid is not None else None
        self.message = None  # _show_screen молча пропускает отправку
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answers.append((text, bool(show_alert)))


def _dispatch_handler():
    router = pd.build_pdsync_router()
    handlers = router.callback_query.handlers
    assert len(handlers) == 1  # единый диспетчер на весь префикс
    return handlers[0].callback


@pytest.fixture
def fresh_env():
    """Чистый masterroom-стейт + владелец 111, режим private."""
    settings = storage._settings()  # noqa: SLF001
    settings.setdefault("addons", {}).pop("masterroom", None)
    saved_owner = storage.get_owner_id()
    saved_mode = storage.get_access_mode()
    saved_co = list(storage.get_co_owners())
    for co in saved_co:
        storage.remove_co_owner(co)
    storage.set_owner_id(OWNER)
    storage.set_access_mode("private")
    yield
    settings = storage._settings()  # noqa: SLF001
    settings.setdefault("addons", {}).pop("masterroom", None)
    for co in storage.get_co_owners():
        storage.remove_co_owner(co)
    for co in saved_co:
        storage.add_co_owner(co)
    if saved_owner is not None:
        storage.set_owner_id(saved_owner)
    storage.set_access_mode(saved_mode)


# --- строгий owner-гейт ---------------------------------------------------


@pytest.mark.parametrize("cb", ALL_CALLBACKS)
async def test_stranger_denied_every_callback(fresh_env, cb):
    handler = _dispatch_handler()
    q = FakeQuery(cb, STRANGER)
    await handler(q)
    assert q.answers == [("Только владелец", True)]
    assert mr_state.is_sync_paused() is False  # состояние не менялось


@pytest.mark.parametrize("cb", ALL_CALLBACKS)
async def test_co_owner_denied_every_callback(fresh_env, cb):
    """Ловушка 18.0 п.4: co-owner проходит can_admin, но НЕ is_owner."""
    storage.add_co_owner(CO_OWNER)
    assert storage.can_admin_bot(CO_OWNER) is True  # ловушка реальна
    handler = _dispatch_handler()
    q = FakeQuery(cb, CO_OWNER)
    await handler(q)
    assert q.answers == [("Только владелец", True)]
    assert mr_state.is_sync_paused() is False


@pytest.mark.parametrize("cb", ALL_CALLBACKS)
async def test_full_public_user_denied_every_callback(fresh_env, cb):
    """В full_public can_admin пропускает ВСЕХ — pdsync обязан отвергнуть."""
    storage.set_access_mode("full_public")
    assert storage.can_admin_bot(STRANGER) is True  # ловушка реальна
    handler = _dispatch_handler()
    q = FakeQuery(cb, STRANGER)
    await handler(q)
    assert q.answers == [("Только владелец", True)]
    assert mr_state.is_sync_paused() is False


async def test_query_without_user_denied(fresh_env):
    handler = _dispatch_handler()
    q = FakeQuery("mr:pdsync", None)
    await handler(q)
    assert q.answers == [("Только владелец", True)]


# --- локальный режим (без DATABASE_URL) ------------------------------------


def test_screen_local_mode_renders_all_categories(fresh_env):
    from bot.addons.masterroom.sync.categories import ALL_CATEGORIES, CATEGORY_LABEL

    body = pd.render_pdsync_screen()
    assert "Локальное хранилище Render" in body
    assert "не подключена" in body
    for cat in ALL_CATEGORIES:
        assert CATEGORY_LABEL[cat] in body
    assert "ещё не было" in body


async def test_owner_opens_screen(fresh_env):
    handler = _dispatch_handler()
    q = FakeQuery("mr:pdsync", OWNER)
    await handler(q)
    # обычный ack, не отказ
    assert q.answers == [("", False)]


# --- пауза — НЕ заглушка ----------------------------------------------------


async def test_pause_actually_toggles_state(fresh_env):
    handler = _dispatch_handler()
    assert mr_state.is_sync_paused() is False
    q = FakeQuery("mr:pdsync:pause", OWNER)
    await handler(q)
    assert mr_state.is_sync_paused() is True
    assert q.answers[0][0] == "Синхронизация на паузе"
    q2 = FakeQuery("mr:pdsync:pause", OWNER)
    await handler(q2)
    assert mr_state.is_sync_paused() is False
    assert q2.answers[0][0] == "Синхронизация возобновлена"


def test_pause_state_shown_on_screen(fresh_env):
    mr_state.set_sync_paused(True)
    assert "да ⏸" in pd.render_pdsync_screen()
    mr_state.set_sync_paused(False)
    assert "нет ▶️" in pd.render_pdsync_screen()


# --- 18.6: retry без внешней БД — честный отказ, не имитация успеха --------


async def test_retry_without_db_honest_refusal(fresh_env):
    """18.6 §4: без DATABASE_URL «Повторить ошибки» честно отказывает,
    ничего не пишет и не изображает успех."""
    handler = _dispatch_handler()
    q = FakeQuery("mr:pdsync:retry", OWNER)
    await handler(q)
    assert len(q.answers) == 1
    text, alert = q.answers[0]
    assert alert is True
    assert "DATABASE_URL" in text
    for fake_success in ("Готово", "Успешно", "Синхронизировано", "✅"):
        assert fake_success not in text
    # состояние не тронуто
    assert mr_state.is_sync_paused() is False
    assert mr_state.get_sync_state()["db_status"] == "not_connected"


# --- 18.5: «Предпросмотр» = dry-run, «Синхронизировать всё» = confirm -------


async def test_preview_is_dry_run_writes_nothing(fresh_env, monkeypatch):
    """18.5 §1: dry-run не пишет ни в журнал, ни в state, ни наружу."""
    from bot.addons.masterroom.sync import journal as sync_journal

    handler = _dispatch_handler()
    state_before = dict(mr_state.get_sync_state())
    journal_existed = sync_journal.journal_db_path().exists()
    q = FakeQuery("mr:pdsync:preview", OWNER)
    await handler(q)
    # dry-run отвечает без alert-отказа и не имитирует передачу
    assert q.answers and q.answers[0][1] is False
    # состояние sync не изменилось
    assert dict(mr_state.get_sync_state()) == state_before
    # dry-run НЕ создаёт локальный журнал
    assert sync_journal.journal_db_path().exists() == journal_existed


async def test_sync_all_without_db_is_honest(fresh_env, monkeypatch):
    """18.5 §2: без DATABASE_URL «Синхронизировать всё» честно отказывает
    и НЕ показывает подтверждение."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    handler = _dispatch_handler()
    q = FakeQuery("mr:pdsync:all", OWNER)
    await handler(q)
    assert len(q.answers) == 1
    text, alert = q.answers[0]
    assert alert is True
    assert "DATABASE_URL" in text


async def test_sync_all_confirm_without_db_does_not_start(fresh_env, monkeypatch):
    """18.5 §2: даже прямой confirm-callback без БД ничего не запускает."""
    from bot.addons.masterroom.sync import bootstrap

    monkeypatch.delenv("DATABASE_URL", raising=False)
    started: list[bool] = []
    monkeypatch.setattr(
        bootstrap, "start_bootstrap", lambda: started.append(True) or True
    )
    handler = _dispatch_handler()
    q = FakeQuery("mr:pdsync:all:confirm", OWNER)
    await handler(q)
    assert started == []  # bootstrap НЕ стартовал
    text, alert = q.answers[0]
    assert alert is True
    assert "DATABASE_URL" in text


async def test_sync_all_confirm_paused_does_not_start(fresh_env, monkeypatch):
    """«Пауза» блокирует старт: честный alert, bootstrap не запускается."""
    from bot.addons.masterroom.sync import bootstrap, db

    monkeypatch.setattr(db, "is_configured", lambda: True)
    started: list[bool] = []
    monkeypatch.setattr(
        bootstrap, "start_bootstrap", lambda: started.append(True) or True
    )
    mr_state.set_sync_paused(True)
    handler = _dispatch_handler()
    q = FakeQuery("mr:pdsync:all:confirm", OWNER)
    await handler(q)
    assert started == []
    text, alert = q.answers[0]
    assert alert is True
    assert "пауз" in text.lower()


async def test_sync_all_confirm_starts_bootstrap(fresh_env, monkeypatch):
    """18.5 §3: подтверждение owner запускает bootstrap ровно один раз."""
    from bot.addons.masterroom.sync import bootstrap, db

    monkeypatch.setattr(db, "is_configured", lambda: True)
    started: list[bool] = []
    monkeypatch.setattr(
        bootstrap, "start_bootstrap", lambda: started.append(True) or True
    )
    handler = _dispatch_handler()
    q = FakeQuery("mr:pdsync:all:confirm", OWNER)
    await handler(q)
    assert started == [True]
    assert q.answers[0][0].startswith("Передача начата")


async def test_sync_all_shows_confirm_not_start(fresh_env, monkeypatch):
    """18.5 §2: «Синхронизировать всё» показывает отчёт + подтверждение,
    но НЕ стартует bootstrap без отдельной кнопки."""
    from bot.addons.masterroom.sync import bootstrap, db

    monkeypatch.setattr(db, "is_configured", lambda: True)
    started: list[bool] = []
    monkeypatch.setattr(
        bootstrap, "start_bootstrap", lambda: started.append(True) or True
    )
    handler = _dispatch_handler()
    q = FakeQuery("mr:pdsync:all", OWNER)
    await handler(q)
    assert started == []  # без подтверждения ничего не передаётся


async def test_check_without_database_url_is_honest(fresh_env, monkeypatch):
    """18.3 §4: без DATABASE_URL кнопка «Проверить подключение» не
    имитирует успех — статус остаётся «не подключена», просит env."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("bot.config.DATABASE_URL", "", raising=False)
    handler = _dispatch_handler()
    q = FakeQuery("mr:pdsync:check", OWNER)
    await handler(q)
    assert len(q.answers) == 1
    text, alert = q.answers[0]
    assert alert is True
    assert "DATABASE_URL" in text
    assert mr_state.get_sync_state()["db_status"] == "not_connected"


# --- санитизация на записи ---------------------------------------------------


def test_sanitize_strips_connection_string():
    raw = ("connect failed: postgresql://admin:Sup3rS3cret@db.neon.tech:5432/"
           "prod?sslmode=require (timeout)")
    out = sanitize_error_public(raw)
    assert "Sup3rS3cret" not in out
    assert "postgresql://" not in out
    assert "[скрыто]" in out


def test_sanitize_strips_tokens_cookies_userinfo():
    raw = ("auth error; token=ghp_abcDEF1234567890abcDEF1234567890 "
           "cookie: session=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 "
           "user:hunter2@db.example.com refused")
    out = sanitize_error_public(raw)
    assert "ghp_abcDEF" not in out
    assert "eyJhbGci" not in out
    assert "hunter2" not in out


async def test_error_with_connection_string_never_reaches_screen(fresh_env):
    """Приёмка: подложить в last_error_public-путь ошибку с connection
    string — в state и на экран попадает только санитизированный текст."""
    mr_state.set_sync_last_error_public(
        "sync failed: postgres://u:PlainPass@host/db password=hunter2"
    )
    stored = mr_state.get_sync_state()["last_error_public"]
    assert "PlainPass" not in stored
    assert "hunter2" not in stored
    assert "postgres://" not in stored
    body = pd.render_pdsync_screen()
    assert "PlainPass" not in body
    assert "hunter2" not in body
    assert "Последняя ошибка" in body


# --- проводка в меню и отсутствие коллизий -----------------------------------


def test_home_keyboard_has_pdsync_button(fresh_env):
    kb = mr_handlers._kb_home()
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "mr:pdsync" in datas
    assert "mr:storage" in datas  # экран «💾 Хранилище» не тронут


def test_pdsync_prefix_does_not_collide_with_storage():
    assert not pd.PDSYNC_PREFIX.startswith("mr:storage")
    assert not "mr:storage".startswith(pd.PDSYNC_PREFIX)


def test_masterroom_router_includes_pdsync(fresh_env):
    router = mr_handlers.build_masterroom_router()
    names = [r.name for r in router.sub_routers]
    assert "masterroom_pdsync" in names
