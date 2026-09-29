"""
function_matrix.py — docs/FUNCTION_MATRIX.md

One table per invocation surface, wiring each Apex entry point to the LWC
handlers, Redux thunks and RTK Query endpoints that reach it, plus the objects
it touches and the security mode it touches them in. Ends with a gap list:
entry points nothing calls, and client imports that resolve to no indexed class.
"""

from __future__ import annotations

from rtk_sf.docgen.extract import ProjectModel
from rtk_sf.docgen.render import (
    bullets,
    code,
    doc_header,
    empty_notice,
    joined,
    section,
    table,
    truncate,
)

OUTPUT = "FUNCTION_MATRIX.md"
TITLE = "Function Matrix"

# Apex annotations that expose a method to something other than an LWC.
_SURFACE_ANNOTATIONS = {
    "invocablemethod",
    "future",
    "httpget",
    "httppost",
    "httpput",
    "httpdelete",
    "httppatch",
    "remoteaction",
}


def generate(model: ProjectModel, **_: object) -> str:
    counts = (
        f"{len(model.apex)} Apex class(es), {len(model.lwc)} LWC bundle(s), "
        f"{len(model.thunks)} thunk(s), {len(model.endpoints)} RTK endpoint(s)"
    )
    header = doc_header(
        TITLE,
        "Every callable entry point and the client code that reaches it.",
        model.project_root.name,
        counts,
    )
    if model.is_empty:
        return header + empty_notice("components")

    return header + "".join(
        (
            _apex_entry_points(model),
            _other_apex_surfaces(model),
            _redux(model),
            _lwc_handlers(model),
            _gaps(model),
        )
    )


def _apex_entry_points(model: ProjectModel) -> str:
    rows = []
    for cls, method in model.aura_methods():
        qualified = f"{cls.name}.{method.name}"
        rows.append(
            [
                f"`{qualified}`",
                "cacheable (read-only)" if method.is_cacheable else "read/write",
                method.returns or "void",
                code(model.lwc_callers_of(qualified)),
                code(model.thunk_callers_of(qualified)),
                code(model.endpoint_callers_of(qualified)),
                joined(op.describe() for op in method.reads),
                joined(op.describe() for op in method.writes),
                joined(method.security),
            ]
        )
    return section("Apex `@AuraEnabled` entry points") + table(
        [
            "Entry point",
            "Mode",
            "Returns",
            "LWC caller(s)",
            "Thunk(s)",
            "RTK endpoint(s)",
            "Reads",
            "Writes",
            "Security markers",
        ],
        rows,
        empty="No `@AuraEnabled` methods found.",
    )


def _other_apex_surfaces(model: ProjectModel) -> str:
    rows = []
    for cls in sorted(model.apex.values(), key=lambda c: c.name):
        if cls.is_test:
            continue
        for method in cls.methods:
            surfaces = [
                annotation
                for annotation in method.annotations
                if annotation.split("(")[0].lower() in _SURFACE_ANNOTATIONS
            ]
            if not surfaces:
                continue
            rows.append(
                [
                    f"`{cls.name}.{method.name}`",
                    joined(surfaces),
                    joined(op.describe() for op in method.db_ops),
                    joined(method.security),
                    truncate(method.description, 80),
                ]
            )
    return section("Other invocation surfaces (Flow, REST, async)") + table(
        ["Method", "Annotation(s)", "Database operations", "Security markers", "Description"],
        rows,
        empty="No invocable, REST or async entry points found.",
    )


def _redux(model: ProjectModel) -> str:
    out = section("Redux state layer")

    out += "\n**Slices**\n\n"
    out += table(
        ["Slice", "Reducers", "Thunks", "File"],
        [
            [f"`{s.name}`", code(s.reducers), code(s.thunks), f"`{s.file}`"]
            for s in sorted(model.slices, key=lambda s: s.name)
        ],
        empty="No `createSlice` definitions found.",
    )

    out += "\n**Async thunks**\n\n"
    out += table(
        ["Thunk", "Action type", "Apex called", "File"],
        [
            [f"`{t.name}`", f"`{t.action_type}`", code(t.apex_calls), f"`{t.file}`"]
            for t in sorted(model.thunks, key=lambda t: t.name)
        ],
        empty="No `createAsyncThunk` definitions found.",
    )

    out += "\n**RTK Query endpoints**\n\n"
    out += table(
        ["Endpoint", "Kind", "Apex called", "File"],
        [
            [f"`{e.qualified}`", e.kind, code(e.apex_calls), f"`{e.file}`"]
            for e in sorted(model.endpoints, key=lambda e: (e.api, e.name))
        ],
        empty="No `createApi` endpoints found.",
    )
    return out


def _lwc_handlers(model: ProjectModel) -> str:
    rows = [
        [
            f"`{c.name}`",
            joined(f"on{event}" for event in c.events_handled),
            code(c.public_methods),
            code(c.apex_calls),
            code(c.wired),
            code(c.store_dispatches),
            joined(c.events_published),
        ]
        for c in sorted(model.lwc.values(), key=lambda c: c.name)
    ]
    return section("LWC handlers and bindings") + table(
        [
            "Component",
            "Template handlers",
            "Public methods",
            "Apex imports",
            "@wire",
            "Store dispatches",
            "Events published",
        ],
        rows,
        empty="No LWC bundles found.",
    )


def _gaps(model: ProjectModel) -> str:
    unwired = []
    for cls, method in model.aura_methods():
        qualified = f"{cls.name}.{method.name}"
        if not (
            model.lwc_callers_of(qualified)
            or model.thunk_callers_of(qualified)
            or model.endpoint_callers_of(qualified)
        ):
            unwired.append(
                f"`{qualified}` — exposed to clients, but no caller found in this project"
            )

    dangling = []
    for component in sorted(model.lwc.values(), key=lambda c: c.name):
        for call in component.apex_calls:
            cls_name, _, method_name = call.rpartition(".")
            if cls_name not in model.apex:
                dangling.append(f"`{component.name}` imports `{call}` — class not in the index")
            elif model.apex[cls_name].method(method_name) is None:
                dangling.append(
                    f"`{component.name}` imports `{call}` — method not found on `{cls_name}`"
                )

    out = section("Wiring gaps")
    out += "\n**Entry points with no detected caller**\n\n"
    out += bullets(unwired, empty="None — every `@AuraEnabled` method has a caller.")
    out += "\n**Client imports that do not resolve**\n\n"
    out += bullets(dangling, empty="None — every Apex import resolves to an indexed method.")
    return out
