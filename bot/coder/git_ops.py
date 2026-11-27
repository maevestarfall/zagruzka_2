"""Real git operations via subprocess for the coder pipeline.

Replaces the former stubs (``rebase_branch`` always returned True,
``assert_head_sha`` only logged).  All operations run ``git`` via
``subprocess.run`` inside the given ``work_dir`` so they operate on the
actual cloned repository that the coder-bot has checked out.

Design decisions
----------------
* No gitpython dependency — ``git`` CLI is universally available and its
  output is well-documented.
* Every function raises :class:`GitOpsError` on failure so callers can
  distinguish git errors from other exceptions.
* ``work_dir`` is always required — never assume cwd because the bot
  process may be running from a different directory than the repo.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60  # seconds per git command


class GitOpsError(Exception):
    """A git operation failed.  Carries ``returncode`` and ``stderr``."""

    def __init__(self, message: str, returncode: int = -1, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _run(args: list[str], work_dir: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run a git command in ``work_dir``, return stdout, raise on error."""
    result = subprocess.run(
        args,
        cwd=work_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise GitOpsError(
            f"git {' '.join(args[1:])} failed (exit {result.returncode})",
            returncode=result.returncode,
            stderr=(result.stderr or "").strip(),
        )
    return (result.stdout or "").strip()


# ─── Public API ───────────────────────────────────────────────────────────────

def get_head_sha(work_dir: str) -> str:
    """Return the current HEAD commit SHA (full 40 chars)."""
    return _run(["git", "rev-parse", "HEAD"], work_dir)


def assert_head_sha(expected_sha: str, work_dir: str) -> None:
    """Raise :class:`GitOpsError` if HEAD does not match ``expected_sha``.

    Used as a pre-push safety check so the integrator never fast-forwards
    the remote if a local rebase or amend shifted HEAD since planning.
    """
    actual = get_head_sha(work_dir)
    if expected_sha and actual != expected_sha:
        raise GitOpsError(
            f"HEAD SHA mismatch: expected {expected_sha[:8]} got {actual[:8]}",
            returncode=1,
            stderr=f"expected={expected_sha} actual={actual}",
        )
    logger.info("git_ops: HEAD SHA verified (%s)", actual[:8])


def rebase_branch(branch: str, onto: str, work_dir: str) -> bool:
    """Rebase ``branch`` onto ``onto`` in ``work_dir``.

    Checks out ``branch``, runs ``git rebase <onto>``, and returns True on
    success.  If the rebase fails (conflicts) it aborts and raises
    :class:`GitOpsError` so the caller can decide whether to skip or fail
    the task.
    """
    try:
        _run(["git", "checkout", branch], work_dir)
        _run(["git", "rebase", onto], work_dir)
        logger.info("git_ops: rebased '%s' onto '%s'", branch, onto)
        return True
    except GitOpsError as exc:
        logger.error("git_ops: rebase failed: %s — aborting", exc)
        try:
            _run(["git", "rebase", "--abort"], work_dir)
        except GitOpsError:
            pass  # best-effort abort
        raise


def checkout_branch(branch: str, work_dir: str, *, create: bool = False) -> None:
    """Checkout ``branch`` (optionally creating it with ``-b``)."""
    args = ["git", "checkout"]
    if create:
        args.append("-b")
    args.append(branch)
    _run(args, work_dir)
    logger.info("git_ops: checked out '%s' (create=%s)", branch, create)


def stage_all(work_dir: str) -> None:
    """Stage all changes (``git add -A``)."""
    _run(["git", "add", "-A"], work_dir)
    logger.info("git_ops: staged all changes in '%s'", work_dir)


def commit(message: str, work_dir: str, *, allow_empty: bool = False) -> str:
    """Create a commit and return the new HEAD SHA.

    ``allow_empty`` is useful when the coder only updated comments/docs and
    the diff is identical — still want to record the event.
    """
    args = ["git", "commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    _run(args, work_dir)
    sha = get_head_sha(work_dir)
    logger.info("git_ops: committed '%s' → %s", message[:60], sha[:8])
    return sha


def push_branch(branch: str, work_dir: str, *, force: bool = False) -> None:
    """Push ``branch`` to ``origin``.

    ``force`` uses ``--force-with-lease`` (safer than ``--force``) so it
    refuses to overwrite if someone else has pushed in the meantime.
    """
    args = ["git", "push", "origin", branch]
    if force:
        args.append("--force-with-lease")
    _run(args, work_dir, timeout=120)
    logger.info("git_ops: pushed '%s' to origin (force=%s)", branch, force)


def set_remote_url(url: str, work_dir: str, remote: str = "origin") -> None:
    """Set (or add) the remote URL for ``remote``."""
    try:
        _run(["git", "remote", "set-url", remote, url], work_dir)
    except GitOpsError:
        # Remote may not exist yet
        _run(["git", "remote", "add", remote, url], work_dir)
    logger.info("git_ops: remote '%s' → %s", remote, url)


def configure_author(name: str, email: str, work_dir: str) -> None:
    """Set ``user.name`` and ``user.email`` for this repo."""
    _run(["git", "config", "user.name", name], work_dir)
    _run(["git", "config", "user.email", email], work_dir)
    logger.info("git_ops: author set to '%s <%s>'", name, email)


def clone(url: str, dest: str, *, depth: int = 0, branch: str = "") -> None:
    """Clone ``url`` into ``dest`` (shallow if ``depth > 0``)."""
    args = ["git", "clone", url, dest]
    if depth > 0:
        args += ["--depth", str(depth)]
    if branch:
        args += ["--branch", branch]
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        raise GitOpsError(
            f"git clone {url} failed",
            returncode=result.returncode,
            stderr=(result.stderr or "").strip(),
        )
    logger.info("git_ops: cloned %s → %s", url, dest)


def get_log(work_dir: str, n: int = 5) -> list[str]:
    """Return the last ``n`` one-line commit summaries (newest first)."""
    try:
        out = _run(
            ["git", "log", f"-{n}", "--oneline", "--no-decorate"],
            work_dir,
        )
        return [l for l in out.splitlines() if l]
    except GitOpsError:
        return []
