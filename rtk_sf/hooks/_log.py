"""Shared append-only log for rtk-sf hooks.

Log file: ~/.rtk-sf-hooks.log
Format (JSONL):  {"ts": "ISO8601", "hook": "compact|ocr", "status": "...", ...}
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

_LOG_PATH = Path.home() / ".rtk-sf-hooks.log"
_MAX_LINES = 500


def append(hook: str, status: str, **kwargs: object) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(), "hook": hook, "status": status, **kwargs}
    try:
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        _trim()
    except Exception:
        pass


def _trim() -> None:
    try:
        lines = _LOG_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) > _MAX_LINES:
            _LOG_PATH.write_text("".join(lines[-_MAX_LINES:]), encoding="utf-8")
    except Exception:
        pass


def tail(n: int = 20) -> list[dict]:
    try:
        lines = _LOG_PATH.read_text(encoding="utf-8").splitlines()
        return [json.loads(l) for l in lines[-n:] if l.strip()]
    except Exception:
        return []
