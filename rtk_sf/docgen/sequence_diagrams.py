"""
sequence_diagrams.py — docs/SEQUENCE_DIAGRAMS.md

Renders Mermaid sequence diagrams for the execution boundaries the extractor
found: LWC → Redux/RTK → Apex → Salesforce DB for user-initiated flows, plus
DML → trigger → handler chains and record-triggered automation.

Every message in a diagram comes from an extracted fact (an Apex import, a
dispatch call, a SOQL/DML statement, a trigger declaration). Nothing is filled
in to make a diagram look complete.
"""

from __future__ import annotations

from rtk_sf.docgen.extract import ApexClassFacts, ApexMethod, LwcFacts, ProjectModel
from rtk_sf.docgen.render import (
    bullets,
    doc_header,
    empty_notice,
    joined,
    mermaid,
    section,
    truncate,
)

OUTPUT = "SEQUENCE_DIAGRAMS.md"
TITLE = "Sequence Diagrams"

# A project with hundreds of entry points would produce an unreadable document;
# past this many UI flows we stop and say how many were omitted.
MAX_UI_DIAGRAMS = 40


def generate(model: ProjectModel, **_: object) -> str:
    header = doc_header(
        TITLE,
        "Execution boundaries traced from source: client → state → Apex → database.",
        model.project_root.name,
        f"{len(model.lwc)} LWC bundle(s), {len(model.apex)} Apex class(es), "
        f"{len(model.triggers)} trigger(s)",
    )
    if model.is_empty:
        return header + empty_notice("components")

    return header + "".join(
        (
            _ui_flows(model),
            _trigger_flows(model),
            _automation(model),
            _legend(),
        )
    )


# ---------------------------------------------------------------------------
# UI-initiated flows
# ---------------------------------------------------------------------------


def _ui_flows(model: ProjectModel) -> str:
    out = section("User-initiated flows")
    pairs = _ui_pairs(model)
    if not pairs:
        return out + "\nNo LWC bundle imports an Apex method, so no UI flow could be traced.\n"

    shown = pairs[:MAX_UI_DIAGRAMS]
    for component, cls, method in shown:
        out += section(f"{component.name} → {cls.name}.{method.name}", level=3)
        out += _ui_diagram(model, component, cls, method)
        out += _ui_notes(model, component, cls, method)

    if len(pairs) > len(shown):
        out += f"\n_{len(pairs) - len(shown)} further UI flow(s) omitted for readability._\n"
    return out


def _ui_pairs(model: ProjectModel) -> list[tuple[LwcFacts, ApexClassFacts, ApexMethod]]:
    """Every (component, class, method) triple where a bundle calls Apex."""
    pairs = []
    for component in sorted(model.lwc.values(), key=lambda c: c.name):
        for call in component.apex_calls:
            cls_name, _, method_name = call.rpartition(".")
            cls = model.apex.get(cls_name)
            if cls is None:
                continue
            method = cls.method(method_name)
            if method is None:
                continue
            pairs.append((component, cls, method))
    # UI components first: a store-only bundle is a weaker starting point.
    return sorted(pairs, key=lambda triple: (not triple[0].is_ui, triple[0].name, triple[2].name))


