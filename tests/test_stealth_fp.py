"""Tests for the stealth fingerprint spoofing (bot.addons.stealth_fp).

Two layers:

  • pure-Python: deterministic profile generation + init-script content;
  • functional: the generated JS is run under Node with a stubbed browser
    environment to PROVE the spoofs behave (WebRTC local-IP leak blocked
    on both event paths, overrides masked as native, canvas non-destructive,
    Proxy preserves statics/instanceof). Skipped automatically if Node is
    not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap

import pytest

from bot.addons import stealth_fp


# ── pure-Python ─────────────────────────────────────────────────────────

def test_profile_is_deterministic():
    a = stealth_fp.generate_profile("seed-1")
    b = stealth_fp.generate_profile("seed-1")
    assert a == b
    c = stealth_fp.generate_profile("seed-2")
    assert a != c  # different seed → different fingerprint


def test_profile_respects_overrides():
    p = stealth_fp.generate_profile("s", os_hint="mac", timezone="Europe/London")
    assert p["os"] == "mac"
    assert p["platform"] == "MacIntel"
    assert p["timezone"] == "Europe/London"
    assert "en-GB" in p["languages"]


def test_init_script_contains_hardening():
    js = stealth_fp.build_init_script(stealth_fp.generate_profile("s"))
    # Hardened markers that must be present.
    assert "new Proxy(RP" in js          # robust WebRTC constructor wrap
    assert "onicecandidate" in js        # property path covered, not just addEventListener
    assert "[native code]" in js         # toString masking
    assert "cloneNoisy" in js            # non-destructive canvas
    assert "typ host" in js and ".local" in js  # local-IP candidate filter


# ── functional (Node) ───────────────────────────────────────────────────

_HARNESS = r"""
"use strict";
const fs = require("fs");
const script = fs.readFileSync(process.argv[2], "utf8");
let failures = 0;
function check(name, cond){ if(cond){console.log("ok - "+name);} else {console.log("FAIL - "+name); failures++;} }

const navigator = { webdriver:true, platform:"x", deviceMemory:4, hardwareConcurrency:2, language:"xx", languages:["xx"] };
const screen = { width:0,height:0,availWidth:0,availHeight:0,colorDepth:0,pixelDepth:0 };
function makeCtx(){ const data=new Uint8ClampedArray(16); for(let i=0;i<16;i++) data[i]=100;
  return { _img:{data,width:2,height:2}, drawImage(){},
    getImageData(){ return {data:Uint8ClampedArray.from(this._img.data),width:2,height:2}; },
    putImageData(img){ this._img.data=Uint8ClampedArray.from(img.data); } }; }
class HTMLCanvasElement{ constructor(){ this.width=2;this.height=2;this._ctx=makeCtx(); }
  getContext(){ return this._ctx; }
  toDataURL(){ return "data:"+Array.from(this._ctx._img.data).join(","); }
  toBlob(cb){ cb&&cb("blob"); } }
class CanvasRenderingContext2D{}
CanvasRenderingContext2D.prototype.getImageData=function(){ const data=new Uint8ClampedArray(16); for(let i=0;i<16;i++) data[i]=50; return {data,width:2,height:2}; };
function WebGLRenderingContext(){}
WebGLRenderingContext.prototype.getParameter=function(p){ return "REAL-"+p; };
function WebGL2RenderingContext(){}
WebGL2RenderingContext.prototype.getParameter=function(p){ return "REAL2-"+p; };
const Intl={ DateTimeFormat:function(){} };
Intl.DateTimeFormat.prototype.resolvedOptions=function(){ return {timeZone:"UTC"}; };
const document={ createElement(){ return new HTMLCanvasElement(); } };
class FakeRTC{ constructor(){ this._l={}; } addEventListener(t,cb){ (this._l[t]||(this._l[t]=[])).push(cb); }
  _emit(s){ const e={candidate: s===null?null:{candidate:s}}; (this._l["icecandidate"]||[]).forEach(cb=>cb(e)); } }
FakeRTC.generateCertificate=function(){ return "cert"; };
const window={ HTMLCanvasElement,CanvasRenderingContext2D,WebGLRenderingContext,WebGL2RenderingContext,Intl,
  RTCPeerConnection:FakeRTC, webkitRTCPeerConnection:FakeRTC };

eval(script);

