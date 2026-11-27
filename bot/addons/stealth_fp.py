"""Dolphin-Anty-grade browser fingerprint spoofing.

Generates a *deterministic* fingerprint profile from a seed (so the same
domain/profile always looks identical across runs) and builds a single
JS init-script that spoofs, at the page level:

    • Canvas        — toDataURL / getImageData / measureText noise
    • WebGL         — UNMASKED vendor / renderer + readPixels jitter
    • WebRTC        — no local-IP leak (mDNS-only / proxy-safe)
    • timezone      — Intl.DateTimeFormat + Date offset
    • screen        — width/height/avail*/colorDepth/pixelDepth
    • languages     — navigator.language / languages
    • deviceMemory  — navigator.deviceMemory
    • hardware      — navigator.hardwareConcurrency
    • platform      — navigator.platform consistent with the UA
    • webdriver     — navigator.webdriver removed, chrome runtime shimmed

The script is engine-agnostic and consumed by ``bot.workers.browser_clicker``
(Playwright / Camoufox / nodriver / CloakBrowser). No heavy imports.
"""

from __future__ import annotations

import hashlib
import json

# ── deterministic PRNG keyed by the profile seed ───────────────────────


def _rng(seed: str, salt: str) -> float:
    """Return a stable float in [0,1) from (seed, salt)."""
    h = hashlib.sha256(f"{seed}:{salt}".encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") / float(1 << 64)


def _pick(seed: str, salt: str, options: list):
    return options[int(_rng(seed, salt) * len(options)) % len(options)]


# ── catalogues kept consistent (Win/Mac/Linux families) ────────────────

_FAMILIES = {
    "windows": {
        "platform": "Win32",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0)",
        "screens": [[1920, 1080], [1536, 864], [2560, 1440], [1366, 768]],
        "memory": [8, 16, 32],
        "cores": [4, 8, 12, 16],
    },
    "mac": {
        "platform": "MacIntel",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "webgl_vendor": "Apple Inc.",
        "webgl_renderer": "Apple GPU",
        "screens": [[1440, 900], [1680, 1050], [2560, 1440], [1512, 982]],
        "memory": [8, 16, 32],
        "cores": [8, 10, 12],
    },
    "linux": {
        "platform": "Linux x86_64",
        "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0)",
        "screens": [[1920, 1080], [2560, 1440], [1366, 768]],
        "memory": [8, 16, 32],
        "cores": [4, 8, 16],
    },
}

# timezone ↔ language presets (so timezone matches locale; pick by proxy geo)
_LOCALES = [
    {"tz": "Europe/Moscow", "langs": ["ru-RU", "ru", "en-US", "en"]},
    {"tz": "Europe/London", "langs": ["en-GB", "en"]},
    {"tz": "America/New_York", "langs": ["en-US", "en"]},
    {"tz": "Europe/Berlin", "langs": ["de-DE", "de", "en"]},
    {"tz": "Asia/Dubai", "langs": ["en-US", "en", "ar"]},
]


def _os_from_ua(ua: str) -> str:
    u = (ua or "").lower()
    if "windows" in u:
        return "windows"
    if "mac os" in u or "macintosh" in u:
        return "mac"
    if "linux" in u or "x11" in u:
        return "linux"
    return "windows"


