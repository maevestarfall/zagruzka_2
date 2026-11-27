"""versiff addon — aiogram router: pick a reference zip, compare every other
project archive's functions/classes against it, output a status table.

Reached from «🧩 Прочие → 🧬 Сравнить версии». Flow:
  1. 📂 Список архивов — scan storage for *.zip, show them.
  2. 📌 tap an archive — set it as the reference («нынешняя версия»).
  3. ▶️ Сравнить — diff all other archives vs the reference, return a CSV table
     (Версия, Эталон, Модуль, Объект, Тип, Статус) + a short summary.

Pure stdlib analysis (``analyzer.py``); heavy work runs in a thread executor.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from .. import state as addon_state
from . import analyzer

logger = logging.getLogger(__name__)

_ADDON = "versiff"


def _scan_root() -> Path:
    from ...config import DATA_DIR
    return Path(DATA_DIR)


def _find_zips() -> list[Path]:
    """All project *.zip under the data dir (Slack downloads, releases, …)."""
    root = _scan_root()
    if not root.exists():
        return []
    zips = [p for p in root.rglob("*.zip") if p.is_file()]
    zips.sort(key=lambda p: analyzer.version_key(p.name))
    return zips[:200]


def _ref_path() -> str:
    return addon_state.get(_ADDON, "reference_path", "") or ""


# ---- keyboards / text ----------------------------------------------------


def _kb_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📂 Список архивов", callback_data="versiff:list")],
        [InlineKeyboardButton(text="▶️ Сравнить с эталоном", callback_data="versiff:run")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="ctx:home")],
    ])


def _home_text() -> str:
    ref = _ref_path()
    ref_label = Path(ref).name if ref else "не выбран"
    zips = _find_zips()
    return (
        "<b>🧬 Сравнить версии (zip)</b>\n\n"
        "Сравниваю функции/классы каждого zip-архива проекта с эталонным "
        "(«нынешней версией») и заполняю таблицу: добавлена / удалена / "
        "изменилась / без изменений.\n\n"
        f"Архивов в хранилище: <b>{len(zips)}</b>\n"
        f"Эталон: <code>{ref_label}</code>\n\n"
        "1) «📂 Список архивов» → выбери эталон.\n"
        "2) «▶️ Сравнить» → получишь таблицу (CSV)."
    )


async def show_main(target) -> None:
    text, kb = _home_text(), _kb_home()
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            await target.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    else:
        await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


# ---- router --------------------------------------------------------------


def build_versiff_router() -> Router:
    router = Router(name="versiff_addon")

    @router.callback_query(F.data == "versiff:back")
    async def on_back(query: CallbackQuery) -> None:
        with contextlib.suppress(Exception):
            from ...wizard import _kb_main_after_claim  # type: ignore
            uid = query.from_user.id if query.from_user else None
            if query.message is not None:
                await query.message.edit_text("Главное меню. Выбери что сделать:",
                                              reply_markup=_kb_main_after_claim(uid))
        await query.answer()

    @router.callback_query(F.data == "versiff:home")
    async def on_home(query: CallbackQuery) -> None:
        await query.answer()
        await show_main(query)

    @router.callback_query(F.data == "versiff:list")
    async def on_list(query: CallbackQuery) -> None:
        if query.message is None:
            await query.answer()
            return
        zips = _find_zips()
        await query.answer()
        if not zips:
            await query.message.answer(
                "В хранилище не найдено zip-архивов. Сначала скачай их через "
                "«💬 Слак» (промт «скачать все zip»).",
                reply_markup=_kb_home(),
            )
            return
        # Index → path map (callback_data can't carry full paths).
        addon_state.set_(_ADDON, "zip_index", {str(i): str(p) for i, p in enumerate(zips)})
        ref = _ref_path()
        rows: list[list[InlineKeyboardButton]] = []
        for i, p in enumerate(zips):
            mark = "✅ " if str(p) == ref else "📌 "
            rows.append([InlineKeyboardButton(text=f"{mark}{p.name}"[:60],
                                              callback_data=f"versiff:ref:{i}")])
        rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="versiff:home")])
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                f"Найдено архивов: {len(zips)}. Нажми на архив, чтобы сделать его "
                "<b>эталоном</b> (нынешней версией):",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )

    @router.callback_query(F.data.startswith("versiff:ref:"))
    async def on_ref(query: CallbackQuery) -> None:
        if query.message is None or query.data is None:
            await query.answer()
            return
        idx = query.data.split(":", 2)[2]
        index = addon_state.get(_ADDON, "zip_index", {}) or {}
        path = index.get(idx)
        if not path:
            await query.answer("Не нашёл архив, обнови список.")
            return
        addon_state.set_(_ADDON, "reference_path", path)
        await query.answer(f"Эталон: {Path(path).name}")
        await show_main(query)

    @router.callback_query(F.data == "versiff:run")
    async def on_run(query: CallbackQuery) -> None:
        if query.message is None:
            await query.answer()
            return
        await query.answer()
        ref = _ref_path()
        if not ref or not Path(ref).is_file():
            await query.message.answer(
                "Сначала выбери эталон: «📂 Список архивов» → нажми на архив.",
                reply_markup=_kb_home(),
            )
            return
        zips = [p for p in _find_zips() if str(p) != ref]
        if not zips:
            await query.message.answer("Кроме эталона других архивов нет — сравнивать не с чем.",
                                       reply_markup=_kb_home())
            return

        from ..thinking_style import make_status_runner
        status_update, status_finish = make_status_runner(query.message)
        await status_update("Разбираю эталон…")
        loop = asyncio.get_event_loop()
        try:
            ref_objs = await loop.run_in_executor(None, analyzer.extract_zip_objects, ref)
        except Exception as e:  # noqa: BLE001
            await status_finish()
            await query.message.answer(f"Не смог разобрать эталон: {e}", reply_markup=_kb_home())
            return

        all_rows: list[analyzer.DiffRow] = []
        for p in zips:
            await status_update(f"Сравниваю {p.name}…")
            try:
                v_objs = await loop.run_in_executor(None, analyzer.extract_zip_objects, str(p))
                rows = await loop.run_in_executor(
                    None,
                    lambda vo=v_objs, lbl=p.name: analyzer.diff_against_reference(lbl, vo, ref_objs),
                )
                all_rows.extend(rows)
            except Exception as e:  # noqa: BLE001
                logger.warning("versiff compare failed for %s: %s", p, e)
                await query.message.answer(f"⚠ {p.name}: {e}")
        await status_finish()

        if not all_rows:
            await query.message.answer(
                "Различий не найдено: все архивы совпадают с эталоном по функциям/классам.",
                reply_markup=_kb_home(),
            )
            return

        # Write CSV table.
        out_dir = _scan_root() / "versiff"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        csv_path = out_dir / f"versiff_table_{stamp}.csv"
        csv_path.write_text(analyzer.render_csv(all_rows, Path(ref).name), encoding="utf-8-sig")

        # Short summary per version.
        summary = analyzer.summarize(all_rows)
        lines = [f"<b>🧬 Сравнение с эталоном</b> <code>{Path(ref).name}</code>\n"]
        for ver in sorted(summary, key=analyzer.version_key):
            s = summary[ver]
            parts = ", ".join(f"{k}: {v}" for k, v in s.items())
            lines.append(f"• <b>{ver}</b> — {parts}")
        lines.append(f"\nВсего строк в таблице: {len(all_rows)}")
        await query.message.answer("\n".join(lines))
        with contextlib.suppress(Exception):
            await query.message.answer_document(
                FSInputFile(str(csv_path), filename=csv_path.name),
                caption="Таблица сравнения версий (CSV)",
            )
        await query.message.answer("Готово.", reply_markup=_kb_home())

    return router
