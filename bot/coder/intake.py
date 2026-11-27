from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

class IntakeValidationError(Exception):
    """Raised when task intake validation fails."""
    pass

def validate_task_intake(task_json: str) -> dict:
    """Validate task intake payload against structural requirements and schema version."""
    try:
        data = json.loads(task_json)
    except Exception as e:
        raise IntakeValidationError(f"Invalid JSON format: {e}")
        
    required = {"task_id", "run_id", "title", "description", "schema_version"}
    if not required.issubset(data.keys()):
        raise IntakeValidationError(f"Missing required intake fields: {required - data.keys()}")
        
    version = data.get("schema_version")
    if version != 1:
        raise IntakeValidationError(f"Unsupported schema version: {version}. Expected version 1.")
        
    logger.info("Task intake validated successfully for task '%s' (Version: %d)", data["task_id"], version)
    return data
