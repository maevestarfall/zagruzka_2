"""Coder tester — real subprocess execution with multi-framework parser support.

Replaces the former hardcoded stub (passed=3, failed=0, verdict=PASS) with an
actual subprocess run of the project's test suite, followed by structured
output parsing.

Supported test frameworks (auto-detected from ``test_cmd``):
  * pytest  — parses stdout for "N passed, M failed/error" summary lines
  * jest    — parses JSON reporter output (``--json`` flag injected automatically)
  * generic — raw pass/fail extraction via regex as last resort

The Scorecard is persisted to ``coder_scorecards`` in SQLite so the FSM can
read it when deciding whether to stay in TEST_PENDING or roll back to CODING.
The full test output is stored in ``output`` for passing back to the LLM as
error context.
"""
from __future__ import annotations

import logging
import re
import shlex
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"
DEFAULT_TIMEOUT_S = 300  # 5 minutes max per test run


# ─── Scorecard ───────────────────────────────────────────────────────────────

@dataclass
class Scorecard:
    run_id: str
    task_id: str
    passed: int
    failed: int
    verdict: str          # "PASS" | "FAIL" | "ERROR"
    output: str = ""      # full stdout+stderr for LLM error context
    framework: str = ""   # detected framework name
    errors: list[str] = field(default_factory=list)  # extracted failure msgs


# ─── Framework parsers ────────────────────────────────────────────────────────

def _parse_pytest(output: str) -> tuple[int, int, list[str]]:
    """Parse pytest stdout.

    Looks for lines like:
      ``5 passed, 2 failed, 1 error in 3.14s``
      ``FAILED tests/test_foo.py::test_bar - AssertionError: ...``
    """
    passed = failed = 0
    m = re.search(r"(\d+)\s+passed", output)
    if m:
        passed = int(m.group(1))
    m_fail = re.search(r"(\d+)\s+failed", output)
    if m_fail:
        failed += int(m_fail.group(1))
    m_err = re.search(r"(\d+)\s+error", output)
    if m_err:
        failed += int(m_err.group(1))

    # Extract individual failure messages (FAILED lines + short description)
    errors: list[str] = []
    for line in output.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            errors.append(line.strip())
    # Also grab AssertionError / Exception lines after "E " prefix
    for line in output.splitlines():
        if re.match(r"^E\s+\S", line):
            errors.append(line.strip())
            if len(errors) > 30:
                break

    return passed, failed, errors


def _parse_jest(output: str) -> tuple[int, int, list[str]]:
    """Parse Jest JSON reporter output (injected via ``--json`` flag).

    Falls back to plain text heuristics if JSON is missing.
    """
    import json as _json

    # Jest with --json outputs a JSON blob somewhere in stdout
    json_match = re.search(r"(\{.*\"numPassedTests\".*\})", output, re.DOTALL)
    if json_match:
        try:
            data = _json.loads(json_match.group(1))
            passed = int(data.get("numPassedTests", 0))
            failed = int(data.get("numFailedTests", 0))
            errors: list[str] = []
            for suite in data.get("testResults", []):
                for t in suite.get("testResults", []):
                    if t.get("status") == "failed":
                        msg = t.get("failureMessages", [""])[0]
                        errors.append(f"{t.get('fullName', '')}: {msg[:200]}")
            return passed, failed, errors
        except Exception:  # noqa: BLE001
            pass

    # Fallback: plain text patterns like "Tests: 3 failed, 12 passed"
    passed = failed = 0
    m = re.search(r"Tests:\s+.*?(\d+)\s+passed", output)
    if m:
        passed = int(m.group(1))
    m_f = re.search(r"Tests:\s+.*?(\d+)\s+failed", output)
    if m_f:
        failed = int(m_f.group(1))
    errors = [l.strip() for l in output.splitlines() if "● " in l]
    return passed, failed, errors


def _parse_generic(output: str) -> tuple[int, int, list[str]]:
    """Last-resort parser for unknown frameworks."""
    passed = failed = 0
    for pattern in [r"(\d+)\s+pass(?:ed|ing)?", r"ok\s+(\d+)"]:
        m = re.search(pattern, output, re.IGNORECASE)
        if m:
            passed = int(m.group(1))
            break
    for pattern in [r"(\d+)\s+fail(?:ed|ing|ure)?", r"(\d+)\s+error"]:
        m = re.search(pattern, output, re.IGNORECASE)
        if m:
            failed += int(m.group(1))
    return passed, failed, []


def _detect_framework(test_cmd: str) -> str:
    cmd_lower = test_cmd.lower()
    if "pytest" in cmd_lower:
        return "pytest"
    if "jest" in cmd_lower or "npx" in cmd_lower:
        return "jest"
    return "generic"


