"""
kt_skeletonizer.py — Kotlin structural skeleton extractor.

Reads a .kt/.kts file and emits only the structural skeleton:
  - package declaration
  - import statements
  - interface / enum class bodies (kept in full — pure type info)
  - data class primary constructors (kept in full)
  - class / object / sealed class: constructor shown, other method bodies collapsed
  - top-level function signatures, bodies collapsed
  - KDoc comments attached to each declaration

Implementation bodies are replaced with { /* logic hidden */ }.

Algorithm:
  Two-pass approach using _find_closing_brace for reliable body extraction:
  1. Scan for top-level block openers (lines ending with '{')
  2. Extract full block, classify, render appropriately

Token impact: a 400-line Kotlin service → ~80-token skeleton.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Reliable brace-matching on raw string
# ---------------------------------------------------------------------------

def _find_closing_brace(source: str, open_pos: int) -> int:
    """Return the index just after the '}' that closes the '{' at open_pos."""
    assert source[open_pos] == "{"
    depth = 1
    i = open_pos + 1
    n = len(source)
    in_str: str | None = None

    while i < n and depth > 0:
        # Kotlin triple-quoted raw strings
        if not in_str and source[i : i + 3] in ('"""', "'''"):
            quote = source[i : i + 3]
            i += 3
            while i < n:
                if source[i : i + 3] == quote:
                    i += 3
                    break
                i += 1
            continue

        ch = source[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in ('"', "'"):
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
    stripped = re.sub(r"//.*$", "", line).rstrip()
    return stripped.endswith("{")


def _parens_balanced(s: str) -> bool:
    """True if '(' and ')' are balanced in s (ignores string contents)."""
    depth = 0
    in_str: str | None = None
    for ch in s:
        if in_str:
            if ch == in_str:
                in_str = None
        elif ch in ('"', "'"):
            in_str = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    return depth == 0


# ---------------------------------------------------------------------------
# Classification regexes
# ---------------------------------------------------------------------------

_PACKAGE_RE      = re.compile(r"^package\s+\S+")
_INTERFACE_RE    = re.compile(
    r"\b(?:interface|enum\s+class|annotation\s+class)\s+\w+"
)
_DATA_CLASS_RE   = re.compile(r"\bdata\s+class\s+\w+")
_CLASS_RE        = re.compile(
    r"\b(?:(?:data|sealed|abstract|open|inner|value|inline|external)\s+)*"
    r"(?:class|object|interface)\s+\w+"
)
_COMPANION_RE    = re.compile(r"\bcompanion\s+object\b")
_FUN_RE          = re.compile(
    r"(?:^|\s)(?:(?:public|private|protected|internal|override|open|abstract"
    r"|suspend|inline|infix|operator|tailrec|external|actual|expect)\s+)*"
    r"fun\s+(?:<[^>]*>\s*)?\w+"
)
_PROPERTY_RE     = re.compile(
    r"^(?:(?:public|private|protected|internal|override|open|abstract"
    r"|lateinit|const|inline)\s+)*(?:val|var)\s+\w+"
)
_CTOR_RE         = re.compile(
    r"^\s*(?:(?:public|private|protected|internal)\s+)?constructor\s*\("
)
_INIT_RE         = re.compile(r"^\s*init\s*\{")
# fun ... = (expression body opener — '=' is last non-space character on line)
_EXPR_BODY_RE    = re.compile(r"\bfun\b.*=\s*$")
# Single-line class declaration (balanced parens, ends with ')')
_SINGLE_CLASS_RE = re.compile(
    r"^(?:(?:data|sealed|abstract|open|inner|value|inline|external)\s+)*"
    r"(?:class|object|interface)\s+\w+"
)


# ---------------------------------------------------------------------------
# Class body processor
# ---------------------------------------------------------------------------

def _process_class_body(body: str, focus_set: set[str], indent: str = "  ") -> list[str]:
    """Walk a class body, collapse method bodies, show constructor/init verbatim."""
    out: list[str] = []
    lines = body.splitlines()
    i = 0
    n = len(lines)
    jsdoc_buf: list[str] = []
    sig_buf: list[str] = []
    in_jsdoc = False

    def _flush_sig(name_override: str = "") -> None:
        nonlocal jsdoc_buf, sig_buf
        if not sig_buf:
            return
        sig = " ".join(sig_buf)
        fn_m = re.search(r"\bfun\s+(?:<[^>]*>\s*)?(\w+)", sig)
        name = name_override or (fn_m.group(1) if fn_m else "")
        for jl in jsdoc_buf:
            out.append(indent + jl.strip())
        jsdoc_buf = []
        sig_clean = re.sub(r"\s*=\s*$", "", sig).strip()
        if name in focus_set:
            out.append(f"{indent}{sig_clean}")
        else:
            out.append(f"{indent}{sig_clean} {{ /* logic hidden */ }}")
        out.append("")
        sig_buf = []

    while i < n:
        raw = lines[i]
        stripped = raw.strip()

        # ── KDoc ─────────────────────────────────────────────────────────
        if stripped.startswith("/**"):
            _flush_sig()
            jsdoc_buf = [raw]
            in_jsdoc = not stripped.endswith("*/")
            i += 1
            continue
        if in_jsdoc:
            jsdoc_buf.append(raw)
            if stripped.endswith("*/"):
                in_jsdoc = False
            i += 1
            continue

        # ── Block-opening line ────────────────────────────────────────────
        if _line_ends_with_block_brace(raw) and "{" in raw:
            sig_buf.append(stripped)
            full_sig = " ".join(sig_buf)
            sig_buf = []

            for jl in jsdoc_buf:
                out.append(indent + jl.strip())
            jsdoc_buf = []

            fn_m = re.search(r"\bfun\s+(?:<[^>]*>\s*)?(\w+)", full_sig)
            method_name = fn_m.group(1) if fn_m else ""
            is_ctor = bool(_CTOR_RE.match(raw)) or bool(_INIT_RE.match(raw))
            is_companion = bool(_COMPANION_RE.search(full_sig))

            block_source = "\n".join(lines[i:])
            brace_pos = block_source.index("{")
            end_pos = _find_closing_brace(block_source, brace_pos)
            lines_consumed = block_source[:end_pos].count("\n")
            inner_body = block_source[brace_pos + 1 : end_pos - 1]

            if is_ctor or method_name in focus_set:
                # Show constructor / focused method in full
                out.append(indent + full_sig)
                for bl in inner_body.splitlines():
                    out.append((indent + "  " + bl.strip()) if bl.strip() else "")
                out.append(indent + "}")
                out.append("")
            elif is_companion:
                # Recurse into companion object body
                header = re.sub(r"\s*\{\s*$", "", full_sig).strip()
                out.append(f"{indent}{header} {{")
                out.extend(_process_class_body(inner_body, focus_set, indent + "  "))
                out.append(f"{indent}}}")
                out.append("")
            else:
                sig_no_brace = re.sub(r"\s*\{\s*$", "", full_sig).strip()
                out.append(f"{indent}{sig_no_brace} {{ /* logic hidden */ }}")
                out.append("")

            i += lines_consumed + 1
            continue

        # ── Property declaration (val/var) ───────────────────────────────
        if stripped and _PROPERTY_RE.match(stripped):
            _flush_sig()
            for jl in jsdoc_buf:
                out.append(indent + jl.strip())
            jsdoc_buf = []
            out.append(indent + stripped)
            i += 1
            continue

        # ── Other non-block lines ─────────────────────────────────────────
        if stripped and not stripped.startswith("//") and not stripped.startswith("*"):
            if stripped.startswith("@"):
                _flush_sig()
                for jl in jsdoc_buf:
                    out.append(indent + jl.strip())
                jsdoc_buf = []
                out.append(indent + stripped)
            else:
                sig_buf.append(stripped)
                joined = " ".join(sig_buf)

                # Single-line expression-body fun: `fun f(...) = expr` (complete on one line)
                # Detect: has 'fun', has ')' then '= <content>' (not just '=' at line end)
                has_fun = bool(re.search(r"\bfun\s+", joined))
                has_inline_expr = bool(re.search(r"\bfun\b.*?\).*?=\s*\S", joined))
                if has_fun and has_inline_expr and not joined.rstrip().endswith(("{", "(", ",")):
                    fn_m = re.search(r"\bfun\s+(?:<[^>]*>\s*)?(\w+)", joined)
                    name = fn_m.group(1) if fn_m else ""
                    for jl in jsdoc_buf:
                        out.append(indent + jl.strip())
                    jsdoc_buf = []
                    # Strip everything after '= ' to get just the signature
                    sig_clean = re.sub(r"\s*=\s*\S.*$", "", joined).strip()
                    if name in focus_set:
                        out.append(f"{indent}{joined}")  # show full line
                    else:
                        out.append(f"{indent}{sig_clean} {{ /* logic hidden */ }}")
                    out.append("")
                    sig_buf = []
                elif _EXPR_BODY_RE.search(joined):
                    # Expression-body function: '=' at end, body on next line
                    fn_m = re.search(r"\bfun\s+(?:<[^>]*>\s*)?(\w+)", joined)
                    name = fn_m.group(1) if fn_m else ""
                    for jl in jsdoc_buf:
                        out.append(indent + jl.strip())
                    jsdoc_buf = []
                    sig_clean = re.sub(r"\s*=\s*$", "", joined).strip()
                    if name in focus_set:
                        out.append(f"{indent}{sig_clean}")
                    else:
                        out.append(f"{indent}{sig_clean} {{ /* logic hidden */ }}")
                    out.append("")
                    sig_buf = []
                    # Skip next non-blank line (expression body)
                    i += 1
                    while i < n and not lines[i].strip():
                        i += 1
                    if i < n:
                        i += 1  # skip the expression body line
                    continue
        i += 1

    _flush_sig()
    return out


# ---------------------------------------------------------------------------
# Main skeletonizer
# ---------------------------------------------------------------------------

def skeletonize(source: str, focus_names: list[str] | None = None) -> str:
    """
    Parse Kotlin source and return its structural skeleton.

    Args:
        source: Raw Kotlin source text.
        focus_names: Function/method names whose full bodies are shown.

    Returns:
        Multi-line skeleton string.
    """
    focus_set: set[str] = set(focus_names) if focus_names else set()
    lines = source.splitlines()
    out: list[str] = []

    # ── Pass 1: package + imports ─────────────────────────────────────────
    package_line: str | None = None
    import_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if _PACKAGE_RE.match(stripped):
            package_line = stripped
        elif stripped.startswith("import "):
            import_lines.append(stripped)
        elif stripped.startswith("//") or not stripped:
            continue
        else:
            break

    if package_line:
        out.append(package_line)
        out.append("")
    out.extend(import_lines)
    if import_lines:
        out.append("")

    # ── Pass 2: build line-start offsets ─────────────────────────────────
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1

    processed_up_to = 0
    pending_jsdoc: list[str] = []
    sig_lines: list[str] = []
    in_jsdoc = False
    skip_next_nonempty = False  # skip one expression-body line

    for line_idx, line in enumerate(lines):
        stripped = line.strip()
        line_start = offsets[line_idx]

        if line_start < processed_up_to:
            continue

        # Skip package / imports
        if _PACKAGE_RE.match(stripped) or stripped.startswith("import "):
            processed_up_to = line_start + len(line) + 1
            sig_lines = []
            continue

        # ── KDoc ──────────────────────────────────────────────────────
        if stripped.startswith("/**"):
            pending_jsdoc = [stripped]
            in_jsdoc = not stripped.endswith("*/")
            processed_up_to = line_start + len(line) + 1
            continue
        if in_jsdoc:
            if stripped.startswith("*") or stripped.startswith("*/"):
                pending_jsdoc.append(stripped)
                if stripped.endswith("*/"):
                    in_jsdoc = False
                processed_up_to = line_start + len(line) + 1
                continue

        # ── Expression-body skip ──────────────────────────────────────
        if skip_next_nonempty and stripped:
            skip_next_nonempty = False
            processed_up_to = line_start + len(line) + 1
            continue

        # ── Top-level annotation (no body) ───────────────────────────
        if stripped.startswith("@") and not _line_ends_with_block_brace(line):
            sig_lines.append(stripped)
            processed_up_to = line_start + len(line) + 1
            continue

        # ── Block-opening line ─────────────────────────────────────────
        if _line_ends_with_block_brace(line) and "{" in line:
            sig_lines.append(stripped)
            full_decl = " ".join(sig_lines)
            sig_lines = []

            brace_offset = line_start + line.rstrip().rfind("{")
            block_end = _find_closing_brace(source, brace_offset)
            body_content = source[brace_offset + 1 : block_end - 1]

            # ── Interface / enum class — show in full ───────────────
            if _INTERFACE_RE.search(full_decl):
                if pending_jsdoc:
                    out.extend(pending_jsdoc)
                    pending_jsdoc = []
                block_text = source[line_start : block_end].rstrip()
                out.append(block_text)
                out.append("")
                processed_up_to = block_end
                continue

            # ── Data class — show header, process body ──────────────
            if _DATA_CLASS_RE.search(full_decl):
                if pending_jsdoc:
                    out.extend(pending_jsdoc)
                    pending_jsdoc = []
                header = re.sub(r"\s*\{\s*$", "", full_decl).strip()
                body_lines = _process_class_body(body_content, focus_set)
                if any(l.strip() for l in body_lines):
                    out.append(f"{header} {{")
                    out.extend(body_lines)
                    out.append("}")
                else:
                    out.append(f"{header} {{ /* logic hidden */ }}")
                out.append("")
                processed_up_to = block_end
                continue

            # ── Class / object / sealed class ────────────────────────
            class_m = _CLASS_RE.search(full_decl)
            if class_m or _COMPANION_RE.search(full_decl):
                if pending_jsdoc:
                    out.extend(pending_jsdoc)
                    pending_jsdoc = []
                header = re.sub(r"\s*\{\s*$", "", full_decl).strip()
                out.append(f"{header} {{")
                out.extend(_process_class_body(body_content, focus_set))
                out.append("}")
                out.append("")
                processed_up_to = block_end
                continue

            # ── Top-level function with block body ────────────────────
            fn_m = _FUN_RE.search(full_decl)
            if fn_m:
                name_m = re.search(r"\bfun\s+(?:<[^>]*>\s*)?(\w+)", full_decl)
                name = name_m.group(1) if name_m else ""
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

            # ── Unknown top-level block ───────────────────────────────
            pending_jsdoc = []
            processed_up_to = block_end
            continue

        # ── Non-block line ─────────────────────────────────────────────
        if stripped and not stripped.startswith("//") and not stripped.startswith("*"):

            # Single-line class/object declaration (no body block)
            if (_SINGLE_CLASS_RE.match(stripped)
                    and stripped.endswith(")")
                    and _parens_balanced(stripped)):
                if sig_lines:
                    # Flush any preceding annotations/KDoc from sig_lines
                    pending_jsdoc = [l for l in sig_lines if l.startswith("@")] + pending_jsdoc
                    sig_lines = [l for l in sig_lines if not l.startswith("@")]
                if pending_jsdoc:
                    out.extend(pending_jsdoc)
                    pending_jsdoc = []
                out.append(stripped)
                out.append("")
                sig_lines = []
                processed_up_to = line_start + len(line) + 1
                continue

            # Closing paren of multi-line declaration (data class, fun params)
            if stripped == ")" or (stripped.startswith(")") and not stripped.startswith("){")):
                if sig_lines:
                    sig_lines.append(stripped)
                    if pending_jsdoc:
                        out.extend(pending_jsdoc)
                        pending_jsdoc = []
                    out.append(" ".join(sig_lines))
                    out.append("")
                    sig_lines = []
                processed_up_to = line_start + len(line) + 1
                continue

            # Top-level property (val/var) — emit directly when not inside a multi-line sig
            if _PROPERTY_RE.match(stripped) and not sig_lines:
                if pending_jsdoc:
                    out.extend(pending_jsdoc)
                    pending_jsdoc = []
                out.append(stripped)
                out.append("")
                processed_up_to = line_start + len(line) + 1
                continue

            # Typealias — single-line
            if stripped.startswith("typealias "):
                if pending_jsdoc:
                    out.extend(pending_jsdoc)
                    pending_jsdoc = []
                out.append(stripped)
                out.append("")
                processed_up_to = line_start + len(line) + 1
                continue

            # Expression-body function (fun f() = ) or multi-line decl continuation
            if (re.match(r"^(?:public|private|protected|internal|override|open|abstract"
                         r"|suspend|inline|infix|operator|tailrec|fun|class|object"
                         r"|sealed|data|enum|interface|companion|annotation|@)\b", stripped)
                    or sig_lines):
                sig_lines.append(stripped)
                joined = " ".join(sig_lines)
                if _EXPR_BODY_RE.search(joined):
                    # Flush as collapsed expression-body function
                    name_m = re.search(r"\bfun\s+(?:<[^>]*>\s*)?(\w+)", joined)
                    name = name_m.group(1) if name_m else ""
                    if pending_jsdoc:
                        out.extend(pending_jsdoc)
                        pending_jsdoc = []
                    sig_clean = re.sub(r"\s*=\s*$", "", joined).strip()
                    if name in focus_set:
                        out.append(sig_clean)
                    else:
                        out.append(f"{sig_clean} {{ /* logic hidden */ }}")
                    out.append("")
                    sig_lines = []
                    skip_next_nonempty = True
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
    """Read a .kt/.kts file and return its skeleton."""
    path = Path(file_path)
    if not path.exists():
        return f"// File not found: {file_path}"
    if path.suffix.lower() not in {".kt", ".kts"}:
        return f"// Not a Kotlin file: {file_path}"

    source = path.read_text(encoding="utf-8", errors="replace")
    skeleton = skeletonize(source, focus_names)

    token_raw  = len(source) // 4
    token_skel = len(skeleton) // 4
    saved_pct  = 100 - round(token_skel / max(token_raw, 1) * 100)
    header = (
        f"// Kotlin skeleton: {path.name}\n"
        f"// Tokens: ~{token_skel} (vs ~{token_raw} raw, {saved_pct}% saved)\n\n"
    )
    return header + skeleton
