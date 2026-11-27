"""aiogram router + UI screens for the algorithm addon.

Wires three sub-flows onto callback data prefixed with ``algo:``:

* **Slot list** (``algo:menu``) — 10 buttons named after each slot's
  ``name`` (or "Слот N" when empty), plus ← Назад.
* **Slot detail** (``algo:slot:<N>``) — AI-plan / manual / run /
  delete / interval / back.
* **Draft / preview** — after the planner returns we keep the draft
  in FSM data and show «▶ Запустить» / «💾 Сохранить» /
  «🔁 Перепродумать» / «← Назад». Running posts step-by-step
  progress to the chat, then offers «🔁 Перепродумать» / «💾 Сохранить»
  again on the post-run screen.

The addon registers a single ``F.message`` handler gated by an FSM
state — that handler captures the user's typed plan / news text /
interval-minutes number. Because it's gated by ``StateFilter(...)``
it does NOT compete with pretty_text's catch-all (which is gated by
``StateFilter(None)``), so the two coexist regardless of router order.
"""

from __future__ import annotations

import contextlib
import logging
import math

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ...access import can_admin, can_use
from . import executor, planner
from . import state as algo_state
from .state import Slot

logger = logging.getLogger(__name__)


class AlgorithmStates(StatesGroup):
    """FSM states for the multi-step input flows."""

    awaiting_ai_input = State()
    awaiting_manual_plan = State()
    awaiting_interval = State()
    awaiting_link = State()
    awaiting_import = State()

_MIN_PER_DAY = 60 * 24            # 1440
_MIN_PER_WEEK = _MIN_PER_DAY * 7  # 10080
_MIN_PER_MONTH = _MIN_PER_DAY * 30  # 43200 (approx)


# ---- keyboards -------------------------------------------------------------


def _kb_slot_list(chat_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for s in algo_state.list_slots(chat_id):
        rows.append(
            [InlineKeyboardButton(text=s.label(), callback_data=f"algo:slot:{s.index}")]
        )
    rows.append([
        InlineKeyboardButton(text="📤 Экспорт", callback_data="algo:export"),
        InlineKeyboardButton(text="📥 Импорт", callback_data="algo:import"),
    ])
    rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data="algo:close")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_interval(minutes: float) -> str:
    if minutes <= 0:
        return "0"
    if minutes < 1:
        return f"{minutes:g}"
    if minutes == int(minutes):
        return f"{int(minutes)}"
    return f"{minutes:g}"


def _kb_slot_detail(slot: Slot) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    
    # Custom buttons for Slot 3 (улучшение)
    if slot.index == 3:
        from ...addons import state as addon_state
        auto_enabled = addon_state.get("algorithm", "auto_improvement", False)
        auto_label = "🤖 Авто: ВКЛ" if auto_enabled else "🤖 Авто: ВЫКЛ"
        rows.append([
            InlineKeyboardButton(text="➕ Добавить ссылку", callback_data="algo:link:add"),
            InlineKeyboardButton(text="🗑 Сбросить ссылки", callback_data="algo:link:clear")
        ])
        rows.append([
            InlineKeyboardButton(text=auto_label, callback_data="algo:toggle_auto:3")
        ])
    rows.append(
        [InlineKeyboardButton(
            text="🤖 Определить порядок с помощью ИИ",
            callback_data=f"algo:ai:{slot.index}",
        )]
    )
    if slot.is_empty:
        rows.append(
            [InlineKeyboardButton(
                text="✍️ Сохранить вручную",
                callback_data=f"algo:manual:{slot.index}",
            )]
        )
    else:
        rows.append(
            [InlineKeyboardButton(
                text="▶ Запустить",
                callback_data=f"algo:run:{slot.index}",
            )]
        )
        rows.append(
            [InlineKeyboardButton(
                text="✍️ Изменить вручную",
                callback_data=f"algo:manual:{slot.index}",
            )]
        )
    rows.append(
        [InlineKeyboardButton(
            text=f"⏱ Периодичность: {_format_interval(slot.interval_minutes)} мин",
            callback_data=f"algo:interval:{slot.index}",
        )]
    )
    if not slot.is_empty:
        rows.append(
            [InlineKeyboardButton(
                text="🗑 Удалить",
                callback_data=f"algo:delete:{slot.index}",
            )]
        )
    rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data="algo:back_to_list")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_draft_preview(slot_index: int) -> InlineKeyboardMarkup:
    """Buttons shown after the AI returned a draft plan (BEFORE run)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="▶ Запустить",
                callback_data=f"algo:run_draft:{slot_index}",
            )],
            [InlineKeyboardButton(
                text="💾 Сохранить",
                callback_data=f"algo:save_draft:{slot_index}",
            )],
            [InlineKeyboardButton(
                text="🔁 Перепродумать",
                callback_data=f"algo:rethink:{slot_index}",
            )],
            [InlineKeyboardButton(
                text="← Назад",
                callback_data=f"algo:back_to_slot:{slot_index}",
            )],
        ]
    )


def _kb_draft_post_run(slot_index: int) -> InlineKeyboardMarkup:
    """Buttons shown after the AI draft was executed."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🔁 Перепродумать",
                callback_data=f"algo:rethink:{slot_index}",
            )],
            [InlineKeyboardButton(
                text="💾 Сохранить",
                callback_data=f"algo:save_draft:{slot_index}",
            )],
            [InlineKeyboardButton(
                text="▶ Запустить ещё раз",
                callback_data=f"algo:run_draft:{slot_index}",
            )],
            [InlineKeyboardButton(
                text="← Назад",
                callback_data=f"algo:back_to_slot:{slot_index}",
            )],
        ]
    )


