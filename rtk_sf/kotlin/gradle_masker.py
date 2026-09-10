"""
gradle_masker.py — Gradle build & JUnit/Kotest output compactor.

Wraps './gradlew' or 'gradle' commands. Returns:
  - ✅ single-line summary on success
  - ❌ compact failure block on error:
      - Kotlin compiler errors: file:line:col error message only
      - JUnit/Kotest assertion failures: test name + assertion mismatch only
      - JVM framework stack frames (java.*, org.junit.*, kotlin.*, sun.*) stripped

Token savings: a 600-line Gradle test log → ~8 lines.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Lines from JVM internals to discard from stack traces
_JVM_FRAME_RE = re.compile(
    r"^\s+at\s+(?:java\.|javax\.|kotlin\.|kotlinx\.|org\.junit\.|"
    r"org\.gradle\.|com\.sun\.|sun\.|jdk\.|scala\.)",
)

# Kotlin compiler error: e: path/to/File.kt: (line, col): message
_KT_COMPILE_ERROR_RE = re.compile(r"^e:\s+(.+\.kts?:.+)")

# Test failure markers in Gradle output
_TEST_FAIL_RE = re.compile(
    r"(\w[\w.]*)\s+>>\s+(.+)\s+FAILED|"        # Gradle :test output
    r"✗\s+(.+)|"                                 # Kotest
    r"FAIL:\s+(.+)"                              # JUnit plain
)
_EXPECTED_RE  = re.compile(r"expected:|Expected|org\.opentest4j|AssertionError|Mismatch", re.IGNORECASE)

# Task summary lines
_TASK_RE      = re.compile(r"^> Task :")
_BUILD_RE     = re.compile(r"^BUILD (SUCCESSFUL|FAILED)")
_TESTS_RE     = re.compile(r"(\d+) tests? completed(?:, (\d+) failed)?")


def _compact_failure(output: str) -> str:
    """Strip JVM noise, keep compiler errors + test failure assertions."""
    lines = output.splitlines()
    kept: list[str] = []
    in_assertion = False

    for line in lines:
        # Always keep Kotlin compiler errors
        m = _KT_COMPILE_ERROR_RE.match(line)
        if m:
            kept.append(line.strip())
            in_assertion = False
            continue

        # Keep test failure markers
        if _TEST_FAIL_RE.search(line):
            kept.append(line.strip())
            in_assertion = True
            continue

        # Keep assertion / expected lines
        if _EXPECTED_RE.search(line):
            kept.append(line.strip())
            in_assertion = True
            continue

        # Keep lines right after an assertion block (message lines)
        if in_assertion and line.strip() and not _JVM_FRAME_RE.match(line):
            if line.strip().startswith("at "):
                # Only keep first source frame (not JVM internals)
                cls = line.strip()[3:].split("(")[0]
                if not re.match(r"(?:java|javax|kotlin|kotlinx|org\.junit|org\.gradle|sun|jdk)\.", cls):
                    kept.append(line.strip())
                    in_assertion = False
            else:
                kept.append(line.strip())
            continue

        # Strip JVM internal frames
        if _JVM_FRAME_RE.match(line):
            in_assertion = False
            continue

        # Blank lines reset assertion context
        if not line.strip():
            in_assertion = False

    return "\n".join(kept) if kept else "(no parseable failure details)"


def run_build(
    task: str = "build",
    project_dir: str | Path | None = None,
    extra_args: list[str] | None = None,
    timeout: int = 300,
) -> str:
    """
    Run a Gradle task and return a compact summary.

    Args:
        task: Gradle task name, e.g. 'build', 'test', 'assemble'.
        project_dir: Directory containing gradlew (default: cwd).
        extra_args: Extra flags, e.g. ['--tests', 'com.example.FooTest'].
        timeout: Max seconds to wait (default 300).

    Returns:
        One-line success string, or compact failure block.
    """
    cwd = Path(project_dir) if project_dir else Path.cwd()

    # Prefer local wrapper
    gradlew = cwd / "gradlew"
    if gradlew.exists():
        cmd = [str(gradlew), task]
    else:
        cmd = ["gradle", task]

    if extra_args:
        cmd.extend(extra_args)

    # --console=plain keeps output parseable; --continue runs all tasks before failing
    cmd += ["--console=plain"]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=timeout,
        )
    except FileNotFoundError:
        return "❌ Gradle not found. Ensure gradlew exists or 'gradle' is on PATH."
    except subprocess.TimeoutExpired:
        return f"❌ Gradle timed out after {timeout}s."

    full_output = proc.stdout + "\n" + proc.stderr

    # Parse test counts
    test_m = _TESTS_RE.search(full_output)
    total   = int(test_m.group(1)) if test_m else 0
    failed  = int(test_m.group(2)) if test_m and test_m.group(2) else 0

    build_m = _BUILD_RE.search(full_output)
    build_ok = build_m and build_m.group(1) == "SUCCESSFUL"

    if build_ok:
        if total:
            return f"✅ Gradle {task} passed ({total} tests, 0 failed)."
        return f"✅ Gradle {task} successful."

    # Build failed
    compact = _compact_failure(full_output)
    summary = f"❌ Gradle {task} FAILED"
    if total and failed:
        summary += f" ({failed}/{total} tests failed)"
    return f"{summary}:\n{compact}"
