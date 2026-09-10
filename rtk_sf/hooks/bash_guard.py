#!/usr/bin/env python3
"""
PreToolUse[Bash] hook — blocks raw metadata pipeline scans.

Detects commands like:
    find force-app ... | xargs cat
    find ... recordTypes ... | xargs sh -c 'cat ...'

These would flood the context window with thousands of tokens of declarative
Salesforce XML. The hook blocks the command and redirects Claude to the
get_record_types MCP tool instead.

Claude Code hook protocol:
  stdin : JSON {"tool_name": "Bash", "tool_input": {"command": "..."}, ...}
  exit 0: allow the command to run normally
  exit 2: block — stdout text is shown to Claude as the tool result instead
"""

from __future__ import annotations

import json
import re
import sys

# Salesforce metadata folder names that contain large declarative XML
_METADATA_FOLDERS = {
    "recordTypes",
    "fields",
    "businessProcesses",
    "listViews",
    "compactLayouts",
    "webLinks",
    "layouts",
    "validationRules",
    "sharingRules",
}

# Pipeline verbs that indicate a bulk file-read attempt
_PIPELINE_VERBS = re.compile(r"\b(xargs|cat)\b")

# Heuristic: command contains `find` + a pipeline verb + a metadata folder name
def _is_metadata_pipeline(command: str) -> tuple[bool, str]:
    """Return (is_dangerous, matched_folder)."""
    if "find" not in command:
        return False, ""
    if not _PIPELINE_VERBS.search(command):
        return False, ""
    for folder in _METADATA_FOLDERS:
        if folder in command:
            return True, folder
    return False, ""


def _extract_object_hint(command: str) -> str:
    """Try to pull an object API name from the command string."""
    m = re.search(r"([\w]+__[cC]|Account|Contact|Opportunity|Lead|Case|Order)", command)
    return m.group(1) if m else ""


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        sys.exit(0)

    command = data.get("tool_input", {}).get("command", "")
    if not command:
        sys.exit(0)

    dangerous, matched_folder = _is_metadata_pipeline(command)
    if not dangerous:
        sys.exit(0)

    object_hint = _extract_object_hint(command)
    call_hint = (
        f'get_record_types(object_name="{object_hint}")'
        if object_hint
        else 'get_record_types(object_name="<ObjectApiName>")'
    )

    try:
        from rtk_sf.hooks._log import append
        append("bash_guard", "blocked", folder=matched_folder, object=object_hint)
    except Exception:
        pass

    sys.stdout.write(
        f"[rtk-sf] Blocked raw metadata pipeline scan targeting '{matched_folder}/' folder.\n"
        f"Reading multiple XML files this way would flood the context window with thousands "
        f"of tokens of declarative Salesforce markup.\n\n"
        f"Use the MCP tool instead:\n"
        f"  {call_hint}\n\n"
        f"This returns a compressed 5-line summary per record type "
        f"(FullName, Label, Active, tracked picklist names) "
        f"instead of raw XML. Request specific picklist values only if you need to mutate them.\n"
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
