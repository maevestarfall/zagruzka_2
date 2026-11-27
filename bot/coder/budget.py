from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"

def recommend_worker_count(queue_depth: int, remaining_hours: float, profile: str) -> int:
    """Recommend worker count based on budget telemetry and queue depth."""
    if profile == "free":
        # Render free tier caps at 3 workers
        return 3
        
    if remaining_hours < 10.0:
        # Budget is low, scale down to save resources
        return 1
        
    if queue_depth > 10:
        return 10 # Scale up for paid/overflow
        
    return 5
