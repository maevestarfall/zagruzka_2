# Instructions store: single master instruction + use-mode, with agent directive.
# Co-authored with CoCo
"""Storage for the «📜 Инструкции» feature.

Holds ONE master instruction (a default is seeded) and a use-mode that
controls how the agent applies it:

* ``off``      — agent ignores the instruction (default).
* ``only``     — agent relies on the instruction, prefers it over guessing.
* ``internet`` — agent uses the instruction AND may go to the internet/tools.

Everything lives in state.json under the ``instructions`` addon namespace.
"""

from __future__ import annotations

from .. import state as addon_state

ADDON = "instructions"

DEFAULT_INSTRUCTION = (
    "# Главная инструкция бота\n"
    "\n"
    "Связки функций под задачи (используй существующие функции бота):\n"
    "- Генерация фото: img2img по настройкам фото-слота; пресеты — «🧩 Алгоритм».\n"
    "- Генерация видео: из готовой картинки → image_to_video (видео-слот).\n"
    "- Чтение мусора (.mhtml/.htm/файлы/ссылки): «🗑 Скинуть весь мусор» →\n"
    "  markitdown / HTML→текст / OCR; формат вывода по запросу (.md/.json/.jpg/.zip).\n"
    "- Поиск информации: Analyzer PRO (если подключён) → иначе веб-поиск.\n"
    "- Поиск человека (OSINT): профильные источники.\n"
    "- GitHub: Управление GitHub → создать/обновить/удалить репо, push батчами по 95.\n"
    "- GitHub-доступ: «🔐 Вход в GitHub» (логин+пароль сохраняются) → «🔑 Взять токен»\n"
    "  / «🔑 Получить токен с правами» — открывает github.com/settings/tokens с\n"
    "  инструкцией (All repositories + Administration/Contents RW), затем\n"
    "  «🔑 Подключить токен» вставляет github_pat_…\n"
    "- Само-починка: «🪄 Регенерация» (скан → кодер пишет фикс → zip).\n"
    "- Уяснение приказа: «🧩 Прочие → 🧠 Управление ИИ» — приказ обычными словами;\n"
    "  бот прогоняет его по анкете (🎯 цель · 📋 ЧТО/ГДЕ/С ЧЕМ/КАК/ДОСТУПЫ/РЕЗУЛЬТАТ/\n"
    "  ОГРАНИЧЕНИЯ с ✅/❌ · ❓ уточнения · 🚦 вердикт) ПЕРЕД исполнением. Если чего-то\n"
    "  не хватает — сначала добери данные, потом запускай связку функций.\n"
    "- Свободная задача в браузере: «🧩 Прочие → 🤖 Инструкции через ИИ» —\n"
    "  слова → сценарий кликов → выполнение в stealth-браузере.\n"
    "\n"
    "Тайминги: между тяжёлыми шагами держи паузы, не запускай всё разом.\n"
    "Контекст: передавай результат предыдущего шага следующему (картинка→видео и т.п.).\n"
)

_MODES = ("off", "only", "internet")


def get_master() -> str:
    val = addon_state.get(ADDON, "master", None)
    return val if isinstance(val, str) and val.strip() else DEFAULT_INSTRUCTION


def set_master(text: str) -> None:
    addon_state.set_(ADDON, "master", (text or "").strip())


def reset_master() -> None:
    addon_state.set_(ADDON, "master", DEFAULT_INSTRUCTION)


def append_master(extra: str) -> None:
    """Merge an improvement into the master instruction."""
    cur = get_master().rstrip()
    set_master(cur + "\n\n## Дополнение (оптимизация)\n" + (extra or "").strip())


def get_mode() -> str:
    val = str(addon_state.get(ADDON, "mode", "off") or "off")
    return val if val in _MODES else "off"


def set_mode(mode: str) -> None:
    addon_state.set_(ADDON, "mode", mode if mode in _MODES else "off")


def directive() -> str:
    """Return the system-prompt block for the agent, or '' when off."""
    mode = get_mode()
    if mode == "off":
        return ""
    head = (
        "## ГЛАВНАЯ ИНСТРУКЦИЯ ВЛАДЕЛЬЦА (соблюдай в первую очередь)"
        if mode == "only"
        else "## ГЛАВНАЯ ИНСТРУКЦИЯ ВЛАДЕЛЬЦА (+ можно интернет/инструменты)"
    )
    tail = (
        "\nОпирайся прежде всего на эту инструкцию; не уходи в интернет без нужды."
        if mode == "only"
        else "\nСледуй инструкции; при необходимости пользуйся интернетом и инструментами."
    )
    # Portioning hint tied to the active model's measured context capacity.
    portion = ""
    with __import__("contextlib").suppress(Exception):
        from ..context import store as _ctx
        chunk = _ctx.recommended_chunk_chars()
        portion = (
            f"\n\nПОРЦИОННОСТЬ: у активной модели измеренный размер куска ~{chunk} "
            "символов. Тяжёлые задачи дроби на куски этого размера и веди их "
            "по таймингам, чтобы не терять контекст."
        )
    return head + "\n" + get_master().strip() + tail + portion
