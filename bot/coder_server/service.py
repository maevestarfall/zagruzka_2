"""Durable coder orchestration service. It executes real tools, never simulated claims."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from .jobs import JobQueue
from .models import ALLOWED_TRANSITIONS, TERMINAL_STATES, TaskState
from .permissions import PermissionEngine, Risk
from .store import EventStore
from .tools import ToolRegistry, default_registry
from .workspace import Workspace


class CoderService:
    def __init__(self, data_root: str | Path, registry: ToolRegistry | None = None):
        root = Path(data_root)
        self.store = EventStore(root / "coder-server.sqlite3", root / "artifacts")
        self.jobs = JobQueue(root / "coder-server.sqlite3")
        self.workspace_root = root / "workspaces"
        self.registry = registry or default_registry()
        self.permissions = PermissionEngine({item["name"] for item in self.registry.describe()}, Risk.MEDIUM)
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._wakeup: dict[str, asyncio.Event] = {}

    def create_task(self, title: str, request: str, budget: dict[str, Any] | None = None) -> dict[str, Any]:
        task = self.store.create_task(title.strip() or "Без названия", request.strip(), budget)
        self.store.transition(task["id"], TaskState.QUEUED, "queue", "Задача поставлена в durable-очередь")
        self.jobs.enqueue(task["id"], "workflow.intake", {"request": request.strip()}, max_attempts=3)
        return self.store.get_task(task["id"])

    def recover(self) -> list[str]:
        """ТЗ §6: восстановление после рестарта процесса.

        Возвращает «осиротевшие» running-джобы в очередь (lease reclaim) и
        перезапускает воркеры для нетерминальных задач, у которых нет живого
        asyncio-воркера. Задачи на паузе и в ожидании подтверждения не
        трогаются — их продолжает владелец.
        """
        self.jobs.reclaim_expired()
        recovered: list[str] = []
        for task in self.store.list_tasks(limit=500):
            state = TaskState(task["state"])
            if state in TERMINAL_STATES or state in {TaskState.PAUSED, TaskState.WAITING_APPROVAL}:
                continue
            worker = self._workers.get(task["id"])
            if worker is not None and not worker.done():
                continue
            self.store.append_event(task["id"], "worker.recovered", "runtime", "running", "Воркер перезапущен после рестарта сервера")
            self.start(task["id"])
            recovered.append(task["id"])
        return recovered

    def complete(self, task_id: str, required_evidence: set[str] | None = None) -> dict[str, Any]:
        """Evidence-gated завершение (ТЗ §11): задача не может стать
        completed без свежих подтверждений, привязанных к текущему
        workspace hash. Слова «готово» без evidence не проходят."""
        task = self.store.get_task(task_id)
        state = TaskState(task["state"])
        if state not in {TaskState.EXECUTING, TaskState.VERIFYING}:
            raise ValueError(f"completion is not allowed from state {state}")
        required = required_evidence or {"tool_completed"}
        gate = self.evidence_gate(task_id, required)
        if state == TaskState.EXECUTING:
            self.store.transition(task_id, TaskState.VERIFYING, "verify", "Проверка evidence перед завершением")
        if not gate["ok"]:
            self.store.append_event(task_id, "verification.failed", "verify", "failed", "Недостаточно свежих доказательств для завершения", gate)
            self.store.transition(task_id, TaskState.EXECUTING, "execute", f"Завершение отклонено: нет evidence {', '.join(gate['missing'])}")
            return {"completed": False, "gate": gate, "task": self.store.get_task(task_id)}
        self.store.append_event(task_id, "verification.passed", "verify", "completed", "Evidence gate пройден", gate)
        self.store.transition(task_id, TaskState.COMPLETED, "done", "Задача завершена с подтверждёнными доказательствами")
        return {"completed": True, "gate": gate, "task": self.store.get_task(task_id)}

    def start(self, task_id: str) -> None:
        running = self._workers.get(task_id)
        if running is None or running.done():
            self._workers[task_id] = asyncio.create_task(self.run(task_id), name=f"coder:{task_id}")

    async def run(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if TaskState(task["state"]) in TERMINAL_STATES:
            return
        ws = Workspace(self.workspace_root, task_id)
        self._wakeup.setdefault(task_id, asyncio.Event())
        try:
            self.store.transition(task_id, TaskState.ANALYZING, "research", "Анализ репозитория начат")
            manifest = await self.invoke(task_id, "fs.list", {"path": ".", "limit": 1000})
            self.store.checkpoint(task_id, {"phase": "research", "workspace_hash": ws.digest()})
            self.store.transition(task_id, TaskState.PLANNING, "plan", "План построен по фактическому manifest workspace")
            self.store.append_event(task_id, "plan.ready", "plan", "completed", "План готов", {"manifest_entries": len(manifest.get("output", {}).get("entries", []))})
            self.store.transition(task_id, TaskState.WAITING_APPROVAL, "approval", "Требуется подтверждение плана перед изменением файлов")
        except asyncio.CancelledError:
            self.store.append_event(task_id, "worker.stopped", "runtime", "cancelled", "Исполнитель остановлен")
            raise
        except Exception as exc:  # noqa: BLE001
            current = TaskState(self.store.get_task(task_id)["state"])
            if TaskState.FAILED in ALLOWED_TRANSITIONS[current]:
                self.store.transition(task_id, TaskState.FAILED, "runtime", "Выполнение завершилось ошибкой")
            with self.store.connect() as db:
                db.execute("UPDATE tasks SET error=?,updated_at=? WHERE id=?", (str(exc)[:1000], time.time(), task_id))

    async def invoke(self, task_id: str, tool: str, arguments: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        key = idempotency_key or f"{task_id}:{tool}:{uuid.uuid4().hex}"
        cached = self.store.recalled(key)
        if cached is not None:
            return cached
        ws = Workspace(self.workspace_root, task_id)
        call_id = f"tc_{uuid.uuid4().hex[:16]}"
        spec = next((item for item in self.registry.describe() if item["name"] == tool), None)
        decision = self.permissions.decide(tool, (spec or {}).get("risk", "critical"))
        approved = bool(arguments.pop("_approved", False))
        if decision.needs_approval and not approved:
            approval = self.store.request_approval(task_id, "tool_call", {"tool": tool, "arguments": arguments, "tool_call_id": call_id})
            payload = {"ok": False, "tool": tool, "error": decision.reason, "approval_required": True, "approval_id": approval["id"]}
            self.store.append_event(task_id, "approval.required", "approval", "blocked", decision.reason, {"tool": tool, "approval_id": approval["id"]}, tool_call_id=call_id)
            return payload
        if not decision.allowed and not decision.needs_approval:
            return {"ok": False, "tool": tool, "error": decision.reason}
        self.store.append_event(task_id, "tool.started", "execute", "running", f"Запущен {tool}", {"arguments": arguments}, tool_call_id=call_id)
        result = await self.registry.invoke(tool, ws, arguments)
        payload = result.dict()
        raw = json.dumps(payload, ensure_ascii=False).encode()
        artifact = self.store.artifact(task_id, "tool_result", raw, ".json", {"tool": tool, "tool_call_id": call_id})
        payload["artifact_ids"] = [artifact["id"]]
        self.store.remember(key, payload)
        self.store.append_event(task_id, "tool.completed", "execute", "completed" if result.ok else "failed", f"{tool}: {'успешно' if result.ok else 'ошибка'}", {"ok": result.ok, "error": result.error}, [artifact["id"]], call_id)
        if result.ok:
            claim = "tests_passed" if tool in {"test.run", "command.run"} and payload.get("output", {}).get("exit_code") == 0 else "tool_completed"
            self.store.add_evidence(task_id, claim, f"workspace@sha256:{ws.digest()}", f"{tool}@{result.version}", call_id, [artifact["id"]], ws.digest())
        return payload

    def control(self, task_id: str, action: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        state = TaskState(task["state"])
        if action == "pause" and state not in TERMINAL_STATES:
            worker = self._workers.get(task_id)
            if worker and not worker.done():
                worker.cancel()
            # ТЗ §6: pause фиксирует checkpoint с состоянием возврата, чтобы
            # resume продолжал с места остановки, а не с начала очереди.
            checkpoint = dict(task.get("checkpoint") or {})
            checkpoint["resume_state"] = str(state)
            checkpoint.setdefault("phase", task.get("phase", "queue"))
            self.store.checkpoint(task_id, checkpoint)
            return self.store.transition(task_id, TaskState.PAUSED, "control", "Задача приостановлена")
        if action == "resume" and state == TaskState.PAUSED:
            checkpoint = task["checkpoint"] or {}
            target = TaskState(checkpoint.get("resume_state", TaskState.QUEUED))
            if target not in ALLOWED_TRANSITIONS[TaskState.PAUSED]:
                # Например, пауза во время waiting_approval: FSM не допускает
                # прямой возврат — задача безопасно возвращается в очередь.
                target = TaskState.QUEUED
            out = self.store.transition(task_id, target, checkpoint.get("phase", "queue"), "Задача продолжена из checkpoint")
            self.start(task_id)
            return out
        if action == "cancel" and state not in TERMINAL_STATES:
            worker = self._workers.get(task_id)
            if worker and not worker.done():
                worker.cancel()
            return self.store.transition(task_id, TaskState.CANCELLED, "control", "Задача отменена")
        if action == "retry" and state in {TaskState.FAILED, TaskState.CANCELLED}:
            out = self.store.transition(task_id, TaskState.QUEUED, "queue", "Задача поставлена на повтор")
            self.start(task_id)
            return out
        raise ValueError(f"action {action} is not allowed for {state}")

    def approve(self, task_id: str, approved: bool) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if TaskState(task["state"]) != TaskState.WAITING_APPROVAL:
            raise ValueError("task is not waiting for approval")
        if not approved:
            return self.store.transition(task_id, TaskState.CANCELLED, "approval", "План отклонён владельцем")
        return self.store.transition(task_id, TaskState.EXECUTING, "execute", "План подтверждён владельцем")

    def evidence_gate(self, task_id: str, required: set[str]) -> dict[str, Any]:
        workspace_hash = Workspace(self.workspace_root, task_id).digest()
        evidence = self.store.evidence(task_id, workspace_hash)
        available = {e["claim_type"] for e in evidence if e["fresh"]}
        stale = sorted({e["claim_type"] for e in evidence if not e["fresh"]})
        missing = sorted(required - available)
        return {"ok": not missing, "required": sorted(required), "available": sorted(available), "missing": missing, "stale": stale, "workspace_hash": workspace_hash}
