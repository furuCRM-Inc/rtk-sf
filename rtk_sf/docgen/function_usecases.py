"""
function_usecases.py — docs/FUNCTION_USECASES.md

A use-case record per callable entry point: actors, pre-conditions, main flow,
exception and alternative flows, post-conditions and side effects.

Each field is derived, not narrated:

    actors           permission sets / profiles granting the required object
                     access, plus the LWC surfaces that expose the call
    pre-conditions   parameters, required fields, active validation rules,
                     sharing mode, cacheable (no-DML) contract
    main flow        caller → state layer → method → database ops → callees
    exceptions       thrown types, catch blocks, validation rules that can
                     reject the write, callout failure points
    post-conditions  writes performed, triggers fired, flows on the object,
                     formula fields that recalculate

Where a field cannot be derived it is marked `_undetermined_` rather than
filled with plausible prose. Human-written `annotate_component` entries are
surfaced verbatim as business context.
"""

from __future__ import annotations

from rtk_sf.docgen.extract import (
    ApexClassFacts,
    ApexMethod,
    ObjectFacts,
    ProjectModel,
)
from rtk_sf.docgen.render import (
    UNKNOWN,
    bullets,
    code,
    doc_header,
    empty_notice,
    joined,
    numbered,
    section,
    table,
    truncate,
)

OUTPUT = "FUNCTION_USECASES.md"
TITLE = "Function Use Cases"

# Annotations that make a method an entry point worth a use-case record.
_ENTRY_ANNOTATIONS = {"auraenabled", "invocablemethod", "httpget", "httppost",
                      "httpput", "httpdelete", "httppatch", "remoteaction"}


def generate(model: ProjectModel, **_: object) -> str:
    entries = _entry_points(model)
    header = doc_header(
        TITLE,
        "Actors, pre-conditions, flows and side effects per entry point.",
        model.project_root.name,
        f"{len(entries)} entry point(s)",
    )
    if model.is_empty:
        return header + empty_notice("components")
    if not entries:
        return header + (
            "No entry points found. A use case is generated for each method carrying "
            "`@AuraEnabled`, `@InvocableMethod`, or a REST annotation.\n"
        )

    out = [header, _index(model, entries)]
    for cls, method in entries:
        out.append(_use_case(model, cls, method))
    return "".join(out)


def _entry_points(model: ProjectModel) -> list[tuple[ApexClassFacts, ApexMethod]]:
    entries = []
    for cls in sorted(model.apex.values(), key=lambda c: c.name):
        if cls.is_test:
            continue
        for method in cls.methods:
            if any(
                a.split("(")[0].lower() in _ENTRY_ANNOTATIONS for a in method.annotations
            ):
                entries.append((cls, method))
    return sorted(entries, key=lambda pair: (pair[0].name, pair[1].name))


def _index(model: ProjectModel, entries: list[tuple[ApexClassFacts, ApexMethod]]) -> str:
    rows = []
    for cls, method in entries:
        qualified = f"{cls.name}.{method.name}"
        rows.append(
            [
                f"[`{qualified}`](#{_anchor(qualified)})",
                joined(_callers(model, qualified), empty="external caller only"),
                joined([op.sobject for op in method.writes], empty="read-only"),
                truncate(_description(cls, method), 90),
            ]
        )
    return section("Index") + table(
        ["Use case", "Invoked from", "Writes", "Description"], rows
    )


def _use_case(model: ProjectModel, cls: ApexClassFacts, method: ApexMethod) -> str:
    qualified = f"{cls.name}.{method.name}"
    written = _written_objects(model, method)

    out = section(qualified, level=2)
    out += "\n" + bullets(
        [
            f"**Identifier:** `{cls.name}.{method.signature}`",
            f"**Returns:** `{method.returns or 'void'}`",
            f"**Source:** `{cls.file or UNKNOWN}`",
            f"**Annotations:** {code(method.annotations, empty='none')}",
            f"**Description:** {_description(cls, method)}",
        ]
    )

    out += section("Primary actors", level=4)
    out += bullets(_actors(model, cls, method), empty=f"{UNKNOWN} — no permission set or profile in the index grants the required access")

    out += section("Pre-conditions", level=4)
    out += numbered(_preconditions(model, cls, method, written))

    out += section("Main flow", level=4)
    out += numbered(_main_flow(model, cls, method))

    out += section("Exception and alternative flows", level=4)
    out += bullets(_exceptions(model, method, written), empty="No exception path detected in the method body.")

    out += section("Post-conditions and side effects", level=4)
    out += bullets(_postconditions(model, method, written), empty="None — no DML detected, so no persisted change.")

    context = model.annotation_text(cls.name)
    if context:
        out += section("Recorded business context", level=4)
        out += bullets(context)
    return out


