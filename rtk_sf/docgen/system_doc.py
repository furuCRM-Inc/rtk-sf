"""
system_doc.py — docs/SYSTEM_DOCUMENT.md

The entry-point document: a layer-by-layer architecture summary with a Mermaid
overview, the security posture across all entry points, and the project timeline
read straight out of the living memory buckets (last 72 hours, trailing week,
current month, fiscal quarters, fiscal years).
"""

from __future__ import annotations

from rtk_sf.docgen.extract import ProjectModel
from rtk_sf.docgen.render import (
    bullets,
    code,
    doc_header,
    empty_notice,
    joined,
    mermaid,
    section,
    table,
    truncate,
)

OUTPUT = "SYSTEM_DOCUMENT.md"
TITLE = "System Document"

# Companion documents linked from the overview.
_COMPANIONS = (
    ("Function Matrix", "FUNCTION_MATRIX.md", "every entry point and its callers"),
    ("Function Use Cases", "FUNCTION_USECASES.md", "actors, pre/post-conditions, flows"),
    ("Sequence Diagrams", "SEQUENCE_DIAGRAMS.md", "LWC → state → Apex → DB traces"),
    ("Business Scenarios", "BUSINESS_SCENARIOS.md", "end-to-end journeys and compliance"),
    ("Object Definitions", "OBJECT_DEFINITIONS.md", "fields, picklists, validation rules"),
    ("Metadata Inventory", "METADATA_INVENTORY.md", "all components and their consumers"),
    ("Screen List", "SCREEN_LIST.md", "screens, host pages, navigation"),
    ("ERD", "ERD.mmd", "object relationship diagram"),
)


def generate(model: ProjectModel, history: object = None, **_: object) -> str:
    header = doc_header(
        TITLE,
        "Architecture overview, security posture and project timeline.",
        model.project_root.name,
        joined([f"{name}: {count}" for name, count in model.counts().items()]),
    )
    if model.is_empty:
        return header + empty_notice("components")

    return header + "".join(
        (
            _contents(),
            _architecture(model),
            _layers(model),
            _security(model),
            _timeline(history),
        )
    )


def _contents() -> str:
    return section("Document set") + table(
        ["Document", "Contents"],
        [[f"[{title}]({path})", description] for title, path, description in _COMPANIONS],
    )


def _architecture(model: ProjectModel) -> str:
    ui = [c for c in model.lwc.values() if c.is_ui]
    state_count = len(model.slices) + len(model.thunks) + len(model.endpoints)
    aura_count = len(model.aura_methods())

    lines = [
        "flowchart TD",
        '    User([User])',
        f'    UI["UI layer<br/>{len(ui)} LWC screen(s)"]',
    ]
    if state_count:
        lines.append(
            f'    STATE["State layer<br/>{len(model.slices)} slice(s), '
            f'{len(model.thunks)} thunk(s), {len(model.endpoints)} endpoint(s)"]'
        )
    lines.append(
        f'    APEX["Apex layer<br/>{len(model.apex)} class(es), {aura_count} entry point(s)"]'
    )
    lines.append(f'    AUTO["Automation<br/>{len(model.triggers)} trigger(s), {len(model.flows)} Flow(s)"]')
    lines.append(f'    DB[("Salesforce DB<br/>{len(model.objects)} object(s)")]')
    lines.append("")
    lines.append("    User --> UI")
    if state_count:
        lines.append("    UI --> STATE")
        lines.append("    STATE --> APEX")
    else:
        lines.append("    UI --> APEX")
    lines.append("    APEX --> DB")
    lines.append("    DB --> AUTO")
    lines.append("    AUTO --> DB")
    return section("Architecture") + mermaid("\n".join(lines))


def _layers(model: ProjectModel) -> str:
    out = section("Layers")

    ui = sorted((c for c in model.lwc.values() if c.is_ui), key=lambda c: c.name)
    out += "\n**UI layer**\n\n"
    out += table(
        ["Component", "Surfaces", "Apex imports", "Store dispatches"],
        [
            [f"`{c.name}`", joined(c.targets), code(c.apex_calls), code(c.store_dispatches)]
            for c in ui
        ],
        empty="No renderable LWC components.",
    )

    out += "\n**State layer**\n\n"
    state_rows = [[f"`{s.name}`", "slice", code(s.reducers), f"`{s.file}`"] for s in model.slices]
    state_rows += [[f"`{t.name}`", "thunk", code(t.apex_calls), f"`{t.file}`"] for t in model.thunks]
    state_rows += [
        [f"`{e.qualified}`", e.kind, code(e.apex_calls), f"`{e.file}`"] for e in model.endpoints
    ]
    out += table(
        ["Name", "Kind", "Reducers / Apex", "File"],
        state_rows,
        empty="No Redux Toolkit constructs found in this project.",
    )

    out += "\n**Apex layer**\n\n"
    out += table(
        ["Class", "Sharing", "Entry points", "Objects touched", "Summary"],
        [
            [
                f"`{cls.name}`",
                cls.sharing or "—",
                code(m.name for m in cls.aura_methods),
                code(cls.sobjects),
                truncate(cls.summary, 70),
            ]
            for cls in sorted(model.apex.values(), key=lambda c: c.name)
            if not cls.is_test
        ],
        empty="No Apex classes indexed.",
    )

    out += "\n**Automation layer**\n\n"
    auto_rows = [
        [f"`{t.name}`", "trigger", f"`{t.sobject}`", joined(t.events)] for t in model.triggers
    ]
    auto_rows += [
        [f"`{f.name}`", f.process_type or "Flow", "—", f.status] for f in model.flows
    ]
    out += table(
        ["Name", "Kind", "Object", "Events / status"], auto_rows, empty="No automation indexed."
    )
    return out


