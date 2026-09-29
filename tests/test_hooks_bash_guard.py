"""
Pins the PreToolUse[Bash] guard's protocol and its matching rules.

The guard exists to stop raw metadata pipeline scans from flooding the context
window, and to redirect the caller to the MCP tool that answers the same question
cheaply. That redirect only helps if Claude actually receives it — which is why
the hook blocks with a structured `permissionDecision`, not with exit 2 (whose
message is read from stderr, leaving a stdout redirect discarded and surfacing as
"hook error: No stderr output").
"""

from __future__ import annotations

import json
import subprocess
import sys

HOOK = [sys.executable, "-m", "rtk_sf.hooks.bash_guard"]


def _run(command: str):
    return subprocess.run(
        HOOK,
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _decision(result):
    assert result.returncode == 0, f"blocking uses exit 0 with JSON, got {result.returncode}"
    assert result.stderr == "", f"the hook leaked stderr: {result.stderr!r}"
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)["hookSpecificOutput"]


def test_ordinary_commands_are_allowed_silently():
    for command in ["ls -la", "git status", "npm test", "sf org list"]:
        result = _run(command)
        assert _decision(result) is None, f"{command} should pass through untouched"


def test_a_metadata_pipeline_scan_is_denied_with_a_redirect():
    result = _run("find force-app -path '*recordTypes*' -name '*.xml' | xargs cat")
    decision = _decision(result)

    assert decision is not None, "the scan should be blocked"
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    reason = decision["permissionDecisionReason"]
    assert "get_record_types" in reason, "the reason must name the tool to use instead"
    assert "recordTypes" in reason


def test_the_object_name_is_carried_into_the_suggestion():
    result = _run("find force-app/main/default/objects/Account/recordTypes -name '*.xml' | xargs cat")
    reason = _decision(result)["permissionDecisionReason"]
    assert 'get_record_types(object_name="Account")' in reason


def test_an_lwc_metadata_scan_is_denied():
    result = _run("cat force-app/main/default/lwc/myCmp/myCmp.js-meta.xml")
    decision = _decision(result)
    assert decision["permissionDecision"] == "deny"
    assert "get_lwc_targets" in decision["permissionDecisionReason"]


def test_interpreter_commands_are_not_mistaken_for_scans():
    """
    A script body may legitimately mention these file names; only the shell
    pipeline that would read them in bulk is blocked.
    """
    for command in [
        "python3 script.py  # parses js-meta.xml internally",
        "node build.js",
        "git commit -m 'fix js-meta.xml handling'",
    ]:
        assert _decision(_run(command)) is None, f"{command} should be allowed"


def test_listing_without_reading_is_allowed():
    result = _run("find force-app -name '*.js-meta.xml'")
    assert _decision(result) is None, "listing file names reads no content"


def test_a_malformed_payload_does_not_block():
    result = subprocess.run(HOOK, input="not json", capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert result.stderr == ""
