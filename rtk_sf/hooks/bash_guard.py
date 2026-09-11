#!/usr/bin/env python3
"""
PreToolUse[Bash] hook — blocks raw metadata pipeline scans.

Detects commands like:
    find force-app ... | xargs cat
    find ... recordTypes ... | xargs sh -c 'cat ...'
    grep ... lwc/*/*.js-meta.xml
    cat lwc/myComponent/myComponent.js-meta.xml

These would flood the context window with thousands of tokens of declarative
Salesforce XML. The hook blocks the command and redirects Claude to the
appropriate MCP tool instead.

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

# LWC metadata file pattern
_LWC_META_RE = re.compile(r"js-meta\.xml")

# Command prefixes that are safe to pass through — either language interpreters whose
# script bodies may contain js-meta.xml as string literals, or VCS/build tools whose
# arguments (commit messages, log output) are not file-read pipelines.
_INTERPRETER_PREFIX = re.compile(r"^\s*(python3?|node|ruby|perl|bash\s+-c|git|gh|npm|npx|mvn|gradle)\b")

# Direct read verbs that always indicate content consumption when targeting js-meta.xml
_LWC_DIRECT_READ = re.compile(r"\b(grep|cat)\b")


def _is_metadata_pipeline(command: str) -> tuple[bool, str]:
    """Return (is_dangerous, matched_folder) for declarative XML folder scans."""
    if _INTERPRETER_PREFIX.match(command):
        return False, ""
    if "find" not in command:
        return False, ""
    if not _PIPELINE_VERBS.search(command):
        return False, ""
    for folder in _METADATA_FOLDERS:
        if folder in command:
            return True, folder
    return False, ""


def _is_lwc_metadata_scan(command: str) -> bool:
    """Return True if the command would read LWC *.js-meta.xml file contents.

    Blocks:
      grep ... lwc/*/*.js-meta.xml          (direct grep of XML contents)
      cat  ... lwc/foo/foo.js-meta.xml      (direct cat)
      find ... js-meta.xml | xargs cat      (pipeline read via xargs)
      find ... js-meta.xml | xargs grep     (pipeline grep via xargs)

    Allows:
      find ... -name "*.js-meta.xml"        (listing only — no content read)
      ls lwc/                               (directory listing)
    """
    if _INTERPRETER_PREFIX.match(command):
        return False
    if not _LWC_META_RE.search(command):
        return False
    # grep/cat directly on js-meta.xml files — always a content read
    if _LWC_DIRECT_READ.search(command):
        return True
    # find + xargs pipeline — reads content even if find alone is safe
    if "find" in command and "xargs" in command:
        return True
    return False


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

    # --- LWC metadata scan check (higher priority) ---
    if _is_lwc_metadata_scan(command):
        try:
            from rtk_sf.hooks._log import append
            append("bash_guard", "blocked_lwc", command=command[:120])
        except Exception:
            pass

        sys.stdout.write(
            "[rtk-sf] Blocked raw LWC metadata scan targeting *.js-meta.xml files.\n"
            "Reading these XML files directly would flood the context window with dense\n"
            "declarative markup (<targetConfigs>, property definitions, etc.).\n\n"
            "Use the MCP tool instead:\n"
            "  get_lwc_targets()                          — all components\n"
            "  get_lwc_targets(filter_exposed_only=true)  — only Experience Cloud-exposed ones\n\n"
            "This returns a 3-line summary per component "
            "(component name, isExposed boolean, clean target list) "
            "instead of raw XML. Token savings: ~50x.\n"
        )
        sys.exit(2)

    # --- Declarative metadata folder pipeline check ---
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
