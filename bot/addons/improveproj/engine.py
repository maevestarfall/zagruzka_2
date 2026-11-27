# «➕ Улучшить проект из Важного» — движок: предложение + передача Кодеру.
# Co-authored with CoCo
"""Гермес предлагает улучшения проекта из «❗Важно», затем (по кнопке/авто)
отдаёт задачу Кодеру.

propose():   читает «❗Важно» + карту проекта + мастер-инструкцию, следует
             инструкции «как искать/чинить», и формулирует предложение в форме
             «могу сделать X · было так → станет так · плюсы». Ничего не меняет.
enqueue_coder(): ставит задачу Кодеру (run_fsm_loop) — это шаг «Улучшить».
Глубина веб-ресёрча зависит от настроенных ключей поиска; без них предложение
строится на знаниях модели + карте проекта.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_SYSTEM = (
    "Ты — Гермес, старший инженер проекта. Тебе дают: (1) карту проекта, "
    "(2) выводы владельца из раздела «❗Важно» (backlog новых функций), "
    "(3) мастер-инструкцию (порядок действий). Действуй ПО ИНСТРУКЦИИ: как "
    "искать лучшие способы и чинить. Изучи, что сегодня считается лучшей "
    "практикой (скилы, гитхабы, подходы), и ПРЕДЛОЖИ, как расширить проект "
    "НОВЫМИ функциями из «❗Важно».\n\n"
    "Формат ответа (для каждого предложения):\n"
    "• Могу сделать: <что>\n"
    "• Было так: <как сейчас> → Станет так: <как будет>\n"
    "• Плюсы: <в чём выгода>\n"
    "• Как (кратко): <какими инструментами/шагами; ссылки/скилы если знаешь>\n\n"
    "Только ПРЕДЛОЖЕНИЯ. Ничего не выдумывай про несуществующие функции бота."
)


async def propose() -> str:
    """Return Hermes' improvement proposals for the project (text). No changes."""
    from ...agent import oneshot_chat
    # gather context
    notes = ""
    try:
        from ..instructions import important as _imp
        notes = _imp.directive()
    except Exception:  # noqa: BLE001
        pass
    pmap = ""
    try:
        from ..regen.project_map import build_project_map
        pmap = build_project_map()
    except Exception:  # noqa: BLE001
        pass
    master = ""
    try:
        from ..instructions import store as _instr
        master = _instr.get_master()
    except Exception:  # noqa: BLE001
        pass
    user = (
        "КАРТА ПРОЕКТА:\n" + (pmap[:6000] or "(нет)") + "\n\n"
        "ВЫВОДЫ ВЛАДЕЛЬЦА «❗Важно»:\n" + (notes[:4000] or "(нет)") + "\n\n"
        "МАСТЕР-ИНСТРУКЦИЯ:\n" + (master[:3000] or "(нет)") + "\n\n"
        "Предложи 1–3 улучшения проекта новыми функциями из «❗Важно» по формату."
    )
    try:
        text = await oneshot_chat(_SYSTEM, user, purpose="improveproj", temperature=0.4, max_tokens=2500)
    except Exception as e:  # noqa: BLE001
        logger.exception("improveproj propose failed")
        return f"⚠️ Не удалось сформировать предложение: {e}"
    return (text or "").strip() or "(модель вернула пустой ответ)"


def enqueue_coder(task_desc: str, chat_id: int, bot) -> str:
    """Hand the approved proposal to the Кодер (run_fsm_loop). Returns run_id."""
    import asyncio
    import random
    import sqlite3
    import time
    from ...coder.persistence import DB_PATH, init_db
    from ..coder.handlers import run_fsm_loop
    try:
        from ..coder.handlers import get_workers, get_budget
        workers, budget = get_workers(), get_budget()
    except Exception:  # noqa: BLE001
        workers, budget = 3, 5.0
    init_db(DB_PATH)
    run_id = f"impj_{random.randint(1000, 9999)}"
    conn = sqlite3.connect(DB_PATH)
    with conn:
        conn.execute(
            "INSERT INTO coder_runs (run_id, task_desc, state, workers_count, "
            "budget_cap, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, task_desc, "INIT", workers, budget,
             time.strftime("%Y-%m-%d %H:%M:%S")),
        )
    conn.close()
    asyncio.create_task(run_fsm_loop(run_id, task_desc, chat_id, bot))
    return run_id


async def research_via_analyzer(problem: str) -> tuple[str, str, str]:
    """Гермес формулирует запрос из проблемы и отдаёт его Анализатору ПРО.

    Returns ``(status, task_id, solution)`` where status ∈
    {completed, pending, failed, no_server}.
    """
    from ...agent import oneshot_chat
    from ..analyzer import submit_query, poll_result
    try:
        query = await oneshot_chat(
            "Ты — Гермес. Сформулируй КОРОТКИЙ поисковый запрос (1–3 предложения) "
            "для внешнего исследователя (Анализатор ПРО): что найти в интернете/"
            "скилах/гитхабах, чтобы лучше всего решить проблему проекта. Только "
            "сам запрос, без преамбул.",
            f"ПРОБЛЕМА:\n{problem[:3000]}",
            purpose="improveproj-query", temperature=0.2, max_tokens=300,
        )
    except Exception:  # noqa: BLE001
        query = ""
    query = (query or problem)[:1500].strip()
    ok, task_id, err = await submit_query(query)
    if not ok:
        return ("no_server", "", err)
    status, text = await poll_result(task_id, timeout=90.0)
    return (status, task_id, text)


