"""
business_scenarios.py — docs/BUSINESS_SCENARIOS.md

End-to-end scenarios assembled per primary sObject: the screens that start a
journey, the components it passes through, the data it changes, the automation
it triggers, and the compliance posture of that whole path.

A scenario is only emitted when there is something to join — an object that at
least one screen, entry point, trigger or Flow touches. Recent work from the
living memory (`.rtk-sf/history.json`) is attached where turn records name the
files involved, so the document reflects what the team has actually been doing
rather than a static snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rtk_sf.docgen.extract import ApexClassFacts, ApexMethod, LwcFacts, ProjectModel
from rtk_sf.docgen.render import (
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

OUTPUT = "BUSINESS_SCENARIOS.md"
TITLE = "Business Scenarios"


@dataclass
class Scenario:
    """One end-to-end journey centred on a primary object."""

    object_name: str
    label: str = ""
    screens: list[LwcFacts] = field(default_factory=list)
    entry_points: list[tuple[ApexClassFacts, ApexMethod]] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    flows: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return f"{self.label or self.object_name} journey"

    @property
    def weight(self) -> tuple[int, int, int]:
        """Richer scenarios first: screens, then entry points, then automation."""
        return (-len(self.screens), -len(self.entry_points), -len(self.triggers) - len(self.flows))


def generate(model: ProjectModel, history: object = None, **_: object) -> str:
    scenarios = build_scenarios(model)
    header = doc_header(
        TITLE,
        "End-to-end journeys, system interactions, and the security posture of each path.",
        model.project_root.name,
        f"{len(scenarios)} scenario(s)",
    )
    if model.is_empty:
        return header + empty_notice("components")
    if not scenarios:
        return header + (
            "No scenario could be assembled: no object in the index is touched by a screen, "
            "entry point, trigger or Flow.\n"
        )

    out = [header, _index(scenarios)]
    for scenario in scenarios:
        out.append(_render(model, scenario))
    out.append(_recent_activity(model, history))
    return "".join(out)


# ---------------------------------------------------------------------------
# Scenario assembly
# ---------------------------------------------------------------------------


def build_scenarios(model: ProjectModel) -> list[Scenario]:
    scenarios: dict[str, Scenario] = {}

    def bucket(name: str) -> Scenario:
        key = name.lower()
        if key not in scenarios:
            obj = next(
                (o for o in model.objects.values() if o.name.lower() == key),
                None,
            )
            scenarios[key] = Scenario(
                object_name=obj.name if obj else name,
                label=(obj.label if obj and obj.label else name),
            )
        return scenarios[key]

    for cls, method in model.aura_methods():
        for op in method.db_ops:
            if not op.sobject:
                continue
            scenario = bucket(op.sobject)
            if (cls, method) not in scenario.entry_points:
                scenario.entry_points.append((cls, method))
            target = scenario.writes if op.kind != "query" else scenario.reads
            label = f"{op.kind.upper()} by `{cls.name}.{method.name}`" + (
                f" [{op.mode}]" if op.mode else ""
            )
            if label not in target:
                target.append(label)

            for component in model.lwc.values():
                if f"{cls.name}.{method.name}" in component.apex_calls and component.is_ui:
                    if component not in scenario.screens:
                        scenario.screens.append(component)

    for component in model.lwc.values():
        for obj_name in component.objects:
            scenario = bucket(obj_name)
            if component.is_ui and component not in scenario.screens:
                scenario.screens.append(component)

    for trigger in model.triggers:
        if trigger.sobject:
            scenario = bucket(trigger.sobject)
            entry = f"`{trigger.name}` ({joined(trigger.events)})"
            if entry not in scenario.triggers:
                scenario.triggers.append(entry)

    # Flows are not object-bound in the index; attach active record-writing Flows
    # to every scenario that writes, and say so explicitly in the output.
    active_flows = [f for f in model.flows if f.writes and f.status.lower() == "active"]
    for scenario in scenarios.values():
        if scenario.writes:
            scenario.flows = [f"`{f.name}` ({f.process_type or 'Flow'})" for f in active_flows]

    return sorted(scenarios.values(), key=lambda s: (s.weight, s.object_name))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _index(scenarios: list[Scenario]) -> str:
    rows = [
        [
            f"[{s.title}](#{_anchor(s.title)})",
            f"`{s.object_name}`",
            len(s.screens),
            len(s.entry_points),
            len(s.writes),
            joined(s.triggers) if s.triggers else "none",
        ]
        for s in scenarios
    ]
    return section("Scenario index") + table(
        ["Scenario", "Primary object", "Screens", "Entry points", "Write paths", "Triggers"],
        rows,
    )


def _render(model: ProjectModel, scenario: Scenario) -> str:
    out = section(scenario.title, level=2)

    out += "\n" + bullets(
        [
            f"**Primary object:** `{scenario.object_name}`",
            f"**Entry screens:** {code(c.name for c in scenario.screens)}",
            f"**Surfaces:** {joined(sorted({t for c in scenario.screens for t in c.targets}))}",
        ]
    )

    out += section("User journey", level=4)
    out += numbered(_journey(model, scenario))

    out += section("System interactions", level=4)
    out += table(
        ["Layer", "Component(s)", "Role"],
        _interactions(model, scenario),
    )

    out += section("Data touched", level=4)
    out += bullets(
        [f"Reads — {joined(scenario.reads)}" if scenario.reads else "Reads — none detected",
         f"Writes — {joined(scenario.writes)}" if scenario.writes else "Writes — none detected"]
    )
    obj = next((o for o in model.objects.values() if o.name == scenario.object_name), None)
    if obj and obj.fields:
        notable = [f for f in obj.fields if f.required or f.formula or f.picklist_values][:12]
        if notable:
            out += table(
                ["Field", "Type", "Required", "Formula / values"],
                [
                    [
                        f"`{f.name}`",
                        f.field_type,
                        f.required,
                        truncate(f.formula or joined(f.picklist_values, empty=""), 60),
                    ]
                    for f in notable
                ],
            )

    out += section("Automation reached", level=4)
    automation = list(scenario.triggers)
    if scenario.flows:
        automation.append(
            "Active record-writing Flows in this org (not object-scoped in the index): "
            + joined(scenario.flows)
        )
    out += bullets(automation, empty="No trigger or Flow detected on this path.")

    out += section("Compliance and security requirements", level=4)
    out += bullets(_security(model, scenario))
    return out


def _journey(model: ProjectModel, scenario: Scenario) -> list[str]:
    steps = []
    for component in scenario.screens:
        surfaces = f" on {joined(component.targets)}" if component.targets else ""
        steps.append(f"User opens `{component.name}`{surfaces}.")
        if component.wired:
            steps.append(f"`{component.name}` loads data through @wire: {code(component.wired)}.")
        handlers = [h for h in component.public_methods if h.lower().startswith("handle")]
        if handlers:
            steps.append(f"User acts, invoking {code(handlers)}.")
        if component.store_dispatches:
            steps.append(f"Action dispatched to the store: {code(component.store_dispatches)}.")

    for cls, method in scenario.entry_points:
        steps.append(f"`{cls.name}.{method.name}` runs server-side.")
        for op in method.db_ops:
            verb = "reads" if op.kind == "query" else f"{op.kind}s"
            steps.append(
                f"It {verb} `{op.sobject or scenario.object_name}`"
                + (f" in {op.mode} mode." if op.mode else ".")
            )
    for trigger in scenario.triggers:
        steps.append(f"Trigger {trigger} fires on the resulting DML.")
    for component in scenario.screens:
        if component.events_published or component.uses_toast:
            steps.append(
                f"`{component.name}` confirms to the user "
                f"({joined(component.events_published + (['toast'] if component.uses_toast else [])) })."
            )
            break
    return steps


def _interactions(model: ProjectModel, scenario: Scenario) -> list[list[str]]:
    rows = []
    if scenario.screens:
        rows.append(["UI (LWC)", code(c.name for c in scenario.screens), "Renders and captures user intent"])

    thunks: list[str] = []
    endpoints: list[str] = []
    for cls, method in scenario.entry_points:
        qualified = f"{cls.name}.{method.name}"
        thunks.extend(model.thunk_callers_of(qualified))
        endpoints.extend(model.endpoint_callers_of(qualified))
    if thunks or endpoints:
        rows.append(
            [
                "State (Redux/RTK)",
                code(sorted(set(thunks + endpoints))),
                "Dispatches the call and holds the result",
            ]
        )
    if scenario.entry_points:
        rows.append(
            [
                "Server (Apex)",
                code(f"{c.name}.{m.name}" for c, m in scenario.entry_points),
                "Applies business rules and performs DML",
            ]
        )
    if scenario.triggers:
        rows.append(["Automation (Apex trigger)", joined(scenario.triggers), "Runs on commit"])
    if scenario.flows:
        rows.append(["Automation (Flow)", joined(scenario.flows), "Declarative follow-up"])
    rows.append(
        [
            "Data (Salesforce DB)",
            f"`{scenario.object_name}`",
            joined([f"{len(scenario.reads)} read path(s)", f"{len(scenario.writes)} write path(s)"]),
        ]
    )
    return rows


def _security(model: ProjectModel, scenario: Scenario) -> list[str]:
    items = []
    unenforced = []
    for cls, method in scenario.entry_points:
        sharing = cls.sharing or "no sharing keyword declared"
        markers = joined(method.security, empty="no explicit marker")
        items.append(f"`{cls.name}.{method.name}` — class `{sharing}`; method security: {markers}.")
        if not method.enforced_security and method.writes:
            unenforced.append(f"{cls.name}.{method.name}")

    obj = next((o for o in model.objects.values() if o.name == scenario.object_name), None)
    if obj:
        for rule in obj.validation_rules:
            if rule.active:
                items.append(
                    f"Validation rule `{rule.name}` enforces: "
                    f"\"{truncate(rule.error_message or rule.description, 140)}\""
                )
        if obj.record_types:
            items.append(f"Record types in play: {code(obj.record_types)}.")

    for type_name in ("PermissionSet", "Profile"):
        for component in model.components_of(type_name):
            perms = component.spec.get("objectPermissions") or []
            if not isinstance(perms, list):
                continue
            for perm in perms:
                if not isinstance(perm, dict):
                    continue
                if str(perm.get("object", "")).lower() == scenario.object_name.lower():
                    granted = [
                        name for name in ("read", "create", "edit", "delete") if perm.get(name)
                    ]
                    items.append(
                        f"{type_name} `{component.name}` grants "
                        f"{joined(granted, empty='no access')} on `{scenario.object_name}`."
                    )

    for type_name in ("SharingRules",):
        for component in model.components_of(type_name):
            items.append(
                f"Sharing rules defined in `{component.name}` "
                f"(owner: {component.spec.get('ownerRuleCount', 0)}, "
                f"criteria: {component.spec.get('criteriaRuleCount', 0)})."
            )

    if unenforced:
        items.append(
            "**Gap —** writing entry point(s) with no CRUD/FLS or user-mode enforcement "
            f"detected: {code(unenforced)}. Confirm this is intentional."
        )
    return items


def _recent_activity(model: ProjectModel, history: object) -> str:
    """Attach living-memory turn records so scenarios carry current context."""
    out = section("Recent development activity")
    if history is None:
        return out + "\nNo history manager supplied; run the exporter with living memory enabled.\n"

    try:
        recent = history.timeline("last_7_days")["last_7_days"]  # type: ignore[attr-defined]
    except Exception:
        return out + "\nLiving memory unavailable.\n"

    active = [row for row in recent if row.get("events")]
    if not active:
        return out + "\nNo turns recorded in the last 7 days.\n"

    out += table(
        ["Day", "Turns", "+/-", "Top files", "Highlights"],
        [
            [
                row.get("period", ""),
                row.get("events", 0),
                f"+{row.get('insertions', 0)}/-{row.get('deletions', 0)}",
                code(f.get("name") for f in row.get("top_files", [])[:3]),
                truncate(joined(row.get("highlights", []), sep="; "), 120),
            ]
            for row in active
        ],
    )
    return out


def _anchor(text: str) -> str:
    return text.lower().replace(" ", "-").replace("`", "").replace(".", "")
