# «🔐 Персональные данные и синхронизация» — screen + strict owner gate (18.2).
# Co-authored with CoCo
"""Экран «Персональные данные и синхронизация» (ЧАСТЬ 18.2, п.11.2 ТЗ).

Собственный aiogram Router с callback-префиксом ``mr:pdsync`` (никаких
пересечений с ``mr:storage*``). Подключается через ``include_router()``
внутри :func:`bot.addons.masterroom.handlers.build_masterroom_router`.

Доступ — ТОЛЬКО строгий owner (``bot.access.is_owner``, сверка с
``storage.get_owner_id()``). НЕ ``wizard._is_owner`` / ``can_admin``:
те пропускают co-owners и, в режиме ``full_public``, вообще всех — для
управления sync это недопустимо (18.0 п.4, 18.2 §3). Проверка выполняется
единым диспетчером на КАЖДЫЙ callback ``mr:pdsync*`` — обход через прямой
callback невозможен.

С 18.3 кнопка «Проверить подключение» — РЕАЛЬНАЯ: checking → SELECT 1 →
миграции ``masterroom_sync`` → тестовая запись/чтение/удаление в служебной
таблице → (при наличии S3) head bucket → ready/error. Модули ``db`` /
``objects`` импортируются лениво — без ``DATABASE_URL`` asyncpg не грузится.

С 18.5 РЕАЛЬНЫ также:

* «Предпросмотр» — dry-run-отчёт (18.5 §1): БЕЗ единой записи во внешнее
  назначение, журнал или state;
* «Синхронизировать всё» — показывает тот же отчёт, что dry-run, и требует
  явного подтверждения owner отдельной кнопкой «Подтверждаю передачу всех
  данных» (18.5 §2); только после неё стартует bootstrap (18.5 §3) фоновой
  задачей — бот продолжает отвечать на сообщения.

С 18.6 РЕАЛЬНЫ также:

* «Повторить ошибки» — переводит ``failed`` → ``pending`` с ТЕМ ЖЕ
  ``operation_id`` (единый журнал, без дублей) и будит воркер;
* карточки конфликтов (``mr:pdsync:conflicts`` / ``mr:pdsync:cfl:*``) —
  неясный двусторонний конфликт показывается owner'у БЕЗ секретов и без
  полного дампа перс. данных, с выбором версии; до решения сущность
  исключена из авто-применения;
* «Пауза» дополнительно будит воркер 18.6, чтобы тот сразу увидел флаг.

Заморозка kill-switch: Telegram-callback'и покрыты outer-middleware
``_freeze_guard`` в ``bot/main.py`` (дропает каждый update при ``.frozen``),
поэтому здесь отдельная проверка не нужна; фоновым воркерам 18.5/18.6 она
понадобится своя.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from .. import state as mr_state
from .categories import (
    ALL_CATEGORIES,
    CATEGORY_LABEL,
    SyncCategory,
    count_all,
)

logger = logging.getLogger(__name__)

PDSYNC_PREFIX = "mr:pdsync"

_DB_STATUS_LABEL: dict[str, str] = {
    "not_connected": "не подключена",
    "checking": "проверяется",
    "ready": "готова",
    "error": "ошибка",
}


# ─── Строгий owner-гейт (НЕ wizard._is_owner / can_admin) ────────────────────


def _is_strict_owner(query: CallbackQuery) -> bool:
    from ....access import is_owner  # bot.access — ленивый импорт, без циклов

    return is_owner(query.from_user.id if query.from_user else None)


# ─── Рендер экрана ────────────────────────────────────────────────────────────


def _html_escape(s: Any) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _fmt_bytes(n: int) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} КБ"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} МБ"
    return f"{n / (1024 * 1024 * 1024):.2f} ГБ"


def _fmt_ts(epoch: int | None) -> str:
    if not epoch:
        return "ещё не было"
    try:
        return time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime(int(epoch)))
    except (ValueError, OverflowError, OSError):
        return "ещё не было"


def render_pdsync_screen() -> str:
    """Текст экрана: источник, статус БД, счётчики категорий, ошибка."""
    sync = mr_state.get_sync_state()
    db_status = str(sync.get("db_status") or "not_connected")
    db_label = str(sync.get("db_label") or "")
    connected = db_status == "ready"

    lines = ["<b>🔐 Персональные данные и синхронизация</b>", ""]
    source = (
        f"PostgreSQL — {_html_escape(db_label)}"
        if connected and db_label
        else ("PostgreSQL" if connected else "Локальное хранилище Render")
    )
    lines.append(f"Источник: <b>{source}</b>")
    lines.append(
        "Внешняя БД: <b>"
        + _DB_STATUS_LABEL.get(db_status, "ошибка")
        + "</b>"
    )
    if db_label and not connected:
        lines.append(f"Назначение: {_html_escape(db_label)}")
    lines.append(f"Последняя синхронизация: {_fmt_ts(sync.get('last_sync_at'))}")
    lines.append(
        "Пауза: <b>" + ("да ⏸" if sync.get("paused") else "нет ▶️") + "</b>"
    )
    lines.append("")
    lines.append("<b>Категории данных:</b>")
    try:
        counters = count_all()
    except Exception:  # noqa: BLE001 — счётчики не должны ронять экран
        logger.exception("pdsync: category counters failed")
        counters = {c: {"records": 0, "bytes": 0} for c in ALL_CATEGORIES}
    # 18.4: без master key категория «Секреты» ЧЕСТНО помечается
    # недоступной для синхронизации — всё остальное работает как обычно.
    secrets_available = True
    try:
        from .secret_store import is_available as _secrets_ok  # noqa: PLC0415

        secrets_available = _secrets_ok()
    except Exception:  # noqa: BLE001 — недоступность не роняет экран
        secrets_available = False
    for cat in ALL_CATEGORIES:
        agg = counters.get(cat, {"records": 0, "bytes": 0})
        suffix = ""
        if cat is SyncCategory.SECRETS and not secrets_available:
            suffix = " — недоступна для синхронизации (нет ключа шифрования)"
        lines.append(
            f"• {CATEGORY_LABEL[cat]}: {agg['records']} зап., "
            f"{_fmt_bytes(agg['bytes'])}{suffix}"
        )
    # 18.5: прогресс bootstrap (передано/подтверждено/ошибок по категориям).
    progress = _bootstrap_progress()
    status = str(progress.get("status") or "")
    if status:
        lines.append("")
        label = {
            "running": "выполняется ▶️",
            "paused": "на паузе ⏸",
            "done": "завершён ✅",
            "done_with_errors": "завершён с ошибками ⚠️",
            "error": "ошибка ⚠️",
            "frozen": "остановлен (kill-switch) 🧊",
        }.get(status, status)
        lines.append(f"<b>Полный sync:</b> {label}")
        cats = progress.get("categories")
        if isinstance(cats, dict):
            for cat in ALL_CATEGORIES:
                c = cats.get(cat.value)
                if not isinstance(c, dict) or not c.get("total"):
                    continue
                lines.append(
                    f"• {CATEGORY_LABEL[cat]}: "
                    f"{c.get('confirmed', 0)}/{c.get('total', 0)} подтв., "
                    f"ошибок {c.get('failed', 0)}"
                )
    # 18.6: статус инкрементальной синхронизации (кэш обновляет воркер).
    inc = _incremental_status()
    inc_state = str(inc.get("state") or "")
    if inc_state:
        label = {
            "ok": "работает ✅",
            "paused": "на паузе ⏸",
            "error": "ошибка ⚠️",
            "frozen": "остановлена (kill-switch) 🧊",
            "waiting_bootstrap": "ждёт первого полного sync",
        }.get(inc_state, inc_state)
        lines.append("")
        lines.append(f"<b>Инкрементальный sync:</b> {label}")
        lines.append(
            f"• в очереди: {int(inc.get('pending', 0))}, "
            f"ошибок: {int(inc.get('failed', 0))}, "
            f"подтверждено: {int(inc.get('confirmed', 0))}"
        )
        if int(inc.get("conflicts", 0)):
            lines.append(
                f"• ⚠️ конфликтов, ждущих решения: {int(inc['conflicts'])}"
            )
    err = str(sync.get("last_error_public") or "").strip()
    if err:
        lines.append("")
        lines.append(f"⚠️ Последняя ошибка: {_html_escape(err)}")
    return "\n".join(lines)


def _incremental_status() -> dict[str, Any]:
    """Кэш статуса воркера 18.6; ошибки не роняют экран."""
    try:
        from . import incremental  # noqa: PLC0415 — ленивый импорт

        return incremental.get_status()
    except Exception:  # noqa: BLE001
        return {}


def _bootstrap_progress() -> dict[str, Any]:
    """Живой прогресс bootstrap; ошибки не роняют экран."""
    try:
        from . import bootstrap  # noqa: PLC0415 — ленивый импорт

        return bootstrap.get_progress()
    except Exception:  # noqa: BLE001
        return {}


def _kb_pdsync(paused: bool) -> InlineKeyboardMarkup:
    pause_label = "▶️ Продолжить" if paused else "⏸ Пауза"
    rows = [
        [
            InlineKeyboardButton(text="🔍 Проверить подключение",
                                 callback_data="mr:pdsync:check"),
            InlineKeyboardButton(text="👁 Предпросмотр",
                                 callback_data="mr:pdsync:preview"),
        ],
        [
            InlineKeyboardButton(text="🔄 Синхронизировать всё",
                                 callback_data="mr:pdsync:all"),
            InlineKeyboardButton(text=pause_label,
                                 callback_data="mr:pdsync:pause"),
        ],
        [
            InlineKeyboardButton(text="♻️ Повторить ошибки",
                                 callback_data="mr:pdsync:retry"),
            InlineKeyboardButton(text="🗂 Оставлять локально",
                                 callback_data="mr:pdsync:ret"),
        ],
    ]
    conflicts = int(_incremental_status().get("conflicts", 0) or 0)
    if conflicts:
        rows.append([
            InlineKeyboardButton(
                text=f"⚠️ Конфликты ({conflicts})",
                callback_data="mr:pdsync:conflicts",
            ),
        ])
    rows.append([
        InlineKeyboardButton(text="« Назад", callback_data="mr:menu"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_screen(query: CallbackQuery) -> None:
    body = render_pdsync_screen()
    kb = _kb_pdsync(mr_state.is_sync_paused())
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                body, reply_markup=kb, parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return
        with contextlib.suppress(Exception):
            await query.message.answer(
                body, reply_markup=kb, parse_mode="HTML",
                disable_web_page_preview=True,
            )


# ─── Миграция legacy plaintext-секретов (ЧАСТЬ 18.4 §3) ─────────────────────

# Один прогон на процесс: миграция идемпотентна, но нет смысла гонять
# расшифровку всех записей на каждое открытие экрана.
_secrets_migration_ran = False


async def _ensure_secrets_migrated() -> None:
    """Запустить идемпотентную миграцию секретов (18.4 §3) один раз.

    Вызывается при открытии экрана владельцем: owner-гейт уже пройден
    диспетчером, kill-switch покрыт outer-middleware ``_freeze_guard``.
    Без master key миграция — честный no-op (``available=False``), бот
    не падает. Plaintext-оригиналы НЕ удаляются (удаление — 18.7).
    Любая ошибка миграции не роняет экран и не пишет секретов в лог.
    """
    global _secrets_migration_ran
    if _secrets_migration_ran:
        return
    _secrets_migration_ran = True
    try:
        # Ленивый импорт: без master key cryptography не загружается.
        from .migrate_secrets import (  # noqa: PLC0415
            migrate_plaintext_secrets_async,
        )

        report = await migrate_plaintext_secrets_async()
        if report.available and report.failed:
            logger.error(
                "pdsync: secret migration finished with %d failed keys",
                len(report.failed),
            )
    except Exception:  # noqa: BLE001 — миграция не должна ронять экран
        logger.exception("pdsync: secret migration crashed (values not logged)")


# ─── «Проверить подключение» (ЧАСТЬ 18.3 §4) ────────────────────────────────


async def _run_connection_check(query: CallbackQuery) -> None:
    """Проверка внешней БД (+S3): статус → checking → ready/error.

    Ленивые импорты: без ``DATABASE_URL`` модули ``db``/``objects``
    (и asyncpg) вообще не загружаются. Проверка не пишет в state ничего,
    кроме статуса/метки/безопасной ошибки — локальные данные целы при
    любом исходе (гарантия 18.3 §4).
    """
    from . import db  # ленивый импорт — asyncpg не грузится без нужды

    if not db.is_configured():
        mr_state.set_sync_db_status("not_connected")
        await query.answer(
            "Внешняя БД не настроена: задайте DATABASE_URL в env.",
            show_alert=True,
        )
        await _show_screen(query)
        return

    mr_state.set_sync_db_status("checking")
    await query.answer("Проверяю подключение…")
    await _show_screen(query)

    result = await db.check_connection()

    s3_error = ""
    if result.get("ok"):
        from . import objects  # ленивый импорт

        if objects.is_configured():
            try:
                await objects.head_bucket()
            except Exception as exc:  # noqa: BLE001 — безопасный класс
                s3_error = objects.classify_error(exc) \
                    if not isinstance(exc, objects.S3Error) else str(exc)

    if result.get("ok") and not s3_error:
        mr_state.set_sync_db_label(str(result.get("label") or ""))
        mr_state.set_sync_db_status("ready")
        mr_state.set_sync_last_error_public("")
    else:
        err = s3_error or str(result.get("error_public") or "ошибка проверки")
        mr_state.set_sync_last_error_public(err)
        mr_state.set_sync_db_status("error")
    await _show_screen(query)


# ─── «Предпросмотр» = dry-run и «Синхронизировать всё» (ЧАСТЬ 18.5) ─────────


def _kb_back_to_screen() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« К синхронизации",
                              callback_data="mr:pdsync")],
    ])


def _kb_confirm_full_sync() -> InlineKeyboardMarkup:
    """Явное подтверждение первого полного sync (18.5 §2)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Подтверждаю передачу всех данных",
            callback_data="mr:pdsync:all:confirm",
        )],
        [InlineKeyboardButton(text="✖️ Отмена — ничего не передавать",
                              callback_data="mr:pdsync")],
    ])


