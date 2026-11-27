"""Real ``browser-use`` agent worker (github.com/browser-use/browser-use).

This drives the actual ``browser-use`` package — the 100k-star autonomous
browser agent — as the engine behind «🆕 Браузерная новая». You give it a
free-text task; browser-use itself decides which elements to click/type by
reading the page's interactive-element list (the mechanism explained earlier),
using lilush's own LLM brain slot.

Design mirrors ``browser_clicker.py``:
  • Lazy install on first use (browser-use + Playwright Chromium are NOT pinned
    in requirements.txt, so a box without disk/RAM headroom is unaffected until
    the feature is actually used).
  • Graceful degrade — any failure returns ``{"ok": False, "error": ...}``
    instead of crashing the bot.

LLM: reuses lilush's brain (OpenRouter / custom OpenAI-compatible endpoint) via
``storage`` so no second API key is needed.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

# Tri-state readiness cache: None = unknown, True = importable, False = failed.
_BU_READY: bool | None = None


def _pip_install(*pkgs: str) -> bool:
    """pip install one or more packages; return True on success."""
    import subprocess
    import sys
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-input", *pkgs],
            check=True,
            capture_output=True,
            timeout=600,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("pip install %s failed: %s", pkgs, exc)
        return False


def ensure_browser_use() -> bool:
    """Lazily install browser-use + a Chromium for it. Cached after first run."""
    global _BU_READY
    if _BU_READY is not None:
        return _BU_READY
    try:
        import browser_use  # noqa: F401
        _BU_READY = True
        return True
    except ImportError:
        pass
    logger.info("browser-use not present -- bootstrapping (lazy install)...")
    ok = _pip_install("browser-use")
    if ok:
        # browser-use needs a Chromium; install it via Playwright (best-effort).
        import subprocess
        import sys
        with __import__("contextlib").suppress(Exception):
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=False, capture_output=True, timeout=600,
            )
    try:
        import browser_use  # noqa: F401
        _BU_READY = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("browser-use still unavailable after install: %s", exc)
        _BU_READY = False
    return _BU_READY


def _brain_creds() -> tuple[str, str, str]:
    """Return ``(api_key, base_url, model)`` from lilush's brain slot."""
    from ..config import DEFAULT_MODEL, OPENROUTER_BASE_URL
    from ..storage import storage

    api_key = storage.get_provider_key("openrouter")
    if not api_key:
        raise RuntimeError(
            "LLM-мозг не настроен — открой /setup, выбери мозг и пришли API-ключ."
        )
    provider = storage.get_provider()
    if provider == "custom":
        base_url = storage.get_base_url() or OPENROUTER_BASE_URL
    else:
        base_url = OPENROUTER_BASE_URL
    model = storage.get_model() or DEFAULT_MODEL
    return api_key, base_url, model


def _build_llm():
    """Build a browser-use LLM client, tolerant of version differences."""
    api_key, base_url, model = _brain_creds()
    # browser-use >=0.2 ships its own LLM wrappers under browser_use.llm;
    # older builds re-export ChatOpenAI at the top level. Try both.
    ChatOpenAI = None
    try:
        from browser_use.llm import ChatOpenAI as _C
        ChatOpenAI = _C
    except Exception:  # noqa: BLE001
        try:
            from browser_use import ChatOpenAI as _C
            ChatOpenAI = _C
        except Exception:  # noqa: BLE001
            ChatOpenAI = None
    if ChatOpenAI is not None:
        try:
            return ChatOpenAI(model=model, api_key=api_key, base_url=base_url)
        except TypeError:
            # Some versions use ``base_url`` via env / different kwarg names.
            os.environ.setdefault("OPENAI_API_KEY", api_key)
            os.environ.setdefault("OPENAI_BASE_URL", base_url)
            return ChatOpenAI(model=model)
    # Last resort: langchain_openai (older browser-use expected a LangChain LLM).
    from langchain_openai import ChatOpenAI as LCChatOpenAI  # type: ignore
    return LCChatOpenAI(model=model, api_key=api_key, base_url=base_url)


async def run_browser_use_task(
    task: str,
    screenshot_path: str = "session/browser_use.png",
    max_steps: int = 25,
) -> dict:
    """Run an autonomous browser-use agent on a free-text task.

    Returns ``{ok, final, error, screenshot}``.
    """
    error = None
    final = None

    if not await asyncio.to_thread(ensure_browser_use):
        return {
            "ok": False,
            "error": "Не удалось установить browser-use на этом сервере "
                     "(нет места/прав). Попробуй «🌐 Браузерная» (классический движок).",
            "final": None,
            "screenshot": None,
        }

    try:
        from browser_use import Agent
        llm = _build_llm()

        # Agent signature varies slightly across versions; pass task+llm which
        # every version accepts.
        agent = Agent(task=task, llm=llm)
        try:
            history = await agent.run(max_steps=max_steps)
        except TypeError:
            history = await agent.run()

        # Extract a human-readable final answer across versions.
        if hasattr(history, "final_result"):
            try:
                final = history.final_result()
            except Exception:  # noqa: BLE001
                final = None
        if not final:
            for attr in ("final_answer", "result", "output"):
                v = getattr(history, attr, None)
                if v:
                    final = str(v)
                    break
        if not final:
            final = str(history)[:2000]

        # Best-effort screenshot of the final page.
        with __import__("contextlib").suppress(Exception):
            from pathlib import Path
            Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
            page = None
            br = getattr(agent, "browser", None) or getattr(agent, "browser_session", None)
            if br is not None:
                getp = getattr(br, "get_current_page", None)
                if getp:
                    page = await getp()
                if page is not None:
                    await page.screenshot(path=screenshot_path, full_page=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("browser-use task failed")
        error = str(exc)

    return {
        "ok": error is None,
        "error": error,
        "final": final,
        "screenshot": screenshot_path if os.path.exists(screenshot_path) else None,
    }
