"""
build_masker.py — Maven/Gradle build & JUnit output compactor for Java.

Wraps 'mvn' or './gradlew' commands. Returns:
  - ✅ single-line summary on success
  - ❌ compact failure block on error:
      - Java compiler errors: Filename.java:[line,col] error: message only
      - JUnit/TestNG assertion failures: test class + assertion mismatch only
      - Spring/Tomcat/Hibernate/JVM framework stack frames stripped

Token savings: a 600-line Maven/Gradle test log → ~10 lines.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# JVM/framework internals to strip from stack traces
_JVM_FRAME_RE = re.compile(
    r"^\s+at\s+(?:java\.|javax\.|jakarta\.|sun\.|jdk\.|com\.sun\."
    r"|org\.springframework\.|org\.hibernate\.|org\.apache\.tomcat\."
    r"|org\.apache\.catalina\.|org\.junit\.|org\.testng\.|org\.mockito\."
    r"|org\.gradle\.|kotlin\.|kotlinx\.)",
)

# Java compiler error: Foo.java:[12,5] error: cannot find symbol
_JAVAC_ERROR_RE = re.compile(
    r"(?:\[ERROR\]\s+)?(\S+\.java):\[(\d+),\d+\]\s+(error:.*|warning:.*)"
    r"|(?:\[ERROR\]\s+)(\S+\.java):(\d+):\s+error:"
)

# Maven vs Gradle markers
_BUILD_SUCCESS_RE = re.compile(r"BUILD SUCCESS|BUILD SUCCESSFUL")
_BUILD_FAILURE_RE = re.compile(r"BUILD FAIL(?:URE|ED)")
_TESTS_RE         = re.compile(
    r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)"  # Maven surefire
    r"|(\d+) tests? completed(?:, (\d+) failed)?"                    # Gradle
)

# Test assertion errors
_ASSERTION_RE     = re.compile(
    r"AssertionError|expected:|Expected:|org\.opentest4j|ComparisonFailure"
    r"|java\.lang\.AssertionError|Wanted but not invoked",
    re.IGNORECASE,
)
_TEST_FAIL_RE     = re.compile(
    r"FAILED\s+[\w.]+#?\w*|"
    r"testFailure|Tests FAILED|"
    r"\[ERROR\].*FAILED",
)


def _compact_failure(output: str, tool: str) -> str:
    """Strip framework noise, keep compiler errors + assertion failures."""
    lines = output.splitlines()
    kept: list[str] = []
    in_assertion = False

    for line in lines:
        # Javac / ECJ compile errors
        if _JAVAC_ERROR_RE.search(line):
            kept.append(line.strip())
            in_assertion = False
            continue

        # Test failure markers
        if _TEST_FAIL_RE.search(line):
            kept.append(line.strip())
            in_assertion = True
            continue

        # Assertion / expected lines
        if _ASSERTION_RE.search(line):
            kept.append(line.strip())
            in_assertion = True
            continue

        # Lines after assertion block (avoid JVM internals)
        if in_assertion and line.strip() and not _JVM_FRAME_RE.match(line):
            if line.strip().startswith("at "):
                cls = line.strip()[3:].split("(")[0]
                if not re.match(
                    r"(?:java|javax|jakarta|sun|jdk|org\.springframework"
                    r"|org\.hibernate|org\.apache|org\.junit|org\.testng"
                    r"|org\.mockito|org\.gradle|kotlin)\.", cls
                ):
                    kept.append(line.strip())
                    in_assertion = False
            else:
                kept.append(line.strip())
            continue

        if _JVM_FRAME_RE.match(line):
            in_assertion = False
            continue

        if not line.strip():
            in_assertion = False

    return "\n".join(kept) if kept else "(no parseable failure details)"


def run_build(
    tool: str = "auto",
    task: str = "test",
    project_dir: str | Path | None = None,
    extra_args: list[str] | None = None,
    timeout: int = 300,
) -> str:
    """
    Run a Maven or Gradle build task and return a compact summary.

    Args:
        tool: 'mvn', 'gradle', or 'auto' (detect from project files).
        task: Maven goal or Gradle task, e.g. 'test', 'package', 'verify'.
        project_dir: Directory containing pom.xml or gradlew (default: cwd).
        extra_args: Extra flags, e.g. ['-Dtest=FooTest', '--info'].
        timeout: Max seconds to wait (default 300).

    Returns:
        One-line success string, or compact failure block.
    """
    cwd = Path(project_dir) if project_dir else Path.cwd()

    # Auto-detect tool
    if tool == "auto":
        if (cwd / "gradlew").exists():
            tool = "gradle"
        elif (cwd / "pom.xml").exists():
            tool = "mvn"
        else:
            return "❌ No pom.xml or gradlew found. Specify tool='mvn' or tool='gradle'."

    if tool in ("gradle", "gradlew"):
        gradlew = cwd / "gradlew"
        cmd = [str(gradlew) if gradlew.exists() else "gradle", task, "--console=plain"]
    else:
        cmd = ["mvn", task, "--batch-mode", "-q"]

    if extra_args:
        cmd.extend(extra_args)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=timeout,
        )
    except FileNotFoundError:
        tool_name = "Maven (mvn)" if tool == "mvn" else "Gradle"
        return f"❌ {tool_name} not found. Ensure it is installed and on PATH."
    except subprocess.TimeoutExpired:
        return f"❌ Build timed out after {timeout}s."

    full_output = proc.stdout + "\n" + proc.stderr

    # Parse test counts (Maven surefire first, then Gradle)
    total = failed = 0
    for m in _TESTS_RE.finditer(full_output):
        if m.group(1) is not None:  # Maven: Tests run: X, Failures: Y, Errors: Z
            total += int(m.group(1))
            failed += int(m.group(2)) + int(m.group(3))
        elif m.group(4) is not None:  # Gradle: X tests completed, Y failed
            total = int(m.group(4))
            failed = int(m.group(5)) if m.group(5) else 0

    build_ok = bool(_BUILD_SUCCESS_RE.search(full_output))

    if build_ok:
        tool_label = "Maven" if tool == "mvn" else "Gradle"
        if total:
            return f"✅ {tool_label} {task} passed ({total} tests, 0 failed)."
        return f"✅ {tool_label} {task} successful."

    compact = _compact_failure(full_output, tool)
    tool_label = "Maven" if tool == "mvn" else "Gradle"
    summary = f"❌ {tool_label} {task} FAILED"
    if total and failed:
        summary += f" ({failed}/{total} tests failed)"
    return f"{summary}:\n{compact}"
