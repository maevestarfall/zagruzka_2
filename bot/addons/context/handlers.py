# Context addon handlers: model context-test, zip compare, paged big-ТЗ with real timers.
# Co-authored with CoCo
"""«🧠 Контекст» — measure a model's real memory, compare zips, and run a big
ТЗ in context-sized chunks paced by a real timer.

* 🧪 Тест контекста — pick a brain, set a dynamic method (e.g. «перескажи
  точно 4000 знаков короткими словами»), feed escalating marked text, see
  what the model dropped + message count, save the «clay» (capacity) per
  model+API to the Vault/state.
* 📦 Сравнить zip — upload a zip, compare against the current project: what's
  present / missing / changed.
* 🧩 Разгрузка (paging) — split a big ТЗ by the measured chunk size, run a
  chunk now, then a real timer fires the continuation prompt for the next.
"""

from __future__ import annotations

import contextlib
import io
import logging
import time
import zipfile

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import store

logger = logging.getLogger(__name__)

TEST_SIZES = (1000, 2000, 4000, 8000, 16000)  # chars of marked material


class CtxStates(StatesGroup):
    awaiting_method = State()
    awaiting_zip = State()
    awaiting_paging_tz = State()


def _esc(s) -> str:
    import html
    return html.escape(str(s or ""))


async def _owner(query: CallbackQuery) -> bool:
    from ...wizard import _is_owner
    return _is_owner(query.from_user.id if query.from_user else 0)


# ─── zip-from-storage comparison (ADDED; does NOT touch ctx:zip) ───────────────
_CMP_ANCHORS = {"bot", "data", "services", "schemas", "scripts", "deploy", "tests", "docs"}


def _cmp_norm(path: str) -> str:
    """Normalise a path to start at a known project anchor so archives with
    different top folders (lilush-bot/…, bot/…) compare on equal footing."""
    parts = path.split("/")
    for i, p in enumerate(parts):
        if p in _CMP_ANCHORS:
            return "/".join(parts[i:])
    return parts[-1] if parts else path


def _storage_zips():
    from ...config import DATA_DIR
    d = DATA_DIR / "storage"
    if not d.exists():
        return []
    return sorted((p for p in d.rglob("*.zip") if p.is_file()), key=lambda p: p.name)


def _zip_map(path) -> dict:
    out: dict = {}
    with zipfile.ZipFile(path) as zf:
        for n in zf.namelist():
            if n.endswith("/") or "__pycache__" in n:
                continue
            out[_cmp_norm(n)] = zf.read(n)
    return out


def _running_map() -> dict:
    import os
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    out: dict = {}
    for base, _dirs, files in os.walk(repo):
        if "__pycache__" in base or "/.git" in base:
            continue
        for fn in files:
            full = os.path.join(base, fn)
            rel = os.path.relpath(full, repo).replace(os.sep, "/")
            if "__pycache__" in rel or rel.startswith(".git"):
                continue
            with contextlib.suppress(Exception):
                with open(full, "rb") as fh:
                    out[_cmp_norm(rel)] = fh.read()
    return out


def _func_names(data: bytes) -> set:
    import ast
    try:
        tree = ast.parse(data.decode("utf-8", "replace"))
    except Exception:
        return set()
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def _build_store_diff(base: dict, tgt: dict, base_name: str, tgt_name: str) -> str:
    ka, kb = set(base), set(tgt)
    only_a = sorted(ka - kb)   # только в базе
    only_b = sorted(kb - ka)   # только в цели
    changed = sorted(k for k in (ka & kb) if base[k] != tgt[k])
    fn_a, fn_b = [], []
    for k in changed:
        if not k.endswith(".py"):
            continue
        fa, fb = _func_names(base[k]), _func_names(tgt[k])
        fn_a += [f"{k} :: {x}" for x in sorted(fa - fb)]
        fn_b += [f"{k} :: {x}" for x in sorted(fb - fa)]

    def _lst(items, n=15):
        s = "\n".join(f"• <code>{_esc(x)}</code>" for x in items[:n])
        if len(items) > n:
            s += f"\n… ещё {len(items) - n}"
        return s or "—"

    return "\n".join([
        "<b>📦 Сравнение (двустороннее)</b>",
        f"База: <code>{_esc(base_name)}</code> ({len(base)} файлов)",
        f"Цель: <code>{_esc(tgt_name)}</code> ({len(tgt)} файлов)",
        "",
        f"🟩 Только в БАЗЕ: {len(only_a)}", _lst(only_a),
        "",
        f"🟦 Только в ЦЕЛИ: {len(only_b)}", _lst(only_b),
        "",
        f"✏️ Изменённых файлов: {len(changed)}", _lst(changed),
        "",
        f"🧬 Функции: есть в базе, нет в цели: {len(fn_a)}", _lst(fn_a),
        "",
        f"🧬 Функции: есть в цели, нет в базе: {len(fn_b)}", _lst(fn_b),
    ])