# ---------------------------------------------------------------------------
# Field derivation
# ---------------------------------------------------------------------------


def _description(cls: ApexClassFacts, method: ApexMethod) -> str:
    """Method ApexDoc, or the class summary clearly marked as such."""
    if method.description:
        return method.description
    if cls.summary:
        return f"{cls.summary} (class-level summary — method has no ApexDoc)"
    return UNKNOWN


def _callers(model: ProjectModel, qualified: str) -> list[str]:
    callers = [f"LWC `{n}`" for n in model.lwc_callers_of(qualified)]
    callers += [f"thunk `{n}`" for n in model.thunk_callers_of(qualified)]
    callers += [f"endpoint `{n}`" for n in model.endpoint_callers_of(qualified)]
    return callers


def _written_objects(model: ProjectModel, method: ApexMethod) -> list[ObjectFacts]:
    """Indexed objects this method writes to."""
    out = []
    for op in method.writes:
        for obj in model.objects.values():
            if obj.name.lower() == op.sobject.lower() and obj not in out:
                out.append(obj)
    return out


def _actors(model: ProjectModel, cls: ApexClassFacts, method: ApexMethod) -> list[str]:
    """
    Actors are inferred from access grants, not invented job titles.

    A permission set or profile that grants the objects this method touches is
    the closest verifiable proxy for "who can run this".
    """
    needed = {op.sobject.lower() for op in method.db_ops if op.sobject}
    writes = {op.sobject.lower() for op in method.writes if op.sobject}
    actors = []

    for type_name in ("PermissionSet", "Profile"):
        for component in model.components_of(type_name):
            perms = component.spec.get("objectPermissions") or []
            if not isinstance(perms, list):
                continue
            matched = []
            for perm in perms:
                if not isinstance(perm, dict):
                    continue
                obj = str(perm.get("object", ""))
                if obj.lower() not in needed:
                    continue
                if obj.lower() in writes and not (perm.get("edit") or perm.get("create")):
                    continue
                matched.append(obj)
            if matched:
                actors.append(
                    f"{type_name} `{component.name}` — grants {joined(sorted(set(matched)))}"
                )

    # Match the fully-qualified name: many classes can share a method name, and
    # matching on the bare name would list every unrelated caller as an actor.
    qualified = f"{cls.name}.{method.name}"
    surfaces = []
    for component in model.lwc.values():
        if not component.is_ui or qualified not in component.apex_calls:
            continue
        surfaces.append(
            f"Users of LWC `{component.name}`"
            + (f" on {joined(component.targets)}" if component.targets else "")
        )
    return actors + sorted(set(surfaces))


def _preconditions(
    model: ProjectModel,
    cls: ApexClassFacts,
    method: ApexMethod,
    written: list[ObjectFacts],
) -> list[str]:
    items = []
    if method.params:
        items.append(f"Caller supplies {code(method.params)}.")
    else:
        items.append("No parameters required.")

    if cls.sharing:
        items.append(f"Class declared `{cls.sharing}` — record visibility follows that mode.")
    else:
        items.append(
            "Class declares no sharing keyword — it runs without sharing enforcement by default."
        )

    if method.is_cacheable:
        items.append("`cacheable=true`: callable read-only; performing DML would fail at runtime.")

    for op in method.reads:
        mode = op.mode or "system mode (no marker)"
        items.append(f"Caller must be able to read `{op.sobject or 'target object'}` — query runs in {mode}.")

    for obj in written:
        required = [f.name for f in obj.fields if f.required]
        if required:
            items.append(f"`{obj.name}` requires {code(required)} to be populated.")
        for rule in obj.validation_rules:
            if rule.active:
                items.append(
                    f"`{obj.name}` validation rule `{rule.name}` must pass: "
                    f"{truncate(rule.formula or rule.description, 140)}"
                )

    if "CRUD/FLS describe check" in method.security:
        items.append("Method performs its own CRUD/FLS describe check before proceeding.")
    return items


