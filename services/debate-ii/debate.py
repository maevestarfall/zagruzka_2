"""
debate.py — per-door debate endpoints.
Each door (room) has its own independent state, slots, agenda, containers.
"""

import asyncio
import os
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from containers import (
    safe_name,
    container_path,
    append_log,
    remember_key_moment,
    archive_session,
)

router = APIRouter()

MAX_SLOTS = 5
HEARTBEAT_TIMEOUT = 120


def empty_slot(i: int) -> dict:
    return {"slot_id": i, "occupied": False, "name": None, "type": None, "model": None,
            "api_base": None, "api_key": None, "status": "empty", "last_seen": 0.0,
            "joined_at": 0.0, "last_message_at": 0.0, "token": None}


def empty_door_state(door_id: int, title: str = "", topic: str = "",
                     link: str = "", consensus_mode: str = "unanimous") -> dict:
    return {
        "door_id": door_id,
        "door_title": title,
        "door_topic": topic,
        "door_link": link,
        "session_active": False,
        "session_id": None,
        "title": "",
        "tz_text": "",
        "required_debaters": 3,
        "max_slots": MAX_SLOTS,
        "consensus_mode": consensus_mode,
        "created_at": time.time(),
        "slots": [empty_slot(i) for i in range(MAX_SLOTS)],
        "agenda": [],
        "current": 0,
        "proposal": None,
        "proposal_author": None,
        "agreed": [],
        "started_at": None,
        # Relay engine: debate starts only once `required_debaters` have gathered.
        "started": False,
        "debate_mode": "relay",
        # Append-only event feed for live push to the owner. Each event:
        # {"id": int, "ts": float, "kind": str, "text": str}
        "events": [],
        "event_seq": 0,
    }


def new_point(text: str) -> dict:
    """An agenda point = one debate 'atom'. Debaters relay turns over it;
    a free debater may take an open atom ahead of the queue (parallelism)."""
    return {
        "text": str(text),
        "status": "open",          # open | in_progress | decided
        "decision": None,
        "messages": [],
        "assignee": None,          # debater currently working this atom ahead-of-queue
        "order": [],               # relay turn order (join order) for the active atom
        "turn": None,              # name whose turn it is right now
        "contributions": [],       # [{author, text, decision, ts}]
    }


def emit_event(state: dict, kind: str, text: str) -> dict:
    state["event_seq"] = int(state.get("event_seq", 0)) + 1
    ev = {"id": state["event_seq"], "ts": time.time(), "kind": kind, "text": text}
    state.setdefault("events", []).append(ev)
    # keep the feed bounded
    if len(state["events"]) > 500:
        state["events"] = state["events"][-500:]
    return ev


def active_names(state: dict) -> list:
    return [s["name"] for s in state["slots"]
            if s["occupied"] and s["status"] != "offline" and s.get("name")]


def quorum_reached(state: dict) -> bool:
    return len(active_names(state)) >= int(state.get("required_debaters", 1))


def _first_open_index(state: dict):
    """Index of the active atom (first not-decided agenda point), or None."""
    for i, p in enumerate(state.get("agenda", [])):
        if p.get("status") != "decided":
            return i
    return None


def _sync_current(state: dict) -> None:
    idx = _first_open_index(state)
    state["current"] = idx if idx is not None else len(state.get("agenda", []))


def assign_relay(state: dict) -> None:
    """Set up the relay: the active atom gets a turn-order of all active
    debaters; the first debater is on the clock. Parallel atoms are claimed
    on demand via /next (work_ahead)."""
    names = active_names(state)
    idx = _first_open_index(state)
    if idx is None or not names:
        return
    p = state["agenda"][idx]
    p["order"] = names
    if not p.get("turn") or p["turn"] not in names:
        p["turn"] = names[0]
    if p["status"] == "open":
        p["status"] = "in_progress"
    _sync_current(state)


def _advance_turn(state: dict, point: dict) -> None:
    """Move the relay baton to the next active debater on this atom."""
    order = [n for n in point.get("order", []) if n in active_names(state)]
    if not order:
        point["turn"] = None
        return
    cur = point.get("turn")
    if cur in order:
        nxt = order[(order.index(cur) + 1) % len(order)]
    else:
        nxt = order[0]
    point["turn"] = nxt


