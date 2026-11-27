"""Versioned tool registry and sandboxed built-in coder tools."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .models import ToolResult
from .workspace import Workspace

_SECRET = re.compile(r"(?i)(token|secret|password|api[_-]?key)(\s*[:=]\s*)([^\s,;]+)")
_ALLOWED_COMMANDS = {"python", "python3", "pytest", "ruff", "mypy", "git", "npm", "pnpm", "node"}


class ToolExecutionError(RuntimeError):
    """Tool failed but produced verifiable output (stdout/stderr/exit code).

    ТЗ §5.6: результат обязан включать фактическую команду, stdout/stderr,
    exit code и длительность даже при ошибке — Кодеру нужен полный вывод
    для цикла исправления, а не только текст исключения.
    """

    def __init__(self, message: str, output: dict[str, Any] | None = None):
        super().__init__(message)
        self.output = output or {}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if re.search(r"(?i)token|secret|password|key", k) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", value)
    return value


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    version: str
    risk: str
    timeout: float
    input_schema: dict[str, Any]
    handler: Callable[[Workspace, dict[str, Any]], Awaitable[dict[str, Any]]]


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._tools[spec.name] = spec

    def describe(self) -> list[dict[str, Any]]:
        return [{"name": s.name, "version": s.version, "risk": s.risk, "timeout": s.timeout, "input_schema": s.input_schema} for s in self._tools.values()]

    async def invoke(self, name: str, workspace: Workspace, arguments: dict[str, Any], allowed: set[str] | None = None) -> ToolResult:
        started = time.time()
        spec = self._tools.get(name)
        if spec is None or (allowed is not None and name not in allowed):
            return ToolResult(False, name, "unknown", started, time.time(), error="tool is not allowed")
        required = spec.input_schema.get("required", [])
        missing = [key for key in required if key not in arguments]
        if missing:
            return ToolResult(False, name, spec.version, started, time.time(), error=f"missing: {', '.join(missing)}")
        try:
            output = await asyncio.wait_for(spec.handler(workspace, arguments), spec.timeout)
            return ToolResult(True, name, spec.version, started, time.time(), redact(output))
        except ToolExecutionError as exc:
            # ТЗ §5.6: провал команды — это данные, а не только текст ошибки.
            return ToolResult(False, name, spec.version, started, time.time(), redact(exc.output), error=redact(str(exc)))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(False, name, spec.version, started, time.time(), error=redact(str(exc)))


async def _fs_read(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    data = ws.read(str(args["path"]), int(args.get("max_bytes", 2_000_000)))
    return {"path": args["path"], "content": data.decode("utf-8", errors="replace"), "bytes": len(data)}


async def _fs_write(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    data = str(args["content"]).encode()
    path = ws.write_atomic(str(args["path"]), data)
    return {"path": str(path.relative_to(ws.root)), "bytes": len(data), "transaction": "committed"}


async def _fs_list(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    root = ws.resolve(str(args.get("path", ".")), allow_missing=False)
    limit = min(int(args.get("limit", 500)), 2000)
    entries = []
    for p in sorted(root.rglob("*")):
        if len(entries) >= limit:
            break
        entries.append({"path": str(p.relative_to(ws.root)), "type": "dir" if p.is_dir() else "file", "bytes": p.stat().st_size if p.is_file() else 0})
    return {"entries": entries, "truncated": len(entries) >= limit}


async def _fs_stat(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    path = ws.resolve(str(args["path"]), allow_missing=False)
    stat = path.lstat()
    return {"path": str(path.relative_to(ws.root)), "type": "symlink" if path.is_symlink() else "dir" if path.is_dir() else "file", "bytes": stat.st_size, "mode": oct(stat.st_mode & 0o777), "mtime": stat.st_mtime}


async def _fs_mkdir(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    path = ws.resolve(str(args["path"]))
    path.mkdir(parents=bool(args.get("parents", True)), exist_ok=bool(args.get("exist_ok", True)))
    return {"created": str(path.relative_to(ws.root))}


async def _fs_move(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    src = ws.resolve(str(args["source"]), allow_missing=False)
    dst = ws.resolve(str(args["destination"]))
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dst)
    return {"source": str(src.relative_to(ws.root)), "destination": str(dst.relative_to(ws.root))}


async def _fs_delete(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    path = ws.resolve(str(args["path"]), allow_missing=False)
    if path == ws.root:
        raise PermissionError("workspace root cannot be deleted")
    if path.is_dir() and not path.is_symlink():
        import shutil
        shutil.rmtree(path)
    else:
        path.unlink()
    return {"deleted": str(path.relative_to(ws.root))}


async def _fs_glob(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    limit = min(int(args.get("limit", 1000)), 5000)
    matches = []
    for path in ws.root.glob(str(args["pattern"])):
        resolved = ws.resolve(str(path.relative_to(ws.root)), allow_missing=False)
        matches.append({"path": str(resolved.relative_to(ws.root)), "type": "dir" if resolved.is_dir() else "file"})
        if len(matches) >= limit:
            break
    return {"matches": matches, "truncated": len(matches) >= limit}


async def _fs_patch(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    path = ws.resolve(str(args["path"]), allow_missing=False)
    before, after = str(args["before"]), str(args["after"])
    text = path.read_text(encoding="utf-8")
    count = text.count(before)
    if count != 1:
        raise ValueError(f"patch anchor must match exactly once, got {count}")
    path.write_text(text.replace(before, after, 1), encoding="utf-8")
    return {"path": str(path.relative_to(ws.root)), "bytes": path.stat().st_size, "workspace_hash": ws.digest()}


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


async def _fs_edit(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    """ТЗ §5.2 fs.edit: точечная замена с проверкой исходного текста.

    Опционально принимает expected_sha256 — precondition на текущее
    содержимое файла (защита от гонок и правок по устаревшему чтению).
    """
    path = ws.resolve(str(args["path"]), allow_missing=False)
    raw = path.read_bytes()
    expected = args.get("expected_sha256")
    if expected and _sha256(raw) != str(expected).removeprefix("sha256:"):
        raise ValueError("precondition failed: file content hash does not match expected_sha256")
    text = raw.decode("utf-8")
    old, new = str(args["old_text"]), str(args["new_text"])
    count = text.count(old)
    if count == 0:
        raise ValueError("old_text not found in file")
    if count > 1 and not args.get("replace_all"):
        raise ValueError(f"old_text matches {count} times; pass replace_all=true or narrow the anchor")
    updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
    ws.write_atomic(str(args["path"]), updated.encode("utf-8"))
    return {"path": str(path.relative_to(ws.root)), "replacements": count if args.get("replace_all") else 1, "sha256": _sha256(updated.encode("utf-8")), "workspace_hash": ws.digest()}


async def _fs_copy(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    import shutil

    src = ws.resolve(str(args["source"]), allow_missing=False)
    dst = ws.resolve(str(args["destination"]))
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, dst, symlinks=False, dirs_exist_ok=bool(args.get("overwrite", False)))
    else:
        shutil.copy2(src, dst, follow_symlinks=False)
    return {"source": str(src.relative_to(ws.root)), "destination": str(dst.relative_to(ws.root))}


_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".next", "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


async def _fs_tree(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    """ТЗ §5.2 fs.tree: карта проекта (иерархия без vendor/generated шума)."""
    max_depth = min(int(args.get("max_depth", 6)), 12)
    limit = min(int(args.get("limit", 1000)), 4000)
    rel = str(args.get("path", ".") or ".")
    root = ws.root if rel in {".", ""} else ws.resolve(rel, allow_missing=False)
    lines: list[str] = []
    truncated = False

    def walk(directory: Path, depth: int) -> None:
        nonlocal truncated
        if depth > max_depth or truncated:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return
        for child in children:
            if len(lines) >= limit:
                truncated = True
                return
            if child.name in _SKIP_DIRS:
                continue
            rel = str(child.relative_to(ws.root))
            if child.is_dir() and not child.is_symlink():
                lines.append(f"{rel}/")
                walk(child, depth + 1)
            else:
                lines.append(f"{rel} ({child.stat().st_size}b)" if child.is_file() else rel)

    walk(root, 1)
    return {"tree": lines, "truncated": truncated}


async def _fs_encoding_check(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    """ТЗ §5.2 fs.encoding_check: поиск битой кодировки и replacement character."""
    limit = min(int(args.get("limit", 200)), 1000)
    rel = str(args.get("path", ".") or ".")
    target = ws.root if rel in {".", ""} else ws.resolve(rel, allow_missing=False)
    paths = [target] if target.is_file() else [p for p in sorted(target.rglob("*")) if p.is_file() and not p.is_symlink()]
    issues: list[dict[str, Any]] = []
    for path in paths:
        if len(issues) >= limit:
            return {"issues": issues, "truncated": True}
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        raw = path.read_bytes()
        if not raw or b"\x00" in raw[:8000]:
            continue  # binary
        rel = str(path.relative_to(ws.root))
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            issues.append({"path": rel, "problem": "not-utf8", "detail": str(exc)})
            continue
        if "\ufffd" in text:
            line = next((n for n, l in enumerate(text.splitlines(), 1) if "\ufffd" in l), 0)
            issues.append({"path": rel, "problem": "replacement-character", "line": line})
    return {"issues": issues, "truncated": False}


async def _fs_patch_multi(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    """ТЗ §7.3 / приёмочный сценарий 2: транзакционный multi-file patch.

    Сначала verify для всех файлов (expected hash + уникальность якоря),
    затем атомарное применение. Один неверный hash — полный abort без
    частичных изменений.
    """
    patches = args["patches"]
    if not isinstance(patches, list) or not patches:
        raise ValueError("patches must be a non-empty list")
    staged: list[tuple[str, str]] = []
    for i, patch in enumerate(patches):
        path = ws.resolve(str(patch["path"]), allow_missing=False)
        raw = path.read_bytes()
        expected = patch.get("expected_sha256")
        if expected and _sha256(raw) != str(expected).removeprefix("sha256:"):
            raise ValueError(f"abort: patch[{i}] {patch['path']} hash mismatch — no files were modified")
        text = raw.decode("utf-8")
        before, after = str(patch["before"]), str(patch["after"])
        count = text.count(before)
        if count != 1:
            raise ValueError(f"abort: patch[{i}] {patch['path']} anchor matches {count} times — no files were modified")
        staged.append((str(patch["path"]), text.replace(before, after, 1)))
    for rel, updated in staged:
        ws.write_atomic(rel, updated.encode("utf-8"))
    return {"applied": [rel for rel, _ in staged], "workspace_hash": ws.digest()}


async def _repo_manifest(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    limit = min(int(args.get("limit", 2000)), 5000)
    files: list[dict[str, Any]] = []
    for path in sorted(ws.root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rel = str(path.relative_to(ws.root))
            data = path.read_bytes()
            files.append({"path": rel, "bytes": len(data), "sha256": __import__("hashlib").sha256(data).hexdigest()})
            if len(files) >= limit:
                break
    return {"files": files, "truncated": len(files) >= limit, "workspace_hash": ws.digest()}


async def _repo_search(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    needle = str(args["query"])
    limit = min(int(args.get("limit", 100)), 500)
    matches: list[dict[str, Any]] = []
    for path in sorted(ws.root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 2_000_000:
            continue
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if needle.casefold() in line.casefold():
                matches.append({"path": str(path.relative_to(ws.root)), "line": number, "text": line[:500]})
                if len(matches) >= limit:
                    return {"matches": matches, "truncated": True}
    return {"matches": matches, "truncated": False}


async def _repo_rules(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    """ТЗ §7.2 repo.rules: применимые инструкции из README/AGENTS/CONTRIBUTING."""
    names = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "CONTRIBUTING.rst", "README.md", ".editorconfig")
    cap = 40_000
    rules: list[dict[str, Any]] = []
    for path in sorted(ws.root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.name not in names:
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        rules.append({"path": str(path.relative_to(ws.root)), "content": text[:cap], "truncated": len(text) > cap})
        if len(rules) >= 20:
            break
    return {"rules": rules}


async def _dependencies_detect(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    known = ("pyproject.toml", "requirements.txt", "package.json", "pnpm-lock.yaml", "package-lock.json", "uv.lock")
    return {"manifests": [name for name in known if ws.resolve(name).is_file()]}


async def _dependencies_install(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    """ТЗ §5.5 dependencies.install: установка строго через менеджер пакетов.

    Не принимает произвольные argv — только имя менеджера и список пакетов,
    argv собирается детерминированно (никакого shell).
    """
    manager = str(args.get("manager", "")).lower()
    packages = [str(p) for p in args.get("packages", [])]
    for pkg in packages:
        if not re.fullmatch(r"[A-Za-z0-9@/._^~><=+\-\[\],!*]+", pkg) or pkg.startswith("-"):
            raise ValueError(f"invalid package spec: {pkg!r}")
    commands: dict[str, list[str]] = {
        "pip": ["python3", "-m", "pip", "install", *packages] if packages else ["python3", "-m", "pip", "install", "-r", "requirements.txt"],
        "npm": ["npm", "install", *packages],
        "pnpm": ["pnpm", "add", *packages] if packages else ["pnpm", "install"],
    }
    if manager not in commands:
        raise ValueError(f"unsupported manager: {manager!r} (pip, npm, pnpm)")
    return await _command(ws, {"argv": commands[manager], "timeout": min(float(args.get("timeout", 300)), 600)})


async def _dependencies_audit(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    """ТЗ §5.5 dependencies.audit: проверка уязвимостей через менеджер пакетов."""
    manager = str(args.get("manager", "")).lower()
    commands: dict[str, list[str]] = {
        "pip": ["python3", "-m", "pip", "check"],
        "npm": ["npm", "audit", "--json"],
        "pnpm": ["pnpm", "audit", "--json"],
    }
    if manager not in commands:
        raise ValueError(f"unsupported manager: {manager!r} (pip, npm, pnpm)")
    return await _command(ws, {"argv": commands[manager], "timeout": min(float(args.get("timeout", 120)), 300)})


async def _http_request(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    url = str(args["url"])
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only absolute http(s) URLs are allowed")
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"} or parsed.hostname.endswith(".local"):
        raise ValueError("local network targets are forbidden")
    method = str(args.get("method", "GET")).upper()
    if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError("unsupported HTTP method")
    body = json.dumps(args["json"]).encode() if "json" in args else str(args.get("body", "")).encode() or None
    headers = {str(k): str(v) for k, v in args.get("headers", {}).items() if str(k).lower() not in {"authorization", "proxy-authorization", "cookie"}}
    if "json" in args:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    def execute() -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=min(float(args.get("timeout", 30)), 60)) as response:
                data = response.read(2_000_001)
                return {"status": response.status, "headers": dict(response.headers), "body": data[:2_000_000].decode(errors="replace"), "truncated": len(data) > 2_000_000, "url": response.url}
        except urllib.error.HTTPError as exc:
            return {"status": exc.code, "headers": dict(exc.headers), "body": exc.read(2_000_000).decode(errors="replace"), "url": exc.url}
    return await asyncio.to_thread(execute)


async def _database_query(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    path = ws.resolve(str(args["database"]))
    sql = str(args["sql"]).strip()
    if not sql.lower().startswith(("select", "with", "pragma")):
        raise PermissionError("database.query is read-only")
    def execute() -> dict[str, Any]:
        with sqlite3.connect(path) as db:
            db.row_factory = sqlite3.Row
            cursor = db.execute(sql, tuple(args.get("parameters", [])))
            rows = [dict(row) for row in cursor.fetchmany(min(int(args.get("limit", 500)), 2000))]
            return {"columns": [item[0] for item in cursor.description or []], "rows": rows, "count": len(rows)}
    return await asyncio.to_thread(execute)


async def _command(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    argv = args["argv"]
    if isinstance(argv, str):
        argv = shlex.split(argv)
    if not isinstance(argv, list) or not argv or Path(str(argv[0])).name not in _ALLOWED_COMMANDS:
        raise ValueError("command is not allowlisted")
    timeout = min(float(args.get("timeout", 120)), 600)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(ws.root), "PYTHONUNBUFFERED": "1", "CI": "1"}
    proc = await asyncio.create_subprocess_exec(*map(str, argv), cwd=ws.root, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            await asyncio.wait_for(proc.wait(), 2)
        except (ProcessLookupError, TimeoutError):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        if asyncio.current_task() and asyncio.current_task().cancelling():
            raise
        raise TimeoutError("command timed out")
    cap = 1_000_000
    output = {"argv": argv, "exit_code": proc.returncode, "stdout": out[:cap].decode(errors="replace"), "stderr": err[:cap].decode(errors="replace"), "truncated": len(out) > cap or len(err) > cap}
    if proc.returncode != 0:
        raise ToolExecutionError(f"command exited with {proc.returncode}", output)
    return output


async def _git_status(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    return await _command(ws, {"argv": ["git", "status", "--short", "--branch"], "timeout": args.get("timeout", 30)})


async def _git_diff(ws: Workspace, args: dict[str, Any]) -> dict[str, Any]:
    argv = ["git", "diff", "--no-ext-diff"]
    if args.get("staged"):
        argv.append("--staged")
    if args.get("path"):
        argv.extend(["--", str(args["path"])])
    return await _command(ws, {"argv": argv, "timeout": args.get("timeout", 30)})


def default_registry() -> ToolRegistry:
    r = ToolRegistry()
    obj = {"type": "object", "properties": {}}
    r.register(ToolSpec("fs.read", "1.0.0", "low", 10, {**obj, "required": ["path"]}, _fs_read))
    r.register(ToolSpec("fs.write", "1.0.0", "medium", 10, {**obj, "required": ["path", "content"]}, _fs_write))
    r.register(ToolSpec("fs.list", "1.0.0", "low", 10, obj, _fs_list))
    r.register(ToolSpec("fs.stat", "1.0.0", "low", 10, {**obj, "required": ["path"]}, _fs_stat))
    r.register(ToolSpec("fs.mkdir", "1.0.0", "medium", 10, {**obj, "required": ["path"]}, _fs_mkdir))
    r.register(ToolSpec("fs.move", "1.0.0", "medium", 10, {**obj, "required": ["source", "destination"]}, _fs_move))
    r.register(ToolSpec("fs.delete", "1.0.0", "high", 30, {**obj, "required": ["path"]}, _fs_delete))
    r.register(ToolSpec("fs.glob", "1.0.0", "low", 30, {**obj, "required": ["pattern"]}, _fs_glob))
    r.register(ToolSpec("fs.patch", "1.0.0", "medium", 30, {**obj, "required": ["path", "before", "after"]}, _fs_patch))
    r.register(ToolSpec("fs.edit", "1.0.0", "medium", 30, {**obj, "required": ["path", "old_text", "new_text"]}, _fs_edit))
    r.register(ToolSpec("fs.copy", "1.0.0", "medium", 30, {**obj, "required": ["source", "destination"]}, _fs_copy))
    r.register(ToolSpec("fs.tree", "1.0.0", "low", 30, obj, _fs_tree))
    r.register(ToolSpec("fs.encoding_check", "1.0.0", "low", 60, obj, _fs_encoding_check))
    r.register(ToolSpec("fs.patch_multi", "1.0.0", "medium", 60, {**obj, "required": ["patches"]}, _fs_patch_multi))
    r.register(ToolSpec("repository.manifest", "1.0.0", "low", 30, obj, _repo_manifest))
    r.register(ToolSpec("repository.search", "1.0.0", "low", 30, {**obj, "required": ["query"]}, _repo_search))
    r.register(ToolSpec("repository.rules", "1.0.0", "low", 30, obj, _repo_rules))
    r.register(ToolSpec("dependencies.detect", "1.0.0", "low", 10, obj, _dependencies_detect))
    r.register(ToolSpec("dependencies.install", "1.0.0", "high", 600, {**obj, "required": ["manager"]}, _dependencies_install))
    r.register(ToolSpec("dependencies.audit", "1.0.0", "medium", 300, {**obj, "required": ["manager"]}, _dependencies_audit))
    r.register(ToolSpec("command.run", "1.0.0", "high", 600, {**obj, "required": ["argv"]}, _command))
    r.register(ToolSpec("test.run", "1.0.0", "medium", 600, {**obj, "required": ["argv"]}, _command))
    r.register(ToolSpec("build.run", "1.0.0", "medium", 600, {**obj, "required": ["argv"]}, _command))
    r.register(ToolSpec("lint.run", "1.0.0", "medium", 600, {**obj, "required": ["argv"]}, _command))
    r.register(ToolSpec("format.run", "1.0.0", "medium", 600, {**obj, "required": ["argv"]}, _command))
    r.register(ToolSpec("typecheck.run", "1.0.0", "medium", 600, {**obj, "required": ["argv"]}, _command))
    r.register(ToolSpec("git.status", "1.0.0", "low", 30, obj, _git_status))
    r.register(ToolSpec("git.diff", "1.0.0", "low", 30, obj, _git_diff))
    r.register(ToolSpec("git.run", "1.0.0", "high", 120, {**obj, "required": ["argv"]}, _command))
    r.register(ToolSpec("http.request", "1.0.0", "high", 60, {**obj, "required": ["url"]}, _http_request))
    r.register(ToolSpec("database.query", "1.0.0", "low", 30, {**obj, "required": ["database", "sql"]}, _database_query))
    r.register(ToolSpec("browser.run", "1.0.0", "high", 600, {**obj, "required": ["argv"]}, _command))
    r.register(ToolSpec("publication.run", "1.0.0", "critical", 600, {**obj, "required": ["argv"]}, _command))
    return r