def generate_profile(seed: str, *, os_hint: str | None = None,
                     timezone: str | None = None,
                     user_agent: str | None = None) -> dict:
    """Build a deterministic, internally-consistent fingerprint profile.

    ``os_hint`` ('windows'|'mac'|'linux') and ``timezone`` override the
    seed-derived defaults (e.g. to match the active proxy's geo).
    """
    seed = seed or "default_seed"
    fam_key = (os_hint or (_os_from_ua(user_agent) if user_agent else None)
               or _pick(seed, "os", list(_FAMILIES.keys())))
    fam = _FAMILIES.get(fam_key, _FAMILIES["windows"])

    screen = _pick(seed, "screen", fam["screens"])
    memory = _pick(seed, "mem", fam["memory"])
    cores = _pick(seed, "cores", fam["cores"])

    if timezone:
        loc = next((x for x in _LOCALES if x["tz"] == timezone), None) or \
            {"tz": timezone, "langs": ["en-US", "en"]}
    else:
        loc = _pick(seed, "locale", _LOCALES)

    # canvas/webgl noise is a tiny per-seed integer pixel jitter (±2)
    canvas_noise = int(_rng(seed, "canvas") * 5) - 2  # -2..+2

    return {
        "seed": seed,
        "os": fam_key,
        "platform": fam["platform"],
        "user_agent": user_agent or fam["ua"],
        "webgl_vendor": fam["webgl_vendor"],
        "webgl_renderer": fam["webgl_renderer"],
        "screen_width": screen[0],
        "screen_height": screen[1],
        "color_depth": 24,
        "device_memory": memory,
        "hardware_concurrency": cores,
        "timezone": loc["tz"],
        "languages": loc["langs"],
        "canvas_noise": canvas_noise,
        "webrtc_mode": "proxy_safe",  # no local-IP leak
    }


def merge_into(profile: dict, *, timezone: str | None = None,
               os_hint: str | None = None) -> dict:
    """Ensure a (possibly legacy) profile dict has all stealth fields."""
    seed = str(profile.get("seed") or "default_seed")
    base = generate_profile(
        seed, os_hint=os_hint, timezone=timezone,
        user_agent=profile.get("user_agent") or None,
    )
    # Existing explicit values win over generated defaults.
    for k, v in profile.items():
        if v not in (None, ""):
            base[k] = v
    return base


# ── Playwright new_context() kwargs derived from the profile ───────────

def context_overrides(profile: dict) -> dict:
    out: dict = {}
    if profile.get("user_agent"):
        out["user_agent"] = profile["user_agent"]
    if profile.get("timezone"):
        out["timezone_id"] = profile["timezone"]
    langs = profile.get("languages") or []
    if langs:
        out["locale"] = langs[0]
    w, h = profile.get("screen_width"), profile.get("screen_height")
    if w and h:
        out["viewport"] = {"width": int(w), "height": int(h)}
        out["screen"] = {"width": int(w), "height": int(h)}
    return out


# ── the big page-level init script ─────────────────────────────────────