async def _edit_or_send(
    query: CallbackQuery, body: str, kb: InlineKeyboardMarkup,
) -> None:
    if query.message is None:
        return
    with contextlib.suppress(Exception):
        await query.message.edit_text(
            body, reply_markup=kb, parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return
    with contextlib.suppress(Exception):
        await query.message.answer(
            body, reply_markup=kb, parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def _run_preview(query: CallbackQuery) -> None:
    """«Предпросмотр» = dry-run (18.5 §1).

    Отчёт строится ТОЛЬКО чтением (``bootstrap.build_plan`` — read-only):
    ни одной записи во внешнюю БД/S3, в журнал ``sync_journal.db`` или в
    state. Write-транзакции не открываются вовсе — модуль ``db`` даже не
    подключается.
    """
    await query.answer("Считаю dry-run — без единой записи…")
    from . import bootstrap  # noqa: PLC0415 — ленивый импорт

    try:
        report = await bootstrap.build_preview_report()
    except Exception:  # noqa: BLE001 — отчёт не должен ронять экран
        logger.exception("pdsync: dry-run preview failed")
        report = (
            "⚠️ Не удалось построить dry-run-отчёт. Подробности в логах "
            "(без секретов). Ничего не было передано."
        )
    await _edit_or_send(query, report, _kb_back_to_screen())


async def _show_full_sync_confirm(query: CallbackQuery) -> None:
    """«Синхронизировать всё», шаг 1 (18.5 §2): тот же отчёт, что dry-run,
    плюс явное подтверждение. БЕЗ подтверждения — ничего не пишется."""
    from . import bootstrap, db  # noqa: PLC0415 — ленивые импорты

    if bootstrap.is_running():
        await query.answer("Полный sync уже выполняется.", show_alert=True)
        await _show_screen(query)
        return
    if not db.is_configured():
        await query.answer(
            "Внешняя БД не настроена: задайте DATABASE_URL в env.",
            show_alert=True,
        )
        return
    await query.answer()
    try:
        report = await bootstrap.build_preview_report()
    except Exception:  # noqa: BLE001
        logger.exception("pdsync: sync-all preview failed")
        await query.answer(
            "Не удалось построить отчёт — передача не начата.",
            show_alert=True,
        )
        return
    body = (
        report
        + "\n\n<b>Подтверждение передачи</b>\n"
        + "Все перечисленные категории (включая «Остальные данные») будут "
        + "переданы во внешнюю БД/S3. Каждая запись подтверждается "
        + "повторным чтением (read-after-write). Локальные данные НЕ "
        + "изменяются и НЕ удаляются ни при каком исходе."
    )
    await _edit_or_send(query, body, _kb_confirm_full_sync())


async def _start_full_sync(query: CallbackQuery) -> None:
    """«Синхронизировать всё», шаг 2: старт bootstrap ПОСЛЕ явного
    подтверждения owner (18.5 §3). Фоновая задача — бот не блокируется."""
    from . import bootstrap, db  # noqa: PLC0415 — ленивые импорты

    if not db.is_configured():
        await query.answer(
            "Внешняя БД не настроена: задайте DATABASE_URL в env.",
            show_alert=True,
        )
        return
    if mr_state.is_sync_paused():
        await query.answer(
            "Синхронизация на паузе — сначала снимите паузу (▶️).",
            show_alert=True,
        )
        return
    if not bootstrap.start_bootstrap():
        await query.answer("Полный sync уже выполняется.", show_alert=True)
        await _show_screen(query)
        return
    await query.answer("Передача начата — прогресс на экране.")
    await _show_screen(query)


# ─── «Повторить ошибки» и конфликты (ЧАСТЬ 18.6 §3/§4) ──────────────────────


async def _run_retry_failed(query: CallbackQuery) -> None:
    """``failed`` → ``pending`` с ТЕМ ЖЕ ``operation_id`` (18.6 §4).

    Единый журнал: дублей не возникает, ``operation_id`` не меняется —
    только статус. После перевода воркер будится и доставляет повторно.
    """
    from . import db, incremental  # noqa: PLC0415 — ленивые импорты
    from .journal import get_sync_journal  # noqa: PLC0415

    if not db.is_configured():
        await query.answer(
            "Внешняя БД не настроена: задайте DATABASE_URL в env.",
            show_alert=True,
        )
        return
    jr = get_sync_journal()
    try:
        moved = await jr.retry_failed()
        counts = await jr.counts_by_status()
    except Exception:  # noqa: BLE001 — журнал не должен ронять экран
        logger.exception("pdsync retry: journal error (no values logged)")
        await query.answer("Не удалось прочитать журнал.", show_alert=True)
        return
    remaining = int(counts.get("failed", 0))
    incremental.wake()
    await query.answer(
        f"Поставлено на повтор: {moved}. Осталось ошибок: {remaining}.",
        show_alert=True,
    )
    await _show_screen(query)


def _kb_conflicts_list(items: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"⚠️ {c['entity_id'][:48]}",
            callback_data=f"mr:pdsync:cfl:{c['id']}",
        )]
        for c in items
    ]
    rows.append([InlineKeyboardButton(text="« К синхронизации",
                                      callback_data="mr:pdsync")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_conflicts(query: CallbackQuery) -> None:
    """Список неясных конфликтов (18.6 §3) — без секретов и дампов."""
    from .journal import get_sync_journal  # noqa: PLC0415

    jr = get_sync_journal()
    try:
        items = await jr.conflicts_list()
    except Exception:  # noqa: BLE001
        logger.exception("pdsync conflicts: journal error")
        await query.answer("Не удалось прочитать журнал.", show_alert=True)
        return
    await query.answer()
    if not items:
        await _edit_or_send(
            query,
            "<b>⚠️ Конфликты синхронизации</b>\n\nКонфликтов нет.",
            _kb_back_to_screen(),
        )
        return
    body = (
        "<b>⚠️ Конфликты синхронизации</b>\n\n"
        "Обе стороны изменили эти записи после последней подтверждённой "
        "ревизии. Ничего не перетёрто: до вашего решения записи исключены "
        "из автоматической синхронизации, остальные синхронизируются.\n\n"
        "Выберите запись, чтобы решить конфликт:"
    )
    await _edit_or_send(query, body, _kb_conflicts_list(items))


def _kb_conflict_card(conflict_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📱 Оставить локальную",
                callback_data=f"mr:pdsync:cfl:{conflict_id}:local",
            ),
            InlineKeyboardButton(
                text="☁️ Взять внешнюю",
                callback_data=f"mr:pdsync:cfl:{conflict_id}:remote",
            ),
        ],
        [InlineKeyboardButton(text="« К конфликтам",
                              callback_data="mr:pdsync:conflicts")],
    ])


