#!/usr/bin/env python3
"""
PreToolUse[Read] hook — intercepts Read calls on image files and runs local OCR.

Claude Code hook protocol (PreToolUse):
  stdin : JSON {"tool_name": "Read", "tool_input": {"file_path": "..."}, ...}
  allow : exit 0 with no output
  block : exit 0 with a JSON permissionDecision of "deny" on stdout; the
          `permissionDecisionReason` is what Claude receives

Blocking with exit 2 and the text on stdout does NOT work: on exit 2 Claude Code
takes the message from stderr, so stdout is discarded and the user sees only
"hook error: No stderr output". The structured JSON form is used instead.

Anything the hook writes to stderr is reported as a hook *error* and the Read
fails, so the OCR engines must not be allowed to speak. PaddleOCR, torch and
their dependencies emit model-loading notices and UserWarnings on stderr — and
some of it from C++, which `contextlib.redirect_stderr` cannot catch. The OCR
call therefore runs with file descriptors 1 and 2 pointed at /dev/null, and only
this module writes the protocol output, afterwards.

NOTE: inline pasted images bypass this hook entirely because Claude uses native
vision without calling Read. The CLAUDE.md guidance instructs Claude to ask for
a file path in that case.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}


@contextlib.contextmanager
def _silenced():
    """
    Redirect this process's stdout and stderr to /dev/null.

    Applied at the file-descriptor level so output from native extensions is
    caught too, not just writes through Python's sys.stdout/sys.stderr.
    """
    saved_out, saved_err = os.dup(1), os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(devnull)
        os.close(saved_out)
        os.close(saved_err)


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        sys.exit(0)

    file_path = data.get("tool_input", {}).get("file_path", "")
    if not file_path or Path(file_path).suffix.lower() not in IMAGE_EXTS:
        sys.exit(0)

    try:
        from rtk_sf.hooks._log import append as _log
    except Exception:
        _log = None  # type: ignore[assignment]

    text = None
    error = None
    try:
        # Import inside the guard as well: loading the OCR stack is itself noisy.
        with _silenced():
            from rtk_sf.vision_ocr import extract_image_text

            text = extract_image_text(str(file_path))
    except Exception as exc:  # OCR unavailable, model missing, unreadable file …
        error = exc

    if error is not None or not text:
        if _log:
            _log("ocr", "error", path=file_path, err=str(error or "empty result"))
        # Fall through to the native Read. Nothing is written to stderr, which
        # would otherwise surface as a hook failure and block the Read entirely.
        sys.exit(0)

    if _log:
        _log("ocr", "ok", path=file_path, chars=len(text))
    reason = (
        f"[rtk-sf OCR] Read intercepted — local OCR ran instead "
        f"(saves ~1,500 vision tokens).\n\n"
        f"File: {file_path}\n\n"
        f"{text}"
    )
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
