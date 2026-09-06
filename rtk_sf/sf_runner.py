"""
sf_runner.py — Silent execution wrapper for Salesforce CLI commands.

Runs `sf project deploy/retrieve` with --json flag in a background subprocess,
captures the full JSON response, and returns a condensed 3-line summary to
the caller (Claude Code or MCP client). The thousands of table lines that sf
normally dumps to stdout never reach the AI context window.

Token impact: cuts terminal stream response overhead by ~99%.

Supported actions:
    deploy    sf project deploy start   [--source-dir] [--metadata] [--test-level]
    retrieve  sf project retrieve start [--source-dir] [--metadata]
    run_test  sf apex run test          [--class-names] [--test-level]
    describe  sf org describe           (read-only, always safe)

Usage (via MCP tool sf_command):
    result = run_sf_command("deploy", {"target_org": "dev01", "source_dir": "force-app"})
    # → "✅ Deployed 42 files to dev01 in 8.3s"
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Action → sf CLI mapping
# ---------------------------------------------------------------------------

_ACTION_MAP: dict[str, list[str]] = {
    "deploy":   ["sf", "project", "deploy", "start"],
    "retrieve": ["sf", "project", "retrieve", "start"],
    "run_test": ["sf", "apex", "run", "test"],
    "describe": ["sf", "org", "display"],
    "validate": ["sf", "project", "deploy", "start", "--dry-run"],
}

# Safe (read-only) actions — no confirmation needed
_SAFE_ACTIONS = {"retrieve", "describe", "run_test"}


def _build_command(action: str, args: dict[str, Any]) -> list[str]:
    """Build the sf CLI command list from action and argument dict."""
    base = _ACTION_MAP.get(action)
    if base is None:
        raise ValueError(f"Unknown action '{action}'. Valid: {list(_ACTION_MAP)}")

    cmd = list(base) + ["--json"]

    if args.get("target_org"):
        cmd += ["--target-org", str(args["target_org"])]
    if args.get("source_dir"):
        cmd += ["--source-dir", str(args["source_dir"])]
    if args.get("metadata"):
        md = args["metadata"]
        items = md if isinstance(md, list) else [md]
        for item in items:
            cmd += ["--metadata", item]
    if args.get("test_level"):
        cmd += ["--test-level", str(args["test_level"])]
    if args.get("class_names"):
        names = args["class_names"]
        if isinstance(names, list):
            cmd += ["--class-names", ",".join(names)]
        else:
            cmd += ["--class-names", str(names)]
    if args.get("wait"):
        cmd += ["--wait", str(args["wait"])]

    return cmd


def _summarize_deploy(data: dict[str, Any], elapsed: float) -> str:
    """Condense a deploy/retrieve JSON result to 3 lines."""
    result = data.get("result", {})
    status = result.get("status", data.get("status", "Unknown"))
    done = result.get("numberComponentsDeployed", result.get("fileCount", 0))
    errors = result.get("numberComponentErrors", 0)
    org = result.get("orgId", result.get("username", ""))

    if errors:
        # Collect first 3 error messages
        component_failures = result.get("details", {}).get("componentFailures", [])
        msgs = [
            f"  • {f.get('componentType','?')} {f.get('fullName','?')}: {f.get('problem','?')}"
            for f in component_failures[:3]
        ]
        return (
            f"❌ Deploy failed — {errors} error(s)\n"
            + "\n".join(msgs)
            + ("\n  … (see full log)" if len(component_failures) > 3 else "")
        )

    return (
        f"✅ {status.title()}: {done} component(s)"
        + (f" → {org}" if org else "")
        + f" in {elapsed:.1f}s"
    )


def _summarize_run_test(data: dict[str, Any], elapsed: float) -> str:
    """Condense an apex run test JSON result."""
    result = data.get("result", {})
    summary = result.get("summary", {})
    passed = summary.get("passing", 0)
    failed = summary.get("failing", 0)
    total = summary.get("testsRan", passed + failed)

    if failed:
        failures = result.get("tests", [])
        failed_msgs = [
            f"  • {t.get('ApexClass',{}).get('Name','?')}.{t.get('MethodName','?')}: "
            f"{t.get('Message','')}"
            for t in failures
            if t.get("Outcome") == "Fail"
        ][:3]
        return (
            f"❌ Tests: {passed}/{total} passed — {failed} failed\n"
            + "\n".join(failed_msgs)
        )

    return f"✅ Tests: {passed}/{total} passed in {elapsed:.1f}s"


def _summarize_describe(data: dict[str, Any], elapsed: float) -> str:
    result = data.get("result", {})
    alias = result.get("alias", result.get("username", "unknown"))
    instance = result.get("instanceUrl", "")
    return f"✅ Org: {alias} ({instance})"


def _summarize_generic(data: dict[str, Any], action: str, elapsed: float) -> str:
    status = data.get("status", 0)
    if status != 0:
        msg = data.get("message", "Unknown error")
        return f"❌ {action} failed: {msg}"
    return f"✅ {action} completed in {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_sf_command(action: str, args: dict[str, Any] | None = None) -> str:
    """
    Execute an sf CLI command silently and return a condensed summary.

    Args:
        action:  One of 'deploy', 'retrieve', 'run_test', 'describe', 'validate'
        args:    Optional dict with keys: target_org, source_dir, metadata,
                 test_level, class_names, wait

    Returns:
        A 1–4 line human-readable summary string.
    """
    args = args or {}

    try:
        cmd = _build_command(action, args)
    except ValueError as exc:
        return f"❌ {exc}"

    logger.info("sf_runner: %s", " ".join(cmd))
    start = time.time()

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError:
        return "❌ sf CLI not found. Install: https://developer.salesforce.com/tools/salesforcecli"
    except subprocess.TimeoutExpired:
        return f"❌ {action} timed out after 600s"
    except Exception as exc:
        return f"❌ Unexpected error: {exc}"

    elapsed = time.time() - start

    # Parse JSON output
    raw = proc.stdout.strip() or proc.stderr.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # sf printed non-JSON (e.g. missing auth) — return first 200 chars
        snippet = raw[:200].replace("\n", " ")
        return f"❌ sf returned non-JSON output: {snippet}"

    # Summarize by action type
    if action in ("deploy", "validate"):
        return _summarize_deploy(data, elapsed)
    if action == "retrieve":
        files = data.get("result", {}).get("fileCount", "?")
        return f"✅ Retrieved {files} file(s) in {elapsed:.1f}s"
    if action == "run_test":
        return _summarize_run_test(data, elapsed)
    if action == "describe":
        return _summarize_describe(data, elapsed)
    return _summarize_generic(data, action, elapsed)