async def _show_conflict_card(query: CallbackQuery, conflict_id: int) -> None:
    """Карточка конфликта (18.6 §3): БЕЗ секретов и БЕЗ полного дампа
    персональных данных — только идентификатор, категория и ревизии."""
    from .journal import get_sync_journal  # noqa: PLC0415

    jr = get_sync_journal()
    try:
        c = await jr.conflict_get(conflict_id)
    except Exception:  # noqa: BLE001
        logger.exception("pdsync conflict card: journal error")
        c = None
    if c is None:
        await query.answer("Конфликт не найден (уже решён?)", show_alert=True)
        await _show_conflicts(query)
        return
    await query.answer()
    cat = str(c.get("category") or "")
    try:
        cat_label = CATEGORY_LABEL[SyncCategory(cat)]
    except (ValueError, KeyError):
        cat_label = cat or "—"
    body = (
        "<b>⚠️ Конфликт синхронизации</b>\n\n"
        f"Запись: <code>{_html_escape(c['entity_id'])}</code>\n"
        f"Категория: {_html_escape(cat_label)}\n"
        f"Локальная ревизия: {int(c['local_revision'])}\n"
        f"Внешняя ревизия: {int(c['remote_revision'])}\n\n"
        "Обе версии сохранены — ничего не перетёрто. Выберите, какая "
        "версия побеждает; она будет применена в обе стороны."
    )
    await _edit_or_send(query, body, _kb_conflict_card(int(c["id"])))


