"""Public task, event and tool contracts for the Master Room coder server."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class TaskState(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    WAITING_APPROVAL = "waiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
ALLOWED_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.CREATED: {TaskState.QUEUED, TaskState.CANCELLED},
    TaskState.QUEUED: {TaskState.ANALYZING, TaskState.PAUSED, TaskState.CANCELLED},
    TaskState.ANALYZING: {TaskState.PLANNING, TaskState.PAUSED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.PLANNING: {TaskState.WAITING_APPROVAL, TaskState.EXECUTING, TaskState.PAUSED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.WAITING_APPROVAL: {TaskState.EXECUTING, TaskState.PAUSED, TaskState.CANCELLED},
    TaskState.EXECUTING: {TaskState.VERIFYING, TaskState.WAITING_APPROVAL, TaskState.PAUSED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.VERIFYING: {TaskState.EXECUTING, TaskState.COMPLETED, TaskState.PAUSED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.PAUSED: {TaskState.QUEUED, TaskState.ANALYZING, TaskState.PLANNING, TaskState.EXECUTING, TaskState.VERIFYING, TaskState.CANCELLED},
    TaskState.FAILED: {TaskState.QUEUED, TaskState.CANCELLED},
    TaskState.COMPLETED: set(),
    TaskState.CANCELLED: {TaskState.QUEUED},
}


@dataclass(slots=True)
class Event:
    event_id: str
    task_id: str
    seq: int
    event_type: str
    phase: str
    status: str
    summary: str
    created_at: float
    trace_id: str
    tool_call_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    artifact_ids: list[str] = field(default_factory=list)

    def dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ToolResult:
    ok: bool
    tool: str
    version: str
    started_at: float
    finished_at: float
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    artifact_ids: list[str] = field(default_factory=list)

    def dict(self) -> dict[str, Any]:
        return asdict(self)
