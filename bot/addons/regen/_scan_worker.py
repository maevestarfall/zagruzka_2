"""Self-diagnosis worker — run as a subprocess, prints a JSON report.

    python -m bot.addons.regen._scan_worker [repo_root]

Runs the same audit toolkit a human reviewer would use, but against the
bot's *own* source, and emits a machine-readable list of issues. It runs
in a fresh interpreter (spawned by :mod:`bot.addons.regen.scanner`) so
importing every module can't pollute or crash the live bot process.

Checks (cheap → thorough):

1. **syntax** — ``compile()`` every ``bot/**/*.py`` (catches broken files).
2. **import** — import every ``bot.addons.*`` submodule + core modules;
   catches the lilush "incomplete merge" class (NameError /
   ModuleNotFoundError that ``compileall`` misses).
3. **routers** — call ``bot.addons.build_addon_routers()``.
4. **lint** — if ``ruff`` is on PATH: ``F821/F811`` (undefined /
   redefined / unused — undefined names are the real footgun).
5. **dead buttons** — callback_data prefixes with no matching handler
   (heuristic, severity ``warn``).
"""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import re
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

# Core modules worth importing explicitly (heavy wiring lives here).
CORE_MODULES = ["bot.wizard", "bot.media_ui", "bot.handlers", "bot.groq_handlers"]

# Modules that start loops / network / load ML models on import — skip from
# the import walk (they're slow and can block; their import-time bugs would
# already crash the live bot at startup anyway). Substring match on dotted
# module name.
SKIP_IMPORT = {"bot.main", "bot.addons.regen._scan_worker"}
_HEAVY_SUBSTR = (
    "worker",
    "whisper",
    "media_worker",
    "downloader",
    "editor",
    "scenedetect",
    "yt_dlp",
    "publisher",
    "seo",
    "vision_pipeline",
)


def _is_heavy(name: str) -> bool:
    low = name.lower()
    return any(s in low for s in _HEAVY_SUBSTR)


def _tb_location(exc: BaseException, root: Path) -> str:
    """Last in-repo frame of a traceback, as ``file:line``."""
    tb = exc.__traceback__
    loc = ""
    while tb is not None:
        fn = tb.tb_frame.f_code.co_filename
        try:
            rel = str(Path(fn).resolve().relative_to(root))
            loc = f"{rel}:{tb.tb_lineno}"
        except Exception:  # noqa: BLE001
            pass
        tb = tb.tb_next
    return loc


def _check_syntax(root: Path) -> list[dict]:
    issues: list[dict] = []
    for py in (root / "bot").rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        try:
            compile(py.read_text(encoding="utf-8", errors="replace"), str(py), "exec")
        except SyntaxError as exc:
            rel = str(py.relative_to(root))
            issues.append(
                {
                    "severity": "error",
                    "kind": "syntax",
                    "where": f"{rel}:{exc.lineno or 0}",
                    "symbol": "",
                    "message": f"SyntaxError: {exc.msg}",
                    "snippet": (exc.text or "").strip(),
                }
            )
        except Exception as exc:  # noqa: BLE001
            rel = str(py.relative_to(root))
            issues.append(
                {
                    "severity": "warn",
                    "kind": "syntax",
                    "where": rel,
                    "symbol": "",
                    "message": f"{type(exc).__name__}: {exc}",
                    "snippet": "",
                }
            )
    return issues


def _iter_addon_modules() -> list[str]:
    names: list[str] = []
    try:
        import bot.addons as addons_pkg

        for mod in pkgutil.walk_packages(addons_pkg.__path__, "bot.addons."):
            names.append(mod.name)
    except Exception:  # noqa: BLE001
        pass
    return names


def _check_imports(root: Path) -> list[dict]:
    issues: list[dict] = []
    targets = list(CORE_MODULES) + _iter_addon_modules()
    seen: set[str] = set()
    for name in targets:
        if name in seen or name in SKIP_IMPORT or _is_heavy(name):
            continue
        seen.add(name)
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            issues.append(
                {
                    "severity": "error",
                    "kind": "import",
                    "where": _tb_location(exc, root) or name,
                    "symbol": name,
                    "message": f"{type(exc).__name__}: {exc}",
                    "snippet": "",
                }
            )
    return issues


def _check_routers(root: Path) -> list[dict]:
    try:
        from bot.addons import build_addon_routers

        routers = build_addon_routers()
        if not routers:
            return [
                {
                    "severity": "warn",
                    "kind": "routers",
                    "where": "bot/addons/__init__.py",
                    "symbol": "build_addon_routers",
                    "message": "build_addon_routers() вернул пусто",
                    "snippet": "",
                }
            ]
    except Exception as exc:  # noqa: BLE001
        return [
            {
                "severity": "error",
                "kind": "routers",
                "where": _tb_location(exc, root) or "bot/addons/__init__.py",
                "symbol": "build_addon_routers",
                "message": f"{type(exc).__name__}: {exc}",
                "snippet": "",
            }
        ]
    return []