def _kb_interval_edit(slot_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🗑 Удалить периодичность",
                callback_data=f"algo:interval_clear:{slot_index}",
            )],
            [InlineKeyboardButton(
                text="← Назад",
                callback_data=f"algo:back_to_slot:{slot_index}",
            )],
        ]
    )


# ---- screen renderers ------------------------------------------------------


def _initialize_default_slots(chat_id: int) -> None:
    from .state import get_slot, save_slot
    
    # Slot 1: видео хайп
    slot1 = get_slot(chat_id, 1)
    if slot1.is_empty:
        slot1.name = "видео хайп"
        slot1.plan = (
            "1. Изучи тренды фильмов в интернете через tavily_search и выбери 1 самый популярный фильм сегодня.\n"
            "2. Скачай трейлер или хайповые сцены выбранного фильма через downloader.\n"
            "3. Отрежь лучшие моменты скачанного видео в вертикальные клипы 9:16 с субтитрами через editor.\n"
            "4. Опубликуй готовые клипы во все соцсети (YouTube Shorts, TikTok, Instagram Reels) через publisher."
        )
        save_slot(chat_id, slot1)
        
    # Slot 2: ии кодинг
    slot2 = get_slot(chat_id, 2)
    if slot2.is_empty:
        slot2.name = "ии кодинг"
        slot2.plan = (
            "1. Сделай исследование рынка и аналогов по запросу пользователя через researcher и сформируй черновик ТЗ (Лоу ТЗ).\n"
            "2. Запусти дебаты в Двери 1 между Оптимистом и Скептиком для очистки и доработки Лоу ТЗ в Супер ТЗ через debate.\n"
            "3. Передай Супер ТЗ кодеру в Мастерскую (Master Room) для автономного написания кода, тестов и деплоя.\n"
            "4. Покажи готовый сайт и превью приложения пользователю на веб-панели VISION."
        )
        save_slot(chat_id, slot2)

    # Slot 3: улучшение
    slot3 = get_slot(chat_id, 3)
    if slot3.is_empty:
        slot3.name = "улучшение"
        slot3.plan = (
            "1. Проверь новые инструкции и практики по ссылке https://vibe.sukharev.dev/knowledge-base.\n"
            "2. Запусти проверку нехватки данных, ключей или доступа к API нейросетей.\n"
            "3. Запроси ввод недостающих настроек у пользователя (вход на сайт/канал).\n"
            "4. Примени улучшения по цепочке, отслеживая статусы: внедрено / отложено / в очереди."
        )
        save_slot(chat_id, slot3)