# ─── Keyboards ────────────────────────────────────────────────────────────────

def menu_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🧠 Контекст", callback_data="ctx:home")


def _kb_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧪 Тест контекста модели", callback_data="ctx:test")],
        [InlineKeyboardButton(text="📦 Сравнить zip с версией", callback_data="ctx:zip")],
        [InlineKeyboardButton(text="📦 Сравнить с zip из хранилища", callback_data="ctx:zipstore")],
        [InlineKeyboardButton(text="🧬 Сравнить функции версий", callback_data="versiff:home")],
        [InlineKeyboardButton(text="🧩 Разгрузка большого ТЗ (paging)", callback_data="ctx:page")],
        [InlineKeyboardButton(text="📊 Клейма моделей", callback_data="ctx:clay")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="main:settings")],
    ])


def _kb_brains() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=b["label"], callback_data=f"ctx:test:slot:{b['slot']}")]
            for b in store.list_brains()]
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="ctx:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─── Test engine ──────────────────────────────────────────────────────────────

def _marked_text(n_chars: int) -> tuple[str, list[str]]:
    """Build ~n_chars of numbered unique facts; return (text, codewords)."""
    import random
    lines, codes = [], []
    i = 0
    while len("\n".join(lines)) < n_chars:
        i += 1
        code = f"ZQX{i:03d}{random.randint(100, 999)}"
        codes.append(code)
        lines.append(f"Факт {i}: кодовое слово {code} — короткая фраза для проверки памяти.")
    return "\n".join(lines), codes


async def _run_test(method: str, model_label: str, on_status) -> dict:
    """Feed escalating marked text, measure recall, return capacity dict."""
    from .._llm import chat_once
    results = []
    boundary = None
    for size in TEST_SIZES:
        text, codes = _marked_text(size)
        system = (
            "Ты проходишь тест памяти/контекста. Выполни ТОЧНО инструкцию "
            "пользователя по приложенному тексту. " + (method or
            "Перескажи текст максимально полно, сохранив все кодовые слова.")
        )
        try:
            out = await chat_once(system, text, max_tokens=2000, temperature=0.0)
        except Exception as exc:  # noqa: BLE001
            results.append({"size": size, "error": str(exc)})
            break
        kept = sum(1 for c in codes if c in (out or ""))
        recall = kept / max(len(codes), 1)
        results.append({"size": size, "facts": len(codes), "kept": kept,
                        "recall": round(recall, 2)})
        with contextlib.suppress(Exception):
            await on_status(size, recall)
        if recall < 0.85 and boundary is None:
            boundary = size
            break
    cap = boundary or TEST_SIZES[-1]
    chunk = max(800, int(cap * 0.8))
    return {"model": model_label, "boundary_chars": cap, "chunk_chars": chunk,
            "method": method[:200], "runs": results}


