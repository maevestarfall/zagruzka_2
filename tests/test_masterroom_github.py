"""Tests for ``bot.addons.masterroom.github``.

Covers:

* ``_resolve_token`` reads from env-var named in ``account['token_ref']``;
  explicit ``token=`` arg wins
* ``create_repo`` posts to /orgs/<owner>/repos with private + auto_init
* Falls back to /user/repos on 404 (personal account)
* ``get_deploy_status`` aggregates status + check-runs
* ``trigger_workflow`` POSTs workflow_dispatch
* Missing token raises ``GitHubError``
* Multi-account: same client routes by account dict (token_ref +
  owner switch transparently)
"""

from __future__ import annotations

import pytest
import respx

from bot.addons.masterroom import github as gh


ACC_ORG = {
    "id": "a1",
    "name": "main",
    "owner": "chloe-org",
    "token_ref": "GITHUB_TOKEN_MAIN",
    "default_visibility": "private",
}
ACC_PERSONAL = {
    "id": "a2",
    "name": "personal",
    "owner": "chloe",
    "token_ref": "GITHUB_TOKEN_PERSONAL",
    "default_visibility": "private",
}


def test_resolve_token_prefers_explicit():
    assert gh._resolve_token(ACC_ORG, explicit="abc") == "abc"


def test_resolve_token_reads_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_MAIN", "from-env")
    assert gh._resolve_token(ACC_ORG) == "from-env"


def test_resolve_token_missing_returns_empty(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN_MAIN", raising=False)
    assert gh._resolve_token(ACC_ORG) == ""


@pytest.mark.asyncio
async def test_create_repo_org_path(monkeypatch):
    async with respx.mock(base_url="https://api.github.com") as m:
        m.post("/orgs/chloe-org/repos").respond(
            201, json={"name": "demo", "clone_url": "https://github.com/chloe-org/demo.git"}
        )
        repo = await gh.create_repo(ACC_ORG, "demo", token="t")
    assert repo["clone_url"].endswith("/demo.git")


@pytest.mark.asyncio
async def test_create_repo_falls_back_to_user_on_404():
    async with respx.mock(base_url="https://api.github.com") as m:
        m.post("/orgs/chloe/repos").respond(404, json={"message": "not found"})
        m.post("/user/repos").respond(
            201, json={"name": "demo", "clone_url": "https://github.com/chloe/demo.git"}
        )
        repo = await gh.create_repo(ACC_PERSONAL, "demo", token="t")
    assert repo["clone_url"].endswith("/chloe/demo.git")


@pytest.mark.asyncio
async def test_create_repo_no_token_raises(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN_MAIN", raising=False)
    with pytest.raises(gh.GitHubError):
        await gh.create_repo(ACC_ORG, "demo")


@pytest.mark.asyncio
async def test_get_deploy_status_aggregates_status_and_checks():
    async with respx.mock(base_url="https://api.github.com") as m:
        m.get("/repos/chloe-org/demo/commits/abc/status").respond(
            200, json={"state": "success"}
        )
        m.get("/repos/chloe-org/demo/commits/abc/check-runs").respond(
            200, json={
                "check_runs": [
                    {"name": "build", "status": "completed",
                     "conclusion": "success", "html_url": "https://x"},
                ]
            }
        )
        out = await gh.get_deploy_status(ACC_ORG, "demo", "abc", token="t")
    assert out["state"] == "success"
    assert len(out["checks"]) == 1
    assert out["checks"][0]["name"] == "build"


@pytest.mark.asyncio
async def test_trigger_workflow_dispatches():
    async with respx.mock(base_url="https://api.github.com") as m:
        m.post(
            "/repos/chloe-org/demo/actions/workflows/deploy.yml/dispatches"
        ).respond(204)
        out = await gh.trigger_workflow(
            ACC_ORG, "demo", "deploy.yml", ref="main",
            inputs={"env": "prod"}, token="t",
        )
    assert out == {"ok": True, "status": 204}


@pytest.mark.asyncio
async def test_create_repo_uses_account_visibility():
    """Account default_visibility=private → body.private=True."""
    async with respx.mock(base_url="https://api.github.com") as m:
        route = m.post("/orgs/chloe-org/repos").respond(
            201, json={"name": "x"}
        )
        await gh.create_repo(ACC_ORG, "x", token="t")
        import json as _json
        body = _json.loads(route.calls[0].request.content)
        assert body["private"] is True


@pytest.mark.asyncio
async def test_multi_account_routing():
    """Same client, two accounts → two different owners hit."""
    async with respx.mock(base_url="https://api.github.com") as m:
        r1 = m.post("/orgs/chloe-org/repos").respond(201, json={"name": "a"})
        # personal account: org 404 → /user/repos fallback
        m.post("/orgs/chloe/repos").respond(404, json={})
        r2 = m.post("/user/repos").respond(201, json={"name": "b"})
        await gh.create_repo(ACC_ORG, "a", token="t1")
        await gh.create_repo(ACC_PERSONAL, "b", token="t2")
    assert r1.called and r2.called