def _ui_diagram(
    model: ProjectModel,
    component: LwcFacts,
    cls: ApexClassFacts,
    method: ApexMethod,
) -> str:
    qualified = f"{cls.name}.{method.name}"
    thunks = model.thunk_callers_of(qualified)
    endpoints = model.endpoint_callers_of(qualified)
    state_label = _state_label(component, thunks, endpoints)
    services = sorted({call.split(".")[0] for call in method.calls if call.split(".")[0] in model.apex})

    lines = ["sequenceDiagram", "    autonumber", "    actor User"]
    lines.append(f"    participant LWC as LWC ({component.name})")
    if state_label:
        lines.append(f"    participant RTK as {state_label}")
    lines.append(f"    participant Apex as Apex ({cls.name})")
    for service in services:
        lines.append(f"    participant {_pid(service)} as Apex ({service})")
    if method.db_ops:
        lines.append("    participant SFDB as Salesforce DB")
    trigger_names: list[str] = []
    for op in method.writes:
        for trigger in model.triggers_on(op.sobject):
            if trigger.name not in trigger_names:
                trigger_names.append(trigger.name)
                lines.append(f"    participant {_pid(trigger.name)} as Trigger ({trigger.name})")
    lines.append("")

    # User → component
    entry = _entry_action(component, method)
    lines.append(f"    User->>LWC: {entry}")

    # Component → state layer → Apex
    if state_label:
        # Prefer the thunk/endpoint that actually reaches THIS method over the
        # component's full dispatch list, which may belong to another flow.
        if thunks:
            lines.append(f"    LWC->>RTK: dispatch({joined(thunks)})")
        elif endpoints:
            kind = next((e.kind for e in model.endpoints if e.qualified in endpoints), "query")
            lines.append(f"    LWC->>RTK: use{kind.capitalize()}({joined(endpoints)})")
        else:
            lines.append(f"    LWC->>RTK: dispatch({joined(component.store_dispatches)})")
        caller = "RTK"
    else:
        caller = "LWC"
    wire_note = " via @wire" if method.name in component.wired else ""
    lines.append(f"    {caller}->>Apex: {method.signature}{wire_note}")

    # Apex body: database work and service calls, in extraction order.
    for op in method.db_ops:
        verb = "SELECT" if op.kind == "query" else op.kind.upper()
        target = op.sobject or "record"
        mode = f" [{op.mode}]" if op.mode else ""
        lines.append(f"    Apex->>SFDB: {verb} {target}{mode}")
        if op.kind == "query":
            lines.append(f"    SFDB-->>Apex: {target} row(s)")
        else:
            for trigger in model.triggers_on(op.sobject):
                lines.append(
                    f"    SFDB->>{_pid(trigger.name)}: {joined(trigger.events, empty='DML event')}"
                )
                lines.append(f"    {_pid(trigger.name)}-->>SFDB: commit")
            lines.append("    SFDB-->>Apex: SaveResult")
    for service in services:
        calls = [c for c in method.calls if c.startswith(f"{service}.")]
        names = joined([c.split(".", 1)[1] for c in calls])
        lines.append(f"    Apex->>{_pid(service)}: {names}")
        lines.append(f"    {_pid(service)}-->>Apex: return")

    if method.has_callout:
        lines.append("    Apex->>Apex: HTTP callout (external system)")
    for thrown in method.throws:
        lines.append(f"    Note over Apex: throws {thrown}")

    # Return path
    returns = method.returns or "void"
    if state_label:
        lines.append(f"    Apex-->>RTK: {returns}")
        lines.append("    RTK-->>LWC: store state update")
    else:
        lines.append(f"    Apex-->>LWC: {returns}")
    if component.events_published:
        published = joined(component.events_published)
        lines.append(f"    LWC-->>User: CustomEvent {published}")
    if component.uses_toast:
        lines.append("    LWC-->>User: toast notification")
    if not component.events_published and not component.uses_toast:
        lines.append("    LWC-->>User: re-render")

    return mermaid("\n".join(lines))


def _ui_notes(
    model: ProjectModel,
    component: LwcFacts,
    cls: ApexClassFacts,
    method: ApexMethod,
) -> str:
    notes = []
    if cls.sharing:
        notes.append(f"Apex class runs `{cls.sharing}`.")
    if method.enforced_security:
        notes.append(f"Security markers in method: {joined(method.security)}.")
    elif method.security:
        notes.append(
            f"Declares {joined(method.security)} — an explicit system-mode opt-out, "
            "not an access check."
        )
    else:
        notes.append(
            "No explicit CRUD/FLS or user-mode marker found in this method — "
            "it runs in system mode unless the caller enforces access."
        )
    if method.is_cacheable:
        notes.append("`cacheable=true`: client-side cached, must not perform DML.")
    if component.targets:
        notes.append(f"Surfaces on: {joined(component.targets)}.")
    for text in model.annotation_text(cls.name):
        notes.append(f"Annotation — {truncate(text, 200)}")
    return "\n" + bullets(notes) + ""


def _state_label(component: LwcFacts, thunks: list[str], endpoints: list[str]) -> str:
    if endpoints:
        return f"RTK Query ({joined(endpoints)})"
    if thunks:
        return f"Redux thunk ({joined(thunks)})"
    if component.uses_store:
        return "Redux store"
    return ""


