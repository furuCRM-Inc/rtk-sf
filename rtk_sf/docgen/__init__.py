"""
docgen — direct-to-disk documentation engine for rtk-sf.

Builds one behavioral model of the project (`extract.build_model`) and renders
it through the generators below, writing Markdown/Mermaid straight to disk. Only
a short confirmation line comes back to the caller, so exporting a 40-page
document set costs the agent a handful of tokens instead of the whole document.

Usage:
    from rtk_sf.docgen import export_documentation
    print(export_documentation("all", output_dir="docs", project_root="."))
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rtk_sf.docgen import (
    business_scenarios,
    erd,
    function_matrix,
    function_usecases,
    metadata_inventory,
    object_definitions,
    screen_list,
    sequence_diagrams,
    system_doc,
)
from rtk_sf.docgen.extract import ProjectModel, build_model

logger = logging.getLogger(__name__)

# doc_type → module. Each module exposes OUTPUT, TITLE and generate(model, **ctx).
GENERATORS: dict[str, Any] = {
    "function_matrix": function_matrix,
    "function_usecases": function_usecases,
    "sequence_diagrams": sequence_diagrams,
    "business_scenarios": business_scenarios,
    "object_definitions": object_definitions,
    "metadata_inventory": metadata_inventory,
    "screen_list": screen_list,
    "erd": erd,
    "system_doc": system_doc,
}

DOC_TYPES = tuple(GENERATORS) + ("all",)

DEFAULT_OUTPUT_DIR = "docs"

__all__ = ["DOC_TYPES", "GENERATORS", "export_documentation", "write_document"]


def export_documentation(
    doc_type: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    project_root: str | Path = ".",
    model: ProjectModel | None = None,
    history: Any = None,
) -> str:
    """
    Generate one document (or all of them) and write it to disk.

    Args:
        doc_type:     One of DOC_TYPES.
        output_dir:   Destination directory; created if missing. Relative paths
                      resolve against project_root.
        project_root: Project whose `.rtk-sf` index to read.
        model:        Pre-built model (used when exporting several documents).
        history:      Optional HistoryManager for the timeline sections. When
                      omitted, one is opened for project_root if a history file
                      exists.

    Returns:
        A short multi-line summary naming each file written and its size.
    """
    if doc_type not in GENERATORS and doc_type != "all":
        return f"❌ Unknown doc_type '{doc_type}'. Valid: {list(DOC_TYPES)}"

    root = Path(project_root).resolve()
    if not (root / ".rtk-sf" / "specs").is_dir():
        return (
            f"❌ No rtk-sf index at {root / '.rtk-sf'}. "
            "Run `rtk-sf index` in the project first."
        )

    out_dir = Path(output_dir)
    if not out_dir.is_absolute():
        out_dir = root / out_dir

    if model is None:
        model = build_model(root)
    if history is None:
        history = _open_history(root)

    targets = list(GENERATORS) if doc_type == "all" else [doc_type]
    written: list[tuple[str, int]] = []
    failed: list[str] = []

    for name in targets:
        try:
            path, size = write_document(name, model, out_dir, history)
            written.append((path.name, size))
        except Exception as exc:  # one broken generator must not sink the rest
            logger.error("Generator %s failed: %s", name, exc, exc_info=True)
            failed.append(f"{name}: {exc}")

    return _summary(model, out_dir, written, failed)


def write_document(
    doc_type: str,
    model: ProjectModel,
    out_dir: Path,
    history: Any = None,
) -> tuple[Path, int]:
    """Render one document and write it, returning (path, bytes written)."""
    module = GENERATORS[doc_type]
    body = module.generate(model, history=history)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / module.OUTPUT
    path.write_text(body, encoding="utf-8")
    return path, len(body.encode("utf-8"))


def _open_history(root: Path) -> Any:
    """Open the project's living memory, or None when it has no history yet."""
    try:
        from rtk_sf.memory import HistoryManager
    except ImportError:  # pragma: no cover
        return None
    manager = HistoryManager(root)
    return manager if manager.path.exists() else None


def _summary(
    model: ProjectModel,
    out_dir: Path,
    written: list[tuple[str, int]],
    failed: list[str],
) -> str:
    if not written and failed:
        return "❌ Documentation export failed:\n" + "\n".join(f"  • {f}" for f in failed)

    total = sum(size for _name, size in written)
    lines = [
        f"✅ Wrote {len(written)} document(s) to {out_dir} ({total / 1024:.1f} KB total)",
        "  " + ", ".join(name for name, _size in written),
        "  Source: "
        + ", ".join(f"{name} {count}" for name, count in model.counts().items()),
    ]
    if failed:
        lines.append(f"  ⚠ {len(failed)} generator(s) failed:")
        lines.extend(f"    • {f}" for f in failed)
    return "\n".join(lines)
