"""
core_router.py — Language router for rtk-sf.

Detects whether a file path or CLI command belongs to the Salesforce track
or the Python track, and dispatches to the appropriate sub-module.

Detection rules (in priority order):
  Salesforce track: .cls, .trigger, .xml, .cmp, .app, .page, sf/sfdx keywords
  Python track    : .py, pytest/uv/ruff/mypy keywords
  TypeScript track: .ts, .tsx, .js, .jsx, .mjs, .cjs, jest/vitest keywords
  Kotlin track    : .kt, .kts, gradlew/gradle keywords

Usage:
    from rtk_sf.core_router import route_file, Track

    track = route_file("src/mymodule.py")    # → Track.PYTHON
    track = route_file("classes/Foo.cls")    # → Track.SALESFORCE
    track = route_file("Service.kt")         # → Track.KOTLIN
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path


class Track(str, Enum):
    SALESFORCE = "salesforce"
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    KOTLIN = "kotlin"
    UNKNOWN = "unknown"


_SF_EXTENSIONS = {
    ".cls", ".trigger", ".cmp", ".app", ".page", ".component",
}
_SF_XML_SUFFIXES = (
    "-meta.xml", ".object-meta.xml", ".field-meta.xml",
    ".flow-meta.xml", ".permissionset-meta.xml",
)
_SF_CLI_KEYWORDS = re.compile(
    r"\b(sf|sfdx)\s+(project|org|data|apex|deploy|retrieve|sobject|limits|force)\b",
    re.IGNORECASE,
)

_PY_EXTENSIONS = {".py", ".pyw", ".pyi", ".ipynb"}
_PY_CLI_KEYWORDS = re.compile(
    r"\b(pytest|python3?|uv\s+run|ruff|mypy|pip)\b",
    re.IGNORECASE,
)

_TS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_TS_CLI_KEYWORDS = re.compile(
    r"\b(jest|vitest|playwright|npm\s+(?:test|run)|npx|tsc|eslint|prettier)\b",
    re.IGNORECASE,
)

_KT_EXTENSIONS = {".kt", ".kts"}
_KT_CLI_KEYWORDS = re.compile(
    r"\b(gradlew?|kotlinc|kotlin\s+|\.\/gradlew)\b",
    re.IGNORECASE,
)


def route_file(file_path: str | Path) -> Track:
    """Return the Track for a given file path based on its extension."""
    path = Path(file_path)
    suffix = path.suffix.lower()
    name_lower = path.name.lower()

    if suffix in _SF_EXTENSIONS:
        return Track.SALESFORCE
    if any(name_lower.endswith(s) for s in _SF_XML_SUFFIXES):
        return Track.SALESFORCE
    if suffix in _PY_EXTENSIONS:
        return Track.PYTHON
    if suffix in _TS_EXTENSIONS:
        return Track.TYPESCRIPT
    if suffix in _KT_EXTENSIONS:
        return Track.KOTLIN

    return Track.UNKNOWN


def route_command(command: str) -> Track:
    """Return the Track for a CLI command string."""
    if _SF_CLI_KEYWORDS.search(command):
        return Track.SALESFORCE
    if _PY_CLI_KEYWORDS.search(command):
        return Track.PYTHON
    if _TS_CLI_KEYWORDS.search(command):
        return Track.TYPESCRIPT
    if _KT_CLI_KEYWORDS.search(command):
        return Track.KOTLIN
    return Track.UNKNOWN


def route(file_path: str | Path | None = None, command: str | None = None) -> Track:
    """
    Determine the processing track from a file path or command.

    File path takes priority; command is used as a fallback.
    """
    if file_path is not None:
        track = route_file(file_path)
        if track != Track.UNKNOWN:
            return track
    if command is not None:
        return route_command(command)
    return Track.UNKNOWN
