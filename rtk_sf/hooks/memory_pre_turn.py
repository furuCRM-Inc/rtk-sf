#!/usr/bin/env python3
"""
UserPromptSubmit hook — injects a short living-memory digest before the turn.

The design called this a "pre-turn hook"; Claude Code's actual pre-turn event is
UserPromptSubmit. This hook adds context rather than rewriting the prompt, so it
composes with `compact_prompt`, which rewrites it.

Claude Code hook protocol:
  stdin : JSON {"prompt": "...", "session_id": "...", "cwd": "..."}
  stdout: JSON {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                       "additionalContext": "..."}}
  exit 0: allow (additionalContext, when present, is appended to the context)

Budget: MAX_CHARS caps the injection at roughly 150 tokens (~4 chars/token).
Never raises: a memory failure must not block the user's turn.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ~150 tokens at ~4 characters per token.
MAX_CHARS = 600


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        sys.exit(0)

    root = Path(data.get("cwd") or ".").resolve()

    try:
        from rtk_sf.hooks._log import append
        from rtk_sf.memory import HistoryManager

        manager = HistoryManager(root)
        if not manager.path.exists():
            append("memory_pre", "skip:no_history", project=root.name)
            sys.exit(0)

        digest = manager.recent_digest(max_chars=MAX_CHARS)
        if not digest:
            append("memory_pre", "skip:empty")
            sys.exit(0)

        append("memory_pre", "ok", chars=len(digest))
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": digest,
                    }
                }
            )
        )
    except Exception as exc:
        try:
            from rtk_sf.hooks._log import append

            append("memory_pre", "error", error=str(exc)[:200])
        except Exception:
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