async def _show_slot_list(message: Message) -> None:
    """Re-render the 10-slot list. Public so :mod:`bot.wizard` can call
    it directly when the user taps «🧩 Алгоритм» from Settings.

    The header text explains what the addon does and shows a few ready
    examples so the user knows what to paste into «🤖 Определить
    порядок с помощью ИИ».
    """
    _initialize_default_slots(message.chat.id)
    body = (
        "• <b>🤖 ИИ-план</b> — кидаешь новость / описание задачи, "
        "бот сам раскладывает на шаги (использует «Другое» → "
        "Brain 1 → Brain 2 в этом приоритете).\n"
        "• <b>✍️ Вручную</b> — пишешь пронумерованные шаги сам.\n\n"
        "Каждый шаг бот выполняет последовательно через свой "
        "агентский режим (видит локальные и внешние тулзы: "
        "tavily / brave / exa / firecrawl / apify / github / "
        "exec_bash / read_file / write_file).\n\n"
        "⏱ <b>Периодика</b>: в слоте есть «⏱ Периодичность» — "
        "ставишь N минут, бот сам перезапускает план каждые N мин "
        "(можно дробное: 0.5 = 30 сек; 1440 = день, 10080 = "
        "неделя, 43200 = месяц).\n\n"
        "<b>Примеры:</b>\n"
        "1️⃣ Мониторинг новостей про X каждый час: «Найди свежие "
        "новости про Tesla за последние 24 часа через tavily_search "
        "и пришли в чат краткое резюме (3 пункта).» → ⏱ 60 мин.\n\n"
        "2️⃣ GitHub-наблюдение: «Сходи github_search_code «"
        "vulnerability redis», прочитай 3 топовых результата через "
        "github_get_file и пришли краткие выводы.» → ⏱ 360 мин.\n\n"
        "3️⃣ Утренний дайджест: «Сделай brave_search «погода Москва "
        "сегодня», прочитай firecrawl_scrape https://example.com/"
        "news и собери 5 главных пунктов в одно сообщение.» → "
        "⏱ 1440 мин.\n\n"
        "Выбери слот:"
    )
    await message.answer(body, reply_markup=_kb_slot_list(message.chat.id))


async def _show_slot_detail(target: Message, chat_id: int, slot_index: int) -> None:
    slot = algo_state.get_slot(chat_id, slot_index)
    if slot.is_empty:
        body = (
            f"<b>🧩 Алгоритм · Слот {slot.index}</b>\n\n"
            "Слот пустой. Нажми «🤖 Определить порядок с помощью ИИ» "
            "и пришли текст / новость / описание задачи — бот сам "
            "разложит на шаги. Или «✍️ Сохранить вручную», если "
            "хочешь сам ввести план."
        )
    else:
        plan_preview = "\n".join(
            f"{i+1}. {ln}" for i, ln in enumerate(
                line.strip() for line in slot.plan.splitlines() if line.strip()
            )
        )
        interval = _format_interval(slot.interval_minutes)
        period_line = (
            "не повторяется" if slot.interval_minutes <= 0
            else f"раз в {interval} мин"
        )
        body = (
            f"<b>🧩 Алгоритм · Слот {slot.index}: {slot.name}</b>\n"
            f"⏱ {period_line}\n\n"
            f"<b>План:</b>\n<pre>{plan_preview}</pre>"
        )
        if slot.index == 3:
            from ...addons import state as addon_state
            links = addon_state.get("algorithm", "improvement_links", [])
            links_text = ""
            if links:
                for idx, l in enumerate(links, 1):
                    links_text += f"{idx}. <code>{l}</code>\n"
            else:
                links_text = "<i>(ссылок пока нет, нажми «➕ Добавить ссылку» чтобы наполнить)</i>\n"

            body += (
                f"\n\n<b>🔗 Накопленные каналы и источники:</b>\n{links_text}"
                "\n<b>📋 События внедрения (из источников улучшения):</b>\n"
                "• 🔌 Интеграция Unstructured — <b>Внедрено ✅</b>\n"
                "• 💾 Хранилище файлов — <b>Внедрено ✅</b>\n"
                "• 🤖 Кодер-Devin — <b>Внедрено ✅</b>\n"
                "• ⏱ Авто-постинг (Reels/Shorts/TikTok) — <b>Внедрено ✅</b>\n"
                "• 💳 Платежные шлюзы и карты — <b>В очереди ⏳</b>\n"
                "• 🖥 Серверы и деплой на Oracle — <b>В очереди ⏳</b>\n\n"
                "<i>Этот алгоритм запрашивает у человека ключи, доступы и карты, когда боту не хватает данных для работы.</i>"
            )
    await target.answer(body, reply_markup=_kb_slot_detail(slot))


def _format_steps(steps: list[str]) -> str:
    return "\n".join(f"{i+1}. {step}" for i, step in enumerate(steps))


# ---- router ---------------------------------------------------------------


