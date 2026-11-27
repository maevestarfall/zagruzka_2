"""Локальная ИИ (Ollama) — установка, загрузка, запуск и подключение как «мозг».

Кнопка «🧠 Локальная ИИ» в разделе «Перенастроить мозг» позволяет:

* **Включить**  — поставить Ollama (если её нет), скачать выбранную модель
  (по умолчанию Qwen 2.5), запустить локальный сервер и переключить чат-мозг
  бота на неё (OpenAI-совместимый endpoint ``http://127.0.0.1:11434/v1``).
* **Выключить** — вернуть прежний «мозг» (модель остаётся на диске).
* **Удалить**   — выключить и стереть скачанную модель с диска.

⚠️ Тяжёлые модели требуют много RAM. Перед запуском делается честная проверка
оперативной памяти и место сверяется с минимумом модели — если не хватает,
включение блокируется с понятным сообщением (чтобы не убить контейнер OOM-ом
на маленьком хостинге вроде Render Free / 512 МБ).

Все долгие операции (установка, загрузка ~ГБ) запускаются как фоновые
subprocess-ы и репортят прогресс через переданный async-callback.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil

from . import state as addon_state
from ..storage import storage

logger = logging.getLogger(__name__)

ADDON = "local_ai"

# Локальный OpenAI-совместимый endpoint, который поднимает Ollama.
OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
OLLAMA_HOST = "127.0.0.1:11434"
# Любой непустой ключ — Ollama его игнорирует, но OpenAI-клиент требует строку.
DUMMY_KEY = "ollama-local"

# Реестр доступных моделей. ``min_ram_gb`` — грубый минимум ОЗУ для Q4-кванта.
MODELS: dict[str, dict] = {
    "qwen2.5:14b": {
        "label": "Qwen 2.5 14B",
        "size": "~9 ГБ",
        "min_ram_gb": 12,
        "note": "Самая умная, но тяжёлая. Нужен сервер ≥12 ГБ RAM (лучше GPU).",
    },
    "qwen2.5:7b": {
        "label": "Qwen 2.5 7B",
        "size": "~4.7 ГБ",
        "min_ram_gb": 8,
        "note": "Баланс качества и веса. Нужен сервер ≥8 ГБ RAM.",
    },
    "qwen2.5:3b": {
        "label": "Qwen 2.5 3B",
        "size": "~2 ГБ",
        "min_ram_gb": 4,
        "note": "Лёгкая. Поместится на скромный VPS (≥4 ГБ RAM).",
    },
    "qwen2.5:1.5b": {
        "label": "Qwen 2.5 1.5B",
        "size": "~1 ГБ",
        "min_ram_gb": 2,
        "note": "Самая лёгкая. Реально запустить на маленьком сервере (≥2 ГБ).",
    },
}

DEFAULT_MODEL = "qwen2.5:14b"

# Папка на диске, куда владелец кладёт файл модели (.gguf) для установки
# «из хранилища» — без скачивания ~ГБ из интернета. Лежит на смонтированном
# диске (DATA_DIR), поэтому переживает редеплой контейнера.
MODELS_DIR = os.path.join(os.environ.get("DATA_DIR", "/data"), "local_ai_models")
# Имя, под которым импортированная из файла модель регистрируется в Ollama.
IMPORTED_MODEL_NAME = "qwen-local"


# --------------------------------------------------------------------------
# state helpers
# --------------------------------------------------------------------------


def _get(key: str, default=None):
    return addon_state.get(ADDON, key, default)


def _set(key: str, value) -> None:
    addon_state.set_(ADDON, key, value)


def get_model() -> str:
    return str(_get("model", DEFAULT_MODEL) or DEFAULT_MODEL)


def set_model(model: str) -> None:
    if model in MODELS:
        _set("model", model)


def get_status() -> str:
    """idle | installing | downloading | running | stopped | error."""
    return str(_get("status", "idle") or "idle")


def _set_status(status: str, *, error: str = "") -> None:
    _set("status", status)
    if error:
        _set("last_error", error[:500])


def is_enabled() -> bool:
    return bool(_get("enabled", False))


def last_error() -> str:
    return str(_get("last_error", "") or "")


# --------------------------------------------------------------------------
# system / ollama detection
# --------------------------------------------------------------------------


def system_ram_gb() -> float:
    """Total physical RAM in GiB (best-effort, 0.0 if unknown)."""
    with contextlib.suppress(Exception):
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round(pages * page_size / (1024**3), 1)
    return 0.0


def free_disk_gb(path: str = "/") -> float:
    with contextlib.suppress(Exception):
        st = os.statvfs(path)
        return round(st.f_bavail * st.f_frsize / (1024**3), 1)
    return 0.0


def ollama_installed() -> bool:
    return shutil.which("ollama") is not None


def resource_check(model: str) -> tuple[bool, str]:
    """Return (ok, human_message). Blocks enable if RAM is clearly too low."""
    meta = MODELS.get(model, MODELS[DEFAULT_MODEL])
    ram = system_ram_gb()
    need = meta["min_ram_gb"]
    if ram and ram + 0.5 < need:
        return False, (
            f"Недостаточно RAM: на сервере ≈{ram} ГБ, "
            f"для {meta['label']} нужно ≥{need} ГБ. "
            "Выбери модель полегче или перенеси бота на сервер помощнее."
        )
    disk = free_disk_gb(os.environ.get("DATA_DIR", "/"))
    return True, f"RAM ≈{ram or '?'} ГБ · свободно на диске ≈{disk or '?'} ГБ"


# --------------------------------------------------------------------------
# subprocess helpers
# --------------------------------------------------------------------------


async def _run(cmd: list[str], *, timeout: int = 600) -> tuple[int, str]:
    """Run a command, capture combined output. Returns (rc, output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, (out or b"").decode("utf-8", "replace")
    except asyncio.TimeoutError:
        return 124, "timeout"
    except FileNotFoundError as e:
        return 127, str(e)
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


async def _ollama_server_up() -> bool:
    rc, _ = await _run(["ollama", "list"], timeout=15)
    return rc == 0


async def ensure_ollama(progress=None) -> tuple[bool, str]:
    """Install Ollama via the official script if it's missing (needs root/curl)."""
    if ollama_installed():
        return True, "Ollama уже установлена."
    if progress:
        await progress("⏬ Устанавливаю Ollama (официальный скрипт)…")
    if shutil.which("curl") is None:
        return False, "В системе нет curl — не могу скачать установщик Ollama."
    # curl -fsSL https://ollama.com/install.sh | sh
    rc, out = await _run(
        ["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"],
        timeout=900,
    )
    if rc != 0 or not ollama_installed():
        return False, f"Установка Ollama не удалась (rc={rc}): {out[-300:]}"
    return True, "Ollama установлена."


async def ensure_server(progress=None) -> tuple[bool, str]:
    """Start `ollama serve` in the background if it's not already serving."""
    if await _ollama_server_up():
        return True, "Сервер Ollama уже работает."
    if progress:
        await progress("🚀 Запускаю сервер Ollama…")
    env = dict(os.environ, OLLAMA_HOST=OLLAMA_HOST)
    with contextlib.suppress(Exception):
        await asyncio.create_subprocess_exec(
            "ollama", "serve",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
    # Give it a few seconds to bind the port.
    for _ in range(10):
        await asyncio.sleep(1.5)
        if await _ollama_server_up():
            return True, "Сервер Ollama запущен."
    return False, "Сервер Ollama не поднялся за отведённое время."


async def pull_model(model: str, progress=None) -> tuple[bool, str]:
    """Download a model, streaming Ollama's progress lines to the callback."""
    if progress:
        meta = MODELS.get(model, {})
        await progress(f"⏬ Скачиваю {meta.get('label', model)} ({meta.get('size','')})…")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ollama", "pull", model,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"Не смог запустить загрузку: {e}"

    last_line = ""
    assert proc.stdout is not None
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").strip()
        if line and line != last_line:
            last_line = line
            if progress:
                with contextlib.suppress(Exception):
                    await progress(f"⏬ {line[:120]}")
    rc = await proc.wait()
    if rc != 0:
        return False, f"Загрузка завершилась с ошибкой (rc={rc}): {last_line[:200]}"
    return True, "Модель загружена."


# --------------------------------------------------------------------------
# brain wiring (backup / restore)
# --------------------------------------------------------------------------


def _backup_brain() -> None:
    """Snapshot the current brain config so «Выключить» can restore it."""
    if _get("prev_brain"):
        return  # already backed up — don't clobber
    snap = {
        "brain": storage.get_brain(),
        "provider": storage.get_provider(),
        "base_url": storage.get_base_url(),
        "model": storage.get_model(),
        "or_key": storage.get_provider_key("openrouter"),
    }
    _set("prev_brain", snap)


def _wire_brain(model: str) -> None:
    """Point the chat brain at the local Ollama endpoint."""
    _backup_brain()
    storage.set_brain("auto")
    storage.set_provider("custom")
    storage.set_base_url(OLLAMA_BASE_URL)
    storage.set_model(model)
    # The OpenAI client needs a non-empty key even though Ollama ignores it.
    if not storage.get_provider_key("openrouter"):
        storage.set_provider_key("openrouter", DUMMY_KEY)


def _restore_brain() -> None:
    snap = _get("prev_brain")
    if not isinstance(snap, dict):
        return
    with contextlib.suppress(Exception):
        storage.set_brain(snap.get("brain") or "auto")
        storage.set_provider(snap.get("provider") or "openrouter")
        storage.set_base_url(snap.get("base_url") or "")
        storage.set_model(snap.get("model") or "")
        # Only rewrite the key if we had injected the dummy one.
        if storage.get_provider_key("openrouter") == DUMMY_KEY:
            storage.set_provider_key("openrouter", snap.get("or_key") or "")
    _set("prev_brain", None)


# --------------------------------------------------------------------------
# high-level orchestration
# --------------------------------------------------------------------------


async def enable(model: str | None = None, progress=None) -> tuple[bool, str]:
    """Full flow: resource check → install → serve → pull → wire brain."""
    model = model or get_model()
    if model not in MODELS:
        model = DEFAULT_MODEL
    set_model(model)

    ok, msg = resource_check(model)
    if not ok:
        _set_status("error", error=msg)
        return False, f"⛔ {msg}"

    _set_status("installing")
    ok, msg = await ensure_ollama(progress)
    if not ok:
        _set_status("error", error=msg)
        return False, f"❌ {msg}"

    ok, msg = await ensure_server(progress)
    if not ok:
        _set_status("error", error=msg)
        return False, f"❌ {msg}"

    _set_status("downloading")
    ok, msg = await pull_model(model, progress)
    if not ok:
        _set_status("error", error=msg)
        return False, f"❌ {msg}"

    _wire_brain(model)
    _set("enabled", True)
    _set_status("running")
    meta = MODELS[model]
    return True, (
        f"✅ Локальная ИИ включена: <b>{meta['label']}</b>.\n"
        f"Чат-мозг переключён на локальный сервер ({OLLAMA_BASE_URL})."
    )


async def disable(progress=None) -> tuple[bool, str]:
    """Restore the previous brain. Model stays on disk."""
    _restore_brain()
    _set("enabled", False)
    _set_status("stopped")
    return True, "🔌 Локальная ИИ выключена. Прежний мозг восстановлен (модель не удалена)."


async def remove(progress=None) -> tuple[bool, str]:
    """Disable and delete the downloaded model from disk."""
    model = get_model()
    await disable(progress)
    if progress:
        await progress(f"🗑 Удаляю модель {model}…")
    rc, out = await _run(["ollama", "rm", model], timeout=120)
    _set_status("idle")
    if rc != 0:
        return False, f"⚠️ Мозг восстановлен, но удалить модель не вышло: {out[-200:]}"
    return True, f"🗑 Модель {model} удалена с диска. Локальная ИИ выключена."


def models_dir() -> str:
    """Ensure the local-models directory exists and return its path."""
    with contextlib.suppress(Exception):
        os.makedirs(MODELS_DIR, exist_ok=True)
    return MODELS_DIR


def list_local_files() -> list[dict]:
    """List candidate model files (*.gguf/*.bin) sitting in MODELS_DIR."""
    out: list[dict] = []
    d = models_dir()
    with contextlib.suppress(Exception):
        for name in sorted(os.listdir(d)):
            if not name.lower().endswith((".gguf", ".bin")):
                continue
            fp = os.path.join(d, name)
            if not os.path.isfile(fp):
                continue
            size_gb = round(os.path.getsize(fp) / (1024**3), 2)
            out.append({"path": fp, "name": name, "size_gb": size_gb})
    return out


async def install_from_gguf(path: str, progress=None) -> tuple[bool, str]:
    """Import a local GGUF file into Ollama and wire it as the chat brain.

    Avoids the multi-GB internet download: the owner drops a .gguf into
    MODELS_DIR (on the mounted disk), and Ollama registers it via a tiny
    Modelfile (``FROM <path>``). RAM still matters at *runtime*, but there is
    no download step.
    """
    if not os.path.isfile(path):
        _set_status("error", error=f"файл не найден: {path}")
        return False, f"❌ Файл не найден: {path}"

    _set_status("installing")
    ok, msg = await ensure_ollama(progress)
    if not ok:
        _set_status("error", error=msg)
        return False, f"❌ {msg}"
    ok, msg = await ensure_server(progress)
    if not ok:
        _set_status("error", error=msg)
        return False, f"❌ {msg}"

    if progress:
        await progress(f"📦 Импортирую модель из {os.path.basename(path)}…")

    # Write a Modelfile next to the GGUF and let Ollama create the model.
    modelfile = os.path.join(models_dir(), "Modelfile")
    try:
        with open(modelfile, "w", encoding="utf-8") as f:
            f.write(f"FROM {path}\n")
    except Exception as e:  # noqa: BLE001
        _set_status("error", error=str(e))
        return False, f"❌ Не смог записать Modelfile: {e}"

    rc, out = await _run(
        ["ollama", "create", IMPORTED_MODEL_NAME, "-f", modelfile],
        timeout=1800,
    )
    if rc != 0:
        _set_status("error", error=out[-300:])
        return False, f"❌ Ollama create не удалась (rc={rc}): {out[-300:]}"

    # Register the imported model so the rest of the UI knows about it.
    MODELS[IMPORTED_MODEL_NAME] = {
        "label": f"Локальный файл ({os.path.basename(path)})",
        "size": f"~{round(os.path.getsize(path) / (1024**3), 1)} ГБ",
        "min_ram_gb": 0,  # unknown — we don't block file installs on RAM
        "note": "Импортировано из файла в хранилище.",
    }
    _set("model", IMPORTED_MODEL_NAME)
    _wire_brain(IMPORTED_MODEL_NAME)
    _set("enabled", True)
    _set_status("running")
    return True, (
        f"✅ Модель установлена из файла <code>{os.path.basename(path)}</code> "
        f"и подключена как локальный мозг ({OLLAMA_BASE_URL})."
    )


def status_summary() -> str:
    """One-liner for the status screen."""
    model = get_model()
    meta = MODELS.get(model, {})
    st = get_status()
    labels = {
        "idle": "не настроена",
        "installing": "🟡 ставлю Ollama…",
        "downloading": "🟡 качаю модель…",
        "running": "🟢 работает",
        "stopped": "🔴 выключена",
        "error": "⛔ ошибка",
    }
    parts = [f"Модель: {meta.get('label', model)}", f"Статус: {labels.get(st, st)}"]
    if st == "error" and last_error():
        parts.append(f"Последняя ошибка: {last_error()}")
    return " · ".join(parts)
