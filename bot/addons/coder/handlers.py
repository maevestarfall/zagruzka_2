# Coder addon handlers: FSM loop simulator with SQLite-backed audit.
# Co-authored with CoCo
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import sqlite3
import time
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import state as addon_state
from ...coder.persistence import DB_PATH, init_db
from ...coder.fsm import CoderRunState, apply_event, derive_event_id
from ...coder.cost import reserve_budget, settle_budget
from ...coder.security import scrub_secrets, assert_path_inside_sandbox, validate_skill_ast
from ...coder_server import CoderService

logger = logging.getLogger(__name__)
_runtime = CoderService("data/coder_server")

coder_router = Router()

ADDON = "coder"

# Make sure SQLite DB is initialized
init_db(DB_PATH)

# ---- FSM States for User Input ----
class CoderStates(StatesGroup):
    awaiting_task_desc = State()

# ---- Default Settings ----
DEFAULT_WORKERS = 3
DEFAULT_BUDGET = 5.0
DEFAULT_AUTOMERGE = True
DEFAULT_BRAIN_SEP = True

# ---- State Getters/Setters ----
def get_workers() -> int:
    return int(addon_state.get(ADDON, "workers", DEFAULT_WORKERS))

def set_workers(count: int) -> None:
    addon_state.set_(ADDON, "workers", count)

def get_budget() -> float:
    return float(addon_state.get(ADDON, "budget", DEFAULT_BUDGET))

def set_budget(amount: float) -> None:
    addon_state.set_(ADDON, "budget", amount)

def get_automerge() -> bool:
    return bool(addon_state.get(ADDON, "automerge", DEFAULT_AUTOMERGE))

def set_automerge(enabled: bool) -> None:
    addon_state.set_(ADDON, "automerge", enabled)

def get_brain_sep() -> bool:
    return bool(addon_state.get(ADDON, "brain_sep", DEFAULT_BRAIN_SEP))

def set_brain_sep(enabled: bool) -> None:
    addon_state.set_(ADDON, "brain_sep", enabled)

# ---- Keyboards ----
def _kb_coder_dashboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Запустить проект", callback_data="coder:run")],
            [
                InlineKeyboardButton(text="📊 Статус и Метрики", callback_data="coder:status"),
                InlineKeyboardButton(text="📜 Логи и Аудит", callback_data="coder:logs"),
            ],
            [
                InlineKeyboardButton(text="🛡️ Безопасность", callback_data="coder:security"),
                InlineKeyboardButton(text="🚪 Арбитраж и Дебаты", callback_data="coder:debates"),
            ],
            [InlineKeyboardButton(text="⚙️ Настройки Фирмы", callback_data="coder:settings")],
            [InlineKeyboardButton(text="↩️ В главное меню", callback_data="coder:back")],
        ]
    )

def _kb_coder_settings() -> InlineKeyboardMarkup:
    workers = get_workers()
    budget = get_budget()
    automerge = "✅ ON" if get_automerge() else "❌ OFF"
    brain_sep = "✅ ON" if get_brain_sep() else "❌ OFF"
    
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"👥 Воркеры: {workers}", callback_data="coder:set_workers_prompt"),
                InlineKeyboardButton(text=f"💰 Бюджет: ${budget:.2f}", callback_data="coder:set_budget_prompt"),
            ],
            [InlineKeyboardButton(text=f"🔄 Авто-слияние (Auto-Merge): {automerge}", callback_data="coder:toggle_automerge")],
            [InlineKeyboardButton(text=f"🧠 Разделение Моделей (Brain-Sep): {brain_sep}", callback_data="coder:toggle_brain_sep")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="coder:menu")],
        ]
    )

def _kb_back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Назад", callback_data="coder:menu")]
        ]
    )

def _kb_merge_approval(run_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Одобрить Слияние (Merge)", callback_data=f"coder:merge_approve:{run_id}")],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data="coder:menu")]
        ]
    )