async def apply_real(proposal: str, bot, chat_id: int) -> None:
    """РЕАЛЬНОЕ улучшение: proposal → generate_fix (chat_once) → build_fix_zip.

    Использует настоящий конвейер Регенерации (не фейковую «фирму»): кодер-мозг
    пишет конкретные файлы, дерево компилируется и пере-сканируется, собирается
    проверенный zip, который отдаётся владельцу для заливки на GitHub → Render.
    """
    import asyncio
    from pathlib import Path
    from ..coder.regen_brain import generate_fix, NoBrainError
    from ..regen.outputs import build_fix_zip
    from ..regen.project_map import build_project_map, repo_root

    await bot.send_message(chat_id, "🛠 Готовлю реальное улучшение (кодер-мозг + сборка zip)…")
    # S2: кодер должен брать за базу ПОСЛЕДНИЙ zip из хранилища, а не живой код.
    # Если его нет — спрашиваем владельца залить последнюю версию.
    from . import store as _st0
    import os as _os, tempfile as _tf, zipfile as _zf
    stored = _st0.latest_storage_zip()
    if stored is None:
        await bot.send_message(
            chat_id,
            "📦 В хранилище нет zip проекта. Залей ПОСЛЕДНЮЮ версию бота "
            "(дропни zip в чат → «💾 В хранилище»), чтобы кодер взял её за базу. "
            "Улучшение отменено.")
        return
    base_root = None
    with __import__("contextlib").suppress(Exception):
        _src = _tf.mkdtemp(prefix="impj_src_")
        _zf.ZipFile(str(stored)).extractall(_src)
        for _dp, _dns, _fns in _os.walk(_src):
            if "main.py" in _fns and _os.path.basename(_dp) == "bot":
                base_root = _os.path.dirname(_dp)
                break
    if not base_root:
        await bot.send_message(
            chat_id,
            f"⚠️ Не нашёл структуру проекта в zip хранилища ({stored.name}). "
            "Проверь, что это полный архив бота. Улучшение отменено.")
        return
    await bot.send_message(chat_id, f"📦 База для кодера: {stored.name} (из хранилища).")
    try:
        pmap = ""
        with __import__("contextlib").suppress(Exception):
            pmap = build_project_map()
        research = ""
        with __import__("contextlib").suppress(Exception):
            from ..instructions import important as _imp
            research = _imp.directive()
        with __import__("contextlib").suppress(Exception):
            from . import store as _st
            sol = _st.get_last_solution()
            ref = _st.get_refinement()
            if sol:
                research += "\n\n# РЕШЕНИЕ ОТ АНАЛИЗАТОРА ПРО\n" + sol[:4000]
            if ref:
                research += ("\n\n# ДОПОЛНЕНИЕ ВЛАДЕЛЬЦА (что НЕ делать / только часть)\n"
                             + ref)
        fix = await generate_fix(
            proposal, project_map=pmap, research=research, max_tokens=3500,
        )
    except NoBrainError as e:
        await bot.send_message(chat_id, f"🧠 Нет рабочего мозга для кодера: {e}")
        return
    except Exception as e:  # noqa: BLE001
        logger.exception("improveproj apply_real generate_fix failed")
        await bot.send_message(chat_id, f"⚠️ Кодер не смог сформировать фикс: {e}")
        return

    if not fix.files:
        # Модель дала инструкции без готовых файлов — отдаём текстом, честно.
        txt = (fix.instructions or fix.root_cause or "").strip() or "(пусто)"
        await bot.send_message(
            chat_id,
            "🛠 Кодер вернул инструкции без готовых файлов (нужна доработка):\n\n"
            + txt[:3500])
        return

    try:
        zip_path, compile_errors, verify = await asyncio.to_thread(
            build_fix_zip, fix, Path(base_root), version_tag="improveproj",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("improveproj apply_real build_fix_zip failed")
        await bot.send_message(chat_id, f"⚠️ Не удалось собрать fix-zip: {e}")
        return

    n_err = len(compile_errors or [])
    ver = "1.202"
    with __import__("contextlib").suppress(Exception):
        from . import store as _st
        ver = _st.bump_version()
    caption = (
        f"🛠 <b>Реальное улучшение готово — версия {ver}</b>\n"
        f"Изменено файлов: <b>{len(fix.files)}</b>\n"
        f"Компиляция дерева: <b>{'OK' if n_err == 0 else f'{n_err} ошибок — см. REGEN_FIX_NOTES.md'}</b>\n"
        f"Уверенность кодера: {fix.confidence}\n\n"
        "Это НАСТОЯЩИЙ фикс-zip (не фейковый «PR»). Проверь и залей на GitHub → Render, "
        "и положи его в хранилище как новую последнюю версию."
    )
    with __import__("contextlib").suppress(Exception):
        from aiogram.types import FSInputFile
        await bot.send_document(
            chat_id, FSInputFile(zip_path, filename=f"lilush-bot-v{ver}.zip"),
            caption=caption, parse_mode="HTML")