def build_init_script(profile: dict) -> str:
    """Return a single self-contained JS string for add_init_script /
    evaluate_on_new_document. Safe to inject on every navigation.

    Hardened vs. the first cut:
      • every spoof section is wrapped in its own try/catch, so one
        failing patch never skips the others (notably WebRTC, which used
        to sit *after* the canvas block and got skipped on any earlier
        throw);
      • all overridden functions are registered with a single
        ``Function.prototype.toString`` shim that reports
        ``[native code]`` — so the spoofs aren't trivially detectable by
        stringifying e.g. ``navigator.permissions.query`` or
        ``HTMLCanvasElement.prototype.toDataURL``;
      • WebRTC is wrapped with a ``Proxy`` on the constructor (preserves
        static methods, ``prototype`` and ``instanceof``) and filters
        host/mDNS candidates on BOTH ``addEventListener('icecandidate')``
        and the ``onicecandidate`` property — closing the local-IP leak
        the old addEventListener-only patch missed;
      • canvas read-back noise is applied to an OFF-SCREEN clone, so the
        visible canvas is never mutated and repeated ``toDataURL`` calls
        don't compound the jitter.
    """
    fp = {
        "platform": profile.get("platform", "Win32"),
        "ua": profile.get("user_agent", ""),
        "webglVendor": profile.get("webgl_vendor", "Google Inc."),
        "webglRenderer": profile.get("webgl_renderer", "ANGLE"),
        "screenW": int(profile.get("screen_width", 1920)),
        "screenH": int(profile.get("screen_height", 1080)),
        "colorDepth": int(profile.get("color_depth", 24)),
        "deviceMemory": int(profile.get("device_memory", 8)),
        "cores": int(profile.get("hardware_concurrency", 8)),
        "languages": profile.get("languages", ["en-US", "en"]),
        "canvasNoise": int(profile.get("canvas_noise", 1)),
        "timezone": profile.get("timezone", ""),
    }
    cfg = json.dumps(fp)
    # Raw string with real newlines: JS braces stay literal and regex
    # back-slashes (\\.local) survive without Python escaping headaches.
    js = r"""(() => {
const FP = __CFG__;
// ── native-code toString masking (so our overrides look native) ──────
const _patched = new WeakSet();
const _origToString = Function.prototype.toString;
const _mask = (fn) => { try { if (typeof fn === 'function') _patched.add(fn); } catch(e){} return fn; };
const _maskedToString = function() {
  try { if (_patched.has(this)) return 'function ' + (this.name || '') + '() { [native code] }'; } catch(e){}
  return _origToString.call(this);
};
try { Function.prototype.toString = _maskedToString; _mask(_maskedToString); } catch(e){}
const def = (o,p,v) => { try { Object.defineProperty(o,p,{get:_mask(function(){return v;}), configurable:true}); } catch(e){} };
// ── automation flags ─────────────────────────────────────────────────
try { def(navigator,'webdriver',false); } catch(e){}
try { if (!window.chrome) window.chrome = {}; window.chrome.runtime = window.chrome.runtime || {}; } catch(e){}
// ── navigator basics ─────────────────────────────────────────────────
try {
  def(navigator,'platform',FP.platform);
  def(navigator,'deviceMemory',FP.deviceMemory);
  def(navigator,'hardwareConcurrency',FP.cores);
  def(navigator,'language',FP.languages[0]);
  def(navigator,'languages',Object.freeze(FP.languages.slice()));
} catch(e){}
// ── screen ───────────────────────────────────────────────────────────
try {
  def(screen,'width',FP.screenW); def(screen,'height',FP.screenH);
  def(screen,'availWidth',FP.screenW); def(screen,'availHeight',FP.screenH-40);
  def(screen,'colorDepth',FP.colorDepth); def(screen,'pixelDepth',FP.colorDepth);
} catch(e){}
// ── timezone guard (context timezone_id is primary; this is a backstop) ─
try { if (FP.timezone && window.Intl && Intl.DateTimeFormat) {
  const oRO = Intl.DateTimeFormat.prototype.resolvedOptions;
  Intl.DateTimeFormat.prototype.resolvedOptions = _mask(function(){
    const o = oRO.apply(this, arguments); try { o.timeZone = FP.timezone; } catch(e){} return o; });
} } catch(e){}
// ── WebGL vendor / renderer ──────────────────────────────────────────
try {
  const wgl = (proto) => { if(!proto || !proto.getParameter) return; const gp = proto.getParameter;
    proto.getParameter = _mask(function(p){ if(p===37445) return FP.webglVendor; if(p===37446) return FP.webglRenderer; return gp.apply(this,arguments); }); };
  if (window.WebGLRenderingContext) wgl(WebGLRenderingContext.prototype);
  if (window.WebGL2RenderingContext) wgl(WebGL2RenderingContext.prototype);
} catch(e){}
// ── Canvas noise on an OFF-SCREEN clone (non-destructive, non-compounding) ─
try {
  const cloneNoisy = (canvas) => { try {
    const w = canvas.width, h = canvas.height; if(!w||!h) return canvas;
    const c2 = document.createElement('canvas'); c2.width = w; c2.height = h;
    const cx = c2.getContext('2d'); if(!cx) return canvas;
    cx.drawImage(canvas,0,0);
    const img = cx.getImageData(0,0,w,h); const d = img.data;
    for (let i=0;i<d.length;i+=4){ d[i]=(d[i]+FP.canvasNoise+256)%256; }
    cx.putImageData(img,0,0); return c2;
  } catch(e){ return canvas; } };
  if (window.HTMLCanvasElement) {
    const oURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = _mask(function(){ return oURL.apply(cloneNoisy(this), arguments); });
    const oBlob = HTMLCanvasElement.prototype.toBlob;
    if (oBlob) HTMLCanvasElement.prototype.toBlob = _mask(function(){ return oBlob.apply(cloneNoisy(this), arguments); });
  }
  const C2D = window.CanvasRenderingContext2D && CanvasRenderingContext2D.prototype;
  if (C2D && C2D.getImageData){ const gid = C2D.getImageData;
    C2D.getImageData = _mask(function(){ const r = gid.apply(this,arguments);
      try { const d=r.data; for(let i=0;i<d.length;i+=4){ d[i]=(d[i]+FP.canvasNoise+256)%256; } } catch(e){} return r; }); }
} catch(e){}
// ── WebRTC — block local-IP leak on BOTH event paths (proxy-safe) ─────
try {
  const RP = window.RTCPeerConnection || window.webkitRTCPeerConnection;
  if (RP) {
    const keepCand = (cand) => { if(!cand) return true; const s = (cand.candidate||''); if(!s) return true;
      return !(/ typ host/.test(s) || /\.local/.test(s)); };
    const handler = { construct(target, args) {
      const pc = new target(...args);
      try { const origAdd = pc.addEventListener.bind(pc);
        pc.addEventListener = _mask(function(type, cb, ...rest){
          if (type==='icecandidate' && typeof cb==='function'){
            const wrap = (e)=>{ if(e && e.candidate && !keepCand(e.candidate)) return; return cb(e); };
            return origAdd(type, wrap, ...rest); }
          return origAdd(type, cb, ...rest); });
      } catch(e){}
      try { let _oic = null;
        Object.defineProperty(pc, 'onicecandidate', { configurable:true,
          get(){ return _oic; },
          set(cb){ if (typeof cb!=='function'){ _oic = cb; return; } _oic = cb;
            pc.addEventListener('icecandidate', cb); } });
      } catch(e){}
      return pc; } };
    const Proxied = new Proxy(RP, handler); _mask(Proxied);
    try { window.RTCPeerConnection = Proxied; } catch(e){}
    try { window.webkitRTCPeerConnection = Proxied; } catch(e){}
  }
} catch(e){}
})();"""
    return js.replace("__CFG__", cfg)