async def _resolve_conflict_cb(
    query: CallbackQuery, conflict_id: int, choice: str,
) -> None:
    """Применить выбор owner'а (18.6 §3): local | remote."""
    from . import incremental  # noqa: PLC0415 — ленивый импорт

    try:
        result = await incremental.resolve_conflict(conflict_id, choice)
    except Exception:  # noqa: BLE001 — без значений в логе
        logger.exception("pdsync conflict resolve failed (no values logged)")
        result = "Не удалось применить выбор — попробуйте позже."
    await query.answer(result, show_alert=True)
    await _show_conflicts(query)


# ─── Retention: «Оставлять локально» (ЧАСТЬ 18.7 §1/§2) ─────────────────────


def render_retention_screen() -> str:
    """Экран retention: 8 переключателей + предупреждение (18.7 §1).

    Переключатели НЕ влияют на состав передачи (внешняя копия всегда
    полная) — только на локальную копию.
    """
    retention = mr_state.get_sync_retention()
    lines = [
        "<b>🗂 Оставлять локально</b>",
        "",
        "Переключатели управляют ТОЛЬКО локальной копией: во внешнюю "
        "БД всегда передаются все категории полностью.",
        "",
        "⚠️ Постоянное полное дублирование данных локально и во внешней "
        "БД повышает privacy-риск: копий больше — поверхность утечки шире.",
        "",
        "Выключение переключателя действует «на будущее»: новые данные "
        "категории перестают оседать локально после подтверждения "
        "синхронизации. Существующая копия удаляется ТОЛЬКО отдельной "
        "кнопкой ниже — с показом объёма и отдельным подтверждением.",
    ]
    off = [
        c for c in ALL_CATEGORIES
        if not retention.get(c.value, True)
    ]
    if off:
        lines.append("")
        lines.append(
            "Категории с выключенным retention: "
            + ", ".join(CATEGORY_LABEL[c] for c in off)
        )
    return "\n".join(lines)


