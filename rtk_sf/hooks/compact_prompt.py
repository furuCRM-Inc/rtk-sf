#!/usr/bin/env python3
"""
UserPromptSubmit hook — auto-compacts long or bilingual (EN+JA) prompts.

Claude Code hook protocol:
  stdin : JSON {"prompt": "...", "session_id": "...", "transcript": [...]}
  stdout: JSON {"prompt": "<compacted>"} to replace the prompt
  exit 0: allow (stdout JSON replaces prompt if present, else original is used)
  exit 2: block prompt entirely (not used here)

Only fires when:
  - prompt length > 300 chars AND
  - contains Japanese characters OR length > 800 chars (long English prompt)
  - compaction achieves > 5% reduction
"""

from __future__ import annotations

import json
import sys

MIN_CHARS_EN = 300   # English-only prompts
MIN_CHARS_JA = 50    # Japanese is denser — 50 chars ≈ 150 English chars
MIN_SAVINGS_PCT = 5  # only compact if >= 5% reduction


def _has_japanese(text: str) -> bool:
    return any("　" <= c <= "鿿" for c in text)


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        sys.exit(0)

    prompt = data.get("prompt", "")
    has_ja = _has_japanese(prompt)
    threshold = MIN_CHARS_JA if has_ja else MIN_CHARS_EN

    if len(prompt) < threshold:
        from rtk_sf.hooks._log import append
        append("compact", "skip:too_short", chars=len(prompt), threshold=threshold)
        sys.exit(0)

    try:
        from rtk_sf.nlp_compactor import compact_prompt
        from rtk_sf.hooks._log import append
        compacted, orig_chars, new_chars = compact_prompt(prompt)
        savings_pct = (orig_chars - new_chars) / orig_chars * 100 if orig_chars else 0
        if savings_pct < MIN_SAVINGS_PCT:
            append("compact", "skip:no_savings", orig=orig_chars, new=new_chars, pct=round(savings_pct, 1))
            sys.exit(0)
        append("compact", "ok", orig=orig_chars, new=new_chars, pct=round(savings_pct, 1))
        sys.stdout.write(json.dumps({"prompt": compacted}))
    except Exception as exc:
        try:
            from rtk_sf.hooks._log import append
            append("compact", "error", err=str(exc))
        except Exception:
            pass
        sys.exit(0)


if __name__ == "__main__":
    main()
