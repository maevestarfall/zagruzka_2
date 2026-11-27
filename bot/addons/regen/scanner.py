"""Drives the self-scan subprocess and shapes the result for the UI."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["repo_root", "scan", "read_snippet", "Issue"]


def repo_root() -> Path:
    """Repo root that contains the ``bot/`` package.

    ``bot/addons/regen/scanner.py`` → parents[3] == repo root.
    """
    return Path(__file__).resolve().parents[3]


class Issue(dict):
    """Thin dict wrapper so callers can use attribute-ish access if wanted."""


async def scan(root: Path | None = None, *, timeout: int = 150) -> dict:
    """Run ``_scan_worker`` in a subprocess; return its JSON report.

    Never raises — on failure returns a report with a ``fatal`` key.
    """
    root = root or repo_root()
    cmd = [sys.executable, "-m", "bot.addons.regen._scan_worker", str(root)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return {"fatal": f"скан превысил {timeout}с", "issues": [], "counts": {"error": 0, "warn": 0}}
    except Exception as exc:  # noqa: BLE001
        return {"fatal": f"{type(exc).__name__}: {exc}", "issues": [], "counts": {"error": 0, "warn": 0}}
    text = (out or b"").decode("utf-8", "replace").strip()
    # worker prints exactly one JSON line last
    line = text.splitlines()[-1] if text else ""
    try:
        report = json.loads(line)
    except Exception:  # noqa: BLE001
        stderr = (err or b"").decode("utf-8", "replace")[-500:]
        return {
            "fatal": "не удалось распарсить отчёт сканера",
            "stderr": stderr,
            "raw": text[-500:],
            "issues": [],
            "counts": {"error": 0, "warn": 0},
        }
    return report


def read_snippet(where: str, *, root: Path | None = None, context: int = 12) -> str:
    """Read code around ``file:line`` (best-effort) for the coder context."""
    root = root or repo_root()
    if ":" in where:
        fpart, _, lpart = where.rpartition(":")
    else:
        fpart, lpart = where, "0"
    try:
        line = int(lpart)
    except ValueError:
        fpart, line = where, 0
    path = (root / fpart) if fpart else None
    if not path or not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return ""
    if line <= 0:
        head = lines[:80]
        return f"# {fpart} (начало файла)\n" + "\n".join(head)
    lo = max(0, line - context)
    hi = min(len(lines), line + context)
    numbered = [f"{i + 1:>5}| {lines[i]}" for i in range(lo, hi)]
    return f"# {fpart} (вокруг строки {line})\n" + "\n".join(numbered)


def full_file(rel: str, *, root: Path | None = None, max_chars: int = 16000) -> str:
    """Return full text of a repo file (truncated) for codegen context."""
    root = root or repo_root()
    path = root / rel
    if not path.exists():
        return ""
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    if len(txt) > max_chars:
        txt = txt[:max_chars] + "\n# … (обрезано)"
    return f"# ФАЙЛ: {rel}\n{txt}"
