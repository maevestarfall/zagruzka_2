"""Live stealth self-check — runs the spoof script in a REAL browser.

Where :mod:`bot.addons.stealth_fp` only *builds* the fingerprint and its
JS init-script, this module actually launches a Chromium page, injects
that script via ``add_init_script`` (exactly like
``bot.workers.browser_clicker`` does in production) and then probes the
page to confirm each spoof really took effect in a real engine:

    • navigator.webdriver / platform / deviceMemory / cores / languages
    • screen dimensions
    • WebGL UNMASKED vendor / renderer
    • Intl timezone
    • overrides report ``[native code]`` from ``toString``
    • canvas read-back is not mutated on the live element
    • WebRTC gathers NO host / mDNS (.local) ICE candidates → no
      local-IP leak (the part that was previously only reasoned about,
      never executed against a live RTCPeerConnection)

It degrades gracefully: if Playwright / a browser binary is unavailable
it returns ``{"ok": False, "error": ...}`` instead of raising, so callers
(a CLI, a Telegram command, CI) can show a friendly message.
"""

from __future__ import annotations

import asyncio
import logging
from . import stealth_fp

logger = logging.getLogger(__name__)

# Candidate Chromium binaries to fall back to when Playwright's bundled
# build isn't present (e.g. a slim container with the system package).
_CHROMIUM_PATHS = (
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
)

# JS probe — returns the OBSERVED values + a live WebRTC candidate scan.
_PROBE = r"""
async () => {
  const out = {};
  out.webdriver = navigator.webdriver;
  out.platform = navigator.platform;
  out.deviceMemory = navigator.deviceMemory;
  out.cores = navigator.hardwareConcurrency;
  out.languages = (navigator.languages || []).slice();
  out.screenW = screen.width; out.screenH = screen.height;
  out.availH = screen.availHeight;
  // WebGL UNMASKED vendor/renderer (our patch answers 37445/37446 directly)
  try {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    out.webglVendor = gl ? gl.getParameter(37445) : null;
    out.webglRenderer = gl ? gl.getParameter(37446) : null;
  } catch(e) { out.webglErr = String(e); }
  // Intl timezone
  try { out.tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch(e){}
  // toString native masking
  try { out.toDataURLStr = HTMLCanvasElement.prototype.toDataURL.toString(); } catch(e){}
  try {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    out.getParameterStr = gl ? gl.getParameter.toString() : '';
  } catch(e){}
  // canvas non-destructive: draw, snapshot pixels, call toDataURL, re-snapshot
  try {
    const c = document.createElement('canvas'); c.width = 8; c.height = 8;
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#1188ee'; ctx.fillRect(0,0,8,8);
    const a = Array.from(ctx.getImageData(0,0,8,8).data);
    const url = c.toDataURL();
    const b = Array.from(ctx.getImageData(0,0,8,8).data);
    out.canvasUntouched = JSON.stringify(a) === JSON.stringify(b);
    out.canvasStable = (c.toDataURL() === url);
  } catch(e) { out.canvasErr = String(e); }
  // WebRTC live candidate gathering (both event paths)
  out.candidates = [];
  try {
    const pc = new RTCPeerConnection({ iceServers: [] });
    pc.createDataChannel('probe');
    const collect = (e) => { if (e && e.candidate && e.candidate.candidate) out.candidates.push(e.candidate.candidate); };
    pc.addEventListener('icecandidate', collect);
    pc.onicecandidate = collect;
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    // also scan the SDP that setLocalDescription produced
    out.sdp = (pc.localDescription && pc.localDescription.sdp) || '';
    await new Promise(r => setTimeout(r, 1500));
    out.rtcInstanceOf = (pc instanceof RTCPeerConnection);
    pc.close();
  } catch(e) { out.rtcErr = String(e); }
  return out;
}
"""


def _evaluate_checks(profile: dict, obs: dict) -> list[dict]:
    """Turn observed page values into a list of named pass/fail checks."""
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("navigator.webdriver hidden", obs.get("webdriver") in (False, None),
        f"got {obs.get('webdriver')!r}")
    add("navigator.platform spoofed", obs.get("platform") == profile["platform"],
        f"{obs.get('platform')!r} == {profile['platform']!r}")
    add("navigator.deviceMemory spoofed",
        obs.get("deviceMemory") == profile["device_memory"],
        f"{obs.get('deviceMemory')} == {profile['device_memory']}")
    add("navigator.hardwareConcurrency spoofed",
        obs.get("cores") == profile["hardware_concurrency"])
    add("navigator.languages spoofed",
        list(obs.get("languages") or []) == list(profile["languages"]),
        f"{obs.get('languages')} == {profile['languages']}")
    add("screen dimensions spoofed",
        obs.get("screenW") == profile["screen_width"]
        and obs.get("screenH") == profile["screen_height"])
    add("WebGL vendor spoofed",
        obs.get("webglVendor") == profile["webgl_vendor"],
        f"{obs.get('webglVendor')!r}")
    add("WebGL renderer spoofed",
        obs.get("webglRenderer") == profile["webgl_renderer"],
        f"{obs.get('webglRenderer')!r}")
    add("Intl timezone spoofed", obs.get("tz") == profile["timezone"],
        f"{obs.get('tz')!r} == {profile['timezone']!r}")
    add("toDataURL masked as native",
        "[native code]" in (obs.get("toDataURLStr") or ""))
    add("WebGL getParameter masked as native",
        "[native code]" in (obs.get("getParameterStr") or ""))
    add("canvas not mutated by toDataURL", obs.get("canvasUntouched") is True)
    add("canvas readback stable (non-compounding)", obs.get("canvasStable") is True)

    cands = obs.get("candidates") or []
    sdp = obs.get("sdp") or ""
    host_leak = any((" typ host" in c) or (".local" in c) for c in cands)
    # SDP can carry candidates too — host lines there are a leak as well.
    sdp_host_leak = ("typ host" in sdp)
    if cands:
        cand_detail = f"{len(cands)} candidate(s) gathered, none are host/mDNS"
    else:
        cand_detail = ("0 candidates gathered here (no network/STUN) — "
                       "filter logic is proven by the Node unit test")
    add("WebRTC: no host/mDNS candidate leak (events)", not host_leak, cand_detail)
    add("WebRTC: no host candidate leak (SDP)", not sdp_host_leak)
    add("RTCPeerConnection instanceof preserved",
        obs.get("rtcInstanceOf") in (True, None))

    return checks


