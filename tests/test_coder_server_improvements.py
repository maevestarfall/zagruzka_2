"""Тесты улучшений по ТЗ: новые инструменты, durable-восстановление, evidence gate."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import time
import unittest
from pathlib import Path

from bot.coder_server import CoderService
from bot.coder_server.jobs import JobQueue
from bot.coder_server.models import TaskState
from bot.coder_server.tools import default_registry
from bot.coder_server.workspace import Workspace


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class NewToolsTests(unittest.TestCase):
    def _ws(self, temp: str) -> Workspace:
        return Workspace(Path(temp), "task_tools")

    def _run(self, ws: Workspace, tool: str, args: dict):
        return asyncio.run(default_registry().invoke(tool, ws, args))

    def test_fs_edit_applies_and_checks_hash_precondition(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ws = self._ws(temp)
            ws.write_atomic("main.py", b"value = 1\n")
            ok = self._run(ws, "fs.edit", {"path": "main.py", "old_text": "value = 1", "new_text": "value = 2", "expected_sha256": _sha(b"value = 1\n")})
            self.assertTrue(ok.ok)
            self.assertEqual(ws.read("main.py"), b"value = 2\n")
            stale = self._run(ws, "fs.edit", {"path": "main.py", "old_text": "value = 2", "new_text": "value = 3", "expected_sha256": _sha(b"value = 1\n")})
            self.assertFalse(stale.ok)
            self.assertIn("precondition failed", stale.error or "")
            self.assertEqual(ws.read("main.py"), b"value = 2\n")

    def test_fs_edit_rejects_ambiguous_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ws = self._ws(temp)
            ws.write_atomic("dup.py", b"x = 0\nx = 0\n")
            result = self._run(ws, "fs.edit", {"path": "dup.py", "old_text": "x = 0", "new_text": "x = 1"})
            self.assertFalse(result.ok)
            self.assertIn("matches 2 times", result.error or "")

    def test_fs_patch_multi_is_transactional(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ws = self._ws(temp)
            ws.write_atomic("a.py", b"a = 1\n")
            ws.write_atomic("b.py", b"b = 1\n")
            # Второй патч с неверным hash: ни один файл не должен измениться.
            bad = self._run(ws, "fs.patch_multi", {"patches": [
                {"path": "a.py", "before": "a = 1", "after": "a = 2"},
                {"path": "b.py", "before": "b = 1", "after": "b = 2", "expected_sha256": _sha(b"WRONG")},
            ]})
            self.assertFalse(bad.ok)
            self.assertEqual(ws.read("a.py"), b"a = 1\n")
            self.assertEqual(ws.read("b.py"), b"b = 1\n")
            good = self._run(ws, "fs.patch_multi", {"patches": [
                {"path": "a.py", "before": "a = 1", "after": "a = 2"},
                {"path": "b.py", "before": "b = 1", "after": "b = 2"},
            ]})
            self.assertTrue(good.ok)
            self.assertEqual(ws.read("a.py"), b"a = 2\n")
            self.assertEqual(ws.read("b.py"), b"b = 2\n")

    def test_fs_tree_and_encoding_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ws = self._ws(temp)
            ws.write_atomic("src/app.py", b"print('ok')\n")
            ws.write_atomic("bad.txt", "текст с \ufffd маркером".encode("utf-8"))
            (ws.root / "latin.txt").write_bytes("привет".encode("cp1251"))
            tree = self._run(ws, "fs.tree", {})
            self.assertTrue(tree.ok)
            self.assertIn("src/", tree.output["tree"])
            enc = self._run(ws, "fs.encoding_check", {})
            self.assertTrue(enc.ok)
            problems = {issue["path"]: issue["problem"] for issue in enc.output["issues"]}
            self.assertEqual(problems.get("bad.txt"), "replacement-character")
            self.assertEqual(problems.get("latin.txt"), "not-utf8")

    def test_repo_rules_collects_instruction_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ws = self._ws(temp)
            ws.write_atomic("AGENTS.md", b"always run tests\n")
            ws.write_atomic("docs/CONTRIBUTING.md", b"use black\n")
            result = self._run(ws, "repository.rules", {})
            self.assertTrue(result.ok)
            paths = {rule["path"] for rule in result.output["rules"]}
            self.assertIn("AGENTS.md", paths)
            self.assertIn("docs/CONTRIBUTING.md", paths)

    def test_dependencies_install_validates_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ws = self._ws(temp)
            bad_manager = self._run(ws, "dependencies.install", {"manager": "curl-pipe-bash"})
            self.assertFalse(bad_manager.ok)
            self.assertIn("unsupported manager", bad_manager.error or "")
            bad_pkg = self._run(ws, "dependencies.install", {"manager": "pip", "packages": ["--index-url=http://evil"]})
            self.assertFalse(bad_pkg.ok)
            self.assertIn("invalid package spec", bad_pkg.error or "")

    def test_failed_command_returns_structured_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ws = self._ws(temp)
            result = self._run(ws, "command.run", {"argv": ["python3", "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"]})
            self.assertFalse(result.ok)
            self.assertEqual(result.output["exit_code"], 3)
            self.assertIn("out", result.output["stdout"])
            self.assertIn("err", result.output["stderr"])


class DurableRuntimeTests(unittest.TestCase):
    def test_job_queue_reclaims_expired_leases(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            queue = JobQueue(Path(temp) / "jobs.db")
            queue.enqueue("task_x", "workflow.intake", {}, max_attempts=3)
            job = queue.lease("worker-dead", lease_seconds=0)
            self.assertIsNotNone(job)
            time.sleep(0.01)
            self.assertEqual(queue.reclaim_expired(), 1)
            job2 = queue.lease("worker-alive", lease_seconds=30)
            self.assertIsNotNone(job2)
            self.assertEqual(job2["task_id"], "task_x")

    def test_pause_records_checkpoint_and_resume_restores_state(self) -> None:
        async def scenario(root: Path) -> None:
            # control(resume) перезапускает воркер через asyncio — как и в
            # реальном боте, вызов происходит внутри работающего event loop.
            service = CoderService(root)
            task = service.create_task("t", "do work")
            tid = task["id"]
            service.store.transition(tid, TaskState.ANALYZING, "analyze", "x")
            service.store.transition(tid, TaskState.PLANNING, "plan", "x")
            service.store.transition(tid, TaskState.EXECUTING, "execute", "x")
            service.control(tid, "pause")
            paused = service.store.get_task(tid)
            assert paused["state"] == "paused"
            assert paused["checkpoint"].get("resume_state") == "executing"
            service.control(tid, "resume")
            assert service.store.get_task(tid)["state"] == "executing"
            worker = service._workers.get(tid)
            if worker is not None:
                worker.cancel()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_complete_requires_fresh_evidence(self) -> None:
        async def scenario(root: Path) -> None:
            service = CoderService(root)
            task = service.create_task("t", "write then complete")
            tid = task["id"]
            service.store.transition(tid, TaskState.ANALYZING, "analyze", "x")
            service.store.transition(tid, TaskState.PLANNING, "plan", "x")
            service.store.transition(tid, TaskState.EXECUTING, "execute", "x")
            # Без evidence завершение отклоняется, задача возвращается в executing.
            rejected = service.complete(tid)
            assert rejected["completed"] is False
            assert service.store.get_task(tid)["state"] == "executing"
            # Реальный tool result создаёт свежее evidence — завершение проходит.
            result = await service.invoke(tid, "fs.write", {"path": "done.txt", "content": "done"})
            assert result["ok"]
            accepted = service.complete(tid)
            assert accepted["completed"] is True
            assert service.store.get_task(tid)["state"] == "completed"

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))

    def test_recover_restarts_orphaned_tasks(self) -> None:
        async def scenario(root: Path) -> None:
            service = CoderService(root)
            task = service.create_task("t", "orphan me")
            # Воркер не запущен (эмуляция рестарта процесса) — recover должен
            # подхватить нетерминальную задачу без живого воркера.
            recovered = service.recover()
            assert task["id"] in recovered
            events = {e["event_type"] for e in service.store.events(task["id"])}
            assert "worker.recovered" in events
            worker = service._workers.get(task["id"])
            if worker is not None:
                worker.cancel()

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(scenario(Path(temp)))


class TesterShellSafetyTests(unittest.TestCase):
    def test_run_tests_does_not_use_shell(self) -> None:
        import inspect

        from bot.coder import tester

        source = inspect.getsource(tester)
        self.assertNotIn("shell=True", source)


if __name__ == "__main__":
    unittest.main()
