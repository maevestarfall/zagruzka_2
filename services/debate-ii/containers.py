"""
containers.py — per-door containers and archive endpoints.
Each door has its own containers/ and archive/ directory.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

router = APIRouter()

MAX_FILE_BYTES = 256 * 1024
MAX_FILES_PER_CONTAINER = 50
RESERVED_FILES = {"log.jsonl", "summary.md"}

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_\-\.А-Яа-яёЁ]+")


def safe_name(name: str) -> str:
    if not name:
        raise HTTPException(400, "empty name")
    s = _SAFE_NAME_RE.sub("_", name.strip()).strip("._")
    if not s or s in (".", "..") or len(s) > 64:
        raise HTTPException(400, "invalid name")
    return s


def safe_filename(fn: str) -> str:
    fn = os.path.basename(fn or "")
    if not fn or fn in (".", "..") or len(fn) > 128:
        raise HTTPException(400, "invalid filename")
    if "/" in fn or "\\" in fn or "\x00" in fn:
        raise HTTPException(400, "invalid filename")
    return fn


def door_root(door_id: int) -> Path:
    from app import STATE_DIR
    p = STATE_DIR / f"door_{int(door_id)}"
    p.mkdir(parents=True, exist_ok=True)
    (p / "containers").mkdir(parents=True, exist_ok=True)
    (p / "archive").mkdir(parents=True, exist_ok=True)
    return p


def container_path(door_id: int, author: str) -> Path:
    root = door_root(door_id) / "containers"
    safe = safe_name(author)
    p = (root / safe).resolve()
    if root.resolve() not in p.parents and p != root.resolve():
        raise HTTPException(400, "path escape")
    p.mkdir(parents=True, exist_ok=True)
    return p


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_log(door_id: int, author: str, entry: dict) -> None:
    try:
        p = container_path(door_id, author) / "log.jsonl"
        entry = {"ts": now_iso(), **entry}
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def remember_key_moment(door_id: int, author: str, kind: str, text: str, context=None) -> None:
    try:
        p = container_path(door_id, author) / "summary.md"
        first_write = not p.exists()
        marker = {"proposal": "📜 PROPOSAL", "decision": "✅ DECISION",
                  "disagreement": "🔴 DISAGREEMENT", "agreement": "👍 AGREED",
                  "joined": "🟢 JOINED", "session": "🎙 SESSION"}.get(kind, "•")
        ctx_line = ""
        if context:
            bits = []
            if context.get("session_title"):
                bits.append(f"session=«{context['session_title']}»")
            if context.get("point"):
                bits.append(f"point=«{context['point']}»")
            if bits:
                ctx_line = f"\n  _{' · '.join(bits)}_"
        block = f"\n- **[{now_iso()}] {marker}** — {text}{ctx_line}\n"
        with p.open("a", encoding="utf-8") as f:
            if first_write:
                f.write(f"# Личные ключевые моменты — {safe_name(author)} (door {door_id})\n")
                f.write("_Сервер сам пишет сюда важное (предложения, решения, разногласия)._\n")
            f.write(block)
    except Exception:
        pass


def archive_session(door_id: int, state: dict):
    if not state.get("session_active") or not state.get("session_id"):
        return None
    sid = state["session_id"]
    redacted = json.loads(json.dumps(state))
    for slot in redacted.get("slots", []):
        if slot.get("api_key"):
            slot["api_key"] = "***"
    fname = door_root(door_id) / "archive" / f"{sid}.json"
    fname.write_text(json.dumps(redacted, ensure_ascii=False, indent=2), encoding="utf-8")
    return sid


@router.post("/door/{door_id}/container/write")
async def container_write(door_id: int, req: Request, token: str = Query(...)):
    from app import check_token, doors_state_lock, load_door_state, find_slot_by_token
    check_token(token)
    body = await req.json()
    slot_token = body.get("slot_token") or ""
    filename = safe_filename(body.get("filename") or "")
    content = body.get("content")
    mode = (body.get("mode") or "write").strip()
    if not isinstance(content, str):
        raise HTTPException(400, "content must be string")
    if mode not in ("write", "append"):
        raise HTTPException(400, "mode must be 'write' or 'append'")
    if filename in RESERVED_FILES:
        raise HTTPException(400, f"'{filename}' is server-managed")
    async with doors_state_lock(door_id):
        st = load_door_state(door_id)
        slot = find_slot_by_token(st, slot_token)
        if not slot:
            raise HTTPException(404, "slot not found")
        author = slot["name"]
        cdir = container_path(door_id, author)
        existing = [p for p in cdir.iterdir() if p.is_file() and p.name not in RESERVED_FILES]
        if filename not in {p.name for p in existing} and len(existing) >= MAX_FILES_PER_CONTAINER:
            raise HTTPException(400, f"too many files (>{MAX_FILES_PER_CONTAINER})")
        target = (cdir / filename).resolve()
        if cdir.resolve() not in target.parents:
            raise HTTPException(400, "path escape")
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(400, f"file too large (>{MAX_FILE_BYTES} bytes)")
        if mode == "append" and target.exists():
            with target.open("ab") as f:
                f.write(data)
        else:
            target.write_bytes(data)
        size = target.stat().st_size
        append_log(door_id, author, {"kind": "container_write", "filename": filename,
                                     "bytes": len(data), "mode": mode})
    return {"ok": True, "door_id": door_id, "author": author, "filename": filename,
            "size": size, "mode": mode}


@router.get("/door/{door_id}/container/{author}")
async def container_read(door_id: int, author: str, token: str = Query(...),
                         filename: str = Query(None)):
    from app import check_token
    check_token(token)
    cdir = container_path(door_id, author)
    if filename:
        fn = safe_filename(filename)
        target = (cdir / fn).resolve()
        if cdir.resolve() not in target.parents or not target.exists() or not target.is_file():
            raise HTTPException(404, "file not found")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise HTTPException(415, "file is not utf-8 text")
        return {"door_id": door_id, "author": safe_name(author), "filename": fn,
                "content": content, "size": target.stat().st_size}
    files = []
    for p in sorted(cdir.iterdir()):
        if p.is_file():
            files.append({"filename": p.name, "size": p.stat().st_size,
                          "modified": p.stat().st_mtime})
    return {"door_id": door_id, "author": safe_name(author), "files": files}


@router.get("/door/{door_id}/container/{author}/log")
async def container_log(door_id: int, author: str, token: str = Query(...),
                        limit: int = Query(200, ge=1, le=5000)):
    from app import check_token
    check_token(token)
    p = container_path(door_id, author) / "log.jsonl"
    if not p.exists():
        return {"door_id": door_id, "author": safe_name(author), "entries": []}
    lines = p.read_text(encoding="utf-8").splitlines()[-limit:]
    entries = []
    for ln in lines:
        try:
            entries.append(json.loads(ln))
        except Exception:
            continue
    return {"door_id": door_id, "author": safe_name(author), "entries": entries}


@router.get("/door/{door_id}/container/{author}/summary", response_class=PlainTextResponse)
async def container_summary(door_id: int, author: str, token: str = Query(...)):
    from app import check_token
    check_token(token)
    p = container_path(door_id, author) / "summary.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


@router.get("/door/{door_id}/containers")
async def containers_list(door_id: int, token: str = Query(...)):
    from app import check_token
    check_token(token)
    root = door_root(door_id) / "containers"
    out = []
    for d in sorted(root.iterdir()):
        if d.is_dir():
            files = [p.name for p in d.iterdir() if p.is_file()]
            out.append({"author": d.name, "files": files})
    return {"door_id": door_id, "containers": out}


@router.get("/door/{door_id}/archive")
async def archive_list(door_id: int, token: str = Query(...)):
    from app import check_token
    check_token(token)
    root = door_root(door_id) / "archive"
    out = []
    for p in sorted(root.iterdir(), reverse=True):
        if p.is_file() and p.suffix == ".json":
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                out.append({"session_id": p.stem, "title": data.get("title", ""),
                            "created_at": data.get("created_at"),
                            "decided_points": sum(1 for a in data.get("agenda", [])
                                                  if a.get("status") == "decided"),
                            "total_points": len(data.get("agenda", []))})
            except Exception:
                continue
    return {"door_id": door_id, "archive": out}


@router.get("/door/{door_id}/archive/{session_id}")
async def archive_get(door_id: int, session_id: str, token: str = Query(...)):
    from app import check_token
    check_token(token)
    sid = safe_name(session_id)
    p = door_root(door_id) / "archive" / f"{sid}.json"
    if not p.exists():
        raise HTTPException(404, "session not found")
    return json.loads(p.read_text(encoding="utf-8"))
