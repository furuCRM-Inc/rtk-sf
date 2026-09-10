"""
java_skeletonizer.py — Java structural skeleton extractor.

Reads a .java file and emits only the structural skeleton:
  - package declaration
  - import statements
  - interface / enum bodies (kept in full — pure type contracts)
  - class/abstract class declarations: field variables shown, method bodies collapsed
  - Standard getter/setter boilerplate auto-detected and collapsed
  - Javadoc comments (/** ... */) preserved on all declarations
  - Annotation (@Override, @Bean, @Entity, etc.) preserved on methods

Implementation bodies are replaced with { /* logic hidden */ }.

Algorithm:
  Two-pass approach using _find_closing_brace for reliable body extraction:
  1. Scan for top-level block openers (lines ending with '{')
  2. Extract full block, classify, render appropriately

Token impact: a 2500-line Spring Boot controller → ~150-token skeleton.
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
        ch = source[i]
        if in_str:
            if ch == "\\" and in_str != "`":
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


# ---------------------------------------------------------------------------
# Classification regexes
# ---------------------------------------------------------------------------

_PACKAGE_RE   = re.compile(r"^package\s+[\w.]+\s*;")
_IMPORT_RE    = re.compile(r"^import\s+")
_ANNOTATION_RE = re.compile(r"^@\w+")

# Types that should be shown in full (interface, enum, @interface/annotation type)
_INTERFACE_RE = re.compile(
    r"\b(?:interface|enum|@interface)\s+\w+"
)
_CLASS_RE     = re.compile(
    r"\b(?:(?:public|private|protected|static|abstract|final|sealed|non-sealed"
    r"|strictfp)\s+)*(?:class|record)\s+\w+"
)

# Method signature patterns for getter/setter detection
_GETTER_RE    = re.compile(
    r"^(?:public|protected)\s+(?!void)\S+\s+get\w+\s*\(\s*\)\s*\{?\s*$"
)
_SETTER_RE    = re.compile(
    r"^(?:public|protected)\s+void\s+set\w+\s*\(\s*\S.*\)\s*\{?\s*$"
)
_METHOD_RE    = re.compile(
    r"(?:(?:public|private|protected|static|final|abstract|synchronized"
    r"|native|default|strictfp|transient)\s+)*"
    r"(?:<[^>]*>\s*)?[\w\[\]<>,\s]+\s+(\w+)\s*\("
)
_FIELD_RE     = re.compile(
    r"^(?:(?:public|private|protected|static|final|volatile|transient)\s+)*"
    r"[\w\[\]<>,]+\s+\w+\s*(?:=|;)"
)
_CTOR_BODY_RE = re.compile(r"\b(\w+)\s*\(")  # used to detect constructor name == class name


def _is_getter_setter(sig: str) -> bool:
    """True if the signature looks like a standard getter or setter."""
    return bool(_GETTER_RE.match(sig.strip())) or bool(_SETTER_RE.match(sig.strip()))


# ---------------------------------------------------------------------------
# Class body processor
# ---------------------------------------------------------------------------

def _process_class_body(
    body: str,
    class_name: str,
    focus_set: set[str],
    indent: str = "  ",
) -> list[str]:
    """
    Walk a class body, collapse method bodies, show constructors in full.
    Getters/setters are also collapsed but annotated as boilerplate.
    """
    out: list[str] = []
    lines = body.splitlines()
    i = 0
    n = len(lines)
    jsdoc_buf: list[str] = []
    annotation_buf: list[str] = []
    sig_buf: list[str] = []
    in_jsdoc = False

    while i < n:
        raw = lines[i]
        stripped = raw.strip()

        # ── Javadoc ──────────────────────────────────────────────────────
        if stripped.startswith("/**"):
            sig_buf = []  # reset any partial sig
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

        # ── Annotations ──────────────────────────────────────────────────
        if stripped.startswith("@"):
            annotation_buf.append(stripped)
            i += 1
            continue

        # ── Single-line complete block: public Foo() {} or public T get() { return x; }
        if "{" in stripped and stripped.endswith("}") and not _line_ends_with_block_brace(raw):
            sig_buf.append(stripped)
            full_sig = " ".join(sig_buf)
            sig_buf = []
            for jl in jsdoc_buf:
                out.append(indent + jl.strip())
            jsdoc_buf = []
            for ann in annotation_buf:
                out.append(indent + ann)
            annotation_buf = []
            m = re.search(r"(\w+)\s*\(", full_sig)
            method_name = m.group(1) if m else ""
            is_ctor = method_name == class_name
            is_gs = _is_getter_setter(full_sig)
            sig_no_body = re.sub(r"\s*\{.*\}\s*$", "", full_sig).strip()
            if is_ctor or method_name in focus_set:
                out.append(indent + full_sig)
            elif is_gs:
                out.append(f"{indent}{sig_no_body} {{ /* boilerplate */ }}")
            else:
                out.append(f"{indent}{sig_no_body} {{ /* logic hidden */ }}")
            out.append("")
            i += 1
            continue

        # ── Block-opening line ────────────────────────────────────────────
        if _line_ends_with_block_brace(raw) and "{" in raw:
            sig_buf.append(stripped)
            full_sig = " ".join(sig_buf)
            sig_buf = []

            # Flush Javadoc
            for jl in jsdoc_buf:
                out.append(indent + jl.strip())
            jsdoc_buf = []

            # Flush annotations
            for ann in annotation_buf:
                out.append(indent + ann)
            annotation_buf = []

            # Determine method name
            m = re.search(r"(\w+)\s*\(", full_sig)
            method_name = m.group(1) if m else ""
            is_ctor = method_name == class_name
            is_getter_setter = _is_getter_setter(full_sig)

            # Consume the block body
            block_source = "\n".join(lines[i:])
            brace_pos = block_source.index("{")
            end_pos = _find_closing_brace(block_source, brace_pos)
            lines_consumed = block_source[:end_pos].count("\n")
            inner_body = block_source[brace_pos + 1 : end_pos - 1]

            if is_ctor or method_name in focus_set:
                out.append(indent + full_sig)
                for bl in inner_body.splitlines():
                    out.append((indent + "  " + bl.strip()) if bl.strip() else "")
                out.append(indent + "}")
                out.append("")
            elif is_getter_setter:
                sig_no_brace = re.sub(r"\s*\{\s*$", "", full_sig).strip()
                out.append(f"{indent}{sig_no_brace} {{ /* boilerplate */ }}")
                out.append("")
            else:
                sig_no_brace = re.sub(r"\s*\{\s*$", "", full_sig).strip()
                out.append(f"{indent}{sig_no_brace} {{ /* logic hidden */ }}")
                out.append("")

            i += lines_consumed + 1
            continue

        # ── Field declaration or standalone member ────────────────────────
        if stripped and not stripped.startswith("//") and not stripped.startswith("*"):
            if stripped.endswith(";"):
                for jl in jsdoc_buf:
                    out.append(indent + jl.strip())
                jsdoc_buf = []
                for ann in annotation_buf:
                    out.append(indent + ann)
                annotation_buf = []
                if sig_buf:
                    sig_buf.append(stripped)
                    out.append(indent + " ".join(sig_buf))
                    sig_buf = []
                else:
                    out.append(indent + stripped)
            elif stripped == "}":
                # end of anonymous block / enum entry
                pass
            else:
                sig_buf.append(stripped)
        elif not stripped:
            # Blank line — reset partial sig if nothing useful accumulated
            if sig_buf and not any(
                re.match(r"^(?:public|private|protected|static|final|abstract|synchronized"
                         r"|native|default|void|@\w+)\b", s)
                for s in sig_buf
            ):
                sig_buf = []
        i += 1

    return out


# ---------------------------------------------------------------------------
# Main skeletonizer
# ---------------------------------------------------------------------------

def skeletonize(source: str, focus_names: list[str] | None = None) -> str:
    """
    Parse Java source and return its structural skeleton.

    Args:
        source: Raw Java source text.
        focus_names: Method names whose full bodies are shown.

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
        elif stripped.startswith("//") or stripped.startswith("/*") or not stripped:
            continue
        elif stripped.startswith("@") or stripped.startswith("/**"):
            break
        elif _CLASS_RE.search(stripped) or _INTERFACE_RE.search(stripped):
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
    pending_annotations: list[str] = []
    sig_lines: list[str] = []
    in_jsdoc = False

    for line_idx, line in enumerate(lines):
        stripped = line.strip()
        line_start = offsets[line_idx]

        if line_start < processed_up_to:
            continue

        # Skip package / imports already emitted
        if _PACKAGE_RE.match(stripped) or stripped.startswith("import "):
            processed_up_to = line_start + len(line) + 1
            sig_lines = []
            continue

        # ── Javadoc ───────────────────────────────────────────────────
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

        # ── Top-level annotation ──────────────────────────────────────
        if stripped.startswith("@") and not _line_ends_with_block_brace(line):
            pending_annotations.append(stripped)
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

            # ── Interface / enum — show in full ─────────────────────
            if _INTERFACE_RE.search(full_decl):
                if pending_jsdoc:
                    out.extend(pending_jsdoc)
                    pending_jsdoc = []
                for ann in pending_annotations:
                    out.append(ann)
                pending_annotations = []
                block_text = source[line_start : block_end].rstrip()
                out.append(block_text)
                out.append("")
                processed_up_to = block_end
                continue

            # ── Class / abstract class / record ──────────────────────
            class_m = _CLASS_RE.search(full_decl)
            if class_m:
                name_m = re.search(r"(?:class|record)\s+(\w+)", full_decl)
                class_name = name_m.group(1) if name_m else ""
                if pending_jsdoc:
                    out.extend(pending_jsdoc)
                    pending_jsdoc = []
                for ann in pending_annotations:
                    out.append(ann)
                pending_annotations = []
                header = re.sub(r"\s*\{\s*$", "", full_decl).strip()
                out.append(f"{header} {{")
                out.extend(_process_class_body(body_content, class_name, focus_set))
                out.append("}")
                out.append("")
                processed_up_to = block_end
                continue

            # ── Unknown top-level block ───────────────────────────────
            pending_jsdoc = []
            pending_annotations = []
            processed_up_to = block_end
            continue

        # ── Non-block line ─────────────────────────────────────────────
        if stripped and not stripped.startswith("//") and not stripped.startswith("*"):
            if (re.match(r"^(?:public|private|protected|static|final|abstract|class"
                         r"|interface|enum|@interface|record)\b", stripped)
                    or sig_lines):
                sig_lines.append(stripped)
            else:
                pending_jsdoc = []
                pending_annotations = []

        processed_up_to = line_start + len(line) + 1

    return "\n".join(out).rstrip()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def skeletonize_file(
    file_path: str | Path,
    focus_names: list[str] | None = None,
) -> str:
    """Read a .java file and return its skeleton."""
    path = Path(file_path)
    if not path.exists():
        return f"// File not found: {file_path}"
    if path.suffix.lower() != ".java":
        return f"// Not a Java file: {file_path}"

    source = path.read_text(encoding="utf-8", errors="replace")
    skeleton = skeletonize(source, focus_names)

    token_raw  = len(source) // 4
    token_skel = len(skeleton) // 4
    saved_pct  = 100 - round(token_skel / max(token_raw, 1) * 100)
    header = (
        f"// Java skeleton: {path.name}\n"
        f"// Tokens: ~{token_skel} (vs ~{token_raw} raw, {saved_pct}% saved)\n\n"
    )
    return header + skeleton