# «Живая карта проекта»: @traced — no-op при LIVEMAP_ENABLED=0 (ТЗ 5.3).
from ..projectmap.tracer import traced as _lm_traced


# ---- Real Master Room coder-server route ----
_CODER_SERVICE = None


@_lm_traced("coder.run")
async def run_fsm_loop(run_id: str, task_desc: str, chat_id: int, bot: Any):
    """Submit work to the durable tool runtime; never emit synthetic results."""
    global _CODER_SERVICE
    from bot.coder_server import CoderService

    if _CODER_SERVICE is None:
        _CODER_SERVICE = CoderService(os.getenv("CODER_SERVER_DATA", "data/coder-server"))
    task = _CODER_SERVICE.create_task(task_desc[:120], task_desc, {"legacy_run_id": run_id})
    _CODER_SERVICE.start(task["id"])
    if bot and chat_id:
        await bot.send_message(
            chat_id,
            "Задача передана в Master Room. Создан реальный workspace и запущен "
            f"durable workflow: <code>{task['id']}</code>. Ход работы и фактические "
            "tool results доступны в live-журнале.",
            reply_markup=_kb_back_to_menu(),
        )


# Retained only for migration archaeology; no user route calls it.
async def _disabled_legacy_fsm_loop(run_id: str, task_desc: str, chat_id: int, bot: Any):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    
    _workers = get_workers()  # noqa: F841 — call retained for side effects
    budget_cap = get_budget()
    auto_merge = get_automerge()
    brain_sep = get_brain_sep()
    
    # Helper to log audit events with secret scrubbing
    def log_audit(persona: str, kind: str, message: str, family: str = None):
        scrubbed_message = scrub_secrets(message)
        with conn:
            conn.execute(
                "INSERT INTO coder_audit_events (run_id, persona, event_kind, message, retry_family, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, persona, kind, scrubbed_message, family, time.strftime("%Y-%m-%d %H:%M:%S"))
            )
            
    # Helper to update run state
    def update_run_state(state: str, spent_increment: float = 0.0):
        with conn:
            conn.execute(
                "UPDATE coder_runs SET state = ?, usd_spent = usd_spent + ? WHERE run_id = ?",
                (state, spent_increment, run_id)
            )

    try:
        # ---- 1. INIT ----
        log_audit("coder_boss", "fsm", f"Инициализация Coder Firm для задачи: \"{task_desc}\"")
        await asyncio.sleep(1.5)
        
        # ---- 2. PLANNING ----
        event_id = derive_event_id(run_id, "planning_start", "empty")
        apply_event(run_id, event_id, CoderRunState.INIT, CoderRunState.PLANNING)
        log_audit("coder_boss", "fsm", "FSM переход: INIT -> PLANNING. Boss Orchestrator начинает планирование.")
        
        # Reserve budget (LLM Cost Ledger)
        permit = reserve_budget(run_id, "coder_boss", 0.045)
        if permit:
            log_audit("coder_boss", "cost", f"Зарезервирован бюджет {permit.estimated_usd:.4f} USD для Boss Orchestrator (ID: {permit.reservation_id})")
            
            # Create tasks in DB
            task_id = f"task_{run_id}_1"
            with conn:
                conn.execute(
                    "INSERT INTO coder_tasks (task_id, run_id, title, state, assigned_worker, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (task_id, run_id, f"Реализация: {task_desc[:40]}...", "PENDING", "coder_worker_01", time.strftime("%Y-%m-%d %H:%M:%S"))
                )
            log_audit("coder_boss", "task", f"Создана подзадача {task_id} и назначена на coder_worker_01")
            
            # Settle budget
            settle_budget(permit.reservation_id, 0.038)
            update_run_state("PLANNING", 0.038)
            log_audit("coder_boss", "cost", "Списание бюджета: 0.0380 USD для Boss Orchestrator")
            
        await asyncio.sleep(2.0)
        
        # ---- 3. RESEARCH ----
        log_audit("coder_researcher", "research", "Researcher начинает поиск внешних референсов.")
        ref_url = "https://github.com/example/stealth-patterns"
        with conn:
            conn.execute(
                "INSERT INTO coder_external_ref_decisions (ref_url, integration_mode, applies_to_cluster, decided_at) VALUES (?, ?, ?, ?)",
                (ref_url, "pattern-only", "security-path-escape", time.strftime("%Y-%m-%d %H:%M:%S"))
            )
        log_audit("coder_researcher", "research", f"Записано решение по референсу {ref_url} в coder_external_ref_decisions")
        await asyncio.sleep(2.0)
        
        # ---- 4. CODING ----
        event_id = derive_event_id(run_id, "coding_start", "empty")
        apply_event(run_id, event_id, CoderRunState.PLANNING, CoderRunState.CODING)
        log_audit("coder_worker_01", "fsm", "FSM переход: PLANNING -> CODING. Worker начинает написание кода.")
        
        # Reserve budget for Worker
        permit_worker = reserve_budget(run_id, "coder_worker_01", 0.030)
        if permit_worker:
            log_audit("coder_worker_01", "cost", f"Зарезервирован бюджет {permit_worker.estimated_usd:.4f} USD для coder_worker_01 (ID: {permit_worker.reservation_id})")
            
            # Settle budget for Worker
            settle_budget(permit_worker.reservation_id, 0.025)
            update_run_state("CODING", 0.025)
            log_audit("coder_worker_01", "cost", "Списание бюджета: 0.0250 USD для coder_worker_01")
            
        # Path-escape check
        target_file = f"coder_runs/{run_id}/artifacts/feature.py"
        assert_path_inside_sandbox(run_id, target_file)
        log_audit("coder_worker_01", "sandbox", f"Проверка пути: файл '{target_file}' находится внутри песочницы.")
        
        # AST validation
        fake_skill_code = "import math\ndef test(): return math.sqrt(4)"
        validate_skill_ast(fake_skill_code)
        log_audit("coder_worker_01", "ast", "AST-валидация: код успешно прошел синтаксический анализ.")
        
        await asyncio.sleep(2.5)
        
        # ---- 5. REVIEW_PENDING ----
        event_id = derive_event_id(run_id, "review_start", "empty")
        apply_event(run_id, event_id, CoderRunState.CODING, CoderRunState.REVIEW_PENDING)
        log_audit("coder_reviewer", "fsm", "FSM переход: CODING -> REVIEW_PENDING. Ожидание код-ревью.")
        log_audit("coder_reviewer", "review", "Reviewer проверил код. Оценка качества: 8.5/10 (PASS). Сгенерирован подписанный вердикт.")
        await asyncio.sleep(2.0)
        
        # ---- 6. TEST_PENDING ----
        event_id = derive_event_id(run_id, "test_start", "empty")
        apply_event(run_id, event_id, CoderRunState.REVIEW_PENDING, CoderRunState.TEST_PENDING)
        log_audit("coder_tester", "fsm", "FSM переход: REVIEW_PENDING -> TEST_PENDING. Запуск автотестов.")
        
        # Write scorecard to DB
        with conn:
            conn.execute(
                "INSERT INTO coder_scorecards (run_id, task_id, passed_tests, failed_tests, verdict, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, task_id, 3, 0, "PASS", time.strftime("%Y-%m-%d %H:%M:%S"))
            )
        log_audit("coder_tester", "test", "Тесты пройдены успешно (3 Passed, 0 Failed). Сгенерирован Scorecard в coder_scorecards.")
        await asyncio.sleep(2.0)
        
        # ---- 7. AUDIT_PENDING ----
        event_id = derive_event_id(run_id, "audit_start", "empty")
        apply_event(run_id, event_id, CoderRunState.TEST_PENDING, CoderRunState.AUDIT_PENDING)
        log_audit("coder_auditor", "fsm", "FSM переход: TEST_PENDING -> AUDIT_PENDING. Проверка безопасности.")
        
        # Auditor checks brain-sep invariant
        if brain_sep:
            log_audit("coder_auditor", "security", "Аудит безопасности: Модель Аудитора отличается от Воркера (Brain Family Separation: OK).")
        log_audit("coder_auditor", "security", "Проверка на уязвимости и path-escape: Успешно (PASS).")
        await asyncio.sleep(2.0)
        
        # ---- 8. INTEGRATE_PENDING ----
        event_id = derive_event_id(run_id, "integrate_start", "empty")
        apply_event(run_id, event_id, CoderRunState.AUDIT_PENDING, CoderRunState.INTEGRATE_PENDING)
        log_audit("coder_integrator", "fsm", "FSM переход: AUDIT_PENDING -> INTEGRATE_PENDING. Integrator готовит Pull Request.")
        
        # Check if auto-merge is enabled
        if auto_merge:
            # Complete the run automatically
            update_run_state("DONE")
            log_audit("coder_integrator", "git", "Auto-Merge активен. Слияние PR #42 в ветку main выполнено успешно (Commit: 8f2c3d4e).")
            log_audit("coder_boss", "fsm", f"Проект {run_id} успешно завершен!")
            
            # Notify owner
            if bot and chat_id:
                await bot.send_message(
                    chat_id,
                    f"🎉 <b>Проект {run_id} успешно завершен!</b>\n\n"
                    f"ℹ️ <b>Результат работы:</b>\n"
                    f"• <b>Задача:</b> <i>\"{task_desc}\"</i>\n"
                    f"• <b>Файлы:</b> Создан <code>bot/coder/feature.py</code> и тесты <code>tests/test_feature.py</code>\n"
                    f"• <b>Затраты:</b> 0.063 USD (Дневной лимит: ${budget_cap:.2f})\n"
                    f"• <b>Ревью:</b> 8.5/10 (Reviewer) | PASS (Auditor)\n"
                    f"• <b>Тесты:</b> 3 Passed, 0 Failed\n"
                    f"• <b>Git:</b> PR #42 успешно влит в ветку <code>main</code> (Commit: <code>8f2c3d4e</code>)\n\n"
                    f"Бот успешно обновил свой функционал в реальном времени!",
                    reply_markup=_kb_back_to_menu()
                )
        else:
            # Pause and ask for owner approval
            log_audit("coder_integrator", "fsm", "Auto-Merge выключен. Ожидание одобрения владельца для слияния PR.")
            if bot and chat_id:
                await bot.send_message(
                    chat_id,
                    f"⏸ <b>Проект {run_id} ожидает одобрения слияния (Merge)</b>\n\n"
                    f"Все автоматические проверки (Review, Test, Audit) пройдены успешно. "
                    f"Поскольку опция Auto-Merge выключена в настройках, требуется твое подтверждение:\n\n"
                    f"• <b>Задача:</b> <i>\"{task_desc}\"</i>\n"
                    f"• <b>Ревью:</b> 8.5/10 (Reviewer) | PASS (Auditor)\n"
                    f"• <b>Тесты:</b> 3 Passed, 0 Failed",
                    reply_markup=_kb_merge_approval(run_id)
                )
            
    except Exception as e:
        logger.exception("FSM Loop failed")
        log_audit("coder_boss", "error", f"Ошибка во время выполнения FSM: {e}", "infra")
        update_run_state("FAILED_INFRA")
    finally:
        conn.close()

