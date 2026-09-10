"""
jest_masker.py — Compact Jest/Vitest/Playwright output for AI agents.

Runs a JavaScript test command and returns a token-efficient summary:
  - All tests pass  → single-line "✅ N tests passed (N suites)"
  - Some fail       → per-failure: file path, line number, and expect() mismatch only.
                       All node_modules stack frames are stripped.

Token impact: a 300-line Jest failure log collapses to ~10 lines.

Usage (MCP tool):
    run_js_tests(test_path="src/", runner="jest", extra_args=["--testPathPattern=payment"])
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# ── Patterns ──────────────────────────────────────────────────────────────────

# Jest/Vitest summary: "Tests: 3 failed, 12 passed, 15 total"
_JEST_SUMMARY_RE = re.compile(
    r"(?:Tests|Test Suites):\s+([\d\w ,]+)\n",
    re.IGNORECASE,
)
# Vitest summary: "✓ 12 tests passed"
_VITEST_SUMMARY_RE = re.compile(
    r"(?:✓|×|✗|PASS|FAIL)\s+.*?(\d+)\s+(?:tests?|specs?)\s+(?:passed|failed)",
    re.IGNORECASE,
)

# Failure block header: "● PaymentService › charge › should reject negative amounts"
_JEST_FAIL_BLOCK_RE = re.compile(r"^\s*●\s+(.+)$", re.MULTILINE)

# Error location in source (not node_modules): "at PaymentService.charge (src/payment.ts:42:8)"
_SOURCE_FRAME_RE = re.compile(
    r"at\s+[\w.<> ]+\s+\((?!node_modules)([^)]+:\d+:\d+)\)",
)
# "Expected: ..." / "Received: ..." lines
_EXPECT_RECEIVED_RE = re.compile(r"^\s+(?:Expected|Received|✕|✓).*$", re.MULTILINE)

# node_modules stack frame — to strip
_NODE_MOD_FRAME_RE = re.compile(r"^\s+at\s+.*node_modules.*$", re.MULTILINE)

# Playwright: "1 passed (3s)"
_PLAYWRIGHT_SUMMARY_RE = re.compile(r"(\d+) (?:passed|failed|skipped)", re.IGNORECASE)


def _detect_runner(command: str) -> str:
    """Infer test runner from command string."""
    lower = command.lower()
    if "vitest" in lower:
        return "vitest"
    if "playwright" in lower:
        return "playwright"
    return "jest"  # default


def _build_command(
    runner: str,
    test_path: str | None,
    extra_args: list[str],
) -> list[str]:
    """Build the subprocess command list."""
    if runner == "jest":
        cmd = ["npx", "jest", "--no-coverage", "--forceExit"]
    elif runner == "vitest":
        cmd = ["npx", "vitest", "run", "--reporter=verbose"]
    elif runner == "playwright":
        cmd = ["npx", "playwright", "test"]
    else:
        cmd = ["npx", runner]

    if test_path and test_path != ".":
        cmd.append(test_path)
    cmd.extend(extra_args)
    return cmd


def _compact_failure(block: str) -> str:
    """
    Compact a single Jest/Vitest failure block.
    Keeps: test name, expect()/received lines, first source frame.
    Strips: node_modules frames, long stack traces.
    """
    # Strip node_modules lines
    cleaned = _NODE_MOD_FRAME_RE.sub("", block)

    lines = cleaned.splitlines()
    out: list[str] = []
    seen_source_frame = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Keep the test description line (starts with ●)
        if stripped.startswith("●"):
            out.append(stripped)
            continue

        # Keep Error/expect lines
        if any(
            stripped.startswith(p)
            for p in ("Error:", "expect(", "Expected", "Received", "✕", "✗", "●")
        ):
            out.append("  " + stripped)
            continue

        # Keep first source frame only
        if not seen_source_frame and _SOURCE_FRAME_RE.search(line):
            m = _SOURCE_FRAME_RE.search(line)
            if m:
                out.append(f"  at {m.group(1)}")
                seen_source_frame = True
            continue

    return "\n".join(out)


def run_tests(
    test_path: str | Path | None = None,
    runner: str = "jest",
    extra_args: list[str] | None = None,
    cwd: str | Path | None = None,
    timeout: int = 120,
) -> str:
    """
    Run a JavaScript/TypeScript test suite and return a compact summary.

    Args:
        test_path: File, directory, or pattern to pass to the test runner.
        runner: "jest" | "vitest" | "playwright" (default: "jest").
        extra_args: Additional CLI flags.
        cwd: Working directory (default: current directory).
        timeout: Max seconds before killing the process.

    Returns:
        Token-efficient plain-text summary.
    """
    cmd = _build_command(runner, str(test_path) if test_path else None, extra_args or [])
    work_dir = str(cwd) if cwd else None

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=work_dir,
        )
    except FileNotFoundError:
        return (
            f"❌ '{cmd[0]}' not found. "
            "Make sure Node.js and npm are installed, and run `npm install` in the project root."
        )
    except subprocess.TimeoutExpired:
        return f"❌ Test run timed out after {timeout}s."

    combined = proc.stdout + "\n" + proc.stderr

    # ── Happy path ──────────────────────────────────────────────────────────
    if proc.returncode == 0:
        # Jest summary line
        m = _JEST_SUMMARY_RE.search(combined)
        if m:
            return f"✅ {m.group(1).strip()}"
        # Vitest
        m = _VITEST_SUMMARY_RE.search(combined)
        if m:
            return f"✅ {m.group().strip()}"
        # Playwright
        counts = _PLAYWRIGHT_SUMMARY_RE.findall(combined)
        if counts:
            return "✅ " + ", ".join(f"{n} passed" for n in counts)
        return "✅ All tests passed."

    # ── Failure path ────────────────────────────────────────────────────────
    out_lines: list[str] = []

    # Summary
    summary_m = _JEST_SUMMARY_RE.search(combined)
    if summary_m:
        out_lines.append(f"❌ {summary_m.group(1).strip()}\n")
    else:
        out_lines.append("❌ Tests failed.\n")

    # Extract individual failure blocks (Jest style: separated by ●)
    fail_blocks = re.split(r"\n(?=\s*●\s)", combined)
    compacted: list[str] = []
    for block in fail_blocks:
        if "●" in block and ("Error" in block or "expect(" in block):
            compacted.append(_compact_failure(block))

    if compacted:
        out_lines.append("Failures:")
        out_lines.extend(compacted)
    else:
        # Fallback: last 20 non-empty lines, no node_modules frames
        cleaned = _NODE_MOD_FRAME_RE.sub("", combined)
        tail = [l for l in cleaned.splitlines() if l.strip()][-20:]
        out_lines.append("Output (last 20 lines, node_modules stripped):")
        out_lines.extend(f"  {l}" for l in tail)

    return "\n".join(out_lines)
