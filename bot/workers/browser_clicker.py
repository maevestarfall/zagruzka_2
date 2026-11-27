"""Headless browser automation worker with quadruple-fallback stealth engines (Camoufox -> CloakBrowser -> nodriver -> Playwright).

Refactor (ТЗ rev.7.1 §0.6): the monolithic ``run_browser_macro`` is now built on
two extracted primitives so multi-tab routes (bot.addons.scenarios) can reuse the
*exact* same stealth/fingerprint/proxy stack without duplicating engine logic:

    * :func:`open_browser_session` — boots the browser + context with ALL existing
      settings (domain_profiles, hwproxy, stealth_fp, антидетект toggle, tz/os
      overrides, proxy, storage_state) resolved INSIDE from state/addons, exactly
      as before. Returns a :class:`BrowserSession`; the caller owns its lifecycle.
    * :class:`BrowserSession` — ``.new_page()`` (real multi-tab, §15.1),
      ``.run_steps()`` (both nodriver + playwright step branches, unchanged
      behaviour), ``.browser_type``, ``.storage_state()``, ``.close()``.

``run_browser_macro`` keeps its original signature and behaviour (regression
required, §13.1) and is now a thin wrapper over these primitives.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import random
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Try to import Camoufox (Stealth Firefox, compiled C++ fingerprinting)
try:
    from camoufox.async_api import AsyncCamoufox
    USE_CAMOUFOX = True
except ImportError:
    USE_CAMOUFOX = False

# Try to import CloakBrowser (Stealth Chromium, compiled C++ fingerprinting)
try:
    from cloakbrowser import launch as cloak_launch
    USE_CLOAK = True
except ImportError:
    USE_CLOAK = False

# Try to import standard Playwright (universal fallback)
try:
    from playwright.async_api import async_playwright
    USE_PLAYWRIGHT = True
except ImportError:
    USE_PLAYWRIGHT = False

# Try to import nodriver (CDP-based stealth Chrome)
try:
    import nodriver as uc
    USE_NODRIVER = True
except ImportError:
    USE_NODRIVER = False


# ---- LAZY ENGINE BOOTSTRAP (install stealth engines on first use) --------
# The four engines (Camoufox -> CloakBrowser -> nodriver -> Playwright) are
# NOT pinned in requirements.txt. On a box with disk/RAM headroom we install
# them on demand the first time a browser macro runs, then degrade gracefully
# if any single engine cannot be installed. None of the stealth behaviour
# (humanized cursor, fingerprint, proxy) is touched -- this only makes the
# engine modules importable so the existing fallback chain can run.

_ENGINES_BOOTSTRAPPED = False


def _pip_install(*pkgs: str) -> bool:
    """pip install one or more packages; return True on success."""
    import subprocess
    import sys
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-input", "--no-cache-dir", *pkgs],
            check=True, capture_output=True, timeout=900,
        )
        return True
    except Exception as e:  # noqa: BLE001 -- tolerate any install failure
        logger.warning("pip install %s failed: %s", pkgs, e)
        return False


def _run_module(*args: str) -> bool:
    """Run `python -m <args>` (e.g. playwright install); return True on success."""
    import subprocess
    import sys
    try:
        subprocess.run(
            [sys.executable, "-m", *args],
            check=True, capture_output=True, timeout=900,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("python -m %s failed: %s", args, e)
        return False


def _reimport_engines() -> None:
    """Re-attempt engine imports and refresh module-level flags + bindings."""
    global AsyncCamoufox, cloak_launch, async_playwright, uc
    global USE_CAMOUFOX, USE_CLOAK, USE_PLAYWRIGHT, USE_NODRIVER
    try:
        from camoufox.async_api import AsyncCamoufox as _AC
        AsyncCamoufox = _AC
        USE_CAMOUFOX = True
    except Exception:  # noqa: BLE001
        pass
    try:
        from cloakbrowser import launch as _cl
        cloak_launch = _cl
        USE_CLOAK = True
    except Exception:  # noqa: BLE001
        pass
    try:
        from playwright.async_api import async_playwright as _ap
        async_playwright = _ap
        USE_PLAYWRIGHT = True
    except Exception:  # noqa: BLE001
        pass
    try:
        import nodriver as _uc
        uc = _uc
        USE_NODRIVER = True
    except Exception:  # noqa: BLE001
        pass


def ensure_browser_engines() -> None:
    """Lazily install at least one stealth browser engine on first use.

    Installs each engine of the designed fallback chain independently so a
    single unavailable package (e.g. a private CloakBrowser) does not abort
    the rest. Blocking by design (pip/playwright downloads); callers should
    run it in a worker thread to avoid stalling the event loop.
    """
    global _ENGINES_BOOTSTRAPPED
    if any([USE_CAMOUFOX, USE_CLOAK, USE_NODRIVER, USE_PLAYWRIGHT]):
        return  # an engine is already importable -- nothing to do
    if _ENGINES_BOOTSTRAPPED:
        return  # already attempted once this process -- don't reinstall every call
    _ENGINES_BOOTSTRAPPED = True
    logger.info("No browser engine present -- bootstrapping stealth engines (lazy install)...")

    # 1) Playwright + Chromium binary (most reliable universal fallback).
    if _pip_install("playwright"):
        _run_module("playwright", "install", "--with-deps", "chromium")

    # 2) nodriver (lightweight CDP stealth Chrome; reuses Chromium above).
    _pip_install("nodriver")

    # 3) Camoufox (stealth Firefox) + its browser binary.
    if _pip_install("camoufox[geoip]"):
        _run_module("camoufox", "fetch")

    # 4) CloakBrowser (may be private/unavailable -- tolerate failure).
    _pip_install("cloakbrowser")

    _reimport_engines()
    logger.info(
        "Engine bootstrap done (Camoufox=%s, CloakBrowser=%s, nodriver=%s, Playwright=%s)",
        USE_CAMOUFOX, USE_CLOAK, USE_NODRIVER, USE_PLAYWRIGHT,
    )


# ---- HUMANIZED MOUSE TRAJECTORY GENERATORS (BÉZIER CURVES & EASING) ----

def calculate_bezier_point(p0: tuple[float, float], p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float], t: float) -> tuple[float, float]:
    """Calculate coordinates along a Cubic Bézier curve at parameter t (0 <= t <= 1)."""
    x = (1 - t)**3 * p0[0] + 3 * (1 - t)**2 * t * p1[0] + 3 * (1 - t) * t**2 * p2[0] + t**3 * p3[0]
    y = (1 - t)**3 * p0[1] + 3 * (1 - t)**2 * t * p1[1] + 3 * (1 - t) * t**2 * p2[1] + t**3 * p3[1]
    return x, y


def ease_in_out_sine(t: float) -> float:
    """Sine-based easing function (slow start -> fast middle -> slow finish)."""
    return -(math.cos(math.pi * t) - 1) / 2


async def move_mouse_humanlike(page: Any, start_coords: tuple[float, float], target_coords: tuple[float, float], steps: int = 40):
    """Smoothly move Playwright mouse cursor using Cubic Bézier curves, easing, and micro-jitter."""
    start_x, start_y = start_coords
    target_x, target_y = target_coords
    
    # Generate randomized control points to curve the path slightly
    control_x1 = start_x + (target_x - start_x) * random.uniform(0.1, 0.4) + random.randint(-40, 40)
    control_y1 = start_y + (target_y - start_y) * random.uniform(0.1, 0.4) + random.randint(-40, 40)
    control_x2 = start_x + (target_x - start_x) * random.uniform(0.6, 0.9) + random.randint(-40, 40)
    control_y2 = start_y + (target_y - start_y) * random.uniform(0.6, 0.9) + random.randint(-40, 40)
    
    p0 = (start_x, start_y)
    p1 = (control_x1, control_y1)
    p2 = (control_x2, control_y2)
    p3 = (target_x, target_y)
    
    await page.mouse.move(start_x, start_y)
    
    for i in range(1, steps + 1):
        t_linear = i / steps
        t_eased = ease_in_out_sine(t_linear)
        
        x, y = calculate_bezier_point(p0, p1, p2, p3, t_eased)
        
        # Add micro-jitter (shakiness)
        if i < steps:
            x += random.uniform(-1.2, 1.2)
            y += random.uniform(-1.2, 1.2)
            
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.005, 0.015))


# ---- QUADRUPLE-FALLBACK BROWSER AUTOMATION ENGINE ----

# Default starting URL kept identical to the pre-refactor behaviour so a bare
# ``run_browser_macro`` with no leading ``goto`` still lands on chkr.cc.
DEFAULT_START_URL = "https://chkr.cc/"


def _resolve_profile(url: str) -> dict[str, Any]:
    """Resolve the per-domain stealth profile, hwproxy override and
    Dolphin-Anty-grade fingerprint for *url* — exactly the block that used to
    live inline at the top of :func:`run_browser_macro` (§0.5: everything is
    read from state/addons, nothing is passed in).

    Returns a dict with: seed, user_agent, proxy_url, proxy_dict, webgl_vendor,
    webgl_renderer, saved_login, saved_password, fp_profile.
    """
    try:
        from urllib.parse import urlparse
        import secrets

        netloc = urlparse(url).netloc or "default"
        if ":" in netloc:
            netloc = netloc.split(":")[0]

        from ..addons import state as addon_state
        profiles = addon_state.get("browser", "domain_profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}

        # --- Manual fingerprint refresh: drop cached domain profile on request.
        try:
            _force_refresh = bool(addon_state.get("browser", "refresh_fingerprint", False))
        except Exception:
            _force_refresh = False
        if _force_refresh and netloc in profiles:
            profiles.pop(netloc, None)
            addon_state.set_("browser", "domain_profiles", profiles)
            with contextlib.suppress(Exception):
                addon_state.set_("browser", "refresh_fingerprint", False)
            logger.info("Fingerprint refresh requested -> cleared cached profile for %s", netloc)

        if netloc in profiles:
            profile = profiles[netloc]
            logger.info("Loaded persistent browser profile for %s (seed: %s)", netloc, profile.get("seed"))
        else:
            webgl_vendors = ["Google Inc. (Intel)", "Google Inc. (NVIDIA)", "Apple Inc."]
            webgl_renderers = [
                "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0)",
                "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0)",
                "Apple GPU"
            ]
            idx = random.randint(0, len(webgl_vendors) - 1)
            user_agents = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ]
            profile = {
                "seed": f"seed_{netloc}_{secrets.token_hex(6)}",
                "user_agent": random.choice(user_agents),
                "proxy": addon_state.get("issuance", "fingerprint_proxy", ""),
                "webgl_vendor": webgl_vendors[idx],
                "webgl_renderer": webgl_renderers[idx],
                "login": "",
                "password": ""
            }
            profiles[netloc] = profile
            addon_state.set_("browser", "domain_profiles", profiles)
            logger.info("Created and saved new persistent browser profile for %s", netloc)

        seed = profile.get("seed", "default_seed")
        user_agent = profile.get("user_agent", "")
        proxy_url = profile.get("proxy", "")
        webgl_vendor = profile.get("webgl_vendor", "")
        webgl_renderer = profile.get("webgl_renderer", "")
        saved_login = profile.get("login", "")
        saved_password = profile.get("password", "")

        # --- Apply the active "Железо и прокси" (hwproxy) profile, if any.
        try:
            from ..addons import hwproxy as _hwproxy
            _act = _hwproxy.active_profile() or {}
            if _act:
                with contextlib.suppress(Exception):
                    _act = _hwproxy.derive_consistent(_act)
                if _act.get("seed"):
                    seed = str(_act["seed"])
                if _act.get("user_agent"):
                    user_agent = str(_act["user_agent"])
                if _act.get("webgl_vendor"):
                    webgl_vendor = str(_act["webgl_vendor"])
                if _act.get("webgl_renderer"):
                    webgl_renderer = str(_act["webgl_renderer"])
                _eff = ""
                with contextlib.suppress(Exception):
                    _eff = _hwproxy.effective_proxy(_act)
                if _eff:
                    proxy_url = _eff
                elif _act.get("proxy"):
                    proxy_url = str(_act["proxy"])
                logger.info(
                    "Applied active hwproxy profile '%s' to %s (seed=%s, proxy=%s)",
                    _act.get("name") or _act.get("id"), netloc, seed,
                    "set" if proxy_url else "none",
                )
        except Exception as _e:
            logger.warning("Could not apply active hwproxy profile: %s", _e)
    except Exception as e:
        logger.warning("Failed to load/create domain persistent profile: %s", e)
        seed = "default_stealth_seed"
        user_agent = ""
        proxy_url = ""
        webgl_vendor = ""
        webgl_renderer = ""
        saved_login = ""
        saved_password = ""

    # ---- Dolphin-Anty-grade fingerprint enrichment -------------------------
    fp_profile = None
    try:
        from ..addons import stealth_fp
        try:
            from ..addons import stealth as _stealth_toggle
            _stealth_on = _stealth_toggle.is_enabled()
        except Exception:  # noqa: BLE001
            _stealth_on = True
        if not _stealth_on:
            logger.info("Stealth fingerprint disabled by toggle — skipping enrichment")
            raise RuntimeError("stealth_disabled_by_toggle")
        _tz_override = ""
        _os_override = ""
        try:
            from ..addons.issuance import core as _ic
            _tz_override = _ic.get_fingerprint_timezone() or ""
            _os_override = _ic.get_fingerprint_os() or ""
        except Exception:  # noqa: BLE001
            pass
        fp_profile = stealth_fp.merge_into({
            "seed": seed,
            "user_agent": user_agent,
            "webgl_vendor": webgl_vendor,
            "webgl_renderer": webgl_renderer,
        }, timezone=_tz_override or None, os_hint=_os_override or None)
        user_agent = fp_profile.get("user_agent", user_agent)
        webgl_vendor = fp_profile.get("webgl_vendor", webgl_vendor)
        webgl_renderer = fp_profile.get("webgl_renderer", webgl_renderer)
        logger.info(
            "Stealth fingerprint ready (os=%s tz=%s screen=%sx%s mem=%s cores=%s)",
            fp_profile.get("os"), fp_profile.get("timezone"),
            fp_profile.get("screen_width"), fp_profile.get("screen_height"),
            fp_profile.get("device_memory"), fp_profile.get("hardware_concurrency"),
        )
    except Exception as _fp_e:  # noqa: BLE001
        logger.warning("stealth_fp unavailable, using legacy WebGL-only spoof: %s", _fp_e)

    proxy_dict = None
    if proxy_url and proxy_url.lower() != "no proxy":
        proxy_dict = {"server": proxy_url}

    return {
        "seed": seed,
        "user_agent": user_agent,
        "proxy_url": proxy_url,
        "proxy_dict": proxy_dict,
        "webgl_vendor": webgl_vendor,
        "webgl_renderer": webgl_renderer,
        "saved_login": saved_login,
        "saved_password": saved_password,
        "fp_profile": fp_profile,
    }


def _resolve_upload_path(val: str) -> Path | None:
    """Resolve an upload filename against the known attachment locations."""
    file_path = Path("session/attachments") / val
    if not file_path.exists():
        file_path = Path("session") / val
    if not file_path.exists():
        with contextlib.suppress(Exception):
            from ...config import DATA_DIR
            file_path = DATA_DIR / "attachments" / val
    return file_path if file_path.exists() else None


class BrowserSession:
    """A live browser + context that outlives a single page.

    Owns the launched engine and exposes the multi-tab / step-execution
    primitives the scenarios runner (ТЗ §7) needs. The step logic in
    :meth:`run_steps` is the *same* code that used to be inlined in
    ``run_browser_macro`` — both the nodriver and playwright branches — so
    behaviour is unchanged (§0.6). The caller owns the lifecycle and MUST
    call :meth:`close`.
    """

    def __init__(
        self,
        *,
        browser: Any,
        context: Any,
        playwright_instance: Any,
        browser_type: str,
        is_camoufox: bool,
        saved_login: str,
        saved_password: str,
    ) -> None:
        self._browser = browser
        self._context = context
        self._playwright_instance = playwright_instance
        self.browser_type = browser_type
        self._is_camoufox = is_camoufox
        self.saved_login = saved_login
        self.saved_password = saved_password
        self._first_page = True
        self._closed = False

    # ── multi-tab (§15.1) ────────────────────────────────────────────────
    async def new_page(self, url: str | None = None) -> Any:
        """Open a new tab in the SAME browser/context (shared cookies/session).

        nodriver → ``browser.get(url, new_tab=True)`` (plain ``browser.get`` for
        the first tab); playwright → ``context.new_page()``. When *url* is given
        the tab is navigated (domcontentloaded, no networkidle — §15.3) and a
        short settle keeps behaviour close to the old single-page engine.
        """
        if self.browser_type == "nodriver":
            target = url or "about:blank"
            if self._first_page:
                page = await self._browser.get(target)
                self._first_page = False
            else:
                page = await self._browser.get(target, new_tab=True)
            with contextlib.suppress(Exception):
                await page.activate()
            if url:
                with contextlib.suppress(Exception):
                    await page.sleep(2.0)
            return page
        # playwright-based engines
        page = await self._context.new_page()
        self._first_page = False
        if url:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(2.0)
        with contextlib.suppress(Exception):
            await page.bring_to_front()
        return page

    async def navigate(self, page: Any, url: str) -> None:
        """Navigate an EXISTING tab to *url* (§0.4: run_route drives goto, not
        the step engine). domcontentloaded, not networkidle (§15.3)."""
        if self.browser_type == "nodriver":
            navigated = False
            with contextlib.suppress(Exception):
                await page.get(url)
                navigated = True
            if not navigated:
                with contextlib.suppress(Exception):
                    await self._browser.get(url)
            with contextlib.suppress(Exception):
                await page.activate()
                await page.sleep(1.0)
        else:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            with contextlib.suppress(Exception):
                await page.bring_to_front()

    async def activate(self, page: Any) -> None:
        """Bring *page* to the foreground before a step (the engine branches
        assume they operate on the single active tab — §15.1.2)."""
        if self.browser_type == "nodriver":
            with contextlib.suppress(Exception):
                await page.activate()
        else:
            with contextlib.suppress(Exception):
                await page.bring_to_front()

    # ── step execution (both branches, unchanged behaviour) ──────────────
    async def run_steps(self, page: Any, steps: list[tuple[str, Any]], progress_cb=None) -> dict[str, Any]:
        """Execute a flat list of engine ``(op, val)`` tuples against *page*.

        Returns ``{"ok", "error", "copied_texts", "failed_steps"}``.
        ``copied_texts`` collects ``(val, text[:500])`` tuples for every
        ``copy`` op (§0.1). ``failed_steps`` is a non-breaking observability
        addition (§15.4): tuples ``(idx+1, op, reason)`` for click/type/upload
        steps that only logged a warning — behaviour is unchanged (the run
        still continues, ``ok`` stays True), but callers like the scenarios
        runner can now detect silent misses and retry/stop. ``progress_cb``
        is awaited as ``cb(idx+1, total, op)`` before each step (§15.8).
        """
        copied_texts: list[tuple[str, str]] = []
        failed_steps: list[tuple[int, str, str]] = []
        error = None
        # Make sure the tab is focused — the branches below were written for a
        # single always-active page.
        await self.activate(page)
        try:
            if self.browser_type == "nodriver":
                await self._run_steps_nodriver(page, steps, copied_texts, failed_steps, progress_cb)
            else:
                await self._run_steps_playwright(page, steps, copied_texts, failed_steps, progress_cb)
        except Exception as e:  # noqa: BLE001
            logger.exception("run_steps failed")
            error = str(e)
        return {"ok": error is None, "error": error, "copied_texts": copied_texts,
                "failed_steps": failed_steps}

    async def _run_steps_nodriver(self, page, steps, copied_texts, failed_steps, progress_cb) -> None:
        for idx, (op, val) in enumerate(steps):
            logger.info("Executing nodriver step %d: %s -> %s", idx + 1, op, val)
            if progress_cb:
                try:
                    await progress_cb(idx + 1, len(steps), op)
                except Exception:
                    pass

            if op == "click":
                clicked = False
                for selector in [val, "button", "a"]:
                    try:
                        if selector == val:
                            el = await page.select(val)
                        else:
                            el = await page.find(val, best_match=True)

                        if el:
                            await el.click()
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    logger.warning("Nodriver: Could not click element: %s", val)
                    failed_steps.append((idx + 1, op, f"элемент не найден: {val}"))

            elif op in ("type", "type_login", "type_password"):
                if op == "type_login":
                    text_to_type = self.saved_login
                    field = val
                elif op == "type_password":
                    text_to_type = self.saved_password
                    field = val
                else:
                    text_to_type, field = val
                typed = False
                for selector in [
                    f"input[placeholder*='{field}']",
                    f"textarea[placeholder*='{field}']",
                    f"input[name='{field}']",
                    f"input[id='{field}']",
                    field
                ]:
                    try:
                        el = await page.select(selector)
                        if el:
                            await el.send_keys(text_to_type)
                            typed = True
                            break
                    except Exception:
                        continue
                if not typed:
                    try:
                        el = await page.find(field, best_match=True)
                        if el:
                            await el.send_keys(text_to_type)
                            typed = True
                    except Exception:
                        pass
                if not typed:
                    logger.warning("Nodriver: Could not type into element: %s", field)
                    failed_steps.append((idx + 1, op, f"поле не найдено: {field}"))

            elif op == "upload":
                file_path = _resolve_upload_path(val)
                if file_path:
                    logger.info("Nodriver uploading file: %s", file_path)
                    try:
                        el = await page.select("input[type='file']")
                        if el:
                            await el.send_keys(str(file_path.resolve()))
                    except Exception as e:
                        logger.warning("Nodriver file upload failed: %s", e)
                else:
                    logger.warning("Nodriver file not found for upload: %s", val)

            elif op == "pause":
                jitter = random.uniform(-0.1 * val, 0.15 * val)
                await page.sleep(val + jitter)

            elif op == "copy":
                text_content = ""
                for selector in [val, ".results", "#results", "body"]:
                    try:
                        if selector == val:
                            el = await page.find(val, best_match=True)
                            if not el:
                                el = await page.select(val)
                        else:
                            el = await page.select(selector)

                        if el:
                            text_content = el.text
                            if text_content:
                                text_content = text_content.strip()
                                break
                    except Exception:
                        continue

                if text_content:
                    copied_texts.append((val, text_content[:500]))
                else:
                    copied_texts.append((val, "Элемент не найден или пуст"))

    async def _run_steps_playwright(self, page, steps, copied_texts, failed_steps, progress_cb) -> None:
        current_mouse_pos = (0, 0)
        for idx, (op, val) in enumerate(steps):
            logger.info("Executing step %d: %s -> %s", idx + 1, op, val)
            if progress_cb:
                try:
                    await progress_cb(idx + 1, len(steps), op)
                except Exception:
                    pass

            if op == "click":
                clicked = False
                for selector in [
                    f"text={val}",
                    f"button:has-text('{val}')",
                    f"a:has-text('{val}')",
                    val
                ]:
                    try:
                        loc = page.locator(selector).first
                        if await loc.is_visible():
                            box = await loc.bounding_box()
                            if box:
                                target_x = box["x"] + box["width"] / 2
                                target_y = box["y"] + box["height"] / 2
                                await move_mouse_humanlike(page, current_mouse_pos, (target_x, target_y))
                                current_mouse_pos = (target_x, target_y)
                                await page.mouse.click(target_x, target_y)
                            else:
                                await loc.click(timeout=5000)
                            clicked = True
                            break
                    except Exception:
                        continue

                if not clicked:
                    logger.warning("Could not click element with text/selector: %s", val)
                    failed_steps.append((idx + 1, op, f"элемент не найден: {val}"))

            elif op in ("type", "type_login", "type_password"):
                if op == "type_login":
                    text_to_type = self.saved_login
                    field = val
                elif op == "type_password":
                    text_to_type = self.saved_password
                    field = val
                else:
                    text_to_type, field = val
                typed = False
                for selector in [
                    f"input[placeholder*='{field}']",
                    f"textarea[placeholder*='{field}']",
                    f"input[name='{field}']",
                    f"input[id='{field}']",
                    f"text={field}",
                    field
                ]:
                    try:
                        loc = page.locator(selector).first
                        if await loc.is_visible():
                            await loc.focus()
                            for char in text_to_type:
                                await page.keyboard.type(char)
                                await asyncio.sleep(random.uniform(0.05, 0.15))
                            typed = True
                            break
                    except Exception:
                        continue
                if not typed:
                    logger.warning("Could not type into element: %s", field)
                    failed_steps.append((idx + 1, op, f"поле не найдено: {field}"))

            elif op == "upload":
                file_path = _resolve_upload_path(val)
                if file_path:
                    logger.info("Uploading file: %s", file_path)
                    try:
                        await page.locator("input[type='file']").first.set_input_files(str(file_path.resolve()), timeout=10000)
                    except Exception as e:
                        logger.warning("File upload failed: %s", e)
                else:
                    logger.warning("File not found for upload: %s", val)

            elif op == "pause":
                jitter = random.uniform(-0.1 * val, 0.15 * val)
                await asyncio.sleep(val + jitter)

            elif op == "copy":
                text_content = ""
                for selector in [
                    f"text={val}",
                    val,
                    ".results",
                    "#results",
                    "body"
                ]:
                    try:
                        loc = page.locator(selector).first
                        if await loc.is_visible():
                            text_content = await loc.text_content()
                            if text_content:
                                text_content = text_content.strip()
                                break
                    except Exception:
                        continue

                if text_content:
                    copied_texts.append((val, text_content[:500]))
                else:
                    copied_texts.append((val, "Элемент не найден или пуст"))

    # ── screenshots (§8) ─────────────────────────────────────────────────
    async def screenshot(self, page: Any, path: str) -> str | None:
        """Save a screenshot of *page* by engine and return the path (or None)."""
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            if self.browser_type == "nodriver":
                await page.save_screenshot(path)
            else:
                await page.screenshot(path=path, full_page=True)
            return path if os.path.exists(path) else None
        except Exception as e:  # noqa: BLE001
            logger.warning("screenshot failed: %s", e)
            return None

    # ── session persistence (§15.7) ──────────────────────────────────────
    @property
    def supports_session_persist(self) -> bool:
        """Only the Playwright-based engines expose a context that can dump
        storage_state; nodriver cannot (§15.7)."""
        return self._context is not None

    async def storage_state(self, path: str) -> bool:
        if self._context is None:
            logger.warning("storage_state: engine %s has no context — session not saved", self.browser_type)
            return False
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            await self._context.storage_state(path=path)
            logger.info("Saved browser session to %s", path)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to save session storage_state: %s", e)
            return False

    # ── lifecycle ────────────────────────────────────────────────────────
    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.browser_type == "nodriver":
                with contextlib.suppress(Exception):
                    await self._browser.stop()
                return
            if self._is_camoufox and hasattr(self._browser, "__aexit__"):
                with contextlib.suppress(Exception):
                    await self._browser.__aexit__(None, None, None)
            else:
                if self._context is not None:
                    with contextlib.suppress(Exception):
                        await self._context.close()
                if self._browser is not None:
                    with contextlib.suppress(Exception):
                        await self._browser.close()
            if self._playwright_instance is not None:
                with contextlib.suppress(Exception):
                    await self._playwright_instance.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning("BrowserSession.close error: %s", e)


async def open_browser_session(*, session_state_path: str | None = None, url: str | None = None) -> BrowserSession:
    """Boot a stealth browser + context and return a :class:`BrowserSession`.

    All stealth settings (domain_profiles, hwproxy, stealth_fp, антидетект
    toggle, tz/os overrides, proxy, storage_state) are resolved INSIDE from
    state/addons (§0.5) — the only external parameters are ``session_state_path``
    (Playwright-based session load/save) and ``url`` (used solely to pick the
    per-domain fingerprint profile; defaults to the legacy chkr.cc profile).

    No page is opened here — call :meth:`BrowserSession.new_page`. The caller
    owns the lifecycle (§0.6): it must call :meth:`BrowserSession.close`.
    """
    profile_url = url or DEFAULT_START_URL

    # Lazily install the stealth engines on first use if none is present.
    if not any([USE_CAMOUFOX, USE_CLOAK, USE_NODRIVER, USE_PLAYWRIGHT]):
        await asyncio.to_thread(ensure_browser_engines)

    storage_state_load = (
        session_state_path
        if (session_state_path and os.path.exists(session_state_path))
        else None
    )

    cfg = _resolve_profile(profile_url)
    seed = cfg["seed"]
    user_agent = cfg["user_agent"]
    proxy_url = cfg["proxy_url"]
    proxy_dict = cfg["proxy_dict"]
    webgl_vendor = cfg["webgl_vendor"]
    webgl_renderer = cfg["webgl_renderer"]
    fp_profile = cfg["fp_profile"]

    logger.info(
        "Opening browser session for %s (Camoufox=%s, CloakBrowser=%s, nodriver=%s, Playwright=%s)",
        profile_url, USE_CAMOUFOX, USE_CLOAK, USE_NODRIVER, USE_PLAYWRIGHT,
    )

    browser = None
    context = None
    playwright_instance = None
    browser_type = None
    is_camoufox = False

    # ---- FALLBACK 1: CAMOUFOX (Stealth Firefox) ----
    if USE_CAMOUFOX:
        try:
            logger.info("Launching Camoufox (Stealth Firefox) with seed: %s, proxy: %s", seed, proxy_url)
            custom_config = {}
            if fp_profile is not None:
                try:
                    from ..addons import stealth_fp
                    custom_config.update(stealth_fp.camoufox_config(fp_profile))
                except Exception as _cfg_e:  # noqa: BLE001
                    logger.warning("Camoufox stealth config skipped: %s", _cfg_e)
            if user_agent:
                custom_config["navigator.userAgent"] = user_agent
                custom_config["headers.User-Agent"] = user_agent
            if webgl_vendor:
                custom_config["webGl:unmaskedVendor"] = webgl_vendor
            if webgl_renderer:
                custom_config["webGl:unmaskedRenderer"] = webgl_renderer

            browser = AsyncCamoufox(
                headless=True,
                geoip=True if proxy_dict else False,
                proxy=proxy_dict,
                config=custom_config,
                i_know_what_im_doing=True if custom_config else False
            )
            ctx = await browser.__aenter__()
            is_camoufox = True
            try:
                if storage_state_load:
                    context = await ctx.new_context(storage_state=storage_state_load)
                    logger.info("Camoufox: loaded saved session from %s", storage_state_load)
                else:
                    context = await ctx.new_context()
            except Exception as _ctx_e:  # noqa: BLE001
                logger.warning("Camoufox new_context failed, session disabled this run: %s", _ctx_e)
                # Fall back to using the raw context object as the page factory.
                context = ctx
            browser_type = "playwright"
        except Exception as e:
            logger.warning("Camoufox launch failed, falling back: %s", e)
            browser = None
            context = None
            is_camoufox = False

    # ---- FALLBACK 2: CLOAKBROWSER (Stealth Chromium) ----
    if not browser and USE_CLOAK:
        try:
            logger.info("Launching CloakBrowser (Stealth Chromium) with seed: %s, proxy: %s", seed, proxy_url)
            proxy_arg = {}
            if proxy_url and proxy_url.lower() != "no proxy":
                proxy_arg["proxy"] = proxy_url

            browser = await cloak_launch(
                headless=True,
                args=[f"--fingerprint={seed}"],
                **proxy_arg
            )

            context_args = {}
            if user_agent:
                context_args["user_agent"] = user_agent
            if storage_state_load:
                context_args["storage_state"] = storage_state_load
            context = await browser.new_context(**context_args)
            browser_type = "playwright"
        except Exception as e:
            logger.warning("CloakBrowser launch failed, falling back: %s", e)
            browser = None
            context = None

    # ---- FALLBACK 3: NODRIVER (CDP-based stealth Chrome) ----
    if not browser and USE_NODRIVER:
        try:
            logger.info("Launching nodriver (Stealth Chrome) with proxy: %s", proxy_url)
            browser_args = []
            if proxy_url and proxy_url.lower() != "no proxy":
                browser_args.append(f"--proxy-server={proxy_url}")

            browser = await uc.start(
                headless=True,
                browser_args=browser_args
            )
            browser_type = "nodriver"
            context = None
            if storage_state_load:
                logger.warning(
                    "nodriver engine: Playwright storage_state не поддерживается — "
                    "сохранённая сессия НЕ загружена. Вход надо будет выполнить заново."
                )
        except Exception as e:
            logger.warning("nodriver launch failed, falling back: %s", e)
            browser = None

    # ---- FALLBACK 4: STANDARD PLAYWRIGHT (Universal Fallback) ----
    if not browser and USE_PLAYWRIGHT:
        logger.info("Launching standard Playwright (Universal Fallback)...")
        playwright_instance = await async_playwright().start()

        pw_proxy = None
        if proxy_url and proxy_url.lower() != "no proxy":
            pw_proxy = {"server": proxy_url}

        browser = await playwright_instance.chromium.launch(headless=True, proxy=pw_proxy)

        context_args = {}
        if user_agent:
            context_args["user_agent"] = user_agent
        if storage_state_load:
            context_args["storage_state"] = storage_state_load
        if fp_profile is not None:
            try:
                from ..addons import stealth_fp
                context_args.update(stealth_fp.context_overrides(fp_profile))
            except Exception as _ov_e:  # noqa: BLE001
                logger.warning("stealth context overrides skipped: %s", _ov_e)
        context = await browser.new_context(**context_args)

        if fp_profile is not None:
            try:
                from ..addons import stealth_fp
                await context.add_init_script(stealth_fp.build_init_script(fp_profile))
                logger.info("Injected Dolphin-Anty-grade stealth fingerprint")
            except Exception as _inj_e:  # noqa: BLE001
                logger.warning("stealth init-script injection failed: %s", _inj_e)
        elif webgl_vendor and webgl_renderer:
            logger.info("Injecting prototype-level WebGL spoof: Vendor='%s', Renderer='%s'", webgl_vendor, webgl_renderer)
            webgl_script = f"""
            (function () {{
                const spoofedVendor = "{webgl_vendor}";
                const spoofedRenderer = "{webgl_renderer}";
                
                const getParameterProxyHandler = {{
                    apply: function(target, thisArg, args) {{
                        const param = args[0];
                        if (param === 37445) return spoofedVendor; // UNMASKED_VENDOR_WEBGL
                        if (param === 37446) return spoofedRenderer; // UNMASKED_RENDERER_WEBGL
                        return Reflect.apply(target, thisArg, args);
                    }}
                }};

                if (window.WebGLRenderingContext) {{
                    const originalGetParameter = WebGLRenderingContext.prototype.getParameter;
                    WebGLRenderingContext.prototype.getParameter = new Proxy(originalGetParameter, getParameterProxyHandler);
                }}
                if (window.WebGL2RenderingContext) {{
                    const originalGetParameter2 = WebGL2RenderingContext.prototype.getParameter;
                    WebGL2RenderingContext.prototype.getParameter = new Proxy(originalGetParameter2, getParameterProxyHandler);
                }}
            }})();
            """
            await context.add_init_script(webgl_script)

        browser_type = "playwright"

    if not browser:
        raise RuntimeError("No browser automation engine could be loaded.")

    return BrowserSession(
        browser=browser,
        context=context,
        playwright_instance=playwright_instance,
        browser_type=browser_type,
        is_camoufox=is_camoufox,
        saved_login=cfg["saved_login"],
        saved_password=cfg["saved_password"],
    )


async def run_browser_macro(steps: list[tuple[str, Any]], screenshot_path: str = "session/screenshot.png", progress_cb=None, session_state_path: str | None = None) -> dict[str, Any]:
    """Execute browser automation steps using the best available stealth engine with fallback handlers.

    Backward-compatible wrapper (§13.1): same signature and return shape as
    before, now implemented on top of :func:`open_browser_session` /
    :class:`BrowserSession`. Optional extras (both additive, off by default):

      * ``progress_cb`` — async ``cb(step_index, total_steps, op)`` before each step.
      * ``session_state_path`` — JSON file to load/save the browser session
        (cookies + localStorage) for the Playwright-based engines.
    """
    error = None
    browser_type = None
    copied_texts: list[tuple[str, str]] = []

    # Starting URL - default to chkr.cc but can be overridden by a leading "goto".
    url = DEFAULT_START_URL
    if steps and steps[0][0] == "goto":
        url = steps[0][1]
        steps = steps[1:]

    session = None
    page = None
    try:
        session = await open_browser_session(session_state_path=session_state_path, url=url)
        browser_type = session.browser_type
        page = await session.new_page(url=url)

        result = await session.run_steps(page, steps, progress_cb=progress_cb)
        copied_texts = result.get("copied_texts") or []
        if result.get("error"):
            error = result["error"]

        await session.screenshot(page, screenshot_path)

        if session_state_path:
            await session.storage_state(session_state_path)
    except Exception as e:
        logger.exception("Browser automation failed")
        error = str(e)
        if session is not None and page is not None:
            with contextlib.suppress(Exception):
                await session.screenshot(page, screenshot_path)
    finally:
        if session is not None:
            await session.close()

    return {
        "ok": error is None,
        "error": error,
        "browser_type": browser_type,
        "copied_texts": copied_texts,
        "screenshot": screenshot_path if os.path.exists(screenshot_path) else None
    }