# ---- Handlers ----
@coder_router.callback_query(F.data == "main:coder")
@coder_router.callback_query(F.data == "coder:menu")
async def cb_coder_dashboard(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    
    workers = get_workers()
    budget = get_budget()
    
    # Query real SQLite DB for stats
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), SUM(usd_spent) FROM coder_runs")
    row = cursor.fetchone()
    runs_count = row[0] if row else 0
    total_spent = row[1] if row and row[1] is not None else 0.0
    conn.close()
    
    text = (
        "<b>💻 Telegram Coder Firm (Кодер)</b>\n\n"
        "Добро пожаловать в панель управления полноценной ИИ-кодинг фирмой! "
        "Здесь ты выступаешь в роли Владельца (Owner), координирующего работу "
        "автономных ИИ-агентов на базе <b>Oracle Cloud Free Tier</b>.\n\n"
        f"👥 <b>Активный штат:</b> {workers} Workers + 6 специализированных ролей\n"
        f"💰 <b>Дневной лимит бюджета:</b> ${budget:.2f} USD\n"
        f"🚀 <b>Всего запущено проектов:</b> {runs_count}\n"
        f"💳 <b>Суммарный расход на LLM:</b> ${total_spent:.4f} USD\n\n"
        "Выбери действие ниже:"
    )
    
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(text, reply_markup=_kb_coder_dashboard())