check("webdriver false", navigator.webdriver===false);
check("platform spoofed", navigator.platform==="Win32");
check("screen spoofed", screen.width>0 && screen.availHeight===screen.height-40);
const gl=WebGLRenderingContext.prototype;
check("webgl vendor spoofed", gl.getParameter(37445)!=="REAL-37445");
check("webgl renderer spoofed", gl.getParameter(37446)!=="REAL-37446");
check("webgl passthrough", gl.getParameter(1)==="REAL-1");
check("toDataURL native-masked", HTMLCanvasElement.prototype.toDataURL.toString().includes("[native code]"));
check("getParameter native-masked", gl.getParameter.toString().includes("[native code]"));
check("user fn not masked", !(function foo(){}).toString().includes("[native code]"));
const cv=new HTMLCanvasElement(); const before=JSON.stringify(Array.from(cv._ctx._img.data));
const u=cv.toDataURL(); const after=JSON.stringify(Array.from(cv._ctx._img.data));
check("canvas not mutated", before===after);
check("toDataURL stable", cv.toDataURL()===u);
check("intl tz pinned", new Intl.DateTimeFormat().resolvedOptions().timeZone==="Europe/Berlin");
const pc=new window.RTCPeerConnection(); const A=[],P=[];
pc.addEventListener("icecandidate",e=>A.push(e.candidate&&e.candidate.candidate));
pc.onicecandidate=e=>P.push(e.candidate&&e.candidate.candidate);
pc._emit("candidate:1 1 udp 2 192.168.0.5 5000 typ host");
pc._emit("candidate:2 1 udp 2 abcd.local 5000 typ host");
pc._emit("candidate:3 1 udp 2 1.2.3.4 5000 typ srflx");
pc._emit(null);
check("addEv host filtered", !A.some(c=>c&&c.includes("typ host")));
check("addEv mDNS filtered", !A.some(c=>c&&c.includes(".local")));
check("addEv srflx passes", A.some(c=>c&&c.includes("typ srflx")));
check("addEv null passes", A.includes(null));
check("onicecandidate host filtered", !P.some(c=>c&&c.includes("typ host")));
check("onicecandidate srflx passes", P.some(c=>c&&c.includes("typ srflx")));
check("static preserved", typeof window.RTCPeerConnection.generateCertificate==="function");
check("instanceof works", pc instanceof window.RTCPeerConnection);

if(failures){ console.log(failures+" FAILURES"); process.exit(1); }
console.log("ALL PASSED");
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_init_script_runs_and_spoofs(tmp_path):
    profile = stealth_fp.generate_profile(
        "seed-xyz", os_hint="windows", timezone="Europe/Berlin"
    )
    script = stealth_fp.build_init_script(profile)

    script_path = tmp_path / "initscript.js"
    harness_path = tmp_path / "harness.js"
    script_path.write_text(script, encoding="utf-8")
    harness_path.write_text(textwrap.dedent(_HARNESS), encoding="utf-8")

    # Syntax check first for a clear error if the JS is malformed.
    syn = subprocess.run(
        ["node", "--check", str(script_path)], capture_output=True, text=True
    )
    assert syn.returncode == 0, f"init script has a JS syntax error:\n{syn.stderr}"

    res = subprocess.run(
        ["node", str(harness_path), str(script_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, f"stealth harness failed:\n{res.stdout}\n{res.stderr}"
    assert "ALL PASSED" in res.stdout


# ── live check in a real Chromium ───────────────────────────────────────

def test_live_check_in_real_browser():
    """Run the spoof script in an actual Chromium and assert every probe
    passes. Skipped when Playwright / a browser binary isn't available."""
    from bot.addons import stealth_fp_check

    result = stealth_fp_check.run_sync(
        seed="pytest_seed", os_hint="windows", timezone="Europe/Berlin")

    if result.get("error"):
        pytest.skip(f"no live browser available: {result['error']}")

    failed = [c["name"] for c in result["checks"] if not c["ok"]]
    assert not failed, f"live checks failed: {failed}\n{stealth_fp_check.format_report(result)}"
    assert result["passed"] >= 14  # all spoof probes accounted for


# ── enable/disable toggle + menu router ─────────────────────────────────

@pytest.fixture()
def clean_stealth_toggle():
    from bot.addons import state as addon_state
    from bot.addons import stealth

    snap = addon_state.get(stealth.ADDON, "enabled", None)
    addon_state.set_(stealth.ADDON, "enabled", None)
    yield stealth
    addon_state.set_(stealth.ADDON, "enabled", snap)


def test_stealth_toggle_default_on_and_flips(clean_stealth_toggle):
    stealth = clean_stealth_toggle
    assert stealth.is_enabled() is True  # default ON (preserves behaviour)
    stealth.set_enabled(False)
    assert stealth.is_enabled() is False
    stealth.set_enabled(True)
    assert stealth.is_enabled() is True


def test_stealth_router_builds():
    from bot.addons.stealth import build_stealth_router

    router = build_stealth_router()
    assert router.name == "stealth"


