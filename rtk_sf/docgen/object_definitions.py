"""
object_definitions.py — docs/OBJECT_DEFINITIONS.md

Per-object reference: fields with types, picklist values and formulas, plus
validation rules, record types and relationships. Formulas and picklist value
sets are read from the field XML, since the spec index does not carry them.
"""

from __future__ import annotations

from rtk_sf.docgen.extract import ObjectFacts, ProjectModel
from rtk_sf.docgen.render import (
    bullets,
    doc_header,
    empty_notice,
    joined,
    section,
    table,
    truncate,
)

OUTPUT = "OBJECT_DEFINITIONS.md"
TITLE = "Object Definitions"


def generate(model: ProjectModel, **_: object) -> str:
    field_total = sum(len(obj.fields) for obj in model.objects.values())
    header = doc_header(
        TITLE,
        "Objects, fields, picklists, formulas, validation rules and record types.",
        model.project_root.name,
        f"{len(model.objects)} object(s), {field_total} field(s)",
    )
    if not model.objects:
        return header + empty_notice("objects or fields")

    out = [header, _index(model)]
    for obj in model.objects.values():
        out.append(_object(model, obj))
    return "".join(out)


def _index(model: ProjectModel) -> str:
    rows = [
        [
            f"[`{obj.name}`](#{_anchor(obj.name)})",
            obj.label,
            len(obj.fields),
            len([f for f in obj.fields if f.required]),
            len(obj.validation_rules),
            len(obj.record_types),
            joined(obj.relationships) or "none",
        ]
        for obj in model.objects.values()
    ]
    return section("Index") + table(
        ["Object", "Label", "Fields", "Required", "Validation rules", "Record types", "Lookups to"],
        rows,
    )


def _object(model: ProjectModel, obj: ObjectFacts) -> str:
    out = section(f"`{obj.name}`", level=2)
    facts = [f"**Label:** {obj.label or '—'}"]
    if obj.plural_label:
        facts.append(f"**Plural:** {obj.plural_label}")
    if obj.description:
        facts.append(f"**Description:** {obj.description}")
    consumers = _consumers(model, obj.name)
    if consumers:
        facts.append(f"**Touched by:** {joined(consumers)}")
    out += "\n" + bullets(facts)

    out += section("Fields", level=4)
    out += table(
        ["Field", "Type", "Label", "Required", "References", "Formula / picklist values", "Description"],
        [
            [
                f"`{f.name}`",
                f.field_type,
                f.label,
                f.required,
                f"`{f.reference_to}`" if f.reference_to else "",
                truncate(f.formula, 70) if f.formula else joined(f.picklist_values, empty=""),
                truncate(f.description, 70),
            ]
            for f in sorted(obj.fields, key=lambda f: f.name)
        ],
        empty="No fields indexed for this object.",
    )

    out += section("Validation rules", level=4)
    out += table(
        ["Rule", "Active", "Error condition", "Error message"],
        [
            [
                f"`{rule.name}`",
                rule.active,
                truncate(rule.formula, 90),
                truncate(rule.error_message, 90),
            ]
            for rule in sorted(obj.validation_rules, key=lambda r: r.name)
        ],
        empty="No validation rules indexed for this object.",
    )

    if obj.record_types:
        out += section("Record types", level=4)
        out += bullets(obj.record_types)
    return out


def _consumers(model: ProjectModel, object_name: str) -> list[str]:
    """Apex classes, LWC bundles and triggers that reference this object."""
    consumers = []
    lowered = object_name.lower()
    for cls in model.apex.values():
        if any(s.lower() == lowered for s in cls.sobjects):
            consumers.append(f"Apex `{cls.name}`")
    for component in model.lwc.values():
        if any(o.lower() == lowered for o in component.objects):
            consumers.append(f"LWC `{component.name}`")
    for trigger in model.triggers_on(object_name):
        consumers.append(f"Trigger `{trigger.name}`")
    return consumers


def _anchor(text: str) -> str:
    return text.lower().replace("_", "").replace("`", "")