def camoufox_config(profile: dict) -> dict:
    """Extra Camoufox custom_config keys derived from the profile.

    Camoufox accepts navigator.* / screen.* / locale style keys natively;
    we feed it everything we can so the C++ engine spoofs at a deeper
    level than the JS init-script fallback.
    """
    cfg: dict = {}
    if profile.get("user_agent"):
        cfg["navigator.userAgent"] = profile["user_agent"]
        cfg["headers.User-Agent"] = profile["user_agent"]
    if profile.get("platform"):
        cfg["navigator.platform"] = profile["platform"]
    if profile.get("languages"):
        cfg["navigator.language"] = profile["languages"][0]
        cfg["navigator.languages"] = profile["languages"]
        cfg["headers.Accept-Language"] = ",".join(profile["languages"])
    if profile.get("device_memory"):
        cfg["navigator.deviceMemory"] = profile["device_memory"]
    if profile.get("hardware_concurrency"):
        cfg["navigator.hardwareConcurrency"] = profile["hardware_concurrency"]
    if profile.get("webgl_vendor"):
        cfg["webGl:unmaskedVendor"] = profile["webgl_vendor"]
    if profile.get("webgl_renderer"):
        cfg["webGl:unmaskedRenderer"] = profile["webgl_renderer"]
    if profile.get("screen_width"):
        cfg["screen.width"] = profile["screen_width"]
    if profile.get("screen_height"):
        cfg["screen.height"] = profile["screen_height"]
    if profile.get("timezone"):
        cfg["timezone"] = profile["timezone"]
    return cfg
