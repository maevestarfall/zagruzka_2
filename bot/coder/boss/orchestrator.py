from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class AssignmentDecision:
    task_id: str
    worker_id: str
    action: str  # e.g., "assign", "reassign", "pause"
    rationale: str

class Orchestrator:
    def __init__(self, run_id: str):
        self.run_id = run_id

    def assign(self, task_id: str, worker_id: str, rationale: str) -> AssignmentDecision:
        """Assign or reassign a task to a specific worker."""
        logger.info("Orchestrator assigning task '%s' to worker '%s': %s", task_id, worker_id, rationale)
        return AssignmentDecision(
            task_id=task_id,
            worker_id=worker_id,
            action="assign",
            rationale=rationale
        )

    def validate_planning_artifact(self, artifact_json: str) -> bool:
        """Validate planning artifact against schemas/tasks.schema.json."""
        try:
            data = json.loads(artifact_json)
            # Schema validation mock or simple check
            required = {"task_id", "run_id", "title", "dependencies"}
            if not required.issubset(data.keys()):
                logger.error("Planning artifact lacks required fields: %s", required - data.keys())
                return False
            return True
        except Exception as e:
            logger.error("Failed to parse planning artifact JSON: %s", e)
            return False
