"""
screen_list.py — docs/SCREEN_LIST.md

Catalog of everything a user can see: LWC bundles with their surfaces and
bindings, the FlexiPages and layouts that host them, Aura bundles, Visualforce
pages and navigation metadata.
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

OUTPUT = "SCREEN_LIST.md"
TITLE = "Screen List"


def generate(model: ProjectModel, **_: object) -> str:
    ui_components = [c for c in model.lwc.values() if c.is_ui]
    header = doc_header(
        TITLE,
        "LWC screen catalog, host pages and navigation surfaces.",
        model.project_root.name,
        f"{len(ui_components)} screen component(s), {len(model.lwc)} LWC bundle(s) total",
    )
    if model.is_empty:
        return header + empty_notice("components")

    return header + "".join(
        (
            _screens(model),
            _modules(model),
            _hosts(model),
            _legacy(model),
            _navigation(model),
        )
    )


def _screens(model: ProjectModel) -> str:
    rows = []
    for component in sorted(model.lwc.values(), key=lambda c: c.name):
        if not component.is_ui:
            continue
        rows.append(
            [
                f"`{component.name}`",
                component.exposed,
                joined(component.targets) or "not exposed",
                code(component.api_properties),
                joined(f"on{e}" for e in component.events_handled),
                code(component.apex_calls),
                code(component.child_refs),
            ]
        )
    return section("LWC screens") + table(
        [
            "Component",
            "Exposed",
            "Targets",
            "@api properties",
            "Template handlers",
            "Apex imports",
            "Child components",
        ],
        rows,
        empty="No renderable LWC components indexed.",
    )


def _modules(model: ProjectModel) -> str:
    rows = [
        [
            f"`{c.name}`",
            code(c.public_methods),
            code(c.apex_calls),
            code(c.store_dispatches),
            "store/util module (no template)",
        ]
        for c in sorted(model.lwc.values(), key=lambda c: c.name)
        if not c.is_ui
    ]
    return section("Non-rendering LWC modules") + table(
        ["Bundle", "Exports", "Apex imports", "Dispatches", "Role"],
        rows,
        empty="None — every LWC bundle has a template.",
    )


def _hosts(model: ProjectModel) -> str:
    rows = []
    for page in sorted(model.components_of("FlexiPage"), key=lambda c: c.name):
        components = page.spec.get("components") or []
        hosted = [str(c) for c in components] if isinstance(components, list) else []
        rows.append(
            [
                f"`{page.name}`",
                page.spec.get("pageType", ""),
                page.spec.get("template", ""),
                code([name for name in hosted if name in model.lwc]) or joined(hosted),
            ]
        )
    out = section("Host pages (FlexiPage)") + table(
        ["Page", "Type", "Template", "LWC components hosted"],
        rows,
        empty="No FlexiPages indexed.",
    )

    rows = [
        [f"`{c.name}`", c.type, f"`{c.file}`"]
        for type_name in ("Layout", "CompactLayout", "ListView")
        for c in sorted(model.components_of(type_name), key=lambda c: c.name)
    ]
    out += "\n**Layouts**\n\n" + table(["Component", "Type", "File"], rows, empty="None indexed.")
    return out


def _legacy(model: ProjectModel) -> str:
    rows = []
    for type_name in ("AuraDefinitionBundle", "ApexPage", "ApexComponent"):
        for component in sorted(model.components_of(type_name), key=lambda c: c.name):
            spec = component.spec
            rows.append(
                [
                    f"`{component.name}`",
                    type_name,
                    spec.get("controller", spec.get("bundleType", "")),
                    truncate(joined(spec.get("componentRefs", []) or [], empty=""), 60),
                ]
            )
    return section("Aura and Visualforce surfaces") + table(
        ["Component", "Type", "Controller / kind", "References"],
        rows,
        empty="No Aura bundles or Visualforce pages indexed.",
    )


def _navigation(model: ProjectModel) -> str:
    rows = [
        [f"`{c.name}`", c.type, f"`{c.file}`"]
        for type_name in ("CustomTab", "CustomApplication", "AppMenu", "QuickAction", "HomePageLayout")
        for c in sorted(model.components_of(type_name), key=lambda c: c.name)
    ]
    out = section("Navigation") + table(
        ["Component", "Type", "File"], rows, empty="No navigation metadata indexed."
    )

    navigators = [c.name for c in model.lwc.values() if c.uses_navigation]
    if navigators:
        out += "\n" + f"Components using `lightning/navigation`: {code(navigators)}\n"
    return out