def _entry_action(component: LwcFacts, method: ApexMethod) -> str:
    if method.name in component.wired:
        return "render component (wired data load)"
    handlers = [h for h in component.public_methods if h.lower().startswith("handle")]
    if handlers:
        handler = handlers[0]
        bound = component.events_for(handler)
        events = joined([f"on{e}" for e in bound], empty="user action")
        return f"{handler}() — {events}"
    if component.events_handled:
        return joined([f"on{e}" for e in component.events_handled])
    return "user action"


# ---------------------------------------------------------------------------
# Trigger and automation flows
# ---------------------------------------------------------------------------


def _trigger_flows(model: ProjectModel) -> str:
    out = section("Database-initiated flows (Apex triggers)")
    if not model.triggers:
        return out + "\nNo Apex triggers in the index.\n"

    for trigger in sorted(model.triggers, key=lambda t: t.name):
        out += section(f"{trigger.name} on {trigger.sobject or 'unknown object'}", level=3)
        lines = [
            "sequenceDiagram",
            "    autonumber",
            "    participant Src as DML source (UI, Apex, API, Flow)",
            "    participant SFDB as Salesforce DB",
            f"    participant TRG as Trigger ({trigger.name})",
        ]
        handlers = sorted({call.split(".")[0] for call in trigger.handler_calls})
        for handler in handlers:
            lines.append(f"    participant {_pid(handler)} as Apex ({handler})")
        lines.append("")
        lines.append(f"    Src->>SFDB: DML on {trigger.sobject or 'record'}")
        lines.append(f"    SFDB->>TRG: {joined(trigger.events, empty='DML event')}")
        for handler in handlers:
            calls = [c for c in trigger.handler_calls if c.startswith(f"{handler}.")]
            names = joined([c.split(".", 1)[1] for c in calls])
            lines.append(f"    TRG->>{_pid(handler)}: {names}")
            handler_cls = model.apex.get(handler)
            if handler_cls:
                for op in handler_cls.sobjects[:3]:
                    lines.append(f"    {_pid(handler)}->>SFDB: touches {op}")
            lines.append(f"    {_pid(handler)}-->>TRG: return")
        lines.append("    TRG-->>SFDB: commit or add error")
        lines.append("    SFDB-->>Src: SaveResult")
        out += mermaid("\n".join(lines))

        rules = []
        for obj in model.objects.values():
            if obj.name.lower() == (trigger.sobject or "").lower():
                rules = [r for r in obj.validation_rules if r.active]
        if rules:
            out += "\n" + bullets(
                [f"Validation rule `{r.name}` can block this DML: {truncate(r.error_message, 120)}" for r in rules]
            )
    return out


def _automation(model: ProjectModel) -> str:
    out = section("Declarative automation (Flows)")
    if not model.flows:
        return out + "\nNo Flows in the index.\n"

    lines = ["flowchart LR"]
    for flow in sorted(model.flows, key=lambda f: f.name):
        node = _pid(flow.name)
        label = flow.label or flow.name
        elements = joined(
            [f"{k}:{v}" for k, v in sorted(flow.elements.items())], empty="no elements"
        )
        lines.append(f'    {node}["{label}<br/>{flow.process_type or "Flow"} · {flow.status or "?"}"]')
        lines.append(f'    {node}_el["{elements}"]')
        lines.append(f"    {node} --> {node}_el")
    out += mermaid("\n".join(lines))
    out += "\n" + bullets(
        [
            f"`{flow.name}` — {flow.process_type or 'Flow'}, {flow.status or 'status unknown'}"
            + (f": {truncate(flow.description, 120)}" if flow.description else "")
            for flow in sorted(model.flows, key=lambda f: f.name)
        ]
    )
    return out


def _legend() -> str:
    return section("How to read these diagrams") + bullets(
        [
            "`[user]` / `[system]` / `[security_enforced]` on a database message is the "
            "access mode declared in the source (`as user`, `WITH USER_MODE`, "
            "`WITH SECURITY_ENFORCED`); no marker means system mode.",
            "A `Redux thunk` or `RTK Query` participant appears only when a thunk or "
            "endpoint in this project actually references the Apex method.",
            "`Note over Apex: throws …` marks exceptions thrown in the method body.",
            "Diagrams are generated from source. Regenerate after changing wiring.",
        ]
    )


def _pid(name: str) -> str:
    """Mermaid participant id — alphanumeric, collision-free enough in practice."""
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in str(name))
    return cleaned.strip("_") or "node"
