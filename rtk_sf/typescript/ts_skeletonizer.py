"""
ts_skeletonizer.py — TypeScript/JavaScript structural skeleton extractor.

Reads a .ts/.tsx/.js/.jsx file and emits only the structural skeleton:
  - import statements
  - interface / type alias / enum bodies (kept in full — pure type info)
  - class declarations: constructor body shown, other method signatures only
  - export function/const arrow: signature shown, body collapsed
  - JSDoc comments attached to each declaration

Implementation bodies are replaced with { /* logic hidden */ }.

Algorithm:
  Two-pass approach using _find_closing_brace for reliable body extraction:
  1. Scan for top-level block openers (lines ending with '{')
  2. Extract full block, classify, render appropriately

Heuristic: A '{' that opens a block is always the LAST significant character
on its line in TypeScript (e.g. `class Foo {`, `function bar() {`).
Inline '{' like `import { X }`, `opts = {}`, `Record<string, T>` are skipped
by the line-ending check.

Token impact: a 300-line TypeScript service → ~60-token skeleton.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Reliable brace-matching on raw string
# ---------------------------------------------------------------------------

def _find_closing_brace(source: str, open_pos: int) -> int:
    """
    Return the index just after the '}' that closes the '{' at open_pos.
    Handles strings, template literals, and comments correctly.
    """
    assert source[open_pos] == "{"
    depth = 1
    i = open_pos + 1
    n = len(source)
    in_str: str | None = None

    while i < n and depth > 0:
        ch = source[i]
        if in_str:
            if ch == "\\" and in_str != "`":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in ('"', "'", "`"):
            in_str = ch
        elif source[i : i + 2] == "//":
            eol = source.find("\n", i + 2)
            i = eol if eol != -1 else n
            continue
        elif source[i : i + 2] == "/*":
            end = source.find("*/", i + 2)
            i = (end + 2) if end != -1 else n
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return i


def _line_ends_with_block_brace(line: str) -> bool:
    """True if the line's last significant character is '{'."""
    # Strip inline comments and trailing whitespace
    stripped = re.sub(r"//.*$", "", line).rstrip()
    return stripped.endswith("{")


# ---------------------------------------------------------------------------
# Classification regexes
# ---------------------------------------------------------------------------

_IMPORT_RE       = re.compile(r"^import\b", re.MULTILINE)
_INTERFACE_RE    = re.compile(r"\b(?:interface|enum)\s+\w+")
_TYPE_ALIAS_OBJ  = re.compile(r"\btype\s+\w[\w<>, ]*\s*=\s*\{")
_CLASS_NAME_RE   = re.compile(r"\bclass\s+(\w+)")
_FUNCTION_NAME_RE = re.compile(
    r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*[\(<]"
)
_ARROW_NAME_RE   = re.compile(r"(?:export\s+)(?:const|let|var)\s+(\w+)")
_CONSTRUCTOR_RE  = re.compile(r"\bconstructor\s*\(")
_METHOD_SIG_RE   = re.compile(
    r"^\s*(?:(?:public|private|protected|static|async|abstract|override|readonly)\s+)*"
    r"(?:(?:get|set)\s+)?(\w+)\s*[\(<]",
    re.MULTILINE,
)


def _strip_body(header: str) -> str:
    """Return the header with its opening '{' and body replaced by '{ /* logic hidden */ }'."""
    return re.sub(r"\{\s*$", "{ /* logic hidden */ }", header.rstrip()) + "}"


# ---------------------------------------------------------------------------
# Class body processor
# ---------------------------------------------------------------------------

def _process_class_body(body: str, focus_set: set[str]) -> list[str]:
    """
    Walk a class body source string.
    Returns lines to emit for the class interior.
    """
    out: list[str] = []
    lines = body.splitlines()
    i = 0
    n = len(lines)
    sig_buf: list[str] = []   # accumulate multi-line method signature
    jsdoc_buf: list[str] = [] # JSDoc lines before the method
    in_jsdoc = False

    while i < n:
        raw = lines[i]
        stripped = raw.strip()

        # ── JSDoc accumulation ──────────────────────────────────────────
        if stripped.startswith("/**"):
            jsdoc_buf = [raw]
            # Single-line /** ... */ closes immediately on the same line
            in_jsdoc = not stripped.endswith("*/")
            i += 1
            continue
        if in_jsdoc:
            jsdoc_buf.append(raw)
            if stripped.endswith("*/"):
                in_jsdoc = False
            i += 1
            continue

        # ── Block-opening line (method body opener) ─────────────────────
        if _line_ends_with_block_brace(raw) and "{" in raw:
            sig_buf.append(stripped)
            full_sig = " ".join(sig_buf)
            sig_buf = []

            # Emit JSDoc
            for jl in jsdoc_buf:
                out.append("  " + jl.strip())
            jsdoc_buf = []

            is_ctor = bool(_CONSTRUCTOR_RE.search(full_sig))
            m = _METHOD_SIG_RE.match(full_sig)
            method_name = m.group(1) if m else ""

            # Reconstruct the body to find the closing brace
            # (We need the body string starting at the { on this line)
            # Since we only have lines, use the line index to find the { position
            # in the original body string.
            brace_in_line = raw.rstrip().rfind("{")
            # Remaining body: from this line onward
            body_start_offset = sum(len(l) + 1 for l in lines[:i]) + brace_in_line

            if is_ctor or method_name in focus_set:
                # Emit method verbatim until its closing brace
                out.append("  " + full_sig)
                i += 1
                brace_depth = 1
                while i < n and brace_depth > 0:
                    ml = lines[i]
                    # Simplified brace count (good enough for method bodies)
                    temp = re.sub(r'"[^"\\]*"', '""', ml)
                    temp = re.sub(r"'[^'\\]*'", "''", temp)
                    brace_depth += temp.count("{") - temp.count("}")
                    out.append("  " + ml.strip() if ml.strip() else "")
                    i += 1
                out.append("")
            else:
                sig_no_brace = re.sub(r"\s*\{\s*$", "", full_sig).strip()
                out.append(f"  {sig_no_brace} {{ /* logic hidden */ }}")
                out.append("")
                # Skip the method body
                i += 1
                brace_depth = 1
                while i < n and brace_depth > 0:
                    ml = lines[i]
                    temp = re.sub(r'"[^"\\]*"', '""', ml)
                    temp = re.sub(r"'[^'\\]*'", "''", temp)
                    brace_depth += temp.count("{") - temp.count("}")
                    i += 1
            continue

        # ── Property declaration or decorator ───────────────────────────
        if stripped:
            if stripped.endswith(";") or stripped.startswith("@"):
                # Flush any jsdoc
                for jl in jsdoc_buf:
                    out.append("  " + jl.strip())
                jsdoc_buf = []
                # Property — emit directly, or accumulate for multi-line sig
                if sig_buf:
                    sig_buf.append(stripped)
                else:
                    out.append("  " + stripped)
            else:
                # Part of a multi-line method signature
                sig_buf.append(stripped)
        i += 1

    return out


# ---------------------------------------------------------------------------
# Main skeletonizer
# ---------------------------------------------------------------------------

def skeletonize(source: str, focus_names: list[str] | None = None) -> str:
    """
    Parse TypeScript/JavaScript source and return its structural skeleton.

    Args:
        source: Raw TypeScript/JavaScript source.
        focus_names: Function/method names whose full bodies are shown.

    Returns:
        Multi-line skeleton string.
    """
    focus_set: set[str] = set(focus_names) if focus_names else set()
    lines = source.splitlines()
    out: list[str] = []

    # ── Pass 1: collect import lines ─────────────────────────────────────
    import_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("import "):
            import_lines.append(stripped)
        elif stripped.startswith("//") or not stripped:
            continue
        else:
            break  # imports are always at the top

    out.extend(import_lines)
    if import_lines:
        out.append("")

    # ── Pass 2: walk top-level declarations ──────────────────────────────
    # Build line-start offsets for quick source position lookup
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1  # +1 for \n

    # Track which source positions we've already emitted
    processed_up_to = 0
    pending_jsdoc: list[str] = []
    sig_lines: list[str] = []    # accumulate multi-line declaration before {

    for line_idx, line in enumerate(lines):
        stripped = line.strip()
        line_start = offsets[line_idx]

        # Skip already-processed content
        if line_start < processed_up_to:
            continue

        # ── Import lines: already handled ─────────────────────────────
        if stripped.startswith("import "):
            processed_up_to = line_start + len(line) + 1
            sig_lines = []
            continue

        # ── JSDoc accumulation ─────────────────────────────────────────
        if stripped.startswith("/**"):
            pending_jsdoc = [stripped]
            processed_up_to = line_start + len(line) + 1
            continue
        if pending_jsdoc:
            if stripped.startswith("*") or stripped.startswith("*/"):
                pending_jsdoc.append(stripped)
                processed_up_to = line_start + len(line) + 1
                if stripped.endswith("*/"):
                    pass  # will flush when we find the declaration
                continue

        # ── Block-opening line ─────────────────────────────────────────
        if _line_ends_with_block_brace(line) and "{" in line:
            sig_lines.append(stripped)
            full_decl = " ".join(sig_lines)
            sig_lines = []

            # Find the { in the source
            brace_offset = line_start + line.rstrip().rfind("{")
            block_end = _find_closing_brace(source, brace_offset)
            body_content = source[brace_offset + 1 : block_end - 1]

            # ── Interface / Enum / Type-alias object ────────────────
            if _INTERFACE_RE.search(full_decl) or _TYPE_ALIAS_OBJ.search(full_decl):
                if pending_jsdoc:
                    out.extend(pending_jsdoc)
                    pending_jsdoc = []
                block_text = source[line_start : block_end].rstrip()
                out.append(block_text)
                out.append("")
                processed_up_to = block_end
                continue

            # ── Class ───────────────────────────────────────────────
            class_m = _CLASS_NAME_RE.search(full_decl)
            if class_m:
                if pending_jsdoc:
                    out.extend(pending_jsdoc)
                    pending_jsdoc = []
                header_line = re.sub(r"\s*\{\s*$", "", full_decl).strip()
                out.append(f"{header_line} {{")
                out.extend(_process_class_body(body_content, focus_set))
                out.append("}")
                out.append("")
                processed_up_to = block_end
                continue

            # ── Standalone function ──────────────────────────────────
            fn_m = _FUNCTION_NAME_RE.search(full_decl)
            arr_m = _ARROW_NAME_RE.search(full_decl)
            if fn_m or arr_m:
                name = (fn_m or arr_m).group(1)  # type: ignore[union-attr]
                if pending_jsdoc:
                    out.extend(pending_jsdoc)
                    pending_jsdoc = []
                sig = re.sub(r"\s*\{\s*$", "", full_decl).strip()
                if name in focus_set:
                    block_text = source[line_start : block_end].rstrip()
                    out.append(block_text)
                else:
                    out.append(f"{sig} {{ /* logic hidden */ }}")
                out.append("")
                processed_up_to = block_end
                continue

            # ── Unknown top-level block ──────────────────────────────
            pending_jsdoc = []
            processed_up_to = block_end
            continue

        # ── Non-block line at top level ────────────────────────────────
        # Could be: type alias (type X = '...' | '...'), const without body,
        # decorator, or part of a multi-line declaration header.

        # Clear jsdoc if we hit a non-declaration line
        if stripped and not stripped.startswith("//") and not stripped.startswith("*"):
            if stripped.endswith(";"):
                # Standalone statement (type alias, const, etc.)
                if pending_jsdoc:
                    out.extend(pending_jsdoc)
                    pending_jsdoc = []
                if sig_lines:
                    # Multi-line declaration without a block body (e.g. expression arrow fn)
                    sig_lines.append(stripped)
                    out.append(" ".join(sig_lines))
                    sig_lines = []
                else:
                    out.append(stripped)
                out.append("")
            elif stripped.startswith("export") or sig_lines:
                sig_lines.append(stripped)
            else:
                pending_jsdoc = []

        processed_up_to = line_start + len(line) + 1

    return "\n".join(out).rstrip()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def skeletonize_file(
    file_path: str | Path,
    focus_names: list[str] | None = None,
) -> str:
    """Read a .ts/.tsx/.js/.jsx file and return its skeleton."""
    path = Path(file_path)
    if not path.exists():
        return f"// File not found: {file_path}"
    if path.suffix.lower() not in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        return f"// Not a TypeScript/JavaScript file: {file_path}"

    source = path.read_text(encoding="utf-8", errors="replace")
    skeleton = skeletonize(source, focus_names)

    token_raw  = len(source) // 4
    token_skel = len(skeleton) // 4
    saved_pct  = 100 - round(token_skel / max(token_raw, 1) * 100)
    header = (
        f"// TypeScript skeleton: {path.name}\n"
        f"// Tokens: ~{token_skel} (vs ~{token_raw} raw, {saved_pct}% saved)\n\n"
    )
    return header + skeleton
