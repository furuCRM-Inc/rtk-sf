"""
sf_runner.py — Silent execution wrapper for Salesforce CLI commands.

Runs `sf project deploy/retrieve` with --json flag in a background subprocess,
captures the full JSON response, and returns a condensed 3-line summary to
the caller (Claude Code or MCP client). The thousands of table lines that sf
normally dumps to stdout never reach the AI context window.

Token impact: cuts terminal stream response overhead by ~99%.

Supported actions:
    deploy    sf project deploy start   [--source-dir | --metadata] [--test-level] [--tests]
    validate  sf project deploy start --dry-run   (same flags as deploy)
    retrieve  sf project retrieve start [--source-dir] [--metadata]
    run_test  sf apex run test          [--class-names] [--test-level]
    describe  sf org display            (read-only, always safe)

Apex test selection is spelled differently per command: `sf apex run test` takes
`--class-names`, while `sf project deploy start` takes a repeatable `--tests`.
Callers pass `class_names` either way and this module maps it to the flag the
target command actually has.

Usage (via MCP tool sf_command):
    result = run_sf_command("deploy", {"target_org": "dev01", "source_dir": "force-app"})
    # → "✅ Deployed 42 files to dev01 in 8.3s"
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from typing import Any

logger = logging.getLogger(__name__)

# On Windows, sf is installed as sf.cmd (a batch file).
# subprocess without shell=True cannot execute .cmd files directly.
_SHELL = sys.platform == "win32"

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

    source_dir = args.get("source_dir")
    metadata = args.get("metadata")

    # `sf project deploy start` rejects --source-dir and --metadata together:
    #   "--metadata=... cannot also be provided when using --source-dir"
    # Retrieve accepts the combination, so only deploy/validate are constrained.
    if action in ("deploy", "validate") and source_dir and metadata:
        raise ValueError(
            f"{action} accepts either source_dir or metadata, not both — "
            "the sf CLI rejects --source-dir alongside --metadata. "
            "Pass metadata to deploy specific components, or source_dir for a whole path."
        )

    if args.get("target_org"):
        cmd += ["--target-org", str(args["target_org"])]
    if source_dir:
        cmd += ["--source-dir", str(source_dir)]
    if metadata:
        items = metadata if isinstance(metadata, list) else [metadata]
        for item in items:
            cmd += ["--metadata", str(item)]
    if args.get("test_level"):
        cmd += ["--test-level", str(args["test_level"])]
    class_names = args.get("class_names")
    if class_names:
        names = (
            [str(n).strip() for n in class_names if str(n).strip()]
            if isinstance(class_names, (list, tuple))
            else [n.strip() for n in str(class_names).split(",") if n.strip()]
        )
        if action == "run_test":
            # `sf apex run test` takes one comma-separated --class-names value.
            cmd += ["--class-names", ",".join(names)]
        elif action in ("deploy", "validate"):
            # `sf project deploy start` has no --class-names flag — it rejects it
            # with "Nonexistent flag: --class-names". The equivalent is a
            # repeatable --tests, which is only honoured with RunSpecifiedTests.
            for name in names:
                cmd += ["--tests", name]
            if not args.get("test_level"):
                cmd += ["--test-level", "RunSpecifiedTests"]
        else:
            raise ValueError(
                f"{action} does not run Apex tests — drop class_names. "
                "Use run_test to run tests, or deploy/validate to run them as "
                "part of a deployment."
            )
    if args.get("wait"):
        cmd += ["--wait", str(args["wait"])]

    return cmd


def _cli_error(data: dict[str, Any]) -> str | None:
    """
    Return a one-line error summary if the CLI rejected the command itself.

    On a flag error, a missing default org or an auth failure the JSON carries
    no nested `result` and `status` is the process exit code, e.g.

        {"name": "NoDefaultEnvError", "message": "No default environment …",
         "status": 1}

    Every summarizer must consult this first: reading `result` straight off
    such a payload yields an empty dict, which previously surfaced as a
    *successful* summary ("✅ Org: unknown ()") and hid the real error.

    Returns None when the payload looks like a successful command.
    """
    result = data.get("result")
    top_status = data.get("status")

    failed = not isinstance(result, dict) or (not result and top_status not in (0, None))
    if not failed:
        return None

    msg = data.get("message") or data.get("name") or "Unknown error"
    # CLI messages are multi-line; keep the summary contract to one line.
    msg = " ".join(str(msg).split())[:300]
    return f"❌ sf CLI error: {msg}"


def _summarize_deploy(data: dict[str, Any], elapsed: float) -> str:
    """Condense a deploy/retrieve JSON result to 3 lines."""
    error = _cli_error(data)
    if error:
        return error

    result = data.get("result") or {}
    top_status = data.get("status")

    status = result.get("status", top_status if top_status is not None else "Unknown")
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
        f"✅ {str(status).title()}: {done} component(s)"
        + (f" → {org}" if org else "")
        + f" in {elapsed:.1f}s"
    )


def _summarize_run_test(data: dict[str, Any], elapsed: float) -> str:
    """Condense an apex run test JSON result."""
    error = _cli_error(data)
    if error:
        return error

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


# Fields of `sf org display --json` worth a line in the summary, in print order.
_DESCRIBE_FIELDS: list[tuple[str, tuple[str, ...]]] = [
    ("Alias", ("alias",)),
    ("Username", ("username",)),
    ("Org ID", ("id", "orgId")),
    ("Instance", ("instanceUrl",)),
    ("Status", ("connectedStatus", "status")),
    ("API version", ("apiVersion",)),
    ("Expires", ("expirationDate",)),
]


def _one_line(value: Any, limit: int = 160) -> str:
    """Collapse a field value to a single bounded line.

    `connectedStatus` carries a whole multi-line REST error when the org is
    unreachable, which would otherwise blow the summary's size contract.
    """
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _summarize_describe(data: dict[str, Any], elapsed: float) -> str:
    """
    Condense `sf org display --json` to a labelled block.

    Only fields the CLI actually returned are printed, so a scratch org shows
    its expiry and a production org does not.
    """
    error = _cli_error(data)
    if error:
        if "NoDefaultEnv" in str(data.get("name", "")) or "default environment" in error:
            return (
                f"{error}\n"
                "  → Pass target_org, or set a default: sf config set target-org=<alias>"
            )
        return error

    result = data.get("result") or {}

    lines: list[str] = []
    for label, keys in _DESCRIBE_FIELDS:
        value = next(
            (result[k] for k in keys if result.get(k) not in (None, "")),
            None,
        )
        if value is not None:
            lines.append(f"  {label}: {_one_line(value)}")

    if not lines:
        # Exit code 0 but nothing recognisable in the payload — say so rather
        # than inventing an org named "unknown".
        return (
            "⚠️ sf org display returned no org fields. "
            f"Keys present: {', '.join(sorted(result)) or 'none'}"
        )

    headline = _one_line(result.get("alias") or result.get("username") or "org", 80)
    return f"✅ Org: {headline}\n" + "\n".join(lines)


def _summarize_retrieve(data: dict[str, Any], elapsed: float) -> str:
    """Condense a retrieve JSON result to one line."""
    error = _cli_error(data)
    if error:
        return error

    result = data.get("result") or {}
    files = result.get("fileCount")
    if files is None:
        files = len(result.get("files") or [])
    return f"✅ Retrieved {files} file(s) in {elapsed:.1f}s"


def _summarize_generic(data: dict[str, Any], action: str, elapsed: float) -> str:
    error = _cli_error(data)
    if error:
        return error
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
            shell=_SHELL,
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
        return _summarize_retrieve(data, elapsed)
    if action == "run_test":
        return _summarize_run_test(data, elapsed)
    if action == "describe":
        return _summarize_describe(data, elapsed)
    return _summarize_generic(data, action, elapsed)
