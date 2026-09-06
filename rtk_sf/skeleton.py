"""
skeleton.py — Context-aware Apex class skeleton generator.

Produces a surgical view of an Apex class: class-level variables and
constructor signatures are preserved in full; unrelated method bodies are
collapsed to a single-line placeholder. The caller specifies which method(s)
to keep intact via focus_methods.

Token impact: a 10,000-token class becomes ~300 tokens of context-perfect
scaffold that Claude can reason about without hallucinating scope.

Usage (via MCP tool get_class_skeleton):
    skeleton = build_skeleton(source, focus_methods=["saveRecord", "validate"])
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# Matches an opening brace that starts a block (balanced-brace scanner follows)
_BRACE_OPEN = re.compile(r"\{")

# Matches Apex method/constructor signature line — captures access/annotation
# modifier(s), return type, name, and parameter list.
_METHOD_SIG = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?P<mods>(?:(?:@\w+(?:\([^)]*\))?\s+)*)"
    r"(?:(?:public|global|private|protected|override|virtual|abstract|static|"
    r"with\s+sharing|without\s+sharing|inherited\s+sharing)\s+)*)"
    r"(?P<ret>[\w<>\[\], .]+?)\s+"
    r"(?P<name>\w+)\s*"
    r"\((?P<params>[^)]*)\)\s*"
    r"(?P<body_start>\{)",
    re.MULTILINE | re.IGNORECASE,
)

# Class-level field / property / constant — lines NOT inside any method body
_FIELD_LINE = re.compile(
    r"^[ \t]*(?:(?:public|global|private|protected|static|final|"
    r"transient|with\s+sharing|without\s+sharing)\s+)+"
    r"(?!class\b)(?!interface\b)"
    r"[\w<>\[\], .]+\s+\w+",
    re.MULTILINE | re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Core: balanced-brace scanner
# ---------------------------------------------------------------------------

def _find_block_end(source: str, open_pos: int) -> int:
    """Return the index of the closing brace that matches the '{' at open_pos."""
    depth = 0
    i = open_pos
    in_str = False
    str_char = ""
    in_line_comment = False
    in_block_comment = False

    while i < len(source):
        ch = source[i]
        nch = source[i + 1] if i + 1 < len(source) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nch == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_str:
            if ch == "\\" and nch == str_char:
                i += 2
                continue
            if ch == str_char:
                in_str = False
            i += 1
            continue

        if ch == "/" and nch == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nch == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in ("'", '"'):
            in_str = True
            str_char = ch
            i += 1
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1

    return len(source) - 1


# ---------------------------------------------------------------------------
# Skeleton builder
# ---------------------------------------------------------------------------

def build_skeleton(source: str, focus_methods: list[str] | None = None) -> str:
    """
    Return a skeleton of the Apex class source.

    Preserved:
      - Class declaration line
      - Class-level variable/property declarations
      - Constructor bodies (always shown — they establish object state)
      - Bodies of methods listed in focus_methods

    Collapsed (method body replaced with placeholder):
      - All other method bodies
    """
    focus = {m.lower() for m in (focus_methods or [])}

    # Detect class name for constructor identification
    class_match = re.search(
        r"class\s+(\w+)", source, re.IGNORECASE
    )
    class_name = class_match.group(1).lower() if class_match else ""

    # Collect all method blocks: (start_of_sig, end_of_body, name, full_sig)
    blocks: list[dict[str, Any]] = []
    for m in _METHOD_SIG.finditer(source):
        brace_pos = m.end() - 1  # position of opening '{'
        end_pos = _find_block_end(source, brace_pos)
        blocks.append(
            {
                "sig_start": m.start(),
                "body_open": brace_pos,
                "body_close": end_pos,
                "name": m.group("name").lower(),
                "indent": m.group("indent"),
                "full_sig": m.group(0)[: m.end() - m.start()],
            }
        )

    if not blocks:
        return source  # No methods found — return as-is

    parts: list[str] = []
    cursor = 0

    for block in blocks:
        sig_start = block["sig_start"]
        body_open = block["body_open"]
        body_close = block["body_close"]
        name = block["name"]
        indent = block["indent"]

        # Text before this method (class header, fields, annotations)
        parts.append(source[cursor:sig_start])

        is_constructor = name == class_name
        is_focused = name in focus

        if is_constructor or is_focused:
            # Keep full method including body
            parts.append(source[sig_start : body_close + 1])
        else:
            # Keep signature, collapse body
            sig_text = source[sig_start : body_open + 1]
            parts.append(sig_text)
            parts.append(f"\n{indent}    /* Logic Hidden */\n{indent}}}")

        cursor = body_close + 1

    # Remainder (closing class brace, trailing comments)
    parts.append(source[cursor:])

    return "".join(parts)


# ---------------------------------------------------------------------------
# File-level entry point
# ---------------------------------------------------------------------------

def _resolve_source_path(file_path_str: str, project_root: Path) -> Path | None:
    """Resolve a spec file path to an existing Path.

    Handles both absolute paths (from newer indexer runs) and bare filenames
    (from older runs where only the basename was stored).
    """
    candidate = Path(file_path_str)
    if candidate.is_absolute() and candidate.exists():
        return candidate

    # Relative path — try project root first, then glob for the filename
    relative = project_root / file_path_str
    if relative.exists():
        return relative

    filename = candidate.name
    for found in project_root.rglob(filename):
        if found.is_file():
            return found

    return None


def skeleton_from_spec(
    component_name: str,
    rtk_dir: Path,
    focus_methods: list[str] | None = None,
) -> str | None:
    """
    Load the source file path from the YAML spec, then build and return the skeleton.
    Returns None if the component is not found or is not an Apex class.
    """
    import yaml

    spec_file = rtk_dir / "specs" / f"{component_name}.yaml"
    if not spec_file.exists():
        return None

    spec = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        return None

    component_type = spec.get("type", "")
    if "apex" not in component_type.lower() and "trigger" not in component_type.lower():
        return None

    file_path_str = spec.get("file", "")
    if not file_path_str:
        return None

    project_root = rtk_dir.parent
    src_path = _resolve_source_path(file_path_str, project_root)
    if src_path is None:
        return None

    source = src_path.read_text(encoding="utf-8", errors="replace")
    skeleton = build_skeleton(source, focus_methods)

    focused = focus_methods or []
    header = (
        f"// rtk-sf skeleton — {component_name}\n"
        f"// focus: {', '.join(focused) if focused else 'constructors only'}\n"
        f"// full methods shown: constructors"
        + (f" + {', '.join(focused)}" if focused else "")
        + "\n// all others: /* Logic Hidden */\n\n"
    )
    return header + skeleton
