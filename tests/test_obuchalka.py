"""«🎓 Обучалка»: token gating, role split, lock-at-10, handoff, JSONL export."""

from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture()
def ob(tmp_path, monkeypatch):
    """Fresh store + isolated SQLite DB + in-memory addon state."""
    monkeypatch.setenv("OBUCHALKA_DB_PATH", str(tmp_path / "obuchalka.db"))
    from bot.addons.obuchalka import store as _store
    importlib.reload(_store)

    fake_state: dict = {}

    def _get(addon, key, default=None):
        return fake_state.get(key, default)

    def _set(addon, key, value):
        fake_state[key] = value

    monkeypatch.setattr(_store.addon_state, "get", _get)
    monkeypatch.setattr(_store.addon_state, "set_", _set)
    _store.init_db()
    from bot.addons.obuchalka import export as _export
    importlib.reload(_export)
    # export imports store fresh — repoint it at the reloaded module
    monkeypatch.setattr(_export, "store", _store)
    return _store, _export


# ------------------------------------------------------------- tokens -------

def test_resolve_role_unknown_and_disabled(ob):
    store, _ = ob
    assert store.resolve_role("nope") is None
    token, link = store.generate_link("teacher")
    assert token in link
    assert store.resolve_role(token) == "teacher"
    store.disable_link("teacher")
    assert store.resolve_role(token) is None


def test_two_roles_independent_tokens(ob):
    store, _ = ob
    t_tok, _ = store.generate_link("teacher")
    r_tok, _ = store.generate_link("revisor")
    assert t_tok != r_tok
    assert store.resolve_role(t_tok) == "teacher"
    assert store.resolve_role(r_tok) == "revisor"
    # rotating the teacher kills the old teacher token but not the revisor's
    t_tok2, _ = store.generate_link("teacher")
    assert store.resolve_role(t_tok) is None
    assert store.resolve_role(t_tok2) == "teacher"
    assert store.resolve_role(r_tok) == "revisor"


def test_generate_link_bad_role(ob):
    store, _ = ob
    with pytest.raises(ValueError):
        store.generate_link("student")  # ученик токена не имеет вообще


# ---------------------------------------------------------- lock at 10 ------

def _mk_leaf(store, content="знание"):
    vid = store.create_vessel("Тест", macro_map="каркас")
    lid = store.create_leaf(vid, "Листик", task="задание")
    if content:
        store.update_leaf(lid, content=content)
    return vid, lid


def test_score_10_locks_leaf(ob):
    store, _ = ob
    _, lid = _mk_leaf(store)
    ok, _ = store.set_score(lid, 10, "revisor")
    assert ok
    leaf = store.get_leaf(lid)
    assert leaf["locked"] == 1 and leaf["status"] == store.STATUS_FOUNDATION
    # locked leaves reject edits …
    ok, why = store.update_leaf(lid, content="перезапись")
    assert not ok and why == "locked"
    # … and re-scoring below 10
    ok, why = store.set_score(lid, 5, "revisor")
    assert not ok and why == "locked"


def test_unlock_allows_edit_again(ob):
    store, _ = ob
    _, lid = _mk_leaf(store)
    store.set_score(lid, 10, "teacher")
    ok, _ = store.unlock_leaf(lid, "teacher")
    assert ok
    ok, why = store.update_leaf(lid, content="новая форма")
    assert ok, why


def test_score_bounds(ob):
    store, _ = ob
    _, lid = _mk_leaf(store)
    assert store.set_score(lid, 11, "teacher") == (False, "bad_score")
    assert store.set_score(lid, -1, "teacher") == (False, "bad_score")


# -------------------------------------------------------------- handoff -----

def test_handoff_fits_limit_and_mentions_leaves(ob):
    store, _ = ob
    vid = store.create_vessel("Кодинг", macro_map="М" * 2000)
    for i in range(40):
        store.create_leaf(vid, f"Листик {i}", task="Т" * 300)
    text = store.handoff_text(limit=5000)
    assert len(text) <= 5000
    assert "БРИФ ДЛЯ НОВОГО УЧИТЕЛЯ" in text
    assert "Кодинг" in text