def refresh_slot_statuses(state: dict) -> None:
    now = time.time()
    for slot in state["slots"]:
        if slot["occupied"] and slot["status"] != "offline":
            if now - slot["last_seen"] > HEARTBEAT_TIMEOUT:
                slot["status"] = "offline"


def find_slot_by_token(state: dict, slot_token: str):
    for s in state["slots"]:
        if s["occupied"] and s.get("token") == slot_token:
            return s
    return None


def redact(state: dict) -> dict:
    import json
    redacted = json.loads(json.dumps(state))
    for slot in redacted.get("slots", []):
        if slot.get("api_key"):
            slot["api_key"] = "***"
    return redacted


def consensus_threshold(state: dict):
    occupied = [s["name"] for s in state["slots"]
                if s["occupied"] and s["status"] != "offline" and s.get("name")]
    if state.get("consensus_mode", "unanimous") == "majority":
        needed = max(1, (len(occupied) // 2) + 1)
    else:
        needed = len(occupied)
    return occupied, needed


def maybe_decide(state: dict) -> bool:
    if not state["proposal"]:
        return False
    voters, needed = consensus_threshold(state)
    if not voters or len(voters) < state["required_debaters"]:
        return False
    agreed_count = len(set(state["agreed"]) & set(voters))
    if agreed_count >= needed and needed > 0:
        decision_text = state["proposal"]
        point_text = ""
        if state["current"] < len(state["agenda"]):
            state["agenda"][state["current"]]["status"] = "decided"
            state["agenda"][state["current"]]["decision"] = decision_text
            point_text = state["agenda"][state["current"]]["text"]
        door_id = state["door_id"]
        for a in voters:
            remember_key_moment(door_id, a, "decision",
                f"По пункту «{point_text}» принято: {decision_text}",
                {"session_title": state.get("title", "")})
            append_log(door_id, a, {"kind": "decision", "point": point_text,
                "decision": decision_text, "session_title": state.get("title", "")})
        state["proposal"] = None
        state["proposal_author"] = None
        state["agreed"] = []
        state["current"] += 1
        return True
    return False


@router.post("/door/{door_id}/session/start")
async def session_start(door_id: int, req: Request, token: str = Query(...)):
    from app import check_token, doors_state_lock, load_door_state, save_door_state, door_exists
    check_token(token)
    if not door_exists(door_id):
        raise HTTPException(404, f"door {door_id} not found")
    body = await req.json()
    title = (body.get("title") or "").strip()
    tz_text = body.get("tz_text") or ""
    required = int(body.get("required_debaters") or 3)
    agenda_in = body.get("agenda") or []
    mode = (body.get("debate_mode") or "relay").strip()
    if not title:
        raise HTTPException(400, "title required")
    if required < 1 or required > MAX_SLOTS:
        raise HTTPException(400, f"required_debaters must be 1..{MAX_SLOTS}")
    if mode not in ("relay", "vote"):
        raise HTTPException(400, "debate_mode must be 'relay' or 'vote'")
    async with doors_state_lock(door_id):
        old = load_door_state(door_id)
        if old.get("session_active"):
            archive_session(door_id, old)
        sid = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + safe_name(title)[:32]
        st = empty_door_state(door_id, old.get("door_title", ""), old.get("door_topic", ""),
                              old.get("door_link", ""), old.get("consensus_mode", "unanimous"))
        st["session_active"] = True
        st["session_id"] = sid
        st["title"] = title
        st["tz_text"] = tz_text
        st["required_debaters"] = required
        st["debate_mode"] = mode
        st["created_at"] = time.time()
        if isinstance(agenda_in, list) and agenda_in:
            st["agenda"] = [new_point(p) for p in agenda_in]
        else:
            st["agenda"] = [new_point(title)]
        emit_event(st, "session", f"Сессия «{title}» открыта · нужно дебатёров: {required} · режим: {mode}")
        save_door_state(door_id, st)
    return {"ok": True, "door_id": door_id, "session_id": sid, "session": redact(st)}


@router.post("/door/{door_id}/session/end")
async def session_end(door_id: int, token: str = Query(...)):
    from app import check_token, doors_state_lock, load_door_state, save_door_state
    check_token(token)
    async with doors_state_lock(door_id):
        st = load_door_state(door_id)
        sid = archive_session(door_id, st)
        for s in st["slots"]:
            if s["occupied"] and s.get("name"):
                remember_key_moment(door_id, s["name"], "session",
                    f"Сессия «{st.get('title','')}» завершена (id={sid}).")
        fresh = empty_door_state(door_id, st.get("door_title", ""), st.get("door_topic", ""),
                                 st.get("door_link", ""), st.get("consensus_mode", "unanimous"))
        save_door_state(door_id, fresh)
    return {"ok": True, "door_id": door_id, "archived_session_id": sid}


@router.post("/door/{door_id}/tz")
async def upload_tz(door_id: int, req: Request, token: str = Query(...)):
    from app import check_token, doors_state_lock, load_door_state, save_door_state
    check_token(token)
    body = await req.json()
    tz_text = body.get("tz_text") or ""
    async with doors_state_lock(door_id):
        st = load_door_state(door_id)
        st["tz_text"] = tz_text
        save_door_state(door_id, st)
    return {"ok": True, "door_id": door_id, "length": len(tz_text)}


@router.post("/door/{door_id}/join")
async def join(door_id: int, req: Request, token: str = Query(...)):
    from app import check_token, doors_state_lock, load_door_state, save_door_state, door_exists
    check_token(token)
    if not door_exists(door_id):
        raise HTTPException(404, f"door {door_id} not found")
    body = await req.json()
    name = (body.get("name") or "").strip()
    ptype = (body.get("type") or "external").strip()
    model = body.get("model")
    api_key = body.get("api_key")
    api_base = body.get("api_base") or "https://openrouter.ai/api/v1"
    if not name:
        raise HTTPException(400, "name required")
    if ptype not in ("external", "openrouter"):
        raise HTTPException(400, "type must be 'external' or 'openrouter'")
    if ptype == "openrouter" and (not model or not api_key):
        raise HTTPException(400, "openrouter type requires model and api_key")
    safe_name(name)
    async with doors_state_lock(door_id):
        st = load_door_state(door_id)
        if not st["session_active"]:
            raise HTTPException(400, "no active session — host must call /door/{id}/session/start")
        refresh_slot_statuses(st)
        for s in st["slots"]:
            if s["occupied"] and s["name"] == name:
                s["last_seen"] = time.time()
                save_door_state(door_id, st)
                append_log(door_id, name, {"kind": "rejoin", "session_title": st.get("title", "")})
                return {"ok": True, "door_id": door_id, "slot_id": s["slot_id"],
                        "slot_token": s["token"], "reconnected": True}
        empty = next((s for s in st["slots"] if not s["occupied"]), None)
        if empty is None:
            raise HTTPException(409, "all slots full")
        slot_token = os.urandom(16).hex()
        empty.update({"occupied": True, "name": name, "type": ptype, "model": model,
                      "api_base": api_base if ptype == "openrouter" else None,
                      "api_key": api_key if ptype == "openrouter" else None,
                      "status": "waiting", "last_seen": time.time(),
                      "joined_at": time.time(), "token": slot_token})
        container_path(door_id, name)
        remember_key_moment(door_id, name, "joined",
            f"Подключился к двери {door_id}, сессия «{st.get('title','')}». Слот {empty['slot_id']+1}/{MAX_SLOTS}.")
        append_log(door_id, name, {"kind": "joined", "slot_id": empty["slot_id"],
            "type": ptype, "model": model, "session_title": st.get("title", "")})
        have = len(active_names(st))
        emit_event(st, "joined", f"🟢 {name} зашёл ({have}/{st['required_debaters']})")
        occupied = [s for s in st["slots"] if s["occupied"] and s["status"] != "offline"]
        if not st.get("started") and len(occupied) >= st["required_debaters"]:
            # Quorum reached — debate officially begins.
            for s in occupied:
                if s["status"] == "waiting":
                    s["status"] = "thinking"
            if st["started_at"] is None:
                st["started_at"] = time.time()
            st["started"] = True
            assign_relay(st)
            emit_event(st, "start",
                f"🚀 Набралось {len(occupied)} дебатёров — старт! Первый ход: {st['agenda'][_first_open_index(st)]['turn']}"
                if _first_open_index(st) is not None else "🚀 Старт!")
        save_door_state(door_id, st)
        return {"ok": True, "door_id": door_id, "slot_id": empty["slot_id"],
                "slot_token": slot_token, "started": st.get("started", False),
                "have": have, "need": st["required_debaters"], "reconnected": False}


@router.post("/door/{door_id}/heartbeat")
async def heartbeat(door_id: int, req: Request, token: str = Query(...)):
    from app import check_token, doors_state_lock, load_door_state, save_door_state
    check_token(token)
    body = await req.json()
    slot_token = body.get("slot_token") or ""
    async with doors_state_lock(door_id):
        st = load_door_state(door_id)
        slot = find_slot_by_token(st, slot_token)
        if not slot:
            raise HTTPException(404, "slot not found")
        slot["last_seen"] = time.time()
        if slot["status"] == "offline":
            slot["status"] = "thinking"
        save_door_state(door_id, st)
    return {"ok": True}


@router.post("/door/{door_id}/leave")
async def leave(door_id: int, req: Request, token: str = Query(...)):
    from app import check_token, doors_state_lock, load_door_state, save_door_state
    check_token(token)
    body = await req.json()
    slot_token = body.get("slot_token") or ""
    async with doors_state_lock(door_id):
        st = load_door_state(door_id)
        for s in st["slots"]:
            if s.get("token") == slot_token:
                idx = s["slot_id"]
                name = s.get("name")
                if name:
                    append_log(door_id, name, {"kind": "left", "session_title": st.get("title", "")})
                st["slots"][idx] = empty_slot(idx)
                save_door_state(door_id, st)
                return {"ok": True}
        raise HTTPException(404, "slot not found")


@router.get("/door/{door_id}/state")
async def get_state(door_id: int, token: str = Query(...)):
    from app import check_token, doors_state_lock, load_door_state, save_door_state, door_exists
    check_token(token)
    if not door_exists(door_id):
        raise HTTPException(404, f"door {door_id} not found")
    async with doors_state_lock(door_id):
        st = load_door_state(door_id)
        refresh_slot_statuses(st)
        save_door_state(door_id, st)
        return redact(st)


@router.post("/door/{door_id}/say")
async def say(door_id: int, req: Request, token: str = Query(...)):
    from app import check_token, doors_state_lock, load_door_state, save_door_state
    check_token(token)
    body = await req.json()
    slot_token = body.get("slot_token") or ""
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    async with doors_state_lock(door_id):
        st = load_door_state(door_id)
        slot = find_slot_by_token(st, slot_token)
        if not slot:
            raise HTTPException(404, "slot not found")
        if not st.get("started"):
            raise HTTPException(425, f"quorum not reached ({len(active_names(st))}/{st['required_debaters']})")
        if st["current"] >= len(st["agenda"]):
            raise HTTPException(400, "no open agenda point")
        point_text = st["agenda"][st["current"]]["text"]
        st["agenda"][st["current"]]["messages"].append(
            {"author": slot["name"], "text": text, "ts": time.time()})
        slot["last_seen"] = time.time()
        slot["last_message_at"] = time.time()
        slot["status"] = "spoke"
        append_log(door_id, slot["name"], {"kind": "say", "point": point_text,
            "text": text, "session_title": st.get("title", "")})
        save_door_state(door_id, st)
    return {"ok": True}


@router.post("/door/{door_id}/propose")
async def propose(door_id: int, req: Request, token: str = Query(...)):
    from app import check_token, doors_state_lock, load_door_state, save_door_state
    check_token(token)
    body = await req.json()
    slot_token = body.get("slot_token") or ""
    proposal = (body.get("proposal") or "").strip()
    if not proposal:
        raise HTTPException(400, "proposal required")
    async with doors_state_lock(door_id):
        st = load_door_state(door_id)
        slot = find_slot_by_token(st, slot_token)
        if not slot:
            raise HTTPException(404, "slot not found")
        if not st.get("started"):
            raise HTTPException(425, f"quorum not reached ({len(active_names(st))}/{st['required_debaters']})")
        point_text = st["agenda"][st["current"]]["text"] if st["current"] < len(st["agenda"]) else ""
        st["proposal"] = proposal
        st["proposal_author"] = slot["name"]
        st["agreed"] = [slot["name"]]
        slot["last_seen"] = time.time()
        slot["status"] = "spoke"
        for s in st["slots"]:
            if s["occupied"] and s.get("name"):
                remember_key_moment(door_id, s["name"], "proposal",
                    f"{slot['name']} предложил: «{proposal[:200]}» (пункт: «{point_text}»)",
                    {"session_title": st.get("title", "")})
                append_log(door_id, s["name"], {"kind": "proposal_seen", "by": slot["name"],
                    "proposal": proposal, "point": point_text, "session_title": st.get("title", "")})
        decided = maybe_decide(st)
        if decided:
            for s in st["slots"]:
                if s["occupied"]:
                    s["status"] = "thinking"
        save_door_state(door_id, st)
    return {"ok": True, "decided": decided}


@router.post("/door/{door_id}/agree")
async def agree(door_id: int, req: Request, token: str = Query(...)):
    from app import check_token, doors_state_lock, load_door_state, save_door_state
    check_token(token)
    body = await req.json()
    slot_token = body.get("slot_token") or ""
    async with doors_state_lock(door_id):
        st = load_door_state(door_id)
        slot = find_slot_by_token(st, slot_token)
        if not slot:
            raise HTTPException(404, "slot not found")
        if not st["proposal"]:
            raise HTTPException(400, "no active proposal")
        if slot["name"] not in st["agreed"]:
            st["agreed"].append(slot["name"])
        slot["last_seen"] = time.time()
        slot["status"] = "spoke"
        append_log(door_id, slot["name"], {"kind": "agree", "proposal": st["proposal"],
            "session_title": st.get("title", "")})
        decided = maybe_decide(st)
        if decided:
            for s in st["slots"]:
                if s["occupied"]:
                    s["status"] = "thinking"
        save_door_state(door_id, st)
    return {"ok": True, "decided": decided}


@router.post("/door/{door_id}/disagree")
async def disagree(door_id: int, req: Request, token: str = Query(...)):
    from app import check_token, doors_state_lock, load_door_state, save_door_state
    check_token(token)
    body = await req.json()
    slot_token = body.get("slot_token") or ""
    async with doors_state_lock(door_id):
        st = load_door_state(door_id)
        slot = find_slot_by_token(st, slot_token)
        if not slot:
            raise HTTPException(404, "slot not found")
        killed = st.get("proposal")
        killed_by = slot["name"]
        st["proposal"] = None
        st["proposal_author"] = None
        st["agreed"] = []
        slot["last_seen"] = time.time()
        slot["status"] = "spoke"
        if killed:
            for s in st["slots"]:
                if s["occupied"] and s.get("name"):
                    remember_key_moment(door_id, s["name"], "disagreement",
                        f"{killed_by} отклонил предложение: «{killed[:200]}»",
                        {"session_title": st.get("title", "")})
                    append_log(door_id, s["name"], {"kind": "disagree_seen", "by": killed_by,
                        "killed_proposal": killed, "session_title": st.get("title", "")})
        save_door_state(door_id, st)
    return {"ok": True}


async def _run_openrouter_slot(door_id: int, slot: dict, st_snapshot: dict) -> dict:
    point = (st_snapshot["agenda"][st_snapshot["current"]]
             if st_snapshot["current"] < len(st_snapshot["agenda"]) else None)
    if not point:
        return {"slot_id": slot["slot_id"], "text": "", "action": "noop"}
    others = [s["name"] for s in st_snapshot["slots"]
              if s["occupied"] and s["name"] != slot["name"]]
    transcript = "\n".join(f"{m['author']}: {m['text']}" for m in point["messages"][-20:])
    summary_path = container_path(door_id, slot["name"]) / "summary.md"
    personal_notes = ""
    if summary_path.exists():
        try:
            personal_notes = summary_path.read_text(encoding="utf-8")[-2000:]
        except Exception:
            personal_notes = ""
    system = (
        f"Ты — {slot['name']}, участник дебатов на тему: «{st_snapshot['title']}».\n"
        f"Текущий пункт: {point['text']}\n"
        f"Другие участники: {', '.join(others) or '—'}\n"
        f"ТЗ:\n{st_snapshot.get('tz_text','')[:4000]}\n\n"
        f"Твои личные ключевые заметки:\n{personal_notes or '(пусто)'}\n\n"
        "Отвечай кратко (2-4 предложения). В ПОСЛЕДНЕЙ строке поставь одно из:\n"
        "  ПРЕДЛОЖЕНИЕ: <текст>\n  СОГЛАСЕН\n  НЕ СОГЛАСЕН"
    )
    user = (f"Текущее предложение: {st_snapshot.get('proposal') or '—'}\n\n"
            f"История:\n{transcript or '(пусто)'}")
    payload = {"model": slot["model"],
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}],
               "max_tokens": 600}
    headers = {"Authorization": f"Bearer {slot['api_key']}", "Content-Type": "application/json"}
    url = (slot.get("api_base") or "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            text = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return {"slot_id": slot["slot_id"], "text": f"[ошибка LLM: {e}]", "action": "error"}
    last = text.strip().splitlines()[-1].strip() if text else ""
    action = "say"
    proposal_text = None
    if last.startswith("ПРЕДЛОЖЕНИЕ:"):
        action = "propose"
        proposal_text = last.split(":", 1)[1].strip()
    elif last == "СОГЛАСЕН":
        action = "agree"
    elif last == "НЕ СОГЛАСЕН":
        action = "disagree"
    return {"slot_id": slot["slot_id"], "text": text, "action": action, "proposal": proposal_text}


@router.post("/door/{door_id}/round")
async def round_(door_id: int, token: str = Query(...)):
    from app import check_token, doors_state_lock, load_door_state, save_door_state
    check_token(token)
    st = load_door_state(door_id)
    if not st["session_active"]:
        raise HTTPException(400, "no active session")
    if not st.get("started"):
        raise HTTPException(425, f"quorum not reached ({len(active_names(st))}/{st['required_debaters']})")
    if st["current"] >= len(st["agenda"]):
        return {"ok": True, "message": "agenda finished"}
    openrouter_slots = [s for s in st["slots"]
                        if s["occupied"] and s["type"] == "openrouter" and s["status"] != "offline"]
    if not openrouter_slots:
        return {"ok": True, "message": "no openrouter slots", "results": []}
    results = await asyncio.gather(*[_run_openrouter_slot(door_id, s, st) for s in openrouter_slots])
    async with doors_state_lock(door_id):
        st = load_door_state(door_id)
        for res in results:
            slot = st["slots"][res["slot_id"]]
            if not slot["occupied"]:
                continue
            slot["last_seen"] = time.time()
            if res["action"] == "error":
                slot["status"] = "spoke"
                append_log(door_id, slot["name"], {"kind": "llm_error", "text": res["text"]})
                continue
            if st["current"] < len(st["agenda"]):
                st["agenda"][st["current"]]["messages"].append(
                    {"author": slot["name"], "text": res["text"], "ts": time.time()})
                point_text = st["agenda"][st["current"]]["text"]
                append_log(door_id, slot["name"], {"kind": "say", "point": point_text,
                    "text": res["text"], "session_title": st.get("title", "")})
            slot["last_message_at"] = time.time()
            slot["status"] = "spoke"
            if res["action"] == "propose" and res.get("proposal"):
                st["proposal"] = res["proposal"]
                st["proposal_author"] = slot["name"]
                st["agreed"] = [slot["name"]]
                for s in st["slots"]:
                    if s["occupied"] and s.get("name"):
                        remember_key_moment(door_id, s["name"], "proposal",
                            f"{slot['name']} предложил: «{res['proposal'][:200]}»",
                            {"session_title": st.get("title", "")})
            elif res["action"] == "agree":
                if st["proposal"] and slot["name"] not in st["agreed"]:
                    st["agreed"].append(slot["name"])
                    append_log(door_id, slot["name"], {"kind": "agree", "proposal": st["proposal"]})
            elif res["action"] == "disagree":
                killed = st.get("proposal")
                st["proposal"] = None
                st["proposal_author"] = None
                st["agreed"] = []
                if killed:
                    for s in st["slots"]:
                        if s["occupied"] and s.get("name"):
                            remember_key_moment(door_id, s["name"], "disagreement",
                                f"{slot['name']} отклонил: «{killed[:200]}»",
                                {"session_title": st.get("title", "")})
            maybe_decide(st)
        save_door_state(door_id, st)
    return {"ok": True, "door_id": door_id,
            "results": [{"slot_id": r["slot_id"], "action": r["action"]} for r in results]}


# ============================ RELAY ENGINE ============================
# Turn-based relay over agenda "atoms" + parallel work-ahead, with a live
# event feed for the bot to push to the owner. The exact debate rules are
# expected to be refined later; this is a working baseline.

@router.get("/door/{door_id}/next")
async def relay_next(door_id: int, token: str = Query(...), slot_token: str = Query(...)):
    """Tell a debater what to do right now: speak (their turn on the active
    atom), work_ahead (claim a free atom in parallel), or wait."""
    from app import check_token, doors_state_lock, load_door_state, save_door_state, door_exists
    check_token(token)
    if not door_exists(door_id):
        raise HTTPException(404, f"door {door_id} not found")
    async with doors_state_lock(door_id):
        st = load_door_state(door_id)
        refresh_slot_statuses(st)
        slot = find_slot_by_token(st, slot_token)
        if not slot:
            raise HTTPException(404, "slot not found")
        slot["last_seen"] = time.time()
        if not st.get("started"):
            save_door_state(door_id, st)
            return {"action": "wait", "reason": "waiting_quorum",
                    "have": len(active_names(st)), "need": st["required_debaters"]}
        me = slot["name"]
        idx = _first_open_index(st)
        if idx is None:
            save_door_state(door_id, st)
            return {"action": "done", "reason": "agenda_finished"}
        active = st["agenda"][idx]
        if not active.get("turn"):
            assign_relay(st)
            active = st["agenda"][idx]
        # My turn on the active atom?
        if active.get("turn") == me:
            save_door_state(door_id, st)
            return {"action": "speak", "point_index": idx, "point": active["text"],
                    "decision_so_far": active.get("decision"),
                    "history": active.get("contributions", [])[-10:]}
        # Not my turn: try to claim a parallel atom to work ahead.
        for j, p in enumerate(st["agenda"]):
            if j == idx:
                continue
            if p.get("status") == "open" and not p.get("assignee"):
                p["assignee"] = me
                p["status"] = "in_progress"
                emit_event(st, "parallel", f"⚡ {me} параллельно взял пункт {j+1}: «{p['text'][:60]}»")
                append_log(door_id, me, {"kind": "claim_parallel", "point": p["text"]})
                save_door_state(door_id, st)
                return {"action": "work_ahead", "point_index": j, "point": p["text"]}
        # Already assigned a parallel atom of my own and still in progress?
        mine = next((k for k, p in enumerate(st["agenda"])
                     if p.get("assignee") == me and p.get("status") != "decided"), None)
        if mine is not None:
            save_door_state(door_id, st)
            return {"action": "work_ahead", "point_index": mine,
                    "point": st["agenda"][mine]["text"]}
        save_door_state(door_id, st)
        return {"action": "wait", "reason": "not_your_turn",
                "active_point_index": idx, "turn": active.get("turn")}


@router.post("/door/{door_id}/contribute")
async def relay_contribute(door_id: int, req: Request, token: str = Query(...)):
    """A debater submits work on an atom. Body:
       { slot_token, point_index, text, decision? }
    - text: the reasoning / argument (always recorded + pushed).
    - decision: if non-empty, the atom is resolved with this decision and the
      baton is handed on; otherwise the relay baton passes to the next debater."""
    from app import check_token, doors_state_lock, load_door_state, save_door_state
    check_token(token)
    body = await req.json()
    slot_token = body.get("slot_token") or ""
    text = (body.get("text") or "").strip()
    decision = (body.get("decision") or "").strip()
    pidx = body.get("point_index")
    async with doors_state_lock(door_id):
        st = load_door_state(door_id)
        slot = find_slot_by_token(st, slot_token)
        if not slot:
            raise HTTPException(404, "slot not found")
        if not st.get("started"):
            raise HTTPException(425, f"quorum not reached ({len(active_names(st))}/{st['required_debaters']})")
        me = slot["name"]
        if pidx is None:
            pidx = _first_open_index(st)
        try:
            pidx = int(pidx)
            point = st["agenda"][pidx]
        except (TypeError, ValueError, IndexError):
            raise HTTPException(400, "bad point_index")
        if point.get("status") == "decided":
            raise HTTPException(409, "point already decided")
        # Authorisation: turn-holder of the active atom, or its parallel assignee.
        is_turn = point.get("turn") == me
        is_assignee = point.get("assignee") == me
        if not (is_turn or is_assignee):
            raise HTTPException(403, f"not your turn (turn={point.get('turn')}, assignee={point.get('assignee')})")
        if not text and not decision:
            raise HTTPException(400, "text or decision required")
        now = time.time()
        point.setdefault("contributions", []).append(
            {"author": me, "text": text, "decision": decision or None, "ts": now})
        point.setdefault("messages", []).append({"author": me, "text": text or decision, "ts": now})
        slot["last_seen"] = now
        slot["last_message_at"] = now
        slot["status"] = "spoke"
        append_log(door_id, me, {"kind": "contribute", "point": point["text"],
            "text": text, "decision": decision or None, "session_title": st.get("title", "")})
        if text:
            emit_event(st, "think", f"💬 {me} по пункту {pidx+1}: {text[:140]}")
        if decision:
            point["status"] = "decided"
            point["decision"] = decision
            point["turn"] = None
            point["assignee"] = None
            remember_key_moment(door_id, me, "decision",
                f"По пункту «{point['text']}» принято: {decision}",
                {"session_title": st.get("title", "")})
            emit_event(st, "decision", f"✅ Пункт {pidx+1} решён ({me}): {decision[:140]}")
            # advance relay onto the next still-open atom
            nidx = _first_open_index(st)
            if nidx is not None:
                assign_relay(st)
                nxt = st["agenda"][nidx]
                emit_event(st, "handoff", f"➡️ Переходим к пункту {nidx+1}, ход: {nxt.get('turn')}")
            else:
                emit_event(st, "finished", "🏁 Повестка завершена — все пункты решены.")
        elif is_turn:
            prev = me
            _advance_turn(st, point)
            nxt = point.get("turn")
            if nxt and nxt != prev:
                emit_event(st, "handoff", f"➡️ {prev} обдумал и передал ход {nxt} (пункт {pidx+1})")
        _sync_current(st)
        save_door_state(door_id, st)
    return {"ok": True, "door_id": door_id, "point_index": pidx,
            "decided": bool(decision), "turn": st["agenda"][pidx].get("turn")}


@router.get("/door/{door_id}/events")
async def relay_events(door_id: int, token: str = Query(...),
                       since: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500)):
    """Live event feed for the owner's bot to poll. Returns events with id>since."""
    from app import check_token, load_door_state, door_exists
    check_token(token)
    if not door_exists(door_id):
        raise HTTPException(404, f"door {door_id} not found")
    st = load_door_state(door_id)
    evs = [e for e in st.get("events", []) if int(e.get("id", 0)) > since][-limit:]
    return {"door_id": door_id, "last_id": st.get("event_seq", 0), "events": evs}