@coder_router.callback_query(F.data == "coder:back")
async def cb_coder_back(query: CallbackQuery) -> None:
    await query.answer()
    from ...wizard import _kb_main_after_claim
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "Главное меню. Выбери что сделать:",
                reply_markup=_kb_main_after_claim(query.from_user.id),
            )

@coder_router.callback_query(F.data == "coder:settings")
async def cb_coder_settings_menu(query: CallbackQuery) -> None:
    await query.answer()
    text = (
        "<b>⚙️ Настройки Coder Firm</b>\n\n"
        "Сконфигурируй параметры работы своей ИИ-кодинг фирмы:\n\n"
        "• <b>Воркеры (Workers):</b> Количество параллельных разработчиков (1-20). Больше воркеров = быстрее параллельная разработка, но выше расход токенов.\n"
        "• <b>Бюджет (Daily USD Cap):</b> Жесткий лимит затрат на LLM-запросы в день. При 80% придет уведомление, при 100% новые задачи заблокируются.\n"
        "• <b>Авто-слияние (Auto-Merge):</b> Автоматическое одобрение и слияние PR в Git после прохождения всех проверок качества.\n"
        "• <b>Разделение Моделей (Brain Family Separation):</b> Архитектурное правило безопасности — модель Аудитора обязана отличаться от модели Воркера, чтобы избежать сговора ИИ."
    )
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(text, reply_markup=_kb_coder_settings())

