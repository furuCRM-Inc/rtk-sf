"""
ast_skeletonizer.py — Python AST-based class skeleton extractor.

Reads a Python source file and returns only the structural skeleton:
class names, base classes, docstrings, method signatures (with type hints),
and __init__ bodies. Function execution blocks are replaced with
'# ... logic hidden' so Claude sees the interface, not the implementation.

Token impact: a 500-line Python class collapses to ~40-token skeleton.

Usage (MCP tool):
    get_python_skeleton(file_path="src/mymodule.py", focus_methods=["process"])
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path


def _get_docstring(node: ast.AST) -> str | None:
    """Return the docstring of a function or class node, or None."""
    body = getattr(node, "body", [])
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        raw = body[0].value.value
        if isinstance(raw, str):
            return textwrap.shorten(raw.strip(), width=120, placeholder="...")
    return None


def _annotation_str(node: ast.expr | None) -> str:
    """Convert an AST annotation node to a readable string."""
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return "..."


def _format_args(args: ast.arguments) -> str:
    """Format function arguments with type hints into a compact string."""
    parts: list[str] = []

    # positional-only + regular args
    all_args = args.posonlyargs + args.args
    defaults_offset = len(all_args) - len(args.defaults)

    for i, arg in enumerate(all_args):
        part = arg.arg
        if arg.annotation:
            part += f": {_annotation_str(arg.annotation)}"
        default_idx = i - defaults_offset
        if default_idx >= 0:
            part += f" = {ast.unparse(args.defaults[default_idx])}"
        parts.append(part)

    if args.vararg:
        s = f"*{args.vararg.arg}"
        if args.vararg.annotation:
            s += f": {_annotation_str(args.vararg.annotation)}"
        parts.append(s)

    for i, arg in enumerate(args.kwonlyargs):
        part = arg.arg
        if arg.annotation:
            part += f": {_annotation_str(arg.annotation)}"
        kw_default = args.kw_defaults[i]
        if kw_default is not None:
            part += f" = {ast.unparse(kw_default)}"
        parts.append(part)

    if args.kwarg:
        s = f"**{args.kwarg.arg}"
        if args.kwarg.annotation:
            s += f": {_annotation_str(args.kwarg.annotation)}"
        parts.append(s)

    return ", ".join(parts)


def _skeleton_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    indent: str,
    focus_methods: set[str] | None,
    is_init: bool = False,
) -> list[str]:
    """Render a single function as its signature + optional docstring."""
    lines: list[str] = []

    # decorators
    for dec in node.decorator_list:
        try:
            lines.append(f"{indent}@{ast.unparse(dec)}")
        except Exception:
            lines.append(f"{indent}@<decorator>")

    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args_str = _format_args(node.args)
    ret = ""
    if node.returns:
        ret = f" -> {_annotation_str(node.returns)}"
    lines.append(f"{indent}{prefix} {node.name}({args_str}){ret}:")

    doc = _get_docstring(node)
    if doc:
        lines.append(f'{indent}    """{doc}"""')

    # For __init__ and focused methods, try to show the real body
    show_body = is_init or (focus_methods is not None and node.name in focus_methods)

    if show_body:
        # Emit the actual body (skipping the docstring node if present)
        body = node.body
        start = 1 if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)) else 0
        for stmt in body[start:]:
            try:
                src = ast.unparse(stmt)
                # ast.unparse produces flat multi-line strings; re-indent every line
                for src_line in src.splitlines():
                    lines.append(f"{indent}    {src_line}")
            except Exception:
                lines.append(f"{indent}    ...")
    else:
        lines.append(f"{indent}    # ... logic hidden")

    return lines


def skeletonize(source: str, focus_methods: list[str] | None = None) -> str:
    """
    Parse Python source and return its structural skeleton.

    Args:
        source: Raw Python source code.
        focus_methods: If provided, these method bodies are shown in full;
                       all others are collapsed.

    Returns:
        Multi-line string — the skeleton, ready to feed to an LLM.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"# SyntaxError: {exc}"

    focus_set = set(focus_methods) if focus_methods else None
    lines: list[str] = []

    # Module-level docstring
    mod_doc = _get_docstring(tree)
    if mod_doc:
        lines.append(f'"""{mod_doc}"""')
        lines.append("")

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        # Only emit top-level classes (lineno check is a proxy)
        bases = ", ".join(ast.unparse(b) for b in node.bases) if node.bases else ""
        header = f"class {node.name}"
        if bases:
            header += f"({bases})"
        header += ":"
        lines.append(header)

        cls_doc = _get_docstring(node)
        if cls_doc:
            lines.append(f'    """{cls_doc}"""')
            lines.append("")

        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                is_init = item.name == "__init__"
                lines.extend(
                    _skeleton_function(item, "    ", focus_set, is_init=is_init)
                )
                lines.append("")

        lines.append("")

    # If no classes found, emit top-level functions
    if not any(isinstance(n, ast.ClassDef) for n in ast.walk(tree)):
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lines.extend(_skeleton_function(node, "", focus_set))
                lines.append("")

    return "\n".join(lines).rstrip()


def skeletonize_file(
    file_path: str | Path,
    focus_methods: list[str] | None = None,
) -> str:
    """Read a .py file and return its skeleton."""
    path = Path(file_path)
    if not path.exists():
        return f"# File not found: {file_path}"
    if path.suffix != ".py":
        return f"# Not a Python file: {file_path}"

    source = path.read_text(encoding="utf-8", errors="replace")
    skeleton = skeletonize(source, focus_methods)

    token_estimate_raw = len(source) // 4
    token_estimate_skeleton = len(skeleton) // 4
    header = (
        f"# Python skeleton: {path.name}\n"
        f"# Tokens: ~{token_estimate_skeleton} (vs ~{token_estimate_raw} raw, "
        f"{100 - round(token_estimate_skeleton / max(token_estimate_raw, 1) * 100)}% saved)\n\n"
    )
    return header + skeleton