def _inject_flags(test_cmd: str, framework: str) -> str:
    """Inject reporter flags so parsers get structured output."""
    if framework == "jest" and "--json" not in test_cmd:
        return test_cmd + " --json"
    return test_cmd


# ─── Main entry point ─────────────────────────────────────────────────────────

def run_tests(
    run_id: str,
    task_id: str,
    work_dir: str = ".",
    test_cmd: str = "pytest",
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> Scorecard:
    """Execute the test suite in ``work_dir`` and return a structured Scorecard.

    The full output (stdout + stderr) is captured and stored so the FSM can
    feed it back to the LLM when rolling back to CODING.

    Parameters
    ----------
    run_id : str
        Coder run identifier (for DB persistence).
    task_id : str
        Task identifier (for DB persistence).
    work_dir : str
        Directory in which to run ``test_cmd`` (the generated project root).
    test_cmd : str
        Shell command to execute, e.g. ``pytest -x`` or ``npm test``.
    timeout_s : int
        Maximum execution time in seconds (default 300).

    Returns
    -------
    Scorecard
        Structured result with ``passed``, ``failed``, ``verdict``, ``output``,
        and ``errors`` (list of failure messages for LLM context).
    """
    framework = _detect_framework(test_cmd)
    effective_cmd = _inject_flags(test_cmd, framework)

    logger.info(
        "Tester: running '%s' in '%s' (run=%s task=%s framework=%s)",
        effective_cmd, work_dir, run_id, task_id, framework,
    )

    work_path = Path(work_dir).resolve()
    if not work_path.exists():
        output = f"work_dir '{work_dir}' does not exist"
        logger.error("Tester: %s", output)
        return _persist(Scorecard(
            run_id=run_id, task_id=task_id,
            passed=0, failed=1, verdict="ERROR",
            output=output, framework=framework,
            errors=[output],
        ))

    try:
        # ТЗ §10.2/§18: никогда не передавать строку в shell. Команда
        # разбирается в argv-список и исполняется без оболочки, чтобы
        # исключить инъекции через метасимволы (&&, |, ;, $()).
        argv = shlex.split(effective_cmd)
        if not argv:
            raise ValueError("empty test command")
        result = subprocess.run(  # noqa: S603 — argv-list, no shell
            argv,
            shell=False,
            cwd=str(work_path),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        combined = (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired:
        combined = f"Test suite timed out after {timeout_s}s"
        logger.error("Tester timeout: %s", combined)
        return _persist(Scorecard(
            run_id=run_id, task_id=task_id,
            passed=0, failed=1, verdict="ERROR",
            output=combined, framework=framework,
            errors=[combined],
        ))
    except Exception as exc:  # noqa: BLE001
        combined = f"Failed to launch test suite: {exc}"
        logger.error("Tester launch error: %s", combined)
        return _persist(Scorecard(
            run_id=run_id, task_id=task_id,
            passed=0, failed=1, verdict="ERROR",
            output=combined, framework=framework,
            errors=[combined],
        ))

    # Parse output
    if framework == "pytest":
        passed, failed, errors = _parse_pytest(combined)
    elif framework == "jest":
        passed, failed, errors = _parse_jest(combined)
    else:
        passed, failed, errors = _parse_generic(combined)

    # If subprocess exited non-zero but we parsed zero failures, treat as error
    if result.returncode != 0 and failed == 0 and passed == 0:
        failed = 1
        errors = [f"Process exited {result.returncode}: {combined[:300]}"]

    verdict = "PASS" if failed == 0 else "FAIL"

    logger.info(
        "Tester result: %d passed, %d failed → %s (exit=%d)",
        passed, failed, verdict, result.returncode,
    )

    return _persist(Scorecard(
        run_id=run_id, task_id=task_id,
        passed=passed, failed=failed, verdict=verdict,
        output=combined, framework=framework,
        errors=errors,
    ))


# ─── DB persistence ───────────────────────────────────────────────────────────

def _persist(sc: Scorecard) -> Scorecard:
    """Write scorecard to ``coder_scorecards``.  Returns ``sc`` unchanged."""
    try:
        conn = sqlite3.connect(DB_PATH)
        with conn:
            conn.execute("""
                INSERT INTO coder_scorecards
                    (run_id, task_id, owner_persona, passed_tests,
                     failed_tests, verdict, created_at)
                VALUES (?, ?, 'coder_tester', ?, ?, ?, ?)
            """, (
                sc.run_id, sc.task_id,
                sc.passed, sc.failed, sc.verdict,
                time.strftime("%Y-%m-%d %H:%M:%S"),
            ))
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Tester: could not persist scorecard: %s", exc)
    return sc