def _find_ruff() -> str | None:
    """Locate ruff: PATH, the venv bin next to this interpreter, then -m."""
    ruff = shutil.which("ruff")
    if ruff:
        return ruff
    cand = Path(sys.executable).parent / "ruff"
    if cand.exists():
        return str(cand)
    # last resort: `python -m ruff` if the package is importable
    try:
        import ruff  # noqa: F401

        return f"{sys.executable} -m ruff"
    except Exception:  # noqa: BLE001
        return None


def _check_ruff(root: Path) -> list[dict]:
    ruff = _find_ruff()
    if not ruff:
        return []
    ruff_cmd = ruff.split(" ") if " -m " in ruff else [ruff]
    try:
        proc = subprocess.run(
            [
                *ruff_cmd,
                "check",
                "bot/",
                "--select",
                "F821,F811",
                "--output-format",
                "json",
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=90,
        )
        data = json.loads(proc.stdout or "[]")
    except Exception:  # noqa: BLE001
        return []
    issues: list[dict] = []
    sev = {"F821": "error", "F811": "warn"}
    for item in data:
        code = (item.get("code") or "").strip()
        loc = item.get("location") or {}
        fn = item.get("filename") or ""
        try:
            rel = str(Path(fn).resolve().relative_to(root))
        except Exception:  # noqa: BLE001
            rel = fn
        issues.append(
            {
                "severity": sev.get(code, "warn"),
                "kind": f"lint/{code}",
                "where": f"{rel}:{loc.get('row', 0)}",
                "symbol": "",
                "message": item.get("message") or code,
                "snippet": "",
            }
        )
    return issues


_CB_RE = re.compile(r"""callback_data\s*=\s*[fr]?["']([a-zA-Z0-9_]+):""")
_FDATA_RE = re.compile(r"""F\.data[^\n]*?["']([a-zA-Z0-9_]+)[:"']""")


def _check_dead_buttons(root: Path) -> list[dict]:
    """Heuristic: callback_data prefixes with no F.data handler anywhere."""
    used_prefixes: dict[str, str] = {}
    handled: set[str] = set()
    for py in (root / "bot").rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        try:
            txt = py.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        rel = str(py.relative_to(root))
        for m in _CB_RE.finditer(txt):
            used_prefixes.setdefault(m.group(1), rel)
        for m in _FDATA_RE.finditer(txt):
            handled.add(m.group(1))
        # also treat startswith("x:") and == "x" as handled
        for m in re.finditer(r"""startswith\(\s*["']([a-zA-Z0-9_]+):""", txt):
            handled.add(m.group(1))
        for m in re.finditer(r"""\.data\s*==\s*["']([a-zA-Z0-9_]+)""", txt):
            handled.add(m.group(1))
    issues: list[dict] = []
    for prefix, where in sorted(used_prefixes.items()):
        if prefix not in handled:
            issues.append(
                {
                    "severity": "warn",
                    "kind": "dead_button",
                    "where": where,
                    "symbol": prefix,
                    "message": (
                        f"callback_data «{prefix}:…» нигде не обрабатывается "
                        f"(нет F.data/startswith/==) — возможно мёртвая кнопка"
                    ),
                    "snippet": "",
                }
            )
    return issues


def run_scan(root: Path) -> dict:
    issues: list[dict] = []
    issues += _check_syntax(root)
    issues += _check_imports(root)
    issues += _check_routers(root)
    issues += _check_ruff(root)
    issues += _check_dead_buttons(root)
    # stable ids + de-dupe
    seen: set[tuple] = set()
    uniq: list[dict] = []
    for i, it in enumerate(issues):
        key = (it["kind"], it["where"], it["message"][:80])
        if key in seen:
            continue
        seen.add(key)
        it["id"] = f"i{i}"
        uniq.append(it)
    order = {"error": 0, "warn": 1, "info": 2}
    uniq.sort(key=lambda x: order.get(x["severity"], 3))
    return {
        "root": str(root),
        "ruff_available": bool(_find_ruff()),
        "counts": {
            "error": sum(1 for x in uniq if x["severity"] == "error"),
            "warn": sum(1 for x in uniq if x["severity"] == "warn"),
        },
        "issues": uniq,
    }


def main() -> None:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    os.environ.setdefault("DATA_DIR", os.environ.get("DATA_DIR", "/tmp/lilush_data"))
    os.environ.setdefault("PUBLIC_URL", os.environ.get("PUBLIC_URL", "https://example.com"))
    try:
        report = run_scan(root)
    except Exception as exc:  # noqa: BLE001
        report = {
            "root": str(root),
            "fatal": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(),
            "issues": [],
            "counts": {"error": 1, "warn": 0},
        }
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
