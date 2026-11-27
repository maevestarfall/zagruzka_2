from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from bot.coder_server import CoderService, create_app
from bot.coder_server.workspace import Workspace, WorkspaceViolation


class CoderServerTests(unittest.TestCase):
    def test_workspace_blocks_escape_and_writes_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(Path(temp), "task_safe")
            workspace.write_atomic("src/main.py", b"print('ok')\n")
            self.assertEqual(workspace.read("src/main.py"), b"print('ok')\n")
            with self.assertRaises(WorkspaceViolation):
                workspace.read("../secret")
            with self.assertRaises(WorkspaceViolation):
                workspace.write_atomic(".env", b"SECRET=x")

    def test_tool_results_are_durable_idempotent_evidence(self) -> None:
        async def scenario(root: Path) -> None:
            service = CoderService(root)
            task = service.create_task("write", "write a file")
            first = await service.invoke(task["id"], "fs.write", {"path": "hello.txt", "content": "hello"}, "same-key")
            second = await service.invoke(task["id"], "fs.write", {"path": "changed.txt", "content": "changed"}, "same-key")
            self.assertEqual(first, second)
            self.assertTrue(first["ok"])
            self.assertEqual(len(service.store.evidence(task["id"])), 1)
            self.assertGreaterEqual(len(service.store.events(task["id"])), 4)

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_nonzero_command_is_failure(self) -> None:
        async def scenario(root: Path) -> None:
            service = CoderService(root)
            task = service.create_task("test", "run a failing command")
            result = await service.invoke(task["id"], "command.run", {"argv": ["python", "-c", "raise SystemExit(7)"], "_approved": True})
            self.assertFalse(result["ok"])
            self.assertIn("exited with 7", result["error"] or "")

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_api_auth_capabilities_and_task_contract(self) -> None:
        async def scenario(root: Path) -> None:
            service = CoderService(root)
            async with TestClient(TestServer(create_app(service, token="owner-token"))) as client:
                self.assertEqual((await client.get("/api/v1/health")).status, 200)
                self.assertEqual((await client.get("/api/v1/capabilities")).status, 403)
                headers = {"Authorization": "Bearer owner-token"}
                capabilities = await client.get("/api/v1/capabilities", headers=headers)
                self.assertEqual(capabilities.status, 200)
                body = await capabilities.json()
                self.assertTrue(body["features"]["evidence_ledger"])
                created = await client.post(
                    "/api/v1/tasks",
                    headers=headers,
                    json={"title": "real task", "description": "inspect workspace", "auto_start": False},
                )
                self.assertEqual(created.status, 201)
                task = await created.json()
                self.assertEqual(task["request"], "inspect workspace")

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))


if __name__ == "__main__":
    unittest.main()