def _kb_retention() -> InlineKeyboardMarkup:
    retention = mr_state.get_sync_retention()
    rows: list[list[InlineKeyboardButton]] = []
    for cat in ALL_CATEGORIES:
        keep = bool(retention.get(cat.value, True))
        mark = "✅" if keep else "🚫"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {CATEGORY_LABEL[cat]}",
            callback_data=f"mr:pdsync:ret:t:{cat.value}",
        )])
        if not keep:
            rows.append([InlineKeyboardButton(
                text=f"   🗑 Удалить существующую копию — {CATEGORY_LABEL[cat]}",
                callback_data=f"mr:pdsync:ret:purge:{cat.value}",
            )])
    rows.append([InlineKeyboardButton(text="« К синхронизации",
                                      callback_data="mr:pdsync")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_retention_screen(query: CallbackQuery) -> None:
    await _edit_or_send(query, render_retention_screen(), _kb_retention())


def _parse_category(raw: str) -> SyncCategory | None:
    try:
        return SyncCategory(raw)
    except ValueError:
        return None


async def _toggle_retention(query: CallbackQuery, raw_cat: str) -> None:
    """Переключить retention категории (18.7 §1/§2).

    Выключение НЕ удаляет ничего — только «на будущее»; включение обратно
    снимает реестр удалённых, чтобы pull мог восстановить внешние копии.
    Owner-проверка уже выполнена диспетчером (строгий ``is_owner``).
    """
    cat = _parse_category(raw_cat)
    if cat is None:
        await query.answer()
        return
    keep = not bool(mr_state.get_sync_retention().get(cat.value, True))
    mr_state.set_sync_retention(cat.value, keep)
    if keep:
        # Включили обратно: pull снова имеет право восстановить копии.
        try:
            from . import retention  # noqa: PLC0415 — ленивый импорт

            await retention.reenable_category(cat.value)
        except Exception:  # noqa: BLE001 — не роняет экран
            logger.exception("pdsync retention: reenable failed")
        await query.answer(
            "Retention включён: локальные копии категории снова "
            "сохраняются и могут быть восстановлены из внешней БД."
        )
    else:
        await query.answer(
            "Retention выключен «на будущее»: новые данные категории не "
            "будут оседать локально после подтверждения sync. Существующая "
            "копия НЕ тронута.",
            show_alert=True,
        )
    await _show_retention_screen(query)


def _kb_purge_confirm(cat: SyncCategory) -> InlineKeyboardMarkup:
    """ОТДЕЛЬНОЕ явное подтверждение удаления (18.7 §2)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🗑 Подтверждаю удаление локальной копии",
            callback_data=f"mr:pdsync:ret:purge:{cat.value}:confirm",
        )],
        [InlineKeyboardButton(text="✖️ Отмена — ничего не удалять",
                              callback_data="mr:pdsync:ret")],
    ])


async def _show_purge_confirm(query: CallbackQuery, raw_cat: str) -> None:
    """Шаг 1 удаления: объём затрагиваемых данных + отдельное
    подтверждение (18.7 §2). Без подтверждения ничего не удаляется."""
    cat = _parse_category(raw_cat)
    if cat is None:
        await query.answer()
        return
    if mr_state.get_sync_retention().get(cat.value, True):
        await query.answer(
            "Retention категории включён — сначала выключите переключатель.",
            show_alert=True,
        )
        await _show_retention_screen(query)
        return
    await query.answer("Считаю объём затрагиваемых данных…")
    try:
        from . import retention  # noqa: PLC0415 — ленивый импорт

        vol = await retention.estimate_category(cat.value)
    except Exception:  # noqa: BLE001
        logger.exception("pdsync retention: estimate failed")
        await query.answer("Не удалось посчитать объём.", show_alert=True)
        return
    body = (
        "<b>🗑 Удаление локальной копии</b>\n\n"
        f"Категория: <b>{CATEGORY_LABEL[cat]}</b>\n"
        f"Будет удалено локально: <b>{int(vol['records'])} зап., "
        f"{_fmt_bytes(int(vol['bytes']))}</b>\n\n"
        "Удаляется ТОЛЬКО локальная копия. Внешняя копия в БД/S3 остаётся "
        "полной. Перед удалением каждая запись проверяется: операция "
        "подтверждена (read-after-write), для файлов сверяются object key, "
        "размер и checksum в S3. Частично проверенная категория не "
        "удаляется.\n\n"
        "Это действие необратимо локально. Подтвердите отдельно:"
    )
    await _edit_or_send(query, body, _kb_purge_confirm(cat))


async def _run_purge(query: CallbackQuery, raw_cat: str) -> None:
    """Шаг 2: удаление ПОСЛЕ отдельного подтверждения owner (18.7 §3)."""
    cat = _parse_category(raw_cat)
    if cat is None:
        await query.answer()
        return
    await query.answer("Проверяю и удаляю…")
    try:
        from . import retention  # noqa: PLC0415 — ленивый импорт

        ok, message = await retention.purge_category(cat.value)
    except Exception:  # noqa: BLE001 — без значений в логе
        logger.exception("pdsync retention: purge crashed (no values logged)")
        ok, message = False, "Удаление не выполнено: внутренняя ошибка."
    with contextlib.suppress(Exception):
        await query.answer(message, show_alert=True)
    if not ok:
        logger.warning("pdsync retention purge declined/failed")
    await _show_retention_screen(query)


# ─── Router ──────────────────────────────────────────────────────────────────


def build_pdsync_router() -> Router:
    """Router экрана. Один диспетчер на весь префикс ``mr:pdsync`` —
    owner-проверка гарантированно выполняется для КАЖДОГО callback'а."""
    router = Router(name="masterroom_pdsync")

    @router.callback_query(F.data.startswith(PDSYNC_PREFIX))
    async def _dispatch(query: CallbackQuery) -> None:
        # СТРОГИЙ owner: не co-owner, не can_admin, не full_public.
        if not _is_strict_owner(query):
            await query.answer("Только владелец", show_alert=True)
            return

        data = query.data or ""
        if data == "mr:pdsync":
            await query.answer()
            # 18.4 §3: идемпотентная миграция plaintext-секретов в
            # защищённое хранилище (no-op без master key, plaintext цел).
            await _ensure_secrets_migrated()
            await _show_screen(query)
            return
        if data == "mr:pdsync:pause":
            # НЕ заглушка: реально переключаем paused в state.
            new_paused = not mr_state.is_sync_paused()
            mr_state.set_sync_paused(new_paused)
            # 18.6: будим воркер, чтобы он сразу увидел новый флаг
            # (пауза останавливает после текущей операции).
            with contextlib.suppress(Exception):
                from . import incremental  # noqa: PLC0415

                incremental.wake()
            await query.answer(
                "Синхронизация на паузе" if new_paused
                else "Синхронизация возобновлена"
            )
            await _show_screen(query)
            return
        if data == "mr:pdsync:check":
            # РЕАЛЬНАЯ проверка подключения (18.3 §4) — не заглушка.
            await _run_connection_check(query)
            return
        if data == "mr:pdsync:preview":
            # 18.5 §1: dry-run — отчёт БЕЗ единой записи куда-либо.
            await _run_preview(query)
            return
        if data == "mr:pdsync:all":
            # 18.5 §2: отчёт + явное подтверждение, без него не пишем.
            await _show_full_sync_confirm(query)
            return
        if data == "mr:pdsync:all:confirm":
            # 18.5 §3: старт bootstrap только после подтверждения owner.
            await _start_full_sync(query)
            return
        if data == "mr:pdsync:retry":
            # 18.6 §4: failed → pending с тем же operation_id, без дублей.
            await _run_retry_failed(query)
            return
        if data == "mr:pdsync:conflicts":
            # 18.6 §3: список неясных конфликтов (без секретов/дампов).
            await _show_conflicts(query)
            return
        if data == "mr:pdsync:ret":
            # 18.7 §1: экран retention-переключателей (owner-only —
            # проверка уже выполнена диспетчером выше).
            await query.answer()
            await _show_retention_screen(query)
            return
        if data.startswith("mr:pdsync:ret:t:"):
            # 18.7 §1/§2: переключить «Оставлять локально» категории.
            await _toggle_retention(query, data.removeprefix("mr:pdsync:ret:t:"))
            return
        if data.startswith("mr:pdsync:ret:purge:"):
            # 18.7 §2/§3: объём + отдельное подтверждение → удаление.
            rest = data.removeprefix("mr:pdsync:ret:purge:")
            if rest.endswith(":confirm"):
                await _run_purge(query, rest.removesuffix(":confirm"))
            else:
                await _show_purge_confirm(query, rest)
            return
        if data.startswith("mr:pdsync:cfl:"):
            # Карточка конфликта / выбор версии owner'ом (18.6 §3).
            parts = data.split(":")
            try:
                conflict_id = int(parts[3])
            except (IndexError, ValueError):
                await query.answer()
                return
            if len(parts) >= 5 and parts[4] in ("local", "remote"):
                await _resolve_conflict_cb(query, conflict_id, parts[4])
            else:
                await _show_conflict_card(query, conflict_id)
            return
        # Неизвестный pdsync-callback — молча закрываем «часики».
        await query.answer()

    return router
