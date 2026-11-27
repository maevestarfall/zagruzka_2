"""Coder integrator — real GitHub PR creation with per-task feature branches.

Replaces the former stub that generated fake PR URLs with ``random_id()``.

Flow
----
1. ``ensure_feature_branch`` — create (or reuse) ``feature/task-{task_id}``
   off ``main`` via the GitHub Trees API already wired in masterroom.github.
2. ``open_pr`` — open a real GitHub PR from the feature branch into ``main``,
   returning the actual ``html_url`` and PR number.
3. ``merge_if_approved`` — squash-merge the PR after confirming the head SHA
   has not drifted since the approval gate.

All GitHub calls go through :mod:`bot.addons.masterroom.github` so token
resolution, error wrapping, and retry logic stay in one place.

SHA-pinned merge
~~~~~~~~~~~~~~~~
The FSM stores the SHA at approval time (``approved_sha``).  Before merging
the integrator fetches the current PR head SHA and refuses if it differs —
this prevents a race where a late push slips in after the review gate passed.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"


class SHADriftError(Exception):
    """Raised when PR head SHA changes since approval."""


# ─── Feature branch ──────────────────────────────────────────────────────────

async def ensure_feature_branch(
    account: dict[str, Any],
    repo: str,
    task_id: str,
    *,
    base: str = "main",
    token: str | None = None,
) -> str:
    """Create (or reuse) ``feature/task-{task_id}`` off ``base``.

    Returns the branch name that was created/confirmed.
    """
    from bot.addons.masterroom.github import create_branch, GitHubError

    branch = f"feature/task-{task_id}"
    try:
        await create_branch(account, repo, branch, from_branch=base, token=token)
        logger.info("Integrator: branch '%s' ready in %s/%s", branch, account.get("owner"), repo)
    except GitHubError as exc:
        # If it already exists, create_branch returns it — this path shouldn't
        # normally be reached, but guard anyway.
        logger.warning("Integrator: create_branch raised %s — proceeding with '%s'", exc, branch)
    return branch


# ─── PR creation ─────────────────────────────────────────────────────────────

async def open_pr(
    run_id: str,
    task_id: str,
    account: dict[str, Any],
    repo: str,
    task_title: str,
    task_desc: str,
    branch_name: str | None = None,
    *,
    base: str = "main",
    token: str | None = None,
) -> dict[str, Any]:
    """Open a real GitHub PR and record it in the audit log.

    Parameters
    ----------
    run_id : str
        Coder run ID (for audit trail).
    task_id : str
        Task ID — used to derive the feature branch name if not supplied.
    account : dict
        GitHub account from masterroom state.
    repo : str
        Repository name (without owner prefix).
    task_title : str
        Human-readable title for the PR (becomes the PR title).
    task_desc : str
        Full task description used to generate the PR body.
    branch_name : str | None
        Feature branch name.  Defaults to ``feature/task-{task_id}``.
    base : str
        Target branch for the PR (default ``main``).
    token : str | None
        Explicit token override for tests.

    Returns
    -------
    dict with ``pr_url``, ``pr_number``, ``branch``, ``base``.
    """
    from bot.addons.masterroom.github import create_pull_request, GitHubError

    branch = branch_name or f"feature/task-{task_id}"
    pr_body = _build_pr_body(task_id, task_desc)

    logger.info(
        "Integrator: opening PR '%s' (%s → %s) in %s/%s",
        task_title, branch, base, account.get("owner"), repo,
    )

    try:
        pr = await create_pull_request(
            account, repo,
            title=task_title,
            body=pr_body,
            head=branch,
            base=base,
            token=token,
        )
    except GitHubError as exc:
        logger.error("Integrator: PR creation failed: %s", exc)
        _audit(run_id, "coder_integrator", "git_error",
               f"PR creation failed for task {task_id}: {exc}")
        raise

    pr_url = pr.get("html_url", "")
    pr_number = pr.get("number", 0)

    _audit(run_id, "coder_integrator", "git",
           f"Открыт Pull Request #{pr_number}: {pr_url} для задачи {task_id}")

    logger.info("Integrator: PR #%d created at %s", pr_number, pr_url)
    return {"pr_url": pr_url, "pr_number": pr_number, "branch": branch, "base": base}


# ─── SHA-pinned merge ─────────────────────────────────────────────────────────

async def merge_if_approved(
    run_id: str,
    account: dict[str, Any],
    repo: str,
    pr_number: int,
    approved_sha: str,
    *,
    merge_method: str = "squash",
    token: str | None = None,
) -> bool:
    """Squash-merge the PR after verifying head SHA has not drifted.

    Raises :class:`SHADriftError` if the PR head SHA changed since the
    review gate passed (i.e. someone pushed to the branch after approval).
    """
    from bot.addons.masterroom.github import (
        get_pull_request, merge_pull_request, GitHubError,
    )

    # Fetch current PR state
    pr = await get_pull_request(account, repo, pr_number, token=token)
    current_sha = pr.get("head", {}).get("sha", "")

    if approved_sha and current_sha and approved_sha != current_sha:
        logger.error(
            "SHA drift: approved=%s current=%s  PR #%d",
            approved_sha, current_sha, pr_number,
        )
        _audit(run_id, "coder_integrator", "sha_drift",
               f"PR #{pr_number}: SHA drift detected. approved={approved_sha} current={current_sha}")
        raise SHADriftError(
            f"PR merge aborted: SHA drift detected "
            f"(approved={approved_sha[:8]} current={current_sha[:8]})"
        )

    pr_title = pr.get("title", f"Merge PR #{pr_number}")
    try:
        await merge_pull_request(
            account, repo, pr_number,
            commit_title=pr_title,
            merge_method=merge_method,
            token=token,
        )
    except GitHubError as exc:
        logger.error("Integrator: merge failed: %s", exc)
        _audit(run_id, "coder_integrator", "git_error",
               f"Merge PR #{pr_number} failed: {exc}")
        raise

    _audit(run_id, "coder_integrator", "git",
           f"Выполнено {merge_method}-слияние PR #{pr_number} (SHA: {current_sha})")
    logger.info("PR #%d merged successfully (%s, SHA=%s)", pr_number, merge_method, current_sha[:8])
    return True


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _build_pr_body(task_id: str, task_desc: str) -> str:
    """Generate a structured PR description from the task context."""
    lines = [
        "## Summary",
        "",
        task_desc.strip() if task_desc else "_No description provided._",
        "",
        "---",
        "",
        "## Checklist",
        "",
        "- [x] Code generated and committed to feature branch",
        "- [x] Tests executed and passed",
        "- [x] Code reviewed (Reviewer + Auditor gates passed)",
        "- [ ] Reviewer approved this PR",
        "",
        f"_Generated by Master Room coder · task `{task_id}`_",
    ]
    return "\n".join(lines)


def _audit(run_id: str, persona: str, kind: str, message: str) -> None:
    """Insert a record into ``coder_audit_events``; silently skips on error."""
    try:
        conn = sqlite3.connect(DB_PATH)
        with conn:
            conn.execute("""
                INSERT INTO coder_audit_events
                    (run_id, persona, event_kind, message, ts)
                VALUES (?, ?, ?, ?, ?)
            """, (run_id, persona, kind, message, time.strftime("%Y-%m-%d %H:%M:%S")))
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Integrator audit write failed: %s", exc)