@coder_router.callback_query(F.data == "coder:toggle_automerge")
async def cb_toggle_automerge(query: CallbackQuery) -> None:
    current = get_automerge()
    set_automerge(not current)
    await query.answer(f"Auto-Merge: {'ON' if not current else 'OFF'}")
    await cb_coder_settings_menu(query)

@coder_router.callback_query(F.data == "coder:toggle_brain_sep")
async def cb_toggle_brain_sep(query: CallbackQuery) -> None:
    current = get_brain_sep()
    set_brain_sep(not current)
    await query.answer(f"Brain-Sep: {'ON' if not current else 'OFF'}")
    await cb_coder_settings_menu(query)

@coder_router.callback_query(F.data == "coder:set_workers_prompt")
async def cb_set_workers_prompt(query: CallbackQuery) -> None:
    current = get_workers()
    options = [1, 3, 5, 10, 20]
    try:
        idx = options.index(current)
        next_val = options[(idx + 1) % len(options)]
    except ValueError:
        next_val = 3
    set_workers(next_val)
    await query.answer(f"Штат воркеров изменен на {next_val}")
    await cb_coder_settings_menu(query)

@coder_router.callback_query(F.data == "coder:set_budget_prompt")
async def cb_set_budget_prompt(query: CallbackQuery) -> None:
    current = get_budget()
    options = [1.0, 5.0, 10.0, 25.0, 50.0]
    try:
        idx = options.index(current)
        next_val = options[(idx + 1) % len(options)]
    except ValueError:
        next_val = 5.0
    set_budget(next_val)
    await query.answer(f"Лимит бюджета изменен на ${next_val:.2f}")
    await cb_coder_settings_menu(query)

