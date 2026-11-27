from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

class ReviewRejected(Exception):
    """Raised when the review score is below the minimum threshold."""
    pass

def score_diff(diff_content: str) -> float:
    """Analyze code diff quality and return a score between 1.0 and 10.0."""
    # Simplified static review scoring logic
    if "TODO" in diff_content or "FIXME" in diff_content:
        score = 6.5
    elif "test" in diff_content.lower():
        score = 8.5
    else:
        score = 7.5
        
    logger.info("Reviewer scored code diff: %.1f/10.0", score)
    return score