def _main_flow(model: ProjectModel, cls: ApexClassFacts, method: ApexMethod) -> list[str]:
    qualified = f"{cls.name}.{method.name}"
    steps = []

    callers = _callers(model, qualified)
    if callers:
        steps.append(f"Invoked from {joined(callers)}.")
    else:
        steps.append("Invoked by an external client (no in-project caller found).")

    for thunk in model.thunk_callers_of(qualified):
        steps.append(f"Redux thunk `{thunk}` dispatches and awaits the call.")
    for endpoint in model.endpoint_callers_of(qualified):
        steps.append(f"RTK Query endpoint `{endpoint}` issues the request.")

    steps.append(f"`{cls.name}.{method.signature}` executes.")
    for op in method.db_ops:
        verb = {"query": "Queries"}.get(op.kind, f"{op.kind.capitalize()}s")
        mode = f" in {op.mode} mode" if op.mode else ""
        steps.append(f"{verb} `{op.sobject or 'target object'}`{mode}.")
    for call in method.calls:
        steps.append(f"Delegates to `{call}`.")
    if method.has_callout:
        steps.append("Issues an HTTP callout to an external system.")
    steps.append(f"Returns `{method.returns or 'void'}` to the caller.")

    for component in model.lwc.values():
        if qualified in component.apex_calls:
            if component.events_published:
                steps.append(
                    f"`{component.name}` publishes {code(component.events_published)} on success."
                )
            if component.uses_toast:
                steps.append(f"`{component.name}` shows a toast notification.")
            break
    return steps


def _exceptions(
    model: ProjectModel,
    method: ApexMethod,
    written: list[ObjectFacts],
) -> list[str]:
    items = []
    for thrown in method.throws:
        items.append(f"Throws `{thrown}` — surfaced to the client as an error.")
    if method.has_try:
        items.append("Method wraps its work in try/catch; failures are handled in-method.")
    elif method.db_ops:
        items.append(
            "No try/catch around the database work — a `DmlException` propagates to the caller."
        )
    for obj in written:
        for rule in obj.validation_rules:
            if rule.active:
                items.append(
                    f"Alternative flow — `{obj.name}` rule `{rule.name}` rejects the save: "
                    f"\"{truncate(rule.error_message, 140)}\""
                )
    if method.has_callout:
        items.append("Callout timeout or non-2xx response aborts the flow.")
    if not method.enforced_security and method.writes:
        items.append(
            "No CRUD/FLS or user-mode marker on a writing method — access errors will not be "
            "raised for users lacking field permissions."
        )
    return items


def _postconditions(
    model: ProjectModel,
    method: ApexMethod,
    written: list[ObjectFacts],
) -> list[str]:
    items = []
    for op in method.writes:
        mode = f" ({op.mode} mode)" if op.mode else ""
        items.append(f"`{op.sobject or 'target object'}` records {op.kind}ed{mode}.")
        for trigger in model.triggers_on(op.sobject):
            items.append(
                f"Trigger `{trigger.name}` fires ({joined(trigger.events)})"
                + (f", delegating to {code(trigger.handler_calls)}" if trigger.handler_calls else "")
            )
    for obj in written:
        formulas = [f.name for f in obj.fields if f.formula]
        if formulas:
            items.append(f"Formula field(s) on `{obj.name}` recalculate: {code(formulas)}.")
    for flow in model.flows:
        if flow.writes and flow.status.lower() == "active":
            items.append(
                f"Active Flow `{flow.name}` may run on the resulting DML "
                f"({flow.process_type or 'Flow'})."
            )
    for call in method.calls:
        cls_name = call.split(".")[0]
        callee = model.apex.get(cls_name)
        if callee and any(m.writes for m in callee.methods):
            items.append(f"`{call}` performs its own DML — see its use case for details.")
    return items


def _anchor(text: str) -> str:
    return text.lower().replace(".", "").replace(" ", "-")