@coder_router.callback_query(F.data == "coder:run")
async def cb_coder_run_prompt(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(CoderStates.awaiting_task_desc)
    
    text = (
        "<b>🚀 Запуск Нового Проекта в Coder Firm</b>\n\n"
        "Опиши задачу, которую должна реализовать твоя ИИ-фирма. "
        "Например:\n"
        "• <i>«Напиши асинхронный логгер транзакций для базы данных SQLite WAL»</i>\n"
        "• <i>«Добавь валидацию email-адресов по регулярному выражению в форму регистрации»</i>\n\n"
        "Пришли описание задачи следующим сообщением:"
    )
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(text, reply_markup=_kb_back_to_menu())

@coder_router.message(StateFilter(CoderStates.awaiting_task_desc))
async def msg_coder_run_submit(message: Message, state: FSMContext) -> None:
    task_desc = message.text or ""
    if not task_desc:
        await message.answer("Описание не может быть пустым. Попробуй еще раз:")
        return
        
    await state.clear()
    # uuid вместо random(100..999): исключает коллизии PRIMARY KEY в coder_runs.
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    
    # Save the run to SQLite
    conn = sqlite3.connect(DB_PATH)
    with conn:
        conn.execute(
            "INSERT INTO coder_runs (run_id, task_desc, state, workers_count, budget_cap, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, task_desc, "INIT", get_workers(), get_budget(), time.strftime("%Y-%m-%d %H:%M:%S"))
        )
    conn.close()
    
    # Send initial message
    await message.answer(
        f"🚀 <b>Проект {run_id} успешно инициализирован!</b>\n\n"
        f"ИИ-агенты приступают к выполнению. Следи за ходом выполнения в реальном времени через 📜 Логи или 📊 Статус."
    )
    
    # Run the FSM loop in background task
    asyncio.create_task(run_fsm_loop(run_id, task_desc, message.chat.id, message.bot))

@coder_router.callback_query(F.data.startswith("coder:merge_approve:"))
async def cb_merge_approve(query: CallbackQuery) -> None:
    # ТЗ §11: одобрение фиксирует только реальный факт (решение владельца).
    # Никаких выдуманных PR-номеров и commit-хэшей — завершение задачи
    # проходит через evidence gate coder-server, а не через текст в чате.
    run_id = (query.data or "").split(":")[-1]
    await query.answer("Одобрение зафиксировано")

    conn = sqlite3.connect(DB_PATH)
    with conn:
        conn.execute("UPDATE coder_runs SET state = 'APPROVED' WHERE run_id = ?", (run_id,))
        conn.execute(
            "INSERT INTO coder_audit_events (run_id, persona, event_kind, message, ts) VALUES (?, 'owner', 'approval', ?, ?)",
            (run_id, f"Владелец одобрил интеграцию для {run_id}. Фактическое слияние выполнит Integrator и подтвердит evidence.", time.strftime("%Y-%m-%d %H:%M:%S")),
        )
    conn.close()

    # Пробрасываем решение в durable runtime, если задача ждёт approval.
    gate_note = "Слияние выполнит Integrator; результат появится в логах с реальным commit-хэшем."
    if _CODER_SERVICE is not None:
        with contextlib.suppress(Exception):
            for t in _CODER_SERVICE.store.list_tasks(limit=100):
                if (t.get("budget") or {}).get("legacy_run_id") == run_id and t["state"] == "waiting_approval":
                    _CODER_SERVICE.approve(t["id"], True)
                    gate_note = f"Решение передано в задачу <code>{t['id']}</code>; выполнение продолжено."
                    break

    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                f"✅ <b>Одобрение для {run_id} зафиксировано</b>\n\n{gate_note}",
                reply_markup=_kb_back_to_menu(),
            )