def build_context_router() -> Router:
    router = Router(name="context")

    @router.callback_query(F.data == "ctx:home")
    async def _home(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>🧠 Контекст</b>\n\nИнструменты памяти модели и больших ТЗ.",
                parse_mode="HTML", reply_markup=_kb_home())

    # ── Clay overview ──
    @router.callback_query(F.data == "ctx:clay")
    async def _clay(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        await query.answer()
        clay = store.all_clay()
        if not clay:
            body = "Пока нет клейм. Прогони 🧪 Тест контекста."
        else:
            lines = ["<b>📊 Клейма моделей</b>", ""]
            for mk, d in clay.items():
                lines.append(f"• <code>{_esc(mk)}</code>: ~{d.get('boundary_chars')} симв., "
                             f"кусок {d.get('chunk_chars')}")
            body = "\n".join(lines)
        with contextlib.suppress(Exception):
            await query.message.edit_text(body, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ Назад", callback_data="ctx:home")]]))

    # ── Test ──
    @router.callback_query(F.data == "ctx:test")
    async def _test(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>🧪 Тест контекста</b>\n\nВыбери мозг для проверки:",
                parse_mode="HTML", reply_markup=_kb_brains())

    @router.callback_query(F.data.startswith("ctx:test:slot:"))
    async def _test_slot(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        slot = query.data.split(":")[-1]
        await state.update_data(ctx_slot=slot)
        await state.set_state(CtxStates.awaiting_method)
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>🧪 Метод теста</b>\n\nПришли <b>инструкцию</b> теста "
                "(динамическую). Напр.: <i>«Перескажи В ТОЧНОСТИ, сохранив все "
                "кодовые слова, короткими фразами»</i>.\n\nЯ прогоню её на "
                "растущих объёмах текста и покажу, где модель начинает терять "
                "информацию.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отмена", callback_data="ctx:home")]]))

    @router.message(CtxStates.awaiting_method, F.text)
    async def _test_run(message: Message, state: FSMContext) -> None:
        from ...wizard import _is_owner
        if not _is_owner(message.from_user.id if message.from_user else 0):
            return
        data = await state.get_data()
        slot = data.get("ctx_slot", "1")
        method = (message.text or "").strip()
        await state.clear()
        from ...storage import storage
        prev = storage.get_active_brain_slot()
        prog = await message.answer("🧪 Гоняю тест памяти…")
        msgs = []

        async def on_status(size, recall):
            msgs.append(f"• {size} симв → запомнил {int(recall * 100)}%")
            with contextlib.suppress(Exception):
                await prog.edit_text("🧪 Тест…\n" + "\n".join(msgs))

        try:
            with contextlib.suppress(Exception):
                storage.set_active_brain_slot(slot)
            model_label = store.current_model_key()
            res = await _run_test(method, model_label, on_status)
        finally:
            with contextlib.suppress(Exception):
                storage.set_active_brain_slot(prev)
        store.save_clay(model_label, res)
        with contextlib.suppress(Exception):
            await prog.delete()
        body = (
            f"✅ <b>Клеймо сохранено</b> для <code>{_esc(model_label)}</code>\n\n"
            f"Граница памяти: ~<b>{res['boundary_chars']}</b> симв.\n"
            f"Рекоменд. размер куска ТЗ: <b>{res['chunk_chars']}</b> симв.\n"
            f"Сообщений в прогоне: {len(res['runs'])}\n\n"
            + "\n".join(f"• {r.get('size')} симв → {int(r.get('recall', 0) * 100)}% "
                        f"({r.get('kept')}/{r.get('facts')})"
                        for r in res["runs"] if "recall" in r)
        )
        await message.answer(body, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🧠 Контекст", callback_data="ctx:home")]]))

    # ── Zip compare ──
    @router.callback_query(F.data == "ctx:zip")
    async def _zip(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        await state.set_state(CtxStates.awaiting_zip)
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>📦 Сравнить zip с версией</b>\n\nПришли <b>.zip</b> — сравню "
                "с текущим кодом: что на месте, чего не хватает, что изменилось.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отмена", callback_data="ctx:home")]]))

    @router.message(CtxStates.awaiting_zip, F.document)
    async def _zip_run(message: Message, state: FSMContext) -> None:
        from ...wizard import _is_owner
        if not _is_owner(message.from_user.id if message.from_user else 0):
            return
        doc = message.document
        if not (doc.file_name or "").lower().endswith(".zip"):
            await message.answer("Нужен .zip файл.")
            return
        await state.clear()
        prog = await message.answer("📦 Сравниваю с текущей версией…")
        try:
            buf = io.BytesIO()
            await message.bot.download(doc.file_id, destination=buf)
            zf = zipfile.ZipFile(buf)
            entries = {n for n in zf.namelist() if not n.endswith("/")
                       and "__pycache__" not in n}
        except Exception as exc:  # noqa: BLE001
            await prog.edit_text(f"❌ Не смог открыть zip: {exc}")
            return
        import os
        from ...config import DATA_DIR  # noqa: F401  (ensure config importable)
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        # Strip single top-level dir from the zip (like our archives).
        tops = {e.split("/")[0] for e in entries}
        root = (tops.pop() + "/") if len(tops) == 1 else ""
        present = changed = missing = 0
        missing_list = []
        for e in sorted(entries):
            rel = e[len(root):] if root and e.startswith(root) else e
            if not rel:
                continue
            local = os.path.join(repo, rel)
            if os.path.exists(local):
                present += 1
                with contextlib.suppress(Exception):
                    with open(local, "rb") as _local_fh:
                        _local_data = _local_fh.read()
                    if zf.read(e) != _local_data:
                        changed += 1
            else:
                missing += 1
                if len(missing_list) < 25:
                    missing_list.append(rel)
        with contextlib.suppress(Exception):
            await prog.delete()
        body = [
            "<b>📦 Сравнение с текущей версией</b>", "",
            f"Файлов в zip: {len(entries)}",
            f"✅ На месте: {present}",
            f"✏️ Отличаются: {changed}",
            f"❌ Нет в текущей: {missing}",
        ]
        if missing_list:
            body.append("\nОтсутствуют (первые):")
            body += [f"• <code>{_esc(m)}</code>" for m in missing_list]
        await message.answer("\n".join(body), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🧠 Контекст", callback_data="ctx:home")]]))

    # ── Paging (big ТЗ in chunks with a real timer) ──
    @router.callback_query(F.data == "ctx:page")
    async def _page(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        await state.set_state(CtxStates.awaiting_paging_tz)
        await query.answer()
        chunk = store.recommended_chunk_chars()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>🧩 Разгрузка большого ТЗ</b>\n\nПришли большое ТЗ текстом. "
                f"Я раздроблю его на куски по ~{chunk} симв (из клейма модели), "
                "выполню первый кусок и поставлю <b>таймер</b> на продолжение "
                "следующего — память между кусками сохраняю.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отмена", callback_data="ctx:home")]]))

    @router.message(CtxStates.awaiting_paging_tz, F.text)
    async def _page_run(message: Message, state: FSMContext) -> None:
        from ...wizard import _is_owner
        if not _is_owner(message.from_user.id if message.from_user else 0):
            return
        await state.clear()
        tz = (message.text or "").strip()
        chunk = store.recommended_chunk_chars()
        chunks = [tz[i:i + chunk] for i in range(0, len(tz), chunk)] or [tz]
        tid = store.new_task(tz, chunks)
        await message.answer(
            f"🧩 ТЗ разбито на <b>{len(chunks)}</b> куск(ов) по ~{chunk} симв.\n"
            "Выполняю первый, дальше — по таймеру.", parse_mode="HTML")
        await _run_next_chunk(message.bot, tid, message.chat.id)

    # ─── Сравнить с zip из хранилища (ДОБАВЛЕНО; ctx:zip не тронут) ───
    def _kb_pick(names, prefix, back):
        rows = [[InlineKeyboardButton(text=f"📦 {n}", callback_data=f"{prefix}{i}")]
                for i, n in enumerate(names[:20])]
        rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data=back)])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @router.callback_query(F.data == "ctx:zipstore")
    async def _zs_home(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        zips = _storage_zips()
        names = [p.name for p in zips]
        await state.update_data(zs_list=names)
        await query.answer()
        if not names:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    "📦 В хранилище нет .zip.\nЗалей проект: Мастер Рум → 💾 Хранилище.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="↩️ Назад", callback_data="ctx:home")]]))
            return
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "Выбери <b>базовый</b> zip (с чем сравнивать):", parse_mode="HTML",
                reply_markup=_kb_pick(names, "ctx:zs:b:", "ctx:home"))

    @router.callback_query(F.data.startswith("ctx:zs:b:"))
    async def _zs_base(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        data = await state.get_data()
        names = data.get("zs_list") or []
        i = int(query.data.rsplit(":", 1)[-1])
        if i >= len(names):
            await query.answer("Список устарел, открой заново.", show_alert=True)
            return
        await state.update_data(zs_base=names[i])
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                f"База: <code>{_esc(names[i])}</code>\n\nС чем сравнить?", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🤖 С текущей версией бота", callback_data="ctx:zs:tin")],
                    [InlineKeyboardButton(text="📦 С другим zip из хранилища", callback_data="ctx:zs:tz")],
                    [InlineKeyboardButton(text="↩️ Назад", callback_data="ctx:zipstore")]]))

    async def _run_store_diff(query, state, tgt_kind, tgt_idx=None):
        data = await state.get_data()
        names = data.get("zs_list") or []
        base = data.get("zs_base")
        by_name = {p.name: p for p in _storage_zips()}
        if not base or base not in by_name:
            await query.answer("База не найдена — открой заново.", show_alert=True)
            return
        prog = await query.message.answer("📦 Сравниваю…")
        try:
            base_map = _zip_map(by_name[base])
            if tgt_kind == "internal":
                tgt_map, tgt_name = _running_map(), "текущая версия бота"
            else:
                if tgt_idx is None or tgt_idx >= len(names) or names[tgt_idx] not in by_name:
                    with contextlib.suppress(Exception):
                        await prog.edit_text("Список устарел, открой заново.")
                    return
                tgt_name = names[tgt_idx]
                tgt_map = _zip_map(by_name[tgt_name])
            summary = _build_store_diff(base_map, tgt_map, base, tgt_name)
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await prog.edit_text(f"❌ Ошибка сравнения: {_esc(str(exc))}")
            return
        await state.update_data(zs_diff=summary[:6000])
        with contextlib.suppress(Exception):
            await prog.delete()
        await query.message.answer(
            summary[:3800], parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🧠 ИИ-проверка разницы", callback_data="ctx:zs:ai")],
                [InlineKeyboardButton(text="↩️ Контекст", callback_data="ctx:home")]]))

    @router.callback_query(F.data == "ctx:zs:tin")
    async def _zs_tin(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        await query.answer("Сравниваю с текущей…")
        await _run_store_diff(query, state, "internal")

    @router.callback_query(F.data == "ctx:zs:tz")
    async def _zs_tz(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        data = await state.get_data()
        names = data.get("zs_list") or []
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "Выбери <b>второй</b> zip (цель):", parse_mode="HTML",
                reply_markup=_kb_pick(names, "ctx:zs:t:", "ctx:zipstore"))

    @router.callback_query(F.data.startswith("ctx:zs:t:"))
    async def _zs_t(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        i = int(query.data.rsplit(":", 1)[-1])
        await query.answer("Сравниваю…")
        await _run_store_diff(query, state, "zip", i)

    @router.callback_query(F.data == "ctx:zs:ai")
    async def _zs_ai(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        data = await state.get_data()
        diff = data.get("zs_diff") or ""
        if not diff:
            await query.answer("Нет сохранённого диффа — сделай сравнение заново.", show_alert=True)
            return
        await query.answer("ИИ разбирает…")
        prog = await query.message.answer("🧠 ИИ анализирует разницу…")
        try:
            from ...agent import oneshot_chat
            ans = await oneshot_chat(
                "Ты код-ревьюер. По присланному диффу двух версий проекта кратко и по делу "
                "(по-русски) скажи: что важное добавилось/убралось/изменилось и есть ли риск.",
                diff, purpose="diff-review", max_tokens=800)
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await prog.edit_text(f"⚠️ ИИ недоступен: {_esc(str(exc))}")
            return
        with contextlib.suppress(Exception):
            await prog.delete()
        await query.message.answer(
            "🧠 <b>ИИ-разбор разницы:</b>\n\n" + _esc(ans)[:3800], parse_mode="HTML")

    return router


async def _run_next_chunk(bot, task_id: str, chat_id: int) -> None:
    """Run the current chunk; schedule a timer for the next (real timer)."""
    rec = store.load_task(task_id)
    if not rec:
        return
    idx = rec.get("idx", 0)
    chunks = rec.get("chunks", [])
    if idx >= len(chunks):
        return
    from .._llm import chat_once
    progress = "\n".join(rec.get("done", []))[-2000:]
    system = (
        "Ты выполняешь большое ТЗ по частям, чтобы не терять контекст. "
        "Тебе дают КУСОК ТЗ и краткую сводку уже сделанного. Сделай только "
        "этот кусок, кратко зафиксируй результат."
    )
    user = (f"Сводка сделанного:\n{progress or '(пока ничего)'}\n\n"
            f"КУСОК {idx + 1}/{len(chunks)} ТЗ:\n{chunks[idx]}")
    try:
        out = await chat_once(system, user, max_tokens=2000, temperature=0.3)
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await bot.send_message(chat_id, f"⚠️ Кусок {idx + 1} не вышел: {exc}")
        return
    rec.setdefault("done", []).append(f"[{idx + 1}] {(out or '')[:500]}")
    rec["idx"] = idx + 1
    store.save_task(rec)
    with contextlib.suppress(Exception):
        await bot.send_message(chat_id, f"✅ Кусок {idx + 1}/{len(chunks)} готов.")
    if rec["idx"] < len(chunks):
        # Real timer: schedule continuation ~60s later (scheduler fires it).
        store.add_timer(task_id, int(time.time()) + 60,
                        "продолжить следующий кусок", chat_id)
        with contextlib.suppress(Exception):
            await bot.send_message(chat_id, "⏲ Таймер на следующий кусок (~60с).")
    else:
        full = "\n\n".join(rec.get("done", []))
        with contextlib.suppress(Exception):
            await bot.send_message(chat_id, "🎉 Все куски выполнены. Память собрана.")
        with contextlib.suppress(Exception):
            from aiogram.types import BufferedInputFile
            await bot.send_document(chat_id, BufferedInputFile(
                full.encode("utf-8"), filename=f"paged_{task_id}.md"),
                caption="🧩 Результат по всем кускам")


async def process_due_timers(bot) -> None:
    """Called periodically (heartbeat): fire any due continuation timers."""
    for t in store.due_timers():
        store.pop_timer(t["task_id"], int(t["due"]))
        with contextlib.suppress(Exception):
            await _run_next_chunk(bot, t["task_id"], int(t["chat_id"]))