def build_algorithm_router() -> Router:
    router = Router(name="algorithm_addon")

    # ---- entry from /algorithm + /algo command (handy alias) -------

    @router.message(StateFilter(None), F.text.in_({"/algorithm", "/algo"}))
    async def cmd_algorithm(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not can_use(message.from_user.id):
            await message.answer("Нет доступа.")
            return
        await state.clear()
        await _show_slot_list(message)

    # ---- entry from Settings → 🧩 Алгоритм (wizard sends algo:menu) -

    @router.callback_query(F.data == "algo:menu")
    async def cb_menu(query: CallbackQuery, state: FSMContext) -> None:
        if query.message is None:
            await query.answer()
            return
        if query.from_user is None or not can_use(query.from_user.id):
            await query.answer("Нет доступа.", show_alert=True)
            return
        await state.clear()
        await _show_slot_list(query.message)
        await query.answer()

    @router.callback_query(F.data == "algo:close")
    async def cb_close(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_reply_markup(reply_markup=None)
        await query.answer()

    @router.callback_query(F.data == "algo:back_to_list")
    async def cb_back_to_list(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if query.message is not None:
            await _show_slot_list(query.message)
        await query.answer()

    # ---- export / import --------------------------------------------
    @router.callback_query(F.data == "algo:export")
    async def cb_export(query: CallbackQuery, state: FSMContext) -> None:
        if query.message is None or query.from_user is None:
            await query.answer()
            return
        if not can_admin(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
        import json
        data = []
        for s in algo_state.list_slots(query.message.chat.id):
            if s.is_empty:
                continue
            data.append({
                "index": s.index, "name": s.name, "plan": s.plan,
                "interval_minutes": s.interval_minutes,
            })
        if not data:
            await query.answer("Нет сохранённых алгоритмов для экспорта.", show_alert=True)
            return
        blob = json.dumps({"algorithms": data}, ensure_ascii=False, indent=2).encode("utf-8")
        with contextlib.suppress(Exception):
            await query.message.answer_document(
                BufferedInputFile(blob, filename="algorithms.json"),
                caption=f"📤 Экспортировано слотов: {len(data)}")
        await query.answer()

    @router.callback_query(F.data == "algo:import")
    async def cb_import(query: CallbackQuery, state: FSMContext) -> None:
        if query.message is None or query.from_user is None:
            await query.answer()
            return
        if not can_admin(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.set_state(AlgorithmStates.awaiting_import)
        with contextlib.suppress(Exception):
            await query.message.answer(
                "📥 <b>Импорт алгоритмов</b>\n\n"
                "Пришли JSON-файл (из экспорта) или вставь JSON-текст.\n"
                "Формат: <code>{\"algorithms\":[{\"index\":1,\"name\":\"...\","
                "\"plan\":\"...\",\"interval_minutes\":0}]}</code>\n\n"
                "Слоты 1..10; повреждённые элементы пропущу, базу не сотру.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ Отмена", callback_data="algo:back_to_list")]]))
        await query.answer()

    @router.message(StateFilter(AlgorithmStates.awaiting_import), F.document | F.text)
    async def msg_import(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not can_admin(message.from_user.id):
            return
        import html
        import json
        await state.clear()
        # 1) Read raw JSON from a document or the message text.
        if message.document:
            try:
                import io
                buf = io.BytesIO()
                await message.bot.download(message.document, destination=buf)
                raw = buf.getvalue().decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                await message.answer(f"⚠️ Не смог прочитать файл: {html.escape(str(exc))}")
                return
        else:
            raw = message.text or ""
        raw = raw.strip()
        # 2) Parse, salvaging a JSON block if wrapped in prose/fences.
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            import re as _re
            m = _re.search(r"[\[{].*[\]}]", raw, _re.DOTALL)
            if not m:
                await message.answer("⚠️ Не нашёл корректный JSON. Импорт отменён.")
                return
            try:
                data = json.loads(m.group(0))
            except Exception as exc:  # noqa: BLE001
                await message.answer(f"⚠️ Неверный JSON: {html.escape(str(exc))}. Импорт отменён.")
                return
        # 3) Accept {"algorithms":[...]} or a bare list.
        items = data.get("algorithms") if isinstance(data, dict) else data
        if not isinstance(items, list):
            await message.answer("⚠️ Ожидался список алгоритмов. Импорт отменён.")
            return
        # 4) Validate + save each slot; skip broken ones (never wipe the base).
        ok = skipped = 0
        for it in items:
            if not isinstance(it, dict):
                skipped += 1
                continue
            try:
                idx = int(it.get("index", 0))
            except (TypeError, ValueError):
                skipped += 1
                continue
            name = str(it.get("name", "")).strip()
            plan = str(it.get("plan", "")).strip()
            try:
                interval = float(it.get("interval_minutes", 0) or 0)
            except (TypeError, ValueError):
                interval = 0.0
            if not (1 <= idx <= 10) or not plan:
                skipped += 1
                continue
            algo_state.save_slot(message.chat.id, Slot(
                index=idx, name=name, plan=plan,
                interval_minutes=max(0.0, interval)))
            ok += 1
        with contextlib.suppress(Exception):
            await _show_slot_list(message)
        await message.answer(f"✅ Импорт завершён. Восстановлено: {ok}, пропущено: {skipped}.")

    # ---- slot detail ------------------------------------------------

    @router.callback_query(F.data.startswith("algo:slot:"))
    async def cb_slot_detail(query: CallbackQuery, state: FSMContext) -> None:
        if query.message is None or query.from_user is None:
            await query.answer()
            return
        if not can_admin(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        slot_index = int(query.data.split(":")[2])
        await _show_slot_detail(query.message, query.message.chat.id, slot_index)
        await query.answer()

    @router.callback_query(F.data == "algo:toggle_auto:3")
    async def cb_toggle_auto_3(query: CallbackQuery) -> None:
        if query.message is None or query.from_user is None:
            await query.answer()
            return
        if not can_admin(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
            
        from ...addons import state as addon_state
        current = addon_state.get("algorithm", "auto_improvement", False)
        addon_state.set_("algorithm", "auto_improvement", not current)
        
        await query.answer(f"Авто-режим: {'ВКЛ' if not current else 'ВЫКЛ'}")
        await _show_slot_detail(query.message, query.message.chat.id, 3)

    @router.callback_query(F.data.startswith("algo:back_to_slot:"))
    async def cb_back_to_slot(query: CallbackQuery, state: FSMContext) -> None:
        if query.message is None:
            await query.answer()
            return
        await state.clear()
        slot_index = int(query.data.split(":")[2])
        await _show_slot_detail(query.message, query.message.chat.id, slot_index)
        await query.answer()

    @router.callback_query(F.data == "algo:link:add")
    async def cb_link_add(query: CallbackQuery, state: FSMContext) -> None:
        if query.message is None or query.from_user is None:
            await query.answer()
            return
        if not can_admin(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
            
        await state.set_state(AlgorithmStates.awaiting_link)
        await query.answer()
        await query.message.answer(
            "<b>🔗 Добавление источника для улучшения</b>\n\n"
            "Пришли ссылку на твой личный канал, публичный канал или сайт (например, <code>https://t.me/my_channel</code> или <code>https://vibe.sukharev.dev/knowledge-base</code>):\n\n"
            "Бот будет использовать эти ссылки для сбора новых инструкций, ключей и карт.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Отмена", callback_data="algo:back_to_slot:3")]
            ])
        )

    @router.callback_query(F.data == "algo:link:clear")
    async def cb_link_clear(query: CallbackQuery) -> None:
        if query.message is None or query.from_user is None:
            await query.answer()
            return
        if not can_admin(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
            
        from ...addons import state as addon_state
        addon_state.set_("algorithm", "improvement_links", [])
        await query.answer("Список накопленных ссылок очищен", show_alert=True)
        await _show_slot_detail(query.message, query.message.chat.id, 3)

    @router.message(StateFilter(AlgorithmStates.awaiting_link))
    async def msg_link_submit(message: Message, state: FSMContext) -> None:
        link = (message.text or "").strip()
        if not link or not (link.startswith("http://") or link.startswith("https://") or link.startswith("@") or "t.me/" in link):
            await message.answer("❌ Неверный формат ссылки. Попробуй еще раз или нажми «↩️ Отмена»:")
            return
            
        await state.clear()
        
        from ...addons import state as addon_state
        links = addon_state.get("algorithm", "improvement_links", [])
        if not isinstance(links, list):
            links = []
        links.append(link)
        addon_state.set_("algorithm", "improvement_links", links)
        
        await message.answer(f"✅ Ссылка <code>{link}</code> успешно добавлена в Хранилище улучшений!")
        await _show_slot_detail(message, message.chat.id, 3)

    # ---- AI flow ----------------------------------------------------

    @router.callback_query(F.data.startswith("algo:ai:"))
    async def cb_ai(query: CallbackQuery, state: FSMContext) -> None:
        if query.message is None or query.from_user is None:
            await query.answer()
            return
        if not can_admin(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
        slot_index = int(query.data.split(":")[2])
        # Pre-flight: if no brain is configured, tell the user *now*
        # — otherwise they'll type a long prompt and only then see the
        # «нет настроенного мозга» error (which they reported as «ИИ
        # не схватывает мои приказы»).
        try:
            from . import planner as _pl

            if not _pl._planner_chain():
                await query.answer(
                    "Сначала настрой мозг: /setup → 🧠 Мозг.",
                    show_alert=True,
                )
                return
        except Exception:  # noqa: BLE001 — never block the flow on diagnostics
            pass
        await state.set_state(AlgorithmStates.awaiting_ai_input)
        await state.update_data(slot_index=slot_index)
        await query.message.answer(
            f"Слот <b>{slot_index}</b>. Пришли мне <b>одно сообщение</b> с "
            "описанием задачи — что должен сделать бот. Я разложу её на "
            "пошаговый план и покажу тебе, перед тем как выполнять.\n\n"
            "Примеры приказов:\n"
            "• <code>зайди на example.com, сохрани заголовок и пришли мне</code>\n"
            "• <code>проверь почту и расскажи про новые письма</code>\n"
            "• <code>сделай красивый пост про осень в стиле Маяковского</code>\n\n"
            "После того как пришлёшь текст — увидишь план и кнопки "
            "«▶ Запустить» / «💾 Сохранить» / «🔁 Перепродумать»."
        )
        await query.answer()

    @router.message(StateFilter(AlgorithmStates.awaiting_ai_input), F.text)
    async def on_ai_input(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        slot_index = int(data.get("slot_index", 0) or 0)
        if slot_index < 1:
            await state.clear()
            return
        news = message.text or ""
        await message.answer("🤖 Планирую…")
        try:
            plan = await planner.plan_from_text(news)
        except planner.NoBrainAvailable as exc:
            await state.clear()
            await message.answer(
                f"❌ {exc}\n\n"
                "Зайди в <b>⚙️ Настройки → 🧠 Соображалка</b> или "
                "<b>/setup → 🧠 Мозг</b> и пропиши API-ключ хотя бы "
                "для одного слота — без него ИИ ничего не разложит."
            )
            return
        if not plan.steps:
            # The brain answered but we couldn't extract any steps.
            # Show the raw reply so the user can see what happened and
            # tweak their prompt instead of being stuck on «ИИ не
            # схватывает мои приказы».
            preview = (plan.raw or "")[:600] or "(пусто)"
            await state.clear()
            await message.answer(
                "❌ ИИ ответил, но я не смог достать из ответа шаги.\n\n"
                f"<b>Мозг:</b> <code>{plan.used_brain}</code> "
                f"(<code>{plan.used_model}</code>)\n"
                f"<b>Ответ:</b>\n<pre>{preview}</pre>\n\n"
                "Попробуй переформулировать запрос — короче, "
                "конкретнее. Например: «зайди в гугл, найди X, "
                "пришли первый результат»."
            )
            return
        # Stash draft in FSM data so save/run/rethink can find it.
        await state.update_data(
            draft_name=plan.name,
            draft_steps=plan.steps,
            draft_input=news,
        )
        # We keep awaiting_ai_input cleared here — the user's next
        # action is a button-click, not a typed message. Clearing also
        # frees pretty_text from getting throttled by state filter.
        await state.set_state(None)
        await message.answer(
            (
                f"<b>Планировал:</b> <code>{plan.used_brain}</code> "
                f"(<code>{plan.used_model}</code>)\n"
                f"<b>Имя слота:</b> <code>{plan.name}</code>\n\n"
                f"<b>План:</b>\n<pre>{_format_steps(plan.steps) or '(пусто)'}</pre>\n\n"
                "Нажми <b>▶ Запустить</b> чтобы выполнить план, "
                "<b>💾 Сохранить</b> — чтобы оставить в слоте на потом, "
                "<b>🔁 Перепродумать</b> — если хочешь другой вариант."
            ),
            reply_markup=_kb_draft_preview(slot_index),
        )

    @router.callback_query(F.data.startswith("algo:rethink:"))
    async def cb_rethink(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if query.message is None or query.from_user is None:
            await query.answer()
            return
        if not can_admin(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
        slot_index = int(query.data.split(":")[2])
        data = await state.get_data()
        news = data.get("draft_input", "")
        if not news:
            await query.answer(
                "Исходный текст потерян — открой слот заново и пришли ещё раз.",
                show_alert=True,
            )
            return
        await query.message.answer("🔁 Планирую заново…")
        try:
            plan = await planner.plan_from_text(news)
        except planner.NoBrainAvailable as exc:
            await query.message.answer(f"❌ {exc}")
            await query.answer()
            return
        await state.update_data(draft_name=plan.name, draft_steps=plan.steps)
        await query.message.answer(
            (
                f"<b>Планировал:</b> <code>{plan.used_brain}</code> "
                f"(<code>{plan.used_model}</code>)\n"
                f"<b>Имя слота:</b> <code>{plan.name}</code>\n\n"
                f"<b>План:</b>\n<pre>{_format_steps(plan.steps) or '(пусто)'}</pre>"
            ),
            reply_markup=_kb_draft_preview(slot_index),
        )
        await query.answer()

    @router.callback_query(F.data.startswith("algo:save_draft:"))
    async def cb_save_draft(query: CallbackQuery, state: FSMContext) -> None:
        if query.message is None or query.from_user is None:
            await query.answer()
            return
        if not can_admin(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
        slot_index = int(query.data.split(":")[2])
        data = await state.get_data()
        steps = data.get("draft_steps") or []
        raw_name = data.get("draft_name", "") or ""
        if not steps:
            await query.answer("Нечего сохранять.", show_alert=True)
            return
        chat_id = query.message.chat.id
        slot = algo_state.get_slot(chat_id, slot_index)
        slot.name = algo_state.derive_unique_name(
            chat_id, raw_name, exclude_index=slot_index
        )
        slot.plan = "\n".join(steps)
        algo_state.save_slot(chat_id, slot)
        await state.clear()
        await query.message.answer(
            f"💾 Сохранено в слот <b>{slot_index}: {slot.name}</b>."
        )
        await _show_slot_detail(query.message, chat_id, slot_index)
        await query.answer()

    @router.callback_query(F.data.startswith("algo:run_draft:"))
    async def cb_run_draft(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if query.message is None or query.from_user is None:
            await query.answer()
            return
        if not can_admin(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
        slot_index = int(query.data.split(":")[2])
        data = await state.get_data()
        steps = data.get("draft_steps") or []
        if not steps:
            await query.answer("Нечего запускать.", show_alert=True)
            return
        chat_id = query.message.chat.id
        await query.answer()

        async def _status(text: str) -> None:
            with contextlib.suppress(Exception):
                await bot.send_message(chat_id, text)

        await executor.run_plan(
            chat_id=chat_id,
            user_id=query.from_user.id,
            slot_index=slot_index,
            status=_status,
            plan_steps=list(steps),
        )
        await query.message.answer(
            "Готово. Хочешь что-нибудь поменять?",
            reply_markup=_kb_draft_post_run(slot_index),
        )

    # ---- manual save ------------------------------------------------

    @router.callback_query(F.data.startswith("algo:manual:"))
    async def cb_manual(query: CallbackQuery, state: FSMContext) -> None:
        if query.message is None or query.from_user is None:
            await query.answer()
            return
        if not can_admin(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
        slot_index = int(query.data.split(":")[2])
        await state.set_state(AlgorithmStates.awaiting_manual_plan)
        await state.update_data(slot_index=slot_index)
        await query.message.answer(
            (
                f"Слот <b>{slot_index}</b>. Пришли план одним сообщением.\n\n"
                "Можно нумерованным списком (1. ..., 2. ..., 3. ...) или "
                "просто по строкам — я разобью их на шаги. Первая строка "
                "будет именем слота, если она короткая."
            )
        )
        await query.answer()

    @router.message(StateFilter(AlgorithmStates.awaiting_manual_plan), F.text)
    async def on_manual_plan(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        slot_index = int(data.get("slot_index", 0) or 0)
        if slot_index < 1:
            await state.clear()
            return
        text = (message.text or "").strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            await message.answer("Пустой план — попробуй ещё раз.")
            return
        # Derive a name: if the first line is short (<=4 words and <=40 chars),
        # use it as the name and the rest as the plan. Otherwise pick the
        # first two words of the first line as the name and keep the
        # full text as the plan.
        first = lines[0]
        words = first.split()
        if len(words) <= 4 and len(first) <= 40 and len(lines) > 1:
            raw_name = first.lower()
            plan_lines = lines[1:]
        else:
            raw_name = " ".join(words[:2]).lower() if words else "без имени"
            plan_lines = lines
        chat_id = message.chat.id
        slot = algo_state.get_slot(chat_id, slot_index)
        slot.name = algo_state.derive_unique_name(
            chat_id, raw_name, exclude_index=slot_index
        )
        slot.plan = "\n".join(plan_lines)
        algo_state.save_slot(chat_id, slot)
        await state.clear()
        await message.answer(
            f"💾 Сохранено в слот <b>{slot_index}: {slot.name}</b>."
        )
        await _show_slot_detail(message, chat_id, slot_index)

    # ---- run saved slot ---------------------------------------------

    @router.callback_query(F.data.startswith("algo:run:"))
    async def cb_run(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if query.message is None or query.from_user is None:
            await query.answer()
            return
        if not can_admin(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
        slot_index = int(query.data.split(":")[2])
        chat_id = query.message.chat.id
        await query.answer()

        async def _status(text: str) -> None:
            with contextlib.suppress(Exception):
                await bot.send_message(chat_id, text)

        await executor.run_plan(
            chat_id=chat_id,
            user_id=query.from_user.id,
            slot_index=slot_index,
            status=_status,
        )
        await _show_slot_detail(query.message, chat_id, slot_index)

    # ---- delete -----------------------------------------------------

    @router.callback_query(F.data.startswith("algo:delete:"))
    async def cb_delete(query: CallbackQuery, state: FSMContext) -> None:
        if query.message is None or query.from_user is None:
            await query.answer()
            return
        if not can_admin(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
        slot_index = int(query.data.split(":")[2])
        chat_id = query.message.chat.id
        algo_state.clear_slot(chat_id, slot_index)
        await query.message.answer(f"🗑 Слот {slot_index} очищен.")
        await _show_slot_detail(query.message, chat_id, slot_index)
        await query.answer()

    # ---- interval ---------------------------------------------------

    @router.callback_query(F.data.startswith("algo:interval:"))
    async def cb_interval(query: CallbackQuery, state: FSMContext) -> None:
        if query.message is None or query.from_user is None:
            await query.answer()
            return
        if not can_admin(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
        slot_index = int(query.data.split(":")[2])
        await state.set_state(AlgorithmStates.awaiting_interval)
        await state.update_data(slot_index=slot_index)
        await query.message.answer(
            (
                f"Слот <b>{slot_index}</b>. Пришли число — сколько минут "
                "между повторами. Дробные ок (например <code>0.5</code> = "
                "30 секунд). <code>0</code> = не повторять.\n\n"
                "Подсказка:\n"
                f"• <code>{_MIN_PER_DAY}</code> = 1 день\n"
                f"• <code>{_MIN_PER_WEEK}</code> = 1 неделя\n"
                f"• <code>{_MIN_PER_MONTH}</code> = ~1 месяц"
            ),
            reply_markup=_kb_interval_edit(slot_index),
        )
        await query.answer()

    @router.message(StateFilter(AlgorithmStates.awaiting_interval), F.text)
    async def on_interval(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        slot_index = int(data.get("slot_index", 0) or 0)
        if slot_index < 1:
            await state.clear()
            return
        raw = (message.text or "").strip().replace(",", ".")
        try:
            minutes = float(raw)
        except ValueError:
            await message.answer(
                "Не понял число. Пришли только число, например "
                "<code>5</code> или <code>0.5</code>."
            )
            return
        if minutes < 0 or math.isnan(minutes) or math.isinf(minutes):
            await message.answer("Число должно быть >= 0.")
            return
        chat_id = message.chat.id
        slot = algo_state.get_slot(chat_id, slot_index)
        slot.interval_minutes = float(minutes)
        algo_state.save_slot(chat_id, slot)
        await state.clear()
        if minutes <= 0:
            await message.answer("🗑 Периодичность сброшена (не повторяется).")
        else:
            await message.answer(
                f"⏱ Периодичность установлена: каждые "
                f"<b>{_format_interval(minutes)}</b> мин."
            )
        await _show_slot_detail(message, chat_id, slot_index)

    @router.callback_query(F.data.startswith("algo:interval_clear:"))
    async def cb_interval_clear(query: CallbackQuery, state: FSMContext) -> None:
        if query.message is None or query.from_user is None:
            await query.answer()
            return
        if not can_admin(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
        slot_index = int(query.data.split(":")[2])
        chat_id = query.message.chat.id
        slot = algo_state.get_slot(chat_id, slot_index)
        slot.interval_minutes = 0.0
        algo_state.save_slot(chat_id, slot)
        await state.clear()
        if query.message is not None:
            await query.message.answer("🗑 Периодичность сброшена.")
            await _show_slot_detail(query.message, chat_id, slot_index)
        await query.answer()

    return router


# Re-export so :mod:`bot.wizard` can call this without importing the
# router builder (avoids a circular import).
show_screen = _show_slot_list