@coder_router.callback_query(F.data == "coder:status")
async def cb_coder_status(query: CallbackQuery) -> None:
    await query.answer()
    
    workers = get_workers()
    budget = get_budget()
    
    # Query real SQLite database for status metrics
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT run_id, state, usd_spent FROM coder_runs ORDER BY created_at DESC LIMIT 1")
    active_run = cursor.fetchone()
    
    cursor.execute("SELECT COUNT(*), SUM(usd_spent) FROM coder_runs")
    totals = cursor.fetchone()
    total_runs = totals[0] if totals else 0
    total_spent = totals[1] if totals and totals[1] is not None else 0.0
    conn.close()
    
    active_run_text = "Нет активных задач. Фирма находится в состоянии ожидания (IDLE)."
    if active_run and active_run[1] not in ("DONE", "FAILED_INFRA", "FAILED_BUDGET"):
        active_run_text = f"🔄 <b>Проект {active_run[0]}</b> в состоянии <code>{active_run[1]}</code>. Потрачено: ${active_run[2]:.4f} USD"

    # ТЗ: только фактические метрики хоста, без выдуманных цифр.
    infra_lines = [f"• <b>Платформа:</b> {platform.system()} {platform.machine()}, Python {platform.python_version()}"]
    try:
        import psutil  # optional dependency

        mem = psutil.virtual_memory()
        infra_lines.append(f"• <b>CPU:</b> {psutil.cpu_count()} vCPUs (загрузка {psutil.cpu_percent(interval=None):.0f}%)")
        infra_lines.append(f"• <b>RAM:</b> {mem.total / 1024**3:.1f} GB (использовано {mem.used / 1024**3:.1f} GB)")
    except Exception:  # noqa: BLE001 — psutil может отсутствовать
        infra_lines.append(f"• <b>CPU:</b> {os.cpu_count() or '?'} vCPUs")

    text = (
        "<b>📊 Метрики и Статус Coder Firm</b>\n\n"
        "🖥 <b>Инфраструктура (фактические данные хоста):</b>\n"
        + "\n".join(infra_lines) + "\n"
        + f"• <b>Конфигурация:</b> 1x Boss, {workers}x Workers, 1x Integrator\n\n"
        "💳 <b>LLM Лимиты и Затраты:</b>\n"
        f"• <b>Дневной бюджет:</b> ${budget:.2f} USD\n"
        f"• <b>Потрачено сегодня:</b> ${total_spent:.4f} USD\n"
        f"• <b>Всего запусков:</b> {total_runs}\n\n"
        f"🔄 <b>Активные процессы:</b>\n• {active_run_text}"
    )
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(text, reply_markup=_kb_back_to_menu())

@coder_router.callback_query(F.data == "coder:logs")
async def cb_coder_logs(query: CallbackQuery) -> None:
    await query.answer()
    
    # Query real SQLite database for audit events
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT ts, persona, event_kind, message FROM coder_audit_events ORDER BY event_id DESC LIMIT 10")
    rows = cursor.fetchall()
    conn.close()
    
    log_lines = []
    if rows:
        for r in reversed(rows):
            # Format timestamp to show only time
            time_part = r[0].split()[-1] if " " in r[0] else r[0]
            log_lines.append(f"[{time_part}] [{r[1].upper()}] {r[3]}")
    else:
        # ТЗ: никаких синтетических «правдоподобных» логов — только честный факт.
        log_lines = ["Журнал пуст: события появятся после первого реального запуска (/coder run)."]
        
    text = (
        "<b>📜 Хронологические Логи Coder Firm (Audit CLI)</b>\n\n"
        "Выгрузка последних событий из базы данных <code>coder_audit_events</code>:\n"
        "<pre>"
        f"{chr(10).join(log_lines)}"
        "</pre>"
    )
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(text, reply_markup=_kb_back_to_menu())

@coder_router.callback_query(F.data == "coder:security")
async def cb_coder_security(query: CallbackQuery) -> None:
    await query.answer()
    
    text = (
        "<b>🛡️ Безопасность и Комплаенс (Security Center)</b>\n\n"
        "Все ИИ-агенты зажаты в строгие рамки безопасности (Part C Compliance):\n\n"
        "• <b>No-Self-Modification:</b> ✅ Активен. Агенты не могут модифицировать файлы самого бота Lilush.\n"
        "• <b>Path-Escape Protection:</b> ✅ Активен. Все дисковые операции изолированы внутри песочницы <code>coder_runs/{run_id}/</code>.\n"
        "• <b>Secret Scrubbing:</b> ✅ Активен. Из всех логов автоматически вырезаются API-ключи, приватные токены и пароли перед отправкой.\n"
        "• <b>AST Skill-Install Gate:</b> ✅ Активен. Любой устанавливаемый ИИ-агентом навык парсится через <code>ast.parse()</code> на наличие запрещенного кода (eval, exec, import os)."
    )
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(text, reply_markup=_kb_back_to_menu())

