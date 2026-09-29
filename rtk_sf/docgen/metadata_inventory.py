"""
metadata_inventory.py — docs/METADATA_INVENTORY.md

Full inventory of indexed metadata, each entry mapped to its consumers: which
LWC or thunk calls an Apex class, which FlexiPage hosts a component, which
trigger delegates to which handler, and what each permission set grants.
"""

from __future__ import annotations

from rtk_sf.docgen.extract import ProjectModel
from rtk_sf.docgen.render import (
    code,
    doc_header,
    empty_notice,
    joined,
    section,
    table,
    truncate,
)

OUTPUT = "METADATA_INVENTORY.md"
TITLE = "Metadata Inventory"

# Types rendered by a dedicated section below; everything else falls through to
# the generic listing so newly supported metadata types still show up.
_DEDICATED = {"ApexClass", "ApexTrigger", "Flow", "FlexiPage", "PermissionSet", "Profile"}


def generate(model: ProjectModel, **_: object) -> str:
    counts = model.counts()
    header = doc_header(
        TITLE,
        "Every indexed component and the consumers that depend on it.",
        model.project_root.name,
        f"{sum(counts.values())} component(s) across {len(counts)} type(s)",
    )
    if model.is_empty:
        return header + empty_notice("components")

    return header + "".join(
        (
            _totals(counts),
            _apex(model),
            _triggers(model),
            _flows(model),
            _flexipages(model),
            _security(model),
            _remaining(model),
        )
    )


def _totals(counts: dict) -> str:
    return section("Totals by type") + table(
        ["Metadata type", "Count"], [[name, value] for name, value in counts.items()]
    )


def _apex(model: ProjectModel) -> str:
    rows = []
    for cls in sorted(model.apex.values(), key=lambda c: c.name):
        consumers = []
        for component in model.lwc.values():
            if any(call.startswith(f"{cls.name}.") for call in component.apex_calls):
                consumers.append(f"LWC `{component.name}`")
        for thunk in model.thunks:
            if any(call.startswith(f"{cls.name}.") for call in thunk.apex_calls):
                consumers.append(f"thunk `{thunk.name}`")
        for endpoint in model.endpoints:
            if any(call.startswith(f"{cls.name}.") for call in endpoint.apex_calls):
                consumers.append(f"endpoint `{endpoint.qualified}`")
        for other in model.apex.values():
            if other.name != cls.name and any(
                call.startswith(f"{cls.name}.") for m in other.methods for call in m.calls
            ):
                consumers.append(f"Apex `{other.name}`")
        for trigger in model.triggers:
            if any(call.startswith(f"{cls.name}.") for call in trigger.handler_calls):
                consumers.append(f"trigger `{trigger.name}`")

        rows.append(
            [
                f"`{cls.name}`",
                len(cls.methods),
                len(cls.aura_methods),
                cls.sharing or "—",
                "test" if cls.is_test else "prod",
                code(cls.sobjects),
                joined(sorted(set(consumers))) or "no in-project consumer",
            ]
        )
    return section("Apex classes") + table(
        ["Class", "Methods", "@AuraEnabled", "Sharing", "Kind", "Objects touched", "Consumers"],
        rows,
        empty="No Apex classes indexed.",
    )


def _triggers(model: ProjectModel) -> str:
    return section("Apex triggers") + table(
        ["Trigger", "Object", "Events", "Delegates to"],
        [
            [
                f"`{trigger.name}`",
                f"`{trigger.sobject}`" if trigger.sobject else "",
                joined(trigger.events),
                code(trigger.handler_calls),
            ]
            for trigger in sorted(model.triggers, key=lambda t: t.name)
        ],
        empty="No Apex triggers indexed.",
    )


def _flows(model: ProjectModel) -> str:
    return section("Flows") + table(
        ["Flow", "Label", "Type", "Status", "Elements", "Description"],
        [
            [
                f"`{flow.name}`",
                flow.label,
                flow.process_type,
                flow.status,
                joined([f"{k}:{v}" for k, v in sorted(flow.elements.items())]),
                truncate(flow.description, 80),
            ]
            for flow in sorted(model.flows, key=lambda f: f.name)
        ],
        empty="No Flows indexed.",
    )


def _flexipages(model: ProjectModel) -> str:
    rows = []
    for page in sorted(model.components_of("FlexiPage"), key=lambda c: c.name):
        components = page.spec.get("components") or []
        hosted = [str(c) for c in components if isinstance(components, list)]
        known = [name for name in hosted if name in model.lwc]
        rows.append(
            [
                f"`{page.name}`",
                page.spec.get("pageType", ""),
                page.spec.get("componentCount", len(hosted)),
                code(known) if known else joined(hosted),
            ]
        )
    out = section("FlexiPages") + table(
        ["FlexiPage", "Type", "Component count", "Hosted components"],
        rows,
        empty="No FlexiPages indexed.",
    )

    layout_rows = [
        [f"`{c.name}`", c.type, f"`{c.file}`"]
        for type_name in ("Layout", "CompactLayout", "ListView", "QuickAction", "CustomTab")
        for c in sorted(model.components_of(type_name), key=lambda c: c.name)
    ]
    out += "\n**Layouts and actions**\n\n" + table(
        ["Component", "Type", "File"], layout_rows, empty="None indexed."
    )
    return out


def _security(model: ProjectModel) -> str:
    rows = []
    for type_name in ("PermissionSet", "Profile"):
        for component in sorted(model.components_of(type_name), key=lambda c: c.name):
            perms = component.spec.get("objectPermissions") or []
            granted = []
            if isinstance(perms, list):
                for perm in perms:
                    if not isinstance(perm, dict):
                        continue
                    flags = "".join(
                        letter
                        for letter, key in (("R", "read"), ("C", "create"), ("U", "edit"), ("D", "delete"))
                        if perm.get(key)
                    )
                    granted.append(f"{perm.get('object')}:{flags or '-'}")
            user_perms = component.spec.get("enabledUserPermissions") or []
            rows.append(
                [
                    f"`{component.name}`",
                    type_name,
                    component.spec.get("userLicense", ""),
                    joined(granted) or "no object permissions",
                    joined(user_perms[:6]) if isinstance(user_perms, list) else "",
                ]
            )
    return section("Permission sets and profiles") + table(
        ["Component", "Type", "License", "Object permissions (RCUD)", "User permissions"],
        rows,
        empty="No permission sets or profiles indexed.",
    )


def _remaining(model: ProjectModel) -> str:
    rows = []
    for type_name, items in sorted(model.others.items()):
        if type_name in _DEDICATED or type_name in {
            "Layout", "CompactLayout", "ListView", "QuickAction", "CustomTab",
        }:
            continue
        for component in sorted(items, key=lambda c: c.name):
            rows.append([f"`{component.name}`", type_name, f"`{component.file}`"])
    return section("Other metadata") + table(
        ["Component", "Type", "File"], rows, empty="No further metadata indexed."
    )
