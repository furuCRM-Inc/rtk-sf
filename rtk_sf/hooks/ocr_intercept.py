#!/usr/bin/env python3
"""
PreToolUse[Read] hook — intercepts Read calls on image files and runs local OCR.

Claude Code hook protocol:
  stdin : JSON {"tool_name": "Read", "tool_input": {"file_path": "..."}, ...}
  exit 0: allow the Read to proceed normally
  exit 2: block — stdout text is shown to Claude as the result instead
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        sys.exit(0)

    file_path = data.get("tool_input", {}).get("file_path", "")
    if not file_path or Path(file_path).suffix.lower() not in IMAGE_EXTS:
        sys.exit(0)

    try:
        from rtk_sf.vision_ocr import extract_image_text
        text = extract_image_text(str(file_path))
        sys.stdout.write(
            f"[rtk-sf OCR] Intercepted Read on image — ran local OCR instead "
            f"(saves ~1,500 vision tokens).\n\n"
            f"File: {file_path}\n\n"
            f"{text}\n"
        )
        sys.exit(2)
    except Exception as exc:
        sys.stdout.write(f"[rtk-sf OCR] OCR failed ({exc}), falling back to native Read.\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
