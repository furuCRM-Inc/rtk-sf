"""
pytest_masker.py — Compact pytest output for AI agents.

Runs pytest on a given path and returns a token-efficient summary:
  - All tests pass  → single-line "✅ N passed in X.XXs"
  - Some tests fail → condensed failure block: file path, line number,
                      AssertionError (no full traceback noise)

Token impact: a 200-line pytest trace collapses to 5-10 lines.

Usage (MCP tool):
    run_python_tests(test_path="tests/", extra_args=["-x"])
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


# Match "FAILED path/to/test.py::TestClass::test_method" lines
_FAILED_LINE_RE = re.compile(r"^FAILED\s+([\w/\\.\-]+(?:::\S+)?)", re.MULTILINE)

# Match "path/to/test.py:42: AssertionError" inside traceback blocks
_ERROR_LOCATION_RE = re.compile(
    r"([\w/\\.\-]+\.py):(\d+):\s+([\w.]+(?:Error|Exception).*?)$",
    re.MULTILINE,
)

# Match the summary line "X passed", "X failed", "X error" etc.
_SUMMARY_RE = re.compile(
    r"=+\s+([\d\w ,]+)\s+in\s+([\d.]+)s\s*=+",
)


def _extract_failures(output: str) -> list[dict[str, str]]:
    """Pull compact failure info from raw pytest stdout."""
    failures: list[dict[str, str]] = []

    # Split into per-test FAILED sections between "FAILED" markers
    sections = re.split(r"_{5,}", output)

    for section in sections:
        loc_match = _ERROR_LOCATION_RE.search(section)
        if not loc_match:
            continue
        file_path, lineno, error_type = loc_match.groups()

        # Extract the AssertionError / error message (first line after location)
        after_loc = section[loc_match.end():].strip()
        error_msg = after_loc.split("\n")[0].strip() if after_loc else ""

        # Get test node id from "FAILED test_path::test_name" if present
        test_id_match = _FAILED_LINE_RE.search(section)
        test_id = test_id_match.group(1) if test_id_match else f"{file_path}:{lineno}"

        failures.append({
            "test": test_id,
            "file": file_path,
            "line": lineno,
            "error": f"{error_type}: {error_msg}" if error_msg else error_type,
        })

    return failures


def run_tests(
    test_path: str | Path = ".",
    extra_args: list[str] | None = None,
    timeout: int = 120,
) -> str:
    """
    Run pytest and return a compact summary.

    Args:
        test_path: Directory or file to test.
        extra_args: Additional pytest arguments (e.g. ["-x", "-k", "test_auth"]).
        timeout: Max seconds before killing pytest.

    Returns:
        Token-efficient plain-text summary for an AI agent.
    """
    cmd = [sys.executable, "-m", "pytest", str(test_path), "--tb=short", "-q"]
    if extra_args:
        cmd.extend(extra_args)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return "❌ pytest not found. Install it with: pip install pytest"
    except subprocess.TimeoutExpired:
        return f"❌ pytest timed out after {timeout}s."

    combined = proc.stdout + proc.stderr

    # --- Happy path ---
    summary_match = _SUMMARY_RE.search(combined)
    if proc.returncode == 0:
        if summary_match:
            return f"✅ {summary_match.group(1).strip()} in {summary_match.group(2)}s"
        return "✅ All tests passed."

    # --- Failure path ---
    lines: list[str] = []

    if summary_match:
        lines.append(f"❌ {summary_match.group(1).strip()} in {summary_match.group(2)}s\n")
    else:
        lines.append("❌ Tests failed.\n")

    failures = _extract_failures(combined)

    if failures:
        lines.append("Failures:")
        for f in failures:
            lines.append(f"  [{f['file']}:{f['line']}] {f['test']}")
            lines.append(f"    → {f['error']}")
    else:
        # Fallback: grab last 15 lines of output which usually has the error
        tail = [ln for ln in combined.splitlines() if ln.strip()][-15:]
        lines.append("Output (last 15 lines):")
        lines.extend(f"  {ln}" for ln in tail)

    return "\n".join(lines)