def _security(model: ProjectModel) -> str:
    entries = model.aura_methods()
    enforced = [
        (cls, method) for cls, method in entries if method.enforced_security
    ]
    writing_unenforced = [
        (cls, method) for cls, method in entries if method.writes and not method.enforced_security
    ]
    no_sharing = [cls.name for cls in model.apex.values() if not cls.sharing and not cls.is_test]

    out = section("Security posture")
    out += "\n" + bullets(
        [
            f"Entry points with an explicit security marker: **{len(enforced)} / {len(entries)}**.",
            f"Writing entry points with no CRUD/FLS or user-mode marker: **{len(writing_unenforced)}**.",
            f"Non-test classes declaring no sharing keyword: **{len(no_sharing)}**"
            + (f" ({code(no_sharing[:8])})" if no_sharing else ""),
        ]
    )
    if writing_unenforced:
        out += "\n**Unenforced write paths**\n\n"
        out += table(
            ["Entry point", "Writes", "Class sharing"],
            [
                [
                    f"`{cls.name}.{method.name}`",
                    joined(op.describe() for op in method.writes),
                    cls.sharing or "none declared",
                ]
                for cls, method in writing_unenforced
            ],
        )
    return out


def _timeline(history: object) -> str:
    out = section("Project timeline")
    if history is None:
        return out + (
            "\nLiving memory not supplied. Record turns with the post-turn hook "
            "(`python3 -m rtk_sf.hooks.memory_post_turn`) to populate this section.\n"
        )

    try:
        data = history.timeline("all")  # type: ignore[attr-defined]
    except Exception:
        return out + "\nLiving memory unavailable.\n"

    buckets = data.get("buckets", {})
    fiscal_start = data.get("fiscal_year_start_month")
    out += f"\n_Fiscal year starts in month {fiscal_start}; buckets roll up automatically._\n"

    recent = buckets.get("recent_3_days", [])
    out += section("Last 72 hours", level=4)
    out += table(
        ["When", "Summary", "Files", "+/-"],
        [
            [
                event.get("ts", "")[:16].replace("T", " "),
                truncate(event.get("summary", ""), 90),
                code(event.get("files", [])[:3]),
                f"+{event.get('insertions', 0)}/-{event.get('deletions', 0)}",
            ]
            for event in reversed(recent)
        ],
        empty="No turns recorded in the last 72 hours.",
    )

    out += section("Trailing week", level=4)
    out += table(
        ["Day", "Turns", "+/-", "Files touched", "Highlights"],
        [
            [
                row.get("period", ""),
                row.get("events", 0),
                f"+{row.get('insertions', 0)}/-{row.get('deletions', 0)}",
                row.get("files_touched", 0),
                truncate(joined(row.get("highlights", []), sep="; "), 90),
            ]
            for row in buckets.get("last_7_days", [])
        ],
        empty="No weekly data yet.",
    )

    out += section("Current month", level=4)
    out += _summary_table(buckets.get("current_month", []), "day")

    out += section("Fiscal quarters", level=4)
    out += _summary_table(list(buckets.get("fiscal_quarters", {}).values()), "quarter")

    out += section("Fiscal years", level=4)
    out += _summary_table(list(buckets.get("fiscal_years", {}).values()), "fiscal year")
    return out


def _summary_table(rows: list, unit: str) -> str:
    return table(
        ["Period", "Turns", "+/-", "Files touched", "Top files", "Highlights"],
        [
            [
                row.get("period", ""),
                row.get("events", 0),
                f"+{row.get('insertions', 0)}/-{row.get('deletions', 0)}",
                row.get("files_touched", 0),
                code(f.get("name") for f in row.get("top_files", [])[:3]),
                truncate(joined(row.get("highlights", []), sep="; "), 90),
            ]
            for row in sorted(rows, key=lambda r: str(r.get("period", "")))
        ],
        empty=f"No {unit} summaries yet.",
    )