# --------------------------------------------------------------- export -----

def test_export_only_score_10(ob):
    store, export = ob
    vid = store.create_vessel("Сосуд", macro_map="каркас")
    done = store.create_leaf(vid, "Готовый", task="задание готового")
    store.update_leaf(done, content="идеальная форма")
    store.set_score(done, 10, "teacher")
    half = store.create_leaf(vid, "Наполовину", task="задание")
    store.update_leaf(half, content="черновик")
    store.set_score(half, 7, "teacher")
    store.create_leaf(vid, "Пустой", task="задание пустого")

    lines = [json.loads(l) for l in export.build_jsonl().splitlines()]
    assert len(lines) == 1
    msgs = lines[0]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
    assert msgs[1]["content"] == "задание готового"
    assert msgs[2]["content"] == "идеальная форма"


# ----------------------------------------------------------- HTTP roles -----

@pytest.fixture()
def http(ob, monkeypatch):
    """aiohttp test client with the obuchalka routes and a patched store."""
    store, export = ob
    from bot.addons.obuchalka import server as _server
    importlib.reload(_server)
    monkeypatch.setattr(_server, "store", store)
    monkeypatch.setattr(_server, "export", export)
    # seed uses its own store import — repoint that too
    from bot.addons.obuchalka import seed as _seed
    monkeypatch.setattr(_seed, "store", store)
    monkeypatch.setattr(_server, "seed", _seed)
    return _server, store


async def _client(aiohttp_client, server_mod):
    from aiohttp import web
    app = web.Application()
    server_mod.register_http_endpoints(app)
    return await aiohttp_client(app)


@pytest.mark.asyncio
async def test_http_forbidden_without_valid_token(http, aiohttp_client):
    server_mod, _ = http
    client = await _client(aiohttp_client, server_mod)
    resp = await client.get("/obuchalka/badtoken/vessels")
    assert resp.status == 403


@pytest.mark.asyncio
async def test_revisor_cannot_write_content(http, aiohttp_client):
    server_mod, store = http
    client = await _client(aiohttp_client, server_mod)
    _, lid = _mk_leaf(store, content="")
    r_tok, _ = store.generate_link("revisor")
    # content write → forbidden
    resp = await client.put(f"/obuchalka/{r_tok}/leaf/{lid}",
                            json={"content": "взлом"})
    assert resp.status == 403
    # but scoring and notes are allowed
    resp = await client.post(f"/obuchalka/{r_tok}/leaf/{lid}/score", json={"score": 8})
    assert resp.status == 200
    resp = await client.post(f"/obuchalka/{r_tok}/leaf/{lid}/note",
                             json={"text": "держи русло"})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_teacher_full_cycle(http, aiohttp_client):
    server_mod, store = http
    client = await _client(aiohttp_client, server_mod)
    t_tok, _ = store.generate_link("teacher")
    resp = await client.post(f"/obuchalka/{t_tok}/vessel",
                             json={"title": "Новый сосуд", "macro_map": "каркас"})
    assert resp.status == 200
    vid = (await resp.json())["id"]
    resp = await client.post(f"/obuchalka/{t_tok}/leaf",
                             json={"vessel_id": vid, "title": "Л1", "task": "т"})
    lid = (await resp.json())["id"]
    resp = await client.put(f"/obuchalka/{t_tok}/leaf/{lid}", json={"content": "знание"})
    assert resp.status == 200
    resp = await client.post(f"/obuchalka/{t_tok}/leaf/{lid}/score", json={"score": 10})
    assert (await resp.json())["locked"] is True
    # locked → edit rejected with 409
    resp = await client.put(f"/obuchalka/{t_tok}/leaf/{lid}", json={"content": "x"})
    assert resp.status == 409
