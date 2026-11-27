from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.coder_server import CoderService, create_app
from bot.coder_server.models import TaskState
from bot.coder_server.tools import redact
from bot.coder_server.workspace import Workspace, WorkspaceViolation


def test_workspace_blocks_escape_and_symlink(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "task_safe")
    with pytest.raises(WorkspaceViolation):
        ws.resolve("../secret")
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (ws.root / "link").symlink_to(outside)
    with pytest.raises(WorkspaceViolation):
        ws.read("link")


def test_archive_security(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "task_zip")
    archive = ws.root / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../escape.txt", "no")
    with pytest.raises(WorkspaceViolation):
        ws.extract_zip("bad.zip")


def test_events_are_monotonic_and_transitions_checked(tmp_path: Path) -> None:
    service = CoderService(tmp_path)
    task = service.create_task("test", "inspect repository")
    events = service.store.events(task["id"])
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    with pytest.raises(ValueError):
        service.store.transition(task["id"], TaskState.COMPLETED, "done", "invalid")


@pytest.mark.asyncio
async def test_real_tool_result_creates_evidence_and_is_idempotent(tmp_path: Path) -> None:
    service = CoderService(tmp_path)
    task = service.create_task("write", "create real file")
    first = await service.invoke(task["id"], "fs.write", {"path": "src/a.txt", "content": "hello"}, "same-key")
    second = await service.invoke(task["id"], "fs.write", {"path": "src/a.txt", "content": "changed"}, "same-key")
    assert first == second
    assert (tmp_path / "workspaces" / task["id"] / "src" / "a.txt").read_text() == "hello"
    assert service.store.evidence(task["id"])[0]["artifact_ids"]


def test_redaction_is_recursive() -> None:
    value = redact({"token": "abc", "nested": ["password=hello", {"safe": "yes"}]})
    assert value["token"] == "[REDACTED]"
    assert "hello" not in json.dumps(value)


@pytest.mark.asyncio
async def test_http_contract_and_auth(tmp_path: Path) -> None:
    service = CoderService(tmp_path)
    async with TestClient(TestServer(create_app(service, "test-token"))) as client:
        health = await client.get("/api/v1/health")
        assert health.status == 200
        denied = await client.get("/api/v1/tasks")
        assert denied.status == 403
        headers = {"Authorization": "Bearer test-token"}
        created = await client.post("/api/v1/tasks", headers=headers, json={"title": "API", "request": "inspect", "auto_start": False})
        assert created.status == 201
        task = await created.json()
        fetched = await client.get(f"/api/v1/tasks/{task['id']}", headers=headers)
        assert fetched.status == 200
        events = await client.get(f"/api/v1/tasks/{task['id']}/events", headers=headers)
        payload = await events.json()
        assert payload["events"]
