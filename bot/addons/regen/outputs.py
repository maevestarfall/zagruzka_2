# Builds regen deliverables (desc/json/zip) with full-tree compile + re-scan verify.
# Co-authored with CoCo
"""Build the three deliverables Mason asked for from a coder FixResult.

1. **desc**  — plain «где и что болит» (text, no HTML tags: sent via
   ``_send_long`` which escapes everything).
2. **json**  — JSON file with code + instructions + problem (to hand to
   another AI).
3. **zip**   — the bot with the fix applied + ``REGEN_FIX_NOTES.md``,
   compile-validated (whole tree) and re-scanned before zipping.
"""

from __future__ import annotations

import json
import logging
import os
import py_compile
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from ..coder.regen_brain import FixResult

logger = logging.getLogger(__name__)

__all__ = ["build_description", "build_json_file", "build_fix_zip"]

_EXCLUDE_DIRS = {".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".git", "session", "node_modules"}


def _compile_tree(work: Path) -> list[str]:
    """Compile every .py in the patched tree; return human-readable errors."""
    errors: list[str] = []
    for dp, dns, fns in os.walk(work / "bot"):
        dns[:] = [d for d in dns if d not in _EXCLUDE_DIRS]
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            target = Path(dp) / fn
            try:
                py_compile.compile(str(target), doraise=True)
            except py_compile.PyCompileError as exc:
                rel = target.relative_to(work).as_posix()
                errors.append(f"{rel}: {exc.msg}")
            except Exception as exc:  # noqa: BLE001
                rel = target.relative_to(work).as_posix()
                errors.append(f"{rel}: {type(exc).__name__}: {exc}")
    return errors


def _rescan(work: Path, *, timeout: int = 120) -> dict:
    """Run the self-scanner on the patched tree; return its report dict."""
    cmd = [sys.executable, "-m", "bot.addons.regen._scan_worker", str(work)]
    try:
        proc = subprocess.run(
            cmd, cwd=str(work), capture_output=True, timeout=timeout, check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"fatal": f"{type(exc).__name__}: {exc}", "counts": {}}
    text = (proc.stdout or b"").decode("utf-8", "replace").strip()
    line = text.splitlines()[-1] if text else ""
    try:
        return json.loads(line)
    except Exception:  # noqa: BLE001
        return {"fatal": "re-scan unparseable", "counts": {}}


def build_description(fix: FixResult, issue: dict | None = None) -> str:
    """Plain-text «где и что болит» — variant 3."""
    lines = ["🔧 Регенерация — описание проблемы", ""]
    if issue:
        lines.append(f"Где: {issue.get('where', '?')}")
        if issue.get("symbol"):
            lines.append(f"Символ: {issue['symbol']}")
        lines.append(f"Тип: {issue.get('kind', '?')} · важность: {issue.get('severity', '?')}")
        lines.append("")
    lines.append(f"Проблема: {fix.problem}")
    if fix.root_cause:
        lines += ["", "Причина (диагноз):", fix.root_cause]
    if fix.instructions:
        lines += ["", "Что делать:", fix.instructions]
    if fix.files:
        lines += ["", f"Кодер подготовил полные файлы: {', '.join(fix.files)}"]
    if fix.notes:
        lines += ["", f"Заметки: {fix.notes}"]
    lines += ["", f"Уверенность кодера: {fix.confidence}"]
    return "\n".join(lines)


def build_json_file(fix: FixResult, issue: dict | None = None, *, out_dir: str | None = None) -> str:
    """JSON-with-code + instructions (variant 2). Returns the file path."""
    payload = {
        "tool": "lilush-regen",
        "generated_at": int(time.time()),
        "issue": issue or {},
        "fix": fix.to_json(),
        "how_to_use": (
            "Передай этот JSON другому ИИ: в 'fix.files' лежит ПОЛНОЕ новое "
            "содержимое файлов (path -> code) — просто перезапиши ими файлы "
            "в репозитории бота. 'fix.instructions' — шаги для ручной правки, "
            "если files пуст."
        ),
    }
    out_dir = out_dir or tempfile.mkdtemp(prefix="regen_")
    path = os.path.join(out_dir, f"regen_fix_{int(time.time())}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def build_fix_zip(
    fix: FixResult,
    repo_root: Path,
    *,
    issue: dict | None = None,
    out_dir: str | None = None,
    version_tag: str | None = None,
    before_errors: int | None = None,
) -> tuple[str, list[str], dict]:
    """Copy the repo, apply ``fix.files``, validate (whole tree), re-scan, zip.

    Returns ``(zip_path, compile_errors, verify)`` where ``verify`` carries
    ``{before, after, ok}`` from the re-scan. The zip is produced even with
    errors, but ``REGEN_FIX_NOTES.md`` flags them for review before deploy.
    """
    out_dir = out_dir or tempfile.mkdtemp(prefix="regen_")
    work = Path(tempfile.mkdtemp(prefix="regen_repo_"))

    # 1) copy source tree (skip junk) into work/
    rels: list[str] = []
    for dp, dns, fns in os.walk(repo_root):
        dns[:] = [d for d in dns if d not in _EXCLUDE_DIRS]
        for fn in fns:
            if fn.endswith(".pyc"):
                continue
            src = Path(dp) / fn
            rel = src.relative_to(repo_root)
            rels.append(str(rel))
            dst = work / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                dst.write_bytes(src.read_bytes())
            except Exception:  # noqa: BLE001
                pass

    # 2) apply the coder's files
    applied: list[str] = []
    for rel, content in (fix.files or {}).items():
        # never escape the tree
        safe = os.path.normpath(rel)
        if safe.startswith("..") or os.path.isabs(safe):
            continue
        dst = work / safe
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
        applied.append(safe)

    # 3) validate WHOLE patched tree (catches breaks in untouched files too)
    errors = _compile_tree(work)

    # 4) re-scan the patched tree (verify the fix actually helped)
    rescan = _rescan(work)
    after_errors = int((rescan.get("counts") or {}).get("error", 0)) if not rescan.get("fatal") else None
    verify = {"before": before_errors, "after": after_errors, "ok": False}
    if after_errors is not None and before_errors is not None:
        verify["ok"] = after_errors <= before_errors and not errors

    if after_errors is None:
        verify_line = "⚠️ повторный скан не удался — проверь вручную"
        if rescan.get("fatal"):
            verify_line += f" ({rescan['fatal']})"
    elif before_errors is not None:
        arrow = "✅" if after_errors < before_errors else ("➖" if after_errors == before_errors else "⚠️")
        verify_line = f"{arrow} ошибок сканера: было {before_errors} → стало {after_errors}"
    else:
        verify_line = f"ошибок сканера после фикса: {after_errors}"

    # 5) fix notes
    notes = [
        "# REGEN — заметки по фиксу",
        "",
        f"Сгенерировано: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Проблема: {fix.problem}",
        "",
        f"## Причина\n{fix.root_cause or '—'}",
        "",
        f"## Инструкция\n{fix.instructions or '—'}",
        "",
        "## Применённые файлы\n" + ("\n".join(f"- {a}" for a in applied) if applied else "— (кодер не дал полных файлов; см. инструкцию)"),
        "",
        "## Компиляция всего проекта\n" + ("✅ весь проект компилируется" if not errors else "⚠️ ошибки компиляции:\n" + "\n".join(f"- {e}" for e in errors[:30])),
        "",
        f"## Повторный скан\n{verify_line}",
        "",
        f"Уверенность кодера: {fix.confidence}",
        f"Заметки: {fix.notes or '—'}",
    ]
    (work / "REGEN_FIX_NOTES.md").write_text("\n".join(notes), encoding="utf-8")

    # 6) zip
    tag = version_tag or time.strftime("%Y%m%d-%H%M%S")
    zip_path = os.path.join(out_dir, f"lilush-regen-{tag}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dp, dns, fns in os.walk(work):
            dns[:] = [d for d in dns if d not in _EXCLUDE_DIRS]
            for fn in fns:
                if fn.endswith(".pyc"):
                    continue
                fp = Path(dp) / fn
                zf.write(fp, fp.relative_to(work))
    # cleanup copy tree
    try:
        import shutil

        shutil.rmtree(work, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    return zip_path, errors, verify