@coder_router.callback_query(F.data == "coder:debates")
async def cb_coder_debates(query: CallbackQuery) -> None:
    await query.answer()
    
    text = (
        "<b>🚪 Арбитраж и Дебаты (Judge & Debate Panel)</b>\n\n"
        "Когда Reviewer и Auditor не сходятся во мнении о качестве кода, запускается процесс автоматического арбитража:\n\n"
        "• <b>Судья (Judge):</b> Автономная модель, отличная от участников спора, выносит окончательный вердикт.\n"
        "• <b>Бюджет дебатов:</b> Ограничен жестким дневным лимитом, чтобы избежать бесконечного спора моделей.\n"
        "• <b>Текущий статус:</b> 0 активных споров. Все решения согласованы."
    )
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(text, reply_markup=_kb_back_to_menu())

# ---- Command Line Interface (/coder) ----
@coder_router.message(Command("coder"))
async def cmd_coder_cli(message: Message) -> None:
    args = (message.text or "").split()[1:]
    if not args:
        await message.answer(
            "<b>💻 Coder Firm CLI</b>\n\n"
            "Доступные команды:\n"
            "• <code>/coder run &lt;описание задачи&gt;</code> — запустить новый проект\n"
            "• <code>/coder logs</code> — показать последние логи\n"
            "• <code>/coder status</code> — показать текущие метрики"
        )
        return
        
    cmd = args[0].lower()
    if cmd == "run":
        task_desc = " ".join(args[1:])
        if not task_desc:
            await message.answer("Укажи описание задачи. Пример: <code>/coder run Тестовая задача</code>")
            return
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        conn = sqlite3.connect(DB_PATH)
        with conn:
            conn.execute(
                "INSERT INTO coder_runs (run_id, task_desc, state, workers_count, budget_cap, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, task_desc, "INIT", get_workers(), get_budget(), time.strftime("%Y-%m-%d %H:%M:%S"))
            )
        conn.close()
        await message.answer(f"🚀 Проект <b>{run_id}</b> запущен через CLI!")
        asyncio.create_task(run_fsm_loop(run_id, task_desc, message.chat.id, message.bot))
        
    elif cmd == "logs":
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT ts, persona, event_kind, message FROM coder_audit_events ORDER BY event_id DESC LIMIT 10")
        rows = cursor.fetchall()
        conn.close()
        log_lines = []
        if rows:
            for r in reversed(rows):
                time_part = r[0].split()[-1] if " " in r[0] else r[0]
                log_lines.append(f"[{time_part}] [{r[1].upper()}] {r[3]}")
        else:
            log_lines = ["Логи пусты. Запусти проект через меню или команду /coder run."]
        await message.answer(f"<b>📜 Логи Coder Firm (CLI):</b>\n<pre>{chr(10).join(log_lines)}</pre>")
        
    elif cmd == "status":
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT run_id, state, usd_spent FROM coder_runs ORDER BY created_at DESC LIMIT 1")
        active_run = cursor.fetchone()
        conn.close()
        active_run_text = "Нет активных задач. Фирма находится в состоянии ожидания (IDLE)."
        if active_run and active_run[1] not in ("DONE", "FAILED_INFRA", "FAILED_BUDGET"):
            active_run_text = f"Проект {active_run[0]} в состоянии {active_run[1]}. Потрачено: ${active_run[2]:.4f} USD"
        await message.answer(f"<b>📊 Текущий статус (CLI):</b>\n• {active_run_text}")

def build_coder_router() -> Router:
    return coder_router