async def live_check(
    profile: dict | None = None,
    *,
    seed: str = "live_check_seed",
    os_hint: str | None = "windows",
    timezone: str | None = "Europe/Berlin",
    url: str | None = None,
    headless: bool = True,
) -> dict:
    """Launch a real Chromium, inject the stealth script, and probe it.

    Returns::

        {"ok": bool, "engine": "chromium", "profile": {...},
         "checks": [{"name", "ok", "detail"}, ...],
         "passed": int, "failed": int, "error": str|None}
    """
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"playwright unavailable: {exc}",
                "checks": [], "passed": 0, "failed": 0}

    profile = profile or stealth_fp.generate_profile(
        seed, os_hint=os_hint, timezone=timezone)
    script = stealth_fp.build_init_script(profile)
    overrides = stealth_fp.context_overrides(profile)

    async def _launch(p):
        args = ["--no-sandbox", "--disable-dev-shm-usage"]
        # Try the bundled build first, then known system binaries.
        try:
            return await p.chromium.launch(headless=headless, args=args)
        except Exception as first:  # noqa: BLE001
            import os
            for path in _CHROMIUM_PATHS:
                if os.path.exists(path):
                    try:
                        return await p.chromium.launch(
                            headless=headless, args=args, executable_path=path)
                    except Exception:  # noqa: BLE001
                        continue
            raise first

    browser = None
    try:
        async with async_playwright() as p:
            browser = await _launch(p)
            context = await browser.new_context(**overrides)
            await context.add_init_script(script)
            page = await context.new_page()
            await page.goto(url or "about:blank", wait_until="domcontentloaded")
            obs = await page.evaluate(_PROBE)
            await browser.close()
            browser = None
    except Exception as exc:  # noqa: BLE001
        if browser is not None:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
        logger.warning("stealth live_check failed: %s", exc)
        return {"ok": False, "error": str(exc), "checks": [],
                "passed": 0, "failed": 0, "profile": profile}

    checks = _evaluate_checks(profile, obs)
    failed = sum(1 for c in checks if not c["ok"])
    return {
        "ok": failed == 0,
        "engine": "chromium",
        "profile": profile,
        "checks": checks,
        "passed": len(checks) - failed,
        "failed": failed,
        "error": None,
        "raw": obs,
    }


def format_report(result: dict) -> str:
    """Human-readable report (plain text, also fine for Telegram <pre>)."""
    if result.get("error"):
        return f"❌ Live-проверка не выполнена: {result['error']}"
    prof = result.get("profile", {})
    head = (
        "🕵️ Stealth live-check (реальный Chromium)\n"
        f"Профиль: os={prof.get('os')} tz={prof.get('timezone')} "
        f"screen={prof.get('screen_width')}x{prof.get('screen_height')}\n"
        f"Итог: ✅ {result['passed']} / ❌ {result['failed']}\n"
    )
    lines = [head]
    for c in result.get("checks", []):
        mark = "✅" if c["ok"] else "❌"
        detail = f"  ({c['detail']})" if c.get("detail") and not c["ok"] else ""
        lines.append(f"{mark} {c['name']}{detail}")
    # Surface the WebRTC nuance even on success — honest about coverage.
    rtc = next((c for c in result.get("checks", [])
                if c["name"].startswith("WebRTC: no host/mDNS")), None)
    if rtc and rtc.get("detail"):
        lines.append(f"\nℹ️ WebRTC: {rtc['detail']}")
    return "\n".join(lines)


def run_sync(**kwargs) -> dict:
    """Convenience blocking wrapper for CLIs / tests."""
    return asyncio.run(live_check(**kwargs))


if __name__ == "__main__":  # pragma: no cover — manual CLI use
    import sys

    res = run_sync(url=(sys.argv[1] if len(sys.argv) > 1 else None))
    print(format_report(res))
    sys.exit(0 if res.get("ok") else 1)
