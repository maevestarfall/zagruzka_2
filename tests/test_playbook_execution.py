"""Tests for playbook plan resolution + real step execution.

These cover the gaps the previous pass left as «симуляция»:

  • ``record_step_outcome`` is now actually driven by the executor, so
    per-step ok/fail counters AND ``exec_log`` get populated.
  • ``resolve_plan`` / ``plan_directive`` feed a concrete plan into the
    prompt when «сначала смотри инструкцию» is ON.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def clean_pb():
    """Isolate the playbook addon-state around each test."""
    from bot.addons import state as addon_state
    from bot.addons.playbook import store

    keys = ["nodes", "steps", "seq", "exec_log", "instruction_first", "hermes_auto"]
    snap = {k: addon_state.get(store.ADDON, k, None) for k in keys}
    for k in keys:
        addon_state.set_(store.ADDON, k, {} if k in ("nodes", "steps") else None)
    yield store
    for k, v in snap.items():
        addon_state.set_(store.ADDON, k, v)


def _seed_leaf(store):
    root = store.add_node(None, "Контент")
    leaf = store.add_node(root["id"], "Красивая анимация для сайта")
    s1 = store.add_step(leaf["id"], name="Бриф", tools=["chat"], prompt="Собери бриф",
                        timing="сразу")
    s2 = store.add_step(leaf["id"], name="Ключевой кадр", tools=["media:photo"],
                        prompt="Сгенерируй кадр", timing="после брифа")
    return leaf["id"], [s1, s2]


# ── plan resolution / injection ─────────────────────────────────────────

def test_resolve_and_render_plan(clean_pb):
    store = clean_pb
    nid, _ = _seed_leaf(store)
    plan = store.resolve_plan("сделай красивую анимацию для сайта")
    assert plan and plan["node_id"] == nid
    assert len(plan["steps"]) == 2

    rendered = store.render_plan(plan)
    assert "Красивая анимация" in rendered
    assert "media:photo" in rendered
    assert "Шаг 1." in rendered and "Шаг 2." in rendered


def test_plan_directive_respects_toggle(clean_pb):
    store = clean_pb
    _seed_leaf(store)
    # OFF → empty.
    store.set_instruction_first(False)
    assert store.plan_directive("красивая анимация") == ""
    # ON → concrete plan injected.
    store.set_instruction_first(True)
    block = store.plan_directive("красивая анимация для сайта")
    assert "ПОДХОДЯЩИЙ ПЛАН" in block
    assert "media:photo" in block


def test_plan_directive_empty_when_no_match(clean_pb):
    store = clean_pb
    _seed_leaf(store)
    store.set_instruction_first(True)
    # A query with no token overlap should resolve to nothing.
    assert store.plan_directive("погода завтра в москве") == ""


# ── real execution drives Hermes data ───────────────────────────────────

@pytest.mark.asyncio
async def test_run_plan_records_outcomes(clean_pb, monkeypatch):
    store = clean_pb
    from bot.addons.playbook import executor

    nid, steps = _seed_leaf(store)

    calls = []

    async def fake_run_agent(user_id, user_text, cwd, *, cancel_event=None):
        calls.append(user_text)
        # First step "succeeds", second "fails" (admits it couldn't).
        if "Ключевой кадр" in user_text:
            return "Не удалось сгенерировать кадр — генератор недоступен."
        return "Готово, бриф собран."

    import bot.agent as agent
    monkeypatch.setattr(agent, "run_agent", fake_run_agent, raising=False)

    summary = await executor.run_plan(nid, user_id=1)

    assert summary["ok"] is True
    assert summary["ran"] == 2
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1
    assert len(calls) == 2  # the agent was actually invoked per step

    # Per-step counters were updated.
    after = store.get_steps(nid)
    assert after[0]["ok"] == 1 and after[0]["fail"] == 0
    assert after[1]["fail"] == 1 and after[1]["ok"] == 0

    # exec_log is no longer empty — Hermes has real data to learn from.
    log = store.exec_log()
    assert len(log) == 2
    outcomes = {e["outcome"] for e in log}
    assert outcomes == {"ok", "fail"}
    assert all(e["path"] for e in log)


@pytest.mark.asyncio
async def test_run_plan_handles_exceptions(clean_pb, monkeypatch):
    store = clean_pb
    from bot.addons.playbook import executor

    nid, _ = _seed_leaf(store)

    async def boom(user_id, user_text, cwd, *, cancel_event=None):
        raise RuntimeError("brain offline")

    import bot.agent as agent
    monkeypatch.setattr(agent, "run_agent", boom, raising=False)

    summary = await executor.run_plan(nid, user_id=1)
    # A crashing brain is recorded as failures, not an aborted run.
    assert summary["ran"] == 2
    assert summary["failed"] == 2
    assert store.exec_log()[0]["outcome"] == "fail"


@pytest.mark.asyncio
async def test_run_plan_no_steps(clean_pb):
    store = clean_pb
    from bot.addons.playbook import executor

    empty = store.add_node(None, "Пустой раздел")
    summary = await executor.run_plan(empty["id"], user_id=1)
    assert summary["ok"] is False
    assert summary["reason"] == "no_steps"
    assert summary["ran"] == 0


# ── explicit LLM self-assessment overrides the heuristic ────────────────

@pytest.mark.asyncio
async def test_judge_overrides_heuristic(clean_pb, monkeypatch):
    store = clean_pb
    from bot.addons import _llm
    from bot.addons.playbook import executor

    nid, _ = _seed_leaf(store)

    # Agent claims success ("Готово") — the keyword heuristic would call
    # BOTH steps ok. But the judge says FAIL → outcome must be fail.
    async def fake_run_agent(user_id, user_text, cwd, *, cancel_event=None):
        return "Готово, всё отлично."

    async def fake_judge(system, user, *, max_tokens=60, temperature=0.0):
        return "FAIL\nрезультат не соответствует требованиям"

    import bot.agent as agent
    monkeypatch.setattr(agent, "run_agent", fake_run_agent, raising=False)
    monkeypatch.setattr(_llm, "chat_once", fake_judge, raising=False)

    summary = await executor.run_plan(nid, user_id=1)
    assert summary["succeeded"] == 0
    assert summary["failed"] == 2
    # The judge's reason is captured into the note (and exec_log).
    assert "оценка" in summary["steps"][0]["note"].lower()


@pytest.mark.asyncio
async def test_judge_unavailable_falls_back_to_heuristic(clean_pb, monkeypatch):
    store = clean_pb
    from bot.addons import _llm
    from bot.addons.playbook import executor

    nid, _ = _seed_leaf(store)

    async def fake_run_agent(user_id, user_text, cwd, *, cancel_event=None):
        return "Готово."

    async def no_brain(system, user, *, max_tokens=60, temperature=0.0):
        raise _llm.NoBrainError("no key")

    import bot.agent as agent
    monkeypatch.setattr(agent, "run_agent", fake_run_agent, raising=False)
    monkeypatch.setattr(_llm, "chat_once", no_brain, raising=False)

    summary = await executor.run_plan(nid, user_id=1)
    # No judge → heuristic: "Готово" has no failure markers → ok.
    assert summary["succeeded"] == 2
    assert summary["failed"] == 0


# ── isolated execution: chat history is left untouched ──────────────────

@pytest.mark.asyncio
async def test_run_plan_isolates_history(clean_pb, monkeypatch):
    store = clean_pb
    from bot.addons.playbook import executor
    from bot.storage import storage

    nid, _ = _seed_leaf(store)
    uid = 4242

    storage.clear_history(uid)
    storage.append_history(uid, {"role": "user", "content": "привет"})
    before = storage.get_history(uid)

    async def fake_run_agent(user_id, user_text, cwd, *, cancel_event=None):
        # Simulate what the real agent does: write to history.
        storage.append_history(user_id, {"role": "user", "content": user_text})
        storage.append_history(user_id, {"role": "assistant", "content": "ок"})
        return "Готово."

    import bot.agent as agent
    monkeypatch.setattr(agent, "run_agent", fake_run_agent, raising=False)

    await executor.run_plan(nid, user_id=uid)  # isolate_history=True by default
    after = storage.get_history(uid)
    assert after == before  # plan run left the real conversation untouched

    # Opting out keeps the agent's writes.
    await executor.run_plan(nid, user_id=uid, isolate_history=False)
    assert len(storage.get_history(uid)) > len(before)
    storage.clear_history(uid)

