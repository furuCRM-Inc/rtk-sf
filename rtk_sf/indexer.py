"""
indexer.py — Differential Salesforce metadata parser and YAML spec generator.

Parses Apex classes, custom object XML, field XML, Flows, Triggers, Pages,
Components, LWC bundles, Aura bundles, and all major Salesforce metadata types
into compressed YAML specs. Uses mtime-based differential tracking so only
changed files are re-indexed.

Usage:
    python3 -m rtk_sf index [--path ./force-app]
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RTK_DIR = ".rtk-sf"
REGISTRY_FILE = "registry.json"
SPECS_DIR = "specs"
RELATIONS_FILE = "relations.json"

APEX_CLASS_PATTERN = re.compile(
    r"(?:public|global|private|protected)?\s*(?:virtual|abstract|with sharing|without sharing|inherited sharing)?\s*"
    r"class\s+(\w+)",
    re.IGNORECASE,
)

APEX_METHOD_PATTERN = re.compile(
    # Access modifier / annotations (optional)
    r"(?:(?:(?:public|global|private|protected)\s+)(?:(?:static|virtual|abstract|override|"
    r"with\s+sharing|without\s+sharing|inherited\s+sharing)\s+)*)"
    # Return type (must start with a capital or lowercase word, not 'new')
    r"((?!new\b)[\w][\w<>\[\], .]*?)\s+"
    # Method name
    r"(\w+)\s*"
    # Parameters
    r"\(([^)]*)\)\s*"
    # Method body or semicolon
    r"(?:\{|;)",
    re.MULTILINE,
)

APEXDOC_PATTERN = re.compile(
    r"/\*\*\s*(.*?)\s*\*/",
    re.DOTALL,
)

APEXDOC_TAG_PATTERN = re.compile(
    r"@(description|param|return|throws|author|date|example)\s+(.*?)(?=\n\s*@|\Z)",
    re.DOTALL,
)

XML_FIELD_TAG_PATTERN = re.compile(
    r"<fields>(.*?)</fields>",
    re.DOTALL,
)

XML_SIMPLE_TAG_PATTERN = re.compile(r"<(\w+)>(.*?)</\1>", re.DOTALL)

# ---------------------------------------------------------------------------
# File-suffix → metadata type mapping
# (suffix = everything after the first dot in the filename, lowercased)
# ---------------------------------------------------------------------------

FILE_SUFFIX_TO_TYPE: dict[str, str] = {
    # Code
    "trigger": "ApexTrigger",
    "page": "ApexPage",
    "component": "ApexComponent",
    # UI / Metadata
    "flexipage-meta.xml": "FlexiPage",
    "layout-meta.xml": "Layout",
    "compactlayout-meta.xml": "CompactLayout",
    "listview-meta.xml": "ListView",
    "quickaction-meta.xml": "QuickAction",
    "tab-meta.xml": "CustomTab",
    # Security / Access
    "profile-meta.xml": "Profile",
    "permissionset-meta.xml": "PermissionSet",
    "permissionsetgroup-meta.xml": "PermissionSetGroup",
    "custompermission-meta.xml": "CustomPermission",
    # Rules / Automation
    "validationrule-meta.xml": "ValidationRule",
    "workflow-meta.xml": "WorkflowRule",
    "assignmentrules-meta.xml": "AssignmentRules",
    "escalationrules-meta.xml": "EscalationRules",
    "autoresponserules-meta.xml": "AutoResponseRules",
    "sharingrules-meta.xml": "SharingRules",
    # Data / Config
    "md-meta.xml": "CustomMetadata",
    "labels-meta.xml": "CustomLabel",
    "globalvalueset-meta.xml": "GlobalValueSet",
    "standardvalueset-meta.xml": "StandardValueSet",
    "recordtype-meta.xml": "RecordType",
    "matchingrule-meta.xml": "MatchingRule",
    "duplicaterule-meta.xml": "DuplicateRule",
    # App / Navigation
    "app-meta.xml": "CustomApplication",
    "appmenu-meta.xml": "AppMenu",
    "homepagelayout-meta.xml": "HomePageLayout",
    # Integration / External
    "connectedapp-meta.xml": "ConnectedApp",
    "namedcredential-meta.xml": "NamedCredential",
    "remotesite-meta.xml": "RemoteSiteSetting",
    "authprovider-meta.xml": "AuthProvider",
    "csptrustedsite-meta.xml": "CspTrustedSite",
    # Email
    "email-meta.xml": "EmailTemplate",
    # Agentforce / AI
    "prompttemplate-meta.xml": "PromptTemplate",
    "genaiprompttemplate-meta.xml": "GenAiPromptTemplate",
    "genaifunction-meta.xml": "GenAiFunction",
    "aiapplication-meta.xml": "AIApplication",
    "bot-meta.xml": "Bot",
    "botversion-meta.xml": "BotVersion",
    # Analytics
    "wapp-meta.xml": "WaveApplication",
    "wdash-meta.xml": "WaveDashboard",
    # Static / Assets
    "resource-meta.xml": "StaticResource",
    "asset-meta.xml": "ContentAsset",
    # Trigger events (code)
    "trigger-meta.xml": "ApexTrigger",  # companion meta file — skip in practice
}

# Regex to extract trigger declaration
APEX_TRIGGER_PATTERN = re.compile(
    r"trigger\s+(\w+)\s+on\s+(\w+)\s*\(([^)]+)\)",
    re.IGNORECASE,
)

# Regex to match @api decorated properties/getters in LWC JS
LWC_API_PROPERTY_PATTERN = re.compile(r"@api\s+(?:get\s+)?(\w+)", re.MULTILINE)

# Regex to match public (non-underscore-prefixed) methods in LWC JS
LWC_PUBLIC_METHOD_PATTERN = re.compile(
    r"^\s{0,4}(?:async\s+)?([a-zA-Z][a-zA-Z0-9_]*)\s*\(([^)]*)\)\s*\{",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Registry — mtime-based differential tracking
# ---------------------------------------------------------------------------


class Registry:
    """Tracks file modification times to enable differential re-indexing."""

    def __init__(self, rtk_dir: Path) -> None:
        self._path = rtk_dir / REGISTRY_FILE
        self._data: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not load registry (%s); starting fresh.", exc)
                self._data = {}

    def save(self) -> None:
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    def is_stale(self, path: Path) -> bool:
        """Return True if file is new or has been modified since last index."""
        key = str(path.resolve())
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        return self._data.get(key) != mtime

    def mark_indexed(self, path: Path) -> None:
        key = str(path.resolve())
        self._data[key] = path.stat().st_mtime

    def remove(self, path: Path) -> None:
        key = str(path.resolve())
        self._data.pop(key, None)


# ---------------------------------------------------------------------------
# Apex parser
# ---------------------------------------------------------------------------


def _parse_apexdoc(comment: str) -> dict[str, str]:
    """Extract structured ApexDoc fields from a /** ... */ comment block."""
    result: dict[str, str] = {}

    # Strip leading/trailing whitespace and asterisk decoration from each line
    clean_lines = []
    for line in comment.splitlines():
        stripped = line.strip().lstrip("*").strip()
        clean_lines.append(stripped)
    clean = "\n".join(clean_lines).strip()

    # Grab the description (text before first @tag)
    first_tag = clean.find("@")
    if first_tag == -1:
        result["description"] = clean
    else:
        result["description"] = clean[:first_tag].strip()
        for m in APEXDOC_TAG_PATTERN.finditer(clean):
            tag, value = m.group(1), m.group(2).strip(" *\n")
            # If description tag is explicit, it overrides the pre-tag text
            if tag == "description":
                result["description"] = value
            else:
                result[tag] = value
    return result


def _parse_apex(source: str) -> dict[str, Any]:
    """
    Parse an Apex class source file into a structured dict.

    Returns:
        dict with keys: name, summary, methods (list of method dicts)
    """
    result: dict[str, Any] = {"name": "", "summary": "", "methods": []}

    # Find class name
    class_match = APEX_CLASS_PATTERN.search(source)
    if class_match:
        result["name"] = class_match.group(1)

    # Find ApexDoc immediately before the class declaration
    if class_match:
        pre_class = source[: class_match.start()]
        doc_matches = list(APEXDOC_PATTERN.finditer(pre_class))
        if doc_matches:
            apexdoc = _parse_apexdoc(doc_matches[-1].group(1))
            result["summary"] = apexdoc.get("description", "")

    # Extract methods with their preceding ApexDoc
    # Split source into lines for context-aware matching
    lines = source.splitlines()
    line_offsets: list[int] = []
    offset = 0
    for line in lines:
        line_offsets.append(offset)
        offset += len(line) + 1

    methods: list[dict[str, Any]] = []
    for m in APEX_METHOD_PATTERN.finditer(source):
        return_type = m.group(1).strip()
        method_name = m.group(2).strip()
        params_raw = m.group(3).strip()

        # Skip common false positives
        if method_name.lower() in {"if", "while", "for", "catch", "switch"}:
            continue
        if return_type.lower() in {"if", "else", "try", "catch", "finally"}:
            continue

        # Parse params
        params = []
        if params_raw:
            for param in params_raw.split(","):
                parts = param.strip().split()
                if len(parts) >= 2:
                    params.append({"type": parts[-2], "name": parts[-1]})
                elif len(parts) == 1:
                    params.append({"type": parts[0], "name": "arg"})

        # Find the ApexDoc comment immediately before this method.
        # Only look in the 500 chars before the method signature, not the whole
        # file — this prevents the class-level ApexDoc from bleeding into methods.
        pre_method = source[: m.start()].rstrip()
        # Look for the last /** ... */ that ends immediately before the method
        # (allowing only whitespace, annotations, and access modifiers between them)
        short_window = pre_method[-500:]
        doc_match = None
        for candidate in APEXDOC_PATTERN.finditer(short_window):
            # Ensure nothing but whitespace/annotations/access keywords follows the doc
            after = short_window[candidate.end():].strip()
            # If the text after the docblock contains a closing brace, this doc
            # belongs to the previous method/class, not this one.
            if "}" not in after and "{" not in after:
                doc_match = candidate
        method_doc: dict[str, str] = {}
        if doc_match:
            method_doc = _parse_apexdoc(doc_match.group(1))

        methods.append(
            {
                "name": method_name,
                "returnType": return_type,
                "params": params,
                "description": method_doc.get("description", ""),
            }
        )

    result["methods"] = methods
    return result


# ---------------------------------------------------------------------------
# XML parsers (field, object, flow)
# ---------------------------------------------------------------------------


def _extract_xml_tag(xml: str, tag: str) -> str:
    """Extract the first occurrence of a simple XML tag value."""
    m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.DOTALL)
    return m.group(1).strip() if m else ""


def _extract_xml_attr(xml: str, tag: str, attr: str) -> str:
    """Extract an attribute value from the first occurrence of an XML tag."""
    m = re.search(rf'<{tag}\s[^>]*{attr}="([^"]*)"', xml, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _extract_xml_tag_all(xml: str, tag: str) -> list[str]:
    """Extract all occurrences of a simple XML tag value."""
    return [m.group(1).strip() for m in re.finditer(rf"<{tag}>(.*?)</{tag}>", xml, re.DOTALL)]


def _extract_xml_block_all(xml: str, tag: str) -> list[str]:
    """Return the raw inner XML of all occurrences of a block tag."""
    return [m.group(1) for m in re.finditer(rf"<{tag}>(.*?)</{tag}>", xml, re.DOTALL)]


def _parse_field_xml(xml: str) -> dict[str, Any]:
    """
    Parse a Salesforce custom field XML file.

    Returns:
        dict with keys: fullName, type, label, required, description
    """
    return {
        "fullName": _extract_xml_tag(xml, "fullName"),
        "type": _extract_xml_tag(xml, "type"),
        "label": _extract_xml_tag(xml, "label"),
        "required": _extract_xml_tag(xml, "required").lower() == "true",
        "description": _extract_xml_tag(xml, "description"),
        "length": _extract_xml_tag(xml, "length"),
        "defaultValue": _extract_xml_tag(xml, "defaultValue"),
        "relationshipName": _extract_xml_tag(xml, "relationshipName"),
        "referenceTo": _extract_xml_tag(xml, "referenceTo"),
    }


def _parse_object_xml(xml: str) -> dict[str, Any]:
    """
    Parse a Salesforce custom object XML file.

    Returns:
        dict with keys: label, pluralLabel, fields (list), relationships (list)
    """
    label = _extract_xml_tag(xml, "label")
    plural_label = _extract_xml_tag(xml, "pluralLabel")
    description = _extract_xml_tag(xml, "description")

    fields: list[dict[str, str]] = []
    relationships: list[str] = []

    for field_block in XML_FIELD_TAG_PATTERN.findall(xml):
        field_name = _extract_xml_tag(field_block, "fullName")
        field_type = _extract_xml_tag(field_block, "type")
        field_label = _extract_xml_tag(field_block, "label")
        ref = _extract_xml_tag(field_block, "referenceTo")
        if field_name:
            fields.append(
                {"name": field_name, "type": field_type, "label": field_label}
            )
        if ref:
            relationships.append(ref)

    return {
        "label": label,
        "pluralLabel": plural_label,
        "description": description,
        "fields": fields,
        "relationships": list(set(relationships)),
    }


def _parse_flow_xml(xml: str) -> dict[str, Any]:
    """Parse a Salesforce Flow metadata XML file into a summary dict."""
    label = _extract_xml_tag(xml, "label")
    description = _extract_xml_tag(xml, "description")
    flow_type = _extract_xml_tag(xml, "processType")
    status = _extract_xml_tag(xml, "status")

    # Count elements
    element_counts: dict[str, int] = {}
    for tag in [
        "actionCalls",
        "assignments",
        "decisions",
        "loops",
        "recordLookups",
        "recordCreates",
        "recordUpdates",
        "recordDeletes",
        "screens",
        "subflows",
    ]:
        count = len(re.findall(rf"<{tag}>", xml))
        if count > 0:
            element_counts[tag] = count

    return {
        "label": label,
        "description": description,
        "processType": flow_type,
        "status": status,
        "elementCounts": element_counts,
    }


# ---------------------------------------------------------------------------
# New parsers — generic helper
# ---------------------------------------------------------------------------


def _parse_generic_xml(xml: str, fields_to_extract: list[str]) -> dict[str, Any]:
    """
    Extract a list of tag names from XML and return them as a dict.

    Each entry in fields_to_extract may be:
      - "tagName"          → extract first value as a string
      - "tagName[]"        → extract all values as a list
    """
    result: dict[str, Any] = {}
    for field in fields_to_extract:
        if field.endswith("[]"):
            tag = field[:-2]
            result[tag] = _extract_xml_tag_all(xml, tag)
        else:
            result[field] = _extract_xml_tag(xml, field)
    return result


def _build_generic_spec(
    name: str,
    type_str: str,
    parsed: dict[str, Any],
    file_path: Path,
) -> str:
    """Build a YAML spec from a generic parsed dict, omitting empty values."""
    spec: dict[str, Any] = {
        "component": name,
        "type": type_str,
        "file": str(file_path.name),
    }
    for k, v in parsed.items():
        if v or v == 0:  # keep falsy-but-meaningful values like 0
            if isinstance(v, list) and len(v) == 0:
                continue
            spec[k] = v
    return yaml.dump(spec, allow_unicode=True, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Specialized new parsers
# ---------------------------------------------------------------------------


def _parse_apex_trigger(source: str) -> dict[str, Any]:
    """Parse an Apex Trigger source file."""
    result: dict[str, Any] = {"name": "", "sobject": "", "events": []}
    m = APEX_TRIGGER_PATTERN.search(source)
    if m:
        result["name"] = m.group(1)
        result["sobject"] = m.group(2)
        result["events"] = [e.strip() for e in m.group(3).split(",")]
    return result


def _parse_apex_page(source: str, stem: str) -> dict[str, Any]:
    """Parse a Visualforce Page (.page) file."""
    controller = _extract_xml_attr(source, r"apex:page", "controller") or _extract_xml_attr(
        source, r"apex:page", "standardController"
    )
    title = _extract_xml_attr(source, r"apex:page", "title")
    sidebar = _extract_xml_attr(source, r"apex:page", "sidebar")
    return {
        "name": stem,
        "controller": controller,
        "title": title,
        "showSidebar": sidebar,
    }


def _parse_apex_component(source: str, stem: str) -> dict[str, Any]:
    """Parse a Visualforce Component (.component) file."""
    controller = _extract_xml_attr(source, r"apex:component", "controller")
    access = _extract_xml_attr(source, r"apex:component", "access")
    return {"name": stem, "controller": controller, "access": access}


def _parse_validation_rule_xml(xml: str) -> dict[str, Any]:
    """Parse a validationRules block from an object XML."""
    return {
        "fullName": _extract_xml_tag(xml, "fullName"),
        "active": _extract_xml_tag(xml, "active").lower() == "true",
        "description": _extract_xml_tag(xml, "description"),
        "errorConditionFormula": _extract_xml_tag(xml, "errorConditionFormula"),
        "errorMessage": _extract_xml_tag(xml, "errorMessage"),
    }


def _parse_profile_xml(xml: str) -> dict[str, Any]:
    """Parse a Profile or PermissionSet XML — extract object permissions."""
    user_license = _extract_xml_tag(xml, "userLicense")

    object_permissions: list[dict[str, Any]] = []
    for block in _extract_xml_block_all(xml, "objectPermissions"):
        obj = _extract_xml_tag(block, "object")
        if obj:
            object_permissions.append(
                {
                    "object": obj,
                    "read": _extract_xml_tag(block, "allowRead").lower() == "true",
                    "create": _extract_xml_tag(block, "allowCreate").lower() == "true",
                    "edit": _extract_xml_tag(block, "allowEdit").lower() == "true",
                    "delete": _extract_xml_tag(block, "allowDelete").lower() == "true",
                }
            )

    user_permissions = _extract_xml_tag_all(xml, "name")  # inside <userPermissions>
    # Filter to only those inside userPermissions blocks (crude but effective)
    up_blocks = _extract_xml_block_all(xml, "userPermissions")
    user_permissions = [_extract_xml_tag(b, "name") for b in up_blocks if _extract_xml_tag(b, "enabled") == "true"]

    return {
        "userLicense": user_license,
        "objectPermissions": object_permissions,
        "enabledUserPermissions": user_permissions,
    }


def _parse_workflow_xml(xml: str) -> dict[str, Any]:
    """Parse a Workflow metadata XML — extract rules."""
    rules: list[dict[str, Any]] = []
    for block in _extract_xml_block_all(xml, "rules"):
        full_name = _extract_xml_tag(block, "fullName")
        trigger_type = _extract_xml_tag(block, "triggerType")
        actions = _extract_xml_tag_all(block, "name")
        if full_name:
            rules.append(
                {"name": full_name, "triggerType": trigger_type, "actionCount": len(actions)}
            )
    return {"rules": rules}


def _parse_custom_labels_xml(xml: str) -> list[dict[str, Any]]:
    """Parse a CustomLabels XML file — returns list of label dicts."""
    labels: list[dict[str, Any]] = []
    for block in _extract_xml_block_all(xml, "labels"):
        full_name = _extract_xml_tag(block, "fullName")
        if full_name:
            labels.append(
                {
                    "fullName": full_name,
                    "value": _extract_xml_tag(block, "value"),
                    "language": _extract_xml_tag(block, "language"),
                    "categories": _extract_xml_tag(block, "categories"),
                }
            )
    return labels


def _parse_lwc_bundle(bundle_dir: Path) -> dict[str, Any]:
    """
    Parse a Lightning Web Component bundle directory.

    Reads:
      - *-meta.xml for targets and target configs
      - *.js for @api properties and public methods
      - *.html for template root and component references
    """
    name = bundle_dir.name
    result: dict[str, Any] = {
        "name": name,
        "targets": [],
        "apiProperties": [],
        "publicMethods": [],
        "componentRefs": [],
    }

    for f in bundle_dir.iterdir():
        if f.suffix == ".xml" and f.name.endswith("-meta.xml"):
            xml = f.read_text(encoding="utf-8", errors="replace")
            result["targets"] = _extract_xml_tag_all(xml, "target")
            # Extract @LightningElement property names from targetConfigs
            result["targetConfigProps"] = _extract_xml_tag_all(xml, "property")

        elif f.suffix == ".js" and not f.name.endswith(".test.js"):
            js = f.read_text(encoding="utf-8", errors="replace")
            result["apiProperties"] = LWC_API_PROPERTY_PATTERN.findall(js)
            # Public methods: top-level, not starting with _
            pub_methods = []
            for m in LWC_PUBLIC_METHOD_PATTERN.finditer(js):
                method_name = m.group(1)
                if not method_name.startswith("_") and method_name not in {
                    "constructor", "connectedCallback", "disconnectedCallback",
                    "renderedCallback", "errorCallback",
                }:
                    pub_methods.append(method_name)
            result["publicMethods"] = list(dict.fromkeys(pub_methods))  # deduplicate

        elif f.suffix == ".html":
            html = f.read_text(encoding="utf-8", errors="replace")
            # Find <c-foo>, <lightning-foo> component references
            refs = re.findall(r"<(c-[\w-]+|lightning-[\w-]+)\b", html)
            result["componentRefs"] = list(dict.fromkeys(refs))

    return result


def _parse_aura_bundle(bundle_dir: Path) -> dict[str, Any]:
    """Parse an Aura Definition Bundle directory."""
    name = bundle_dir.name
    result: dict[str, Any] = {"name": name, "attributes": []}

    for f in bundle_dir.iterdir():
        if f.suffix in {".cmp", ".app"}:
            source = f.read_text(encoding="utf-8", errors="replace")
            # Extract <aura:attribute> name attributes
            attrs = re.findall(r'<aura:attribute\s[^>]*name="([^"]+)"', source)
            result["attributes"] = attrs
            result["bundleType"] = "Application" if f.suffix == ".app" else "Component"
            break

    return result


# ---------------------------------------------------------------------------
# YAML spec builders
# ---------------------------------------------------------------------------


def _build_apex_spec(parsed: dict[str, Any], file_path: Path) -> str:
    """Convert parsed Apex data into a compressed YAML spec string."""
    spec: dict[str, Any] = {
        "component": parsed["name"],
        "type": "ApexClass",
        "file": str(file_path.name),
        "summary": parsed["summary"] or "(no description)",
        "methods": [],
    }
    for m in parsed["methods"]:
        method_entry: dict[str, Any] = {
            "name": m["name"],
            "returns": m["returnType"],
        }
        if m["params"]:
            method_entry["params"] = [
                f"{p['type']} {p['name']}" for p in m["params"]
            ]
        if m["description"]:
            method_entry["description"] = m["description"]
        spec["methods"].append(method_entry)

    return yaml.dump(spec, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _build_apex_trigger_spec(parsed: dict[str, Any], file_path: Path) -> str:
    """Convert parsed Apex Trigger data into a YAML spec string."""
    spec: dict[str, Any] = {
        "component": parsed["name"] or file_path.stem,
        "type": "ApexTrigger",
        "file": str(file_path.name),
        "sobject": parsed.get("sobject", ""),
        "events": parsed.get("events", []),
    }
    return yaml.dump(spec, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _build_field_spec(name: str, parsed: dict[str, Any], file_path: Path) -> str:
    """Convert parsed field data into a compressed YAML spec string."""
    spec = {k: v for k, v in parsed.items() if v}
    spec["component"] = name
    spec["type"] = "CustomField"
    spec["file"] = str(file_path.name)
    return yaml.dump(spec, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _build_object_spec(name: str, parsed: dict[str, Any], file_path: Path) -> str:
    """Convert parsed object data into a compressed YAML spec string."""
    spec: dict[str, Any] = {
        "component": name,
        "type": "CustomObject",
        "file": str(file_path.name),
        "label": parsed["label"],
        "pluralLabel": parsed["pluralLabel"],
    }
    if parsed["description"]:
        spec["description"] = parsed["description"]
    if parsed["fields"]:
        spec["fields"] = parsed["fields"]
    if parsed["relationships"]:
        spec["relationships"] = parsed["relationships"]
    return yaml.dump(spec, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _build_flow_spec(name: str, parsed: dict[str, Any], file_path: Path) -> str:
    """Convert parsed flow data into a compressed YAML spec string."""
    spec: dict[str, Any] = {
        "component": name,
        "type": "Flow",
        "file": str(file_path.name),
        "label": parsed["label"],
        "processType": parsed["processType"],
        "status": parsed["status"],
    }
    if parsed["description"]:
        spec["description"] = parsed["description"]
    if parsed["elementCounts"]:
        spec["elements"] = parsed["elementCounts"]
    return yaml.dump(spec, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _build_lwc_spec(parsed: dict[str, Any], bundle_dir: Path) -> str:
    """Convert parsed LWC bundle data into a YAML spec string."""
    spec: dict[str, Any] = {
        "component": parsed["name"],
        "type": "LightningComponentBundle",
        "file": str(bundle_dir.name) + "/",
    }
    if parsed.get("targets"):
        spec["targets"] = parsed["targets"]
    if parsed.get("apiProperties"):
        spec["apiProperties"] = parsed["apiProperties"]
    if parsed.get("publicMethods"):
        spec["publicMethods"] = parsed["publicMethods"]
    if parsed.get("componentRefs"):
        spec["componentRefs"] = parsed["componentRefs"]
    return yaml.dump(spec, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _build_aura_spec(parsed: dict[str, Any], bundle_dir: Path) -> str:
    """Convert parsed Aura bundle data into a YAML spec string."""
    spec: dict[str, Any] = {
        "component": parsed["name"],
        "type": "AuraDefinitionBundle",
        "bundleType": parsed.get("bundleType", "Component"),
        "file": str(bundle_dir.name) + "/",
    }
    if parsed.get("attributes"):
        spec["attributes"] = parsed["attributes"]
    return yaml.dump(spec, allow_unicode=True, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Relations graph builder
# ---------------------------------------------------------------------------


def _extract_apex_references(source: str, class_name: str) -> list[str]:
    """Extract referenced class names from Apex source (simple heuristic)."""
    refs: list[str] = []
    # Look for known identifier patterns: Type varName, new Type(, Type.method
    identifier_pattern = re.compile(r"\b([A-Z][A-Za-z0-9_]+)\b")
    keywords = {
        "String", "Integer", "Boolean", "Decimal", "Double", "Long", "Date",
        "Datetime", "Time", "Blob", "ID", "Id", "List", "Map", "Set", "Object",
        "System", "Database", "Schema", "Test", "Math", "JSON", "Type",
        "Exception", "AuraHandledException", "SObject", "void", "null", "true", "false",
    }
    for m in identifier_pattern.finditer(source):
        token = m.group(1)
        if token != class_name and token not in keywords and len(token) > 2:
            refs.append(token)
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique[:30]  # Cap to avoid noise


# ---------------------------------------------------------------------------
# Suffix detection helper
# ---------------------------------------------------------------------------


def _detect_suffix_type(file_path: Path) -> str | None:
    """
    Detect the Salesforce metadata type from a file's compound suffix.

    e.g. "MyFlow.flow-meta.xml" → suffix "flow-meta.xml" → "Flow"
         "MyPage.page"          → suffix "page" → "ApexPage"
    Returns None if not recognized.
    """
    name = file_path.name.lower()
    # Try progressively longer suffixes (after the first dot)
    dot_idx = name.find(".")
    if dot_idx == -1:
        return None
    suffix = name[dot_idx + 1:]
    return FILE_SUFFIX_TO_TYPE.get(suffix)


# ---------------------------------------------------------------------------
# Main indexer
# ---------------------------------------------------------------------------


class SalesforceIndexer:
    """
    Differential Salesforce metadata indexer.

    Scans a Salesforce DX project directory, parses Apex classes, triggers,
    pages, components, LWC bundles, Aura bundles, custom objects, custom
    fields, Flow metadata, and all major Salesforce metadata types, then
    writes compressed YAML specs and a JSON relations graph.
    """

    def __init__(self, project_root: str | Path = ".") -> None:
        self.project_root = Path(project_root).resolve()
        self.rtk_dir = self.project_root / RTK_DIR
        self.specs_dir = self.rtk_dir / SPECS_DIR
        self.registry = Registry(self.rtk_dir)
        self._components: dict[str, dict[str, Any]] = {}  # name → metadata
        self._relations: dict[str, Any] = {"nodes": [], "edges": []}

    def _ensure_dirs(self) -> None:
        self.rtk_dir.mkdir(exist_ok=True)
        self.specs_dir.mkdir(exist_ok=True)

    def _write_spec(self, name: str, yaml_content: str) -> None:
        spec_path = self.specs_dir / f"{name}.yaml"
        spec_path.write_text(yaml_content, encoding="utf-8")
        logger.debug("Wrote spec: %s", spec_path)

    def index_file(self, file_path: Path, force: bool = False) -> bool:
        """
        Index a single file.

        Args:
            file_path: Path to the file to index.
            force: Re-index even if file hasn't changed.

        Returns:
            True if the file was indexed, False if skipped.
        """
        if not force and not self.registry.is_stale(file_path):
            logger.debug("Skipping unchanged file: %s", file_path)
            return False

        suffix = file_path.suffix.lower()
        # Strip compound Salesforce suffixes: .object-meta.xml → stem = name without extension
        # file_path.stem gives "Foo.object-meta" for "Foo.object-meta.xml" — strip further.
        raw_stem = file_path.stem  # e.g. "Account__c.object-meta"
        name = raw_stem.split(".")[0] if "." in raw_stem else raw_stem

        try:
            if suffix == ".cls":
                return self._index_apex(file_path, name)
            elif suffix in {".trigger", ".page", ".component"}:
                return self._index_code_file(file_path, name, suffix)
            elif suffix == ".xml":
                return self._index_xml(file_path, name)
            else:
                return False
        except Exception as exc:
            logger.error("Failed to index %s: %s", file_path, exc)
            return False

    def _index_apex(self, file_path: Path, stem: str) -> bool:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        parsed = _parse_apex(source)
        component_name = parsed["name"] or stem

        yaml_content = _build_apex_spec(parsed, file_path)
        self._write_spec(component_name, yaml_content)

        refs = _extract_apex_references(source, component_name)
        self._components[component_name] = {
            "type": "ApexClass",
            "file": str(file_path),
            "refs": refs,
            "summary": parsed["summary"],
            "methodCount": len(parsed["methods"]),
        }

        self.registry.mark_indexed(file_path)
        logger.info("Indexed Apex class: %s (%d methods)", component_name, len(parsed["methods"]))
        return True

    def _index_code_file(self, file_path: Path, stem: str, suffix: str) -> bool:
        """Index .trigger, .page, or .component files."""
        source = file_path.read_text(encoding="utf-8", errors="replace")

        if suffix == ".trigger":
            parsed = _parse_apex_trigger(source)
            component_name = parsed["name"] or stem
            yaml_content = _build_apex_trigger_spec(parsed, file_path)
            self._write_spec(component_name, yaml_content)
            self._components[component_name] = {
                "type": "ApexTrigger",
                "file": str(file_path),
                "sobject": parsed.get("sobject", ""),
                "events": parsed.get("events", []),
            }
            logger.info(
                "Indexed Apex trigger: %s (on %s)", component_name, parsed.get("sobject", "?")
            )

        elif suffix == ".page":
            parsed_page = _parse_apex_page(source, stem)
            yaml_content = _build_generic_spec(stem, "ApexPage", parsed_page, file_path)
            self._write_spec(stem, yaml_content)
            self._components[stem] = {"type": "ApexPage", "file": str(file_path)}
            logger.info("Indexed Apex page: %s", stem)

        elif suffix == ".component":
            parsed_cmp = _parse_apex_component(source, stem)
            yaml_content = _build_generic_spec(stem, "ApexComponent", parsed_cmp, file_path)
            self._write_spec(stem, yaml_content)
            self._components[stem] = {"type": "ApexComponent", "file": str(file_path)}
            logger.info("Indexed Apex component: %s", stem)

        else:
            return False

        self.registry.mark_indexed(file_path)
        return True

    def _index_xml(self, file_path: Path, stem: str) -> bool:
        xml = file_path.read_text(encoding="utf-8", errors="replace")

        # --- Existing detection (preserve all existing logic) ---

        # Field files live in fields/ subdirectory
        if "fields" in str(file_path).lower() or file_path.parent.name == "fields":
            if "<CustomField>" in xml or file_path.name.endswith(".field-meta.xml"):
                return self._index_field(file_path, stem, xml)

        if "<CustomObject>" in xml:
            return self._index_object(file_path, stem, xml)

        if "<Flow>" in xml or "<processType>" in xml:
            return self._index_flow(file_path, stem, xml)

        # Fallback: try to detect from parent directory name or filename suffix
        parent = file_path.parent.name
        filename = file_path.name
        if parent == "fields" or filename.endswith(".field-meta.xml"):
            return self._index_field(file_path, stem, xml)
        elif parent == "objects" or filename.endswith(".object-meta.xml"):
            return self._index_object(file_path, stem, xml)
        elif parent == "flows" or filename.endswith(".flow-meta.xml"):
            return self._index_flow(file_path, stem, xml)
        elif parent == "validationRules" or filename.endswith(".validationRule-meta.xml"):
            return self._index_standalone_validation_rule(file_path, stem, xml)

        # --- New: detect by compound suffix ---
        detected_type = _detect_suffix_type(file_path)
        if detected_type:
            return self._index_typed_xml(file_path, stem, xml, detected_type)

        # --- Special: validation rules embedded inside object XML ---
        if "<validationRules>" in xml:
            return self._index_validation_rules(file_path, stem, xml)

        logger.debug("Unknown XML type, skipping: %s", file_path)
        return False

    def _index_field(self, file_path: Path, stem: str, xml: str) -> bool:
        # Determine object context from path
        parts = file_path.parts
        try:
            obj_idx = parts.index("fields") - 1
            object_name = parts[obj_idx] if obj_idx >= 0 else "Unknown"
        except ValueError:
            object_name = "Unknown"

        component_name = f"{object_name}.{stem}"
        parsed = _parse_field_xml(xml)
        yaml_content = _build_field_spec(component_name, parsed, file_path)
        self._write_spec(component_name, yaml_content)

        self._components[component_name] = {
            "type": "CustomField",
            "file": str(file_path),
            "object": object_name,
            "fieldType": parsed.get("type", ""),
        }
        self.registry.mark_indexed(file_path)
        logger.info("Indexed field: %s", component_name)
        return True

    def _index_object(self, file_path: Path, stem: str, xml: str) -> bool:
        # Strip .object from stem if present
        component_name = stem.replace(".object", "")
        parsed = _parse_object_xml(xml)
        yaml_content = _build_object_spec(component_name, parsed, file_path)
        self._write_spec(component_name, yaml_content)

        self._components[component_name] = {
            "type": "CustomObject",
            "file": str(file_path),
            "fieldCount": len(parsed["fields"]),
            "relationships": parsed["relationships"],
        }
        self.registry.mark_indexed(file_path)
        logger.info("Indexed object: %s (%d fields)", component_name, len(parsed["fields"]))
        return True

    def _index_flow(self, file_path: Path, stem: str, xml: str) -> bool:
        component_name = stem.replace(".flow", "")
        parsed = _parse_flow_xml(xml)
        yaml_content = _build_flow_spec(component_name, parsed, file_path)
        self._write_spec(component_name, yaml_content)

        self._components[component_name] = {
            "type": "Flow",
            "file": str(file_path),
            "processType": parsed["processType"],
            "status": parsed["status"],
        }
        self.registry.mark_indexed(file_path)
        logger.info("Indexed flow: %s", component_name)
        return True

    def _index_typed_xml(
        self, file_path: Path, stem: str, xml: str, type_str: str
    ) -> bool:
        """
        Route detected metadata types to their specialized or generic parsers.
        Returns True if indexed successfully.
        """
        component_name = stem

        # ---- Profile / PermissionSet ----
        if type_str in {"Profile", "PermissionSet"}:
            parsed = _parse_profile_xml(xml)
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- PermissionSetGroup ----
        elif type_str == "PermissionSetGroup":
            psets = _extract_xml_tag_all(xml, "permissionSets")
            parsed = {"permissionSets": psets}
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- WorkflowRule ----
        elif type_str == "WorkflowRule":
            parsed_wf = _parse_workflow_xml(xml)
            yaml_content = _build_generic_spec(component_name, type_str, parsed_wf, file_path)

        # ---- AssignmentRules / EscalationRules / AutoResponseRules ----
        elif type_str in {"AssignmentRules", "EscalationRules", "AutoResponseRules"}:
            rule_count = len(_extract_xml_tag_all(xml, "fullName"))
            parsed = {"ruleCount": rule_count}
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- SharingRules ----
        elif type_str == "SharingRules":
            owner_count = len(re.findall(r"<sharingOwnerRules>", xml))
            criteria_count = len(re.findall(r"<sharingCriteriaRules>", xml))
            parsed = {"ownerRuleCount": owner_count, "criteriaRuleCount": criteria_count}
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- CustomMetadata ----
        elif type_str == "CustomMetadata":
            label = _extract_xml_tag(xml, "label")
            values: list[dict[str, str]] = []
            for block in _extract_xml_block_all(xml, "values"):
                field = _extract_xml_tag(block, "field")
                value = _extract_xml_tag(block, "value")
                if field:
                    values.append({"field": field, "value": value})
            parsed = {"label": label, "values": values}
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- CustomLabel (labels-meta.xml contains multiple labels) ----
        elif type_str == "CustomLabel":
            labels_list = _parse_custom_labels_xml(xml)
            spec: dict[str, Any] = {
                "component": component_name,
                "type": "CustomLabel",
                "file": str(file_path.name),
                "labelCount": len(labels_list),
                "labels": labels_list,
            }
            yaml_content = yaml.dump(
                spec, allow_unicode=True, default_flow_style=False, sort_keys=False
            )

        # ---- GlobalValueSet ----
        elif type_str == "GlobalValueSet":
            entries = _extract_xml_tag_all(xml, "fullName")
            parsed = {
                "masterLabel": _extract_xml_tag(xml, "masterLabel"),
                "values": entries,
            }
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- StandardValueSet ----
        elif type_str == "StandardValueSet":
            std_entries = _extract_xml_tag_all(xml, "fullName")
            parsed = {"values": std_entries}
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- RecordType ----
        elif type_str == "RecordType":
            parsed = _parse_generic_xml(
                xml, ["fullName", "label", "active", "businessProcess"]
            )
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- FlexiPage ----
        elif type_str == "FlexiPage":
            page_type = _extract_xml_tag(xml, "type")
            template = _extract_xml_tag(xml, "template")
            component_refs = _extract_xml_tag_all(xml, "componentName")
            parsed = {
                "pageType": page_type,
                "template": template,
                "componentCount": len(component_refs),
                "components": component_refs[:20],  # cap for readability
            }
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- Layout ----
        elif type_str == "Layout":
            section_count = len(re.findall(r"<layoutSections>", xml))
            related_count = len(re.findall(r"<relatedLists>", xml))
            parsed = {"sectionCount": section_count, "relatedListCount": related_count}
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- CompactLayout ----
        elif type_str == "CompactLayout":
            fields = _extract_xml_tag_all(xml, "fields")
            parsed = {
                "label": _extract_xml_tag(xml, "label"),
                "fields": fields,
            }
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- ListView ----
        elif type_str == "ListView":
            parsed = _parse_generic_xml(
                xml, ["label", "filterScope", "columns[]"]
            )
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- QuickAction ----
        elif type_str == "QuickAction":
            parsed = _parse_generic_xml(xml, ["type", "targetObject", "label"])
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- CustomTab ----
        elif type_str == "CustomTab":
            custom_obj = _extract_xml_tag(xml, "customObject")
            aura_cmp = _extract_xml_tag(xml, "auraComponent")
            page = _extract_xml_tag(xml, "page")
            parsed = {
                "customObject": custom_obj,
                "auraComponent": aura_cmp,
                "page": page,
            }
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- CustomPermission ----
        elif type_str == "CustomPermission":
            parsed = _parse_generic_xml(xml, ["label", "description"])
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- MatchingRule ----
        elif type_str == "MatchingRule":
            item_count = len(re.findall(r"<matchingRuleItems>", xml))
            active = _extract_xml_tag(xml, "active")
            parsed = {"active": active, "matchingRuleItemCount": item_count}
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- DuplicateRule ----
        elif type_str == "DuplicateRule":
            matching_rules = _extract_xml_tag_all(xml, "matchingRule")
            parsed = {
                "masterLabel": _extract_xml_tag(xml, "masterLabel"),
                "isActive": _extract_xml_tag(xml, "isActive"),
                "matchingRules": matching_rules,
            }
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- CustomApplication ----
        elif type_str == "CustomApplication":
            tabs = _extract_xml_tag_all(xml, "tabs")
            parsed = {
                "label": _extract_xml_tag(xml, "label"),
                "navType": _extract_xml_tag(xml, "navType"),
                "tabCount": len(tabs),
                "tabs": tabs,
            }
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- AppMenu ----
        elif type_str == "AppMenu":
            item_count = len(re.findall(r"<appMenuItems>", xml))
            parsed = {"appMenuItemCount": item_count}
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- HomePageLayout ----
        elif type_str == "HomePageLayout":
            components = _extract_xml_tag_all(xml, "homePageComponents")
            parsed = {"componentCount": len(components), "components": components}
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- ConnectedApp ----
        elif type_str == "ConnectedApp":
            scopes = _extract_xml_tag_all(xml, "scopes")
            parsed = {
                "label": _extract_xml_tag(xml, "label"),
                "scopes": scopes,
            }
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- NamedCredential ----
        elif type_str == "NamedCredential":
            parsed = _parse_generic_xml(xml, ["label", "endpoint", "principalType"])
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- RemoteSiteSetting ----
        elif type_str == "RemoteSiteSetting":
            parsed = _parse_generic_xml(xml, ["url", "isActive", "description"])
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- AuthProvider ----
        elif type_str == "AuthProvider":
            parsed = _parse_generic_xml(xml, ["providerType", "friendlyName"])
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- CspTrustedSite ----
        elif type_str == "CspTrustedSite":
            parsed = _parse_generic_xml(xml, ["endpointUrl", "isActive"])
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- EmailTemplate ----
        elif type_str == "EmailTemplate":
            parsed = _parse_generic_xml(xml, ["name", "subject", "type", "description"])
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- PromptTemplate ----
        elif type_str == "PromptTemplate":
            active_count = len(re.findall(r"<activeVersions>", xml))
            parsed = {
                "masterLabel": _extract_xml_tag(xml, "masterLabel"),
                "type": _extract_xml_tag(xml, "type"),
                "templateType": _extract_xml_tag(xml, "templateType"),
                "activeVersionCount": active_count,
            }
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- GenAiPromptTemplate ----
        elif type_str == "GenAiPromptTemplate":
            parsed = _parse_generic_xml(xml, ["masterLabel", "type"])
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- GenAiFunction ----
        elif type_str == "GenAiFunction":
            parsed = _parse_generic_xml(
                xml, ["masterLabel", "description", "functionDefinition"]
            )
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- AIApplication ----
        elif type_str == "AIApplication":
            parsed = _parse_generic_xml(xml, ["developerName", "status"])
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- Bot / BotVersion ----
        elif type_str in {"Bot", "BotVersion"}:
            dialog_count = len(re.findall(r"<mlSlotClass>|<dialog>|<dialogNode>", xml))
            parsed = {
                "label": _extract_xml_tag(xml, "label"),
                "logPrivateConversationData": _extract_xml_tag(xml, "logPrivateConversationData"),
                "dialogCount": dialog_count,
            }
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- WaveApplication / WaveDashboard ----
        elif type_str in {"WaveApplication", "WaveDashboard"}:
            parsed = _parse_generic_xml(xml, ["name", "label"])
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- StaticResource ----
        elif type_str == "StaticResource":
            parsed = _parse_generic_xml(xml, ["contentType", "cacheControl", "description"])
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- ContentAsset ----
        elif type_str == "ContentAsset":
            parsed = _parse_generic_xml(xml, ["masterLabel", "language"])
            yaml_content = _build_generic_spec(component_name, type_str, parsed, file_path)

        # ---- Fallback for remaining mapped types ----
        else:
            yaml_content = _build_generic_spec(
                component_name, type_str, {"fullName": stem}, file_path
            )

        self._write_spec(component_name, yaml_content)
        self._components[component_name] = {
            "type": type_str,
            "file": str(file_path),
        }
        self.registry.mark_indexed(file_path)
        logger.info("Indexed %s: %s", type_str, component_name)
        return True

    def _index_validation_rules(self, file_path: Path, stem: str, xml: str) -> bool:
        """Index validationRules embedded inside an object XML file."""
        count = 0
        for block in _extract_xml_block_all(xml, "validationRules"):
            parsed = _parse_validation_rule_xml(block)
            rule_name = parsed.get("fullName") or ""
            if not rule_name:
                continue
            component_name = f"{stem}.{rule_name}"
            spec: dict[str, Any] = {
                "component": component_name,
                "type": "ValidationRule",
                "file": str(file_path.name),
                "active": parsed.get("active", False),
                "description": parsed.get("description", ""),
                "errorMessage": parsed.get("errorMessage", ""),
                "errorConditionFormula": parsed.get("errorConditionFormula", ""),
            }
            yaml_content = yaml.dump(
                spec, allow_unicode=True, default_flow_style=False, sort_keys=False
            )
            self._write_spec(component_name, yaml_content)
            self._components[component_name] = {
                "type": "ValidationRule",
                "file": str(file_path),
                "active": parsed.get("active", False),
            }
            count += 1
        if count > 0:
            self.registry.mark_indexed(file_path)
            logger.info("Indexed %d validation rule(s) from: %s", count, file_path.name)
            return True
        return False

    def _index_standalone_validation_rule(self, file_path: Path, stem: str, xml: str) -> bool:
        """Index a standalone *.validationRule-meta.xml file (Salesforce DX format).

        Path convention: objects/<ObjectName>/validationRules/<RuleName>.validationRule-meta.xml
        The object name is derived from the grandparent directory.
        """
        parts = file_path.parts
        try:
            vr_idx = list(parts).index("validationRules")
            object_name = parts[vr_idx - 1]
        except (ValueError, IndexError):
            object_name = file_path.parent.parent.name

        parsed = _parse_validation_rule_xml(xml)
        rule_name = parsed.get("fullName") or stem
        component_name = f"{object_name}.{rule_name}"

        spec: dict[str, Any] = {
            "component": component_name,
            "type": "ValidationRule",
            "file": str(file_path.name),
            "active": parsed.get("active", False),
            "description": parsed.get("description", ""),
            "errorMessage": parsed.get("errorMessage", ""),
            "errorConditionFormula": parsed.get("errorConditionFormula", ""),
        }
        yaml_content = yaml.dump(
            spec, allow_unicode=True, default_flow_style=False, sort_keys=False
        )
        self._write_spec(component_name, yaml_content)
        self._components[component_name] = {
            "type": "ValidationRule",
            "file": str(file_path),
            "object": object_name,
            "active": parsed.get("active", False),
        }
        self.registry.mark_indexed(file_path)
        logger.info("Indexed standalone validation rule: %s", component_name)
        return True

    def _index_lwc_bundle(self, bundle_dir: Path) -> bool:
        """Index a Lightning Web Component bundle directory."""
        if not self.registry.is_stale(bundle_dir / f"{bundle_dir.name}.js") and any(
            (bundle_dir / f"{bundle_dir.name}.js").exists() for _ in [None]
        ):
            # Check any relevant file in bundle for staleness
            pass

        # Always index if any file in the bundle has changed
        any_stale = any(
            self.registry.is_stale(f)
            for f in bundle_dir.iterdir()
            if f.is_file()
        )
        if not any_stale:
            logger.debug("Skipping unchanged LWC bundle: %s", bundle_dir)
            return False

        try:
            parsed = _parse_lwc_bundle(bundle_dir)
            yaml_content = _build_lwc_spec(parsed, bundle_dir)
            component_name = f"lwc__{parsed['name']}"
            self._write_spec(component_name, yaml_content)
            self._components[component_name] = {
                "type": "LightningComponentBundle",
                "file": str(bundle_dir),
            }
            # Mark all files in the bundle as indexed
            for f in bundle_dir.iterdir():
                if f.is_file():
                    self.registry.mark_indexed(f)
            logger.info("Indexed LWC bundle: %s", parsed["name"])
            return True
        except Exception as exc:
            logger.error("Failed to index LWC bundle %s: %s", bundle_dir, exc)
            return False

    def _index_aura_bundle(self, bundle_dir: Path) -> bool:
        """Index an Aura Definition Bundle directory."""
        any_stale = any(
            self.registry.is_stale(f)
            for f in bundle_dir.iterdir()
            if f.is_file()
        )
        if not any_stale:
            logger.debug("Skipping unchanged Aura bundle: %s", bundle_dir)
            return False

        try:
            parsed = _parse_aura_bundle(bundle_dir)
            yaml_content = _build_aura_spec(parsed, bundle_dir)
            component_name = f"aura__{parsed['name']}"
            self._write_spec(component_name, yaml_content)
            self._components[component_name] = {
                "type": "AuraDefinitionBundle",
                "file": str(bundle_dir),
            }
            for f in bundle_dir.iterdir():
                if f.is_file():
                    self.registry.mark_indexed(f)
            logger.info("Indexed Aura bundle: %s", parsed["name"])
            return True
        except Exception as exc:
            logger.error("Failed to index Aura bundle %s: %s", bundle_dir, exc)
            return False

    def _build_relations(self) -> None:
        """Build the nodes+edges graph from indexed components."""
        nodes = []
        edges = []
        known_names = set(self._components.keys())

        for name, meta in self._components.items():
            nodes.append(
                {
                    "id": name,
                    "type": meta["type"],
                    "label": name,
                    "file": meta.get("file", ""),
                }
            )
            # Create edges from Apex refs
            for ref in meta.get("refs", []):
                if ref in known_names:
                    edges.append({"source": name, "target": ref, "relation": "references"})
            # Create edges from object relationships
            for rel in meta.get("relationships", []):
                target = rel.replace("__r", "__c")
                if target in known_names:
                    edges.append({"source": name, "target": target, "relation": "hasLookupTo"})

        self._relations = {"nodes": nodes, "edges": edges}

    def _save_relations(self) -> None:
        relations_path = self.rtk_dir / RELATIONS_FILE
        with open(relations_path, "w") as f:
            json.dump(self._relations, f, indent=2)
        logger.info(
            "Relations saved: %d nodes, %d edges",
            len(self._relations["nodes"]),
            len(self._relations["edges"]),
        )

    def index_project(self, force_app_path: str | Path | None = None) -> dict[str, int]:
        """
        Index an entire Salesforce DX project.

        Args:
            force_app_path: Path to force-app directory. Defaults to ./force-app.

        Returns:
            dict with counts: indexed, skipped, errors.
        """
        self._ensure_dirs()

        search_root = Path(force_app_path) if force_app_path else self.project_root / "force-app"
        if not search_root.exists():
            # Try project root itself
            search_root = self.project_root
            logger.info("force-app not found; scanning project root: %s", search_root)

        counters = {"indexed": 0, "skipped": 0, "errors": 0}

        # Walk all .cls, .xml, .trigger, .page, .component files
        extensions = {".cls", ".xml", ".trigger", ".page", ".component"}
        for root, dirs, files in os.walk(search_root):
            # Skip hidden directories and node_modules
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
            for fname in files:
                file_path = Path(root) / fname
                if file_path.suffix.lower() not in extensions:
                    continue
                # Skip meta.xml companion files for Apex (they carry no useful info)
                if fname.endswith(".cls-meta.xml") or fname.endswith(".trigger-meta.xml"):
                    continue
                try:
                    result = self.index_file(file_path)
                    if result:
                        counters["indexed"] += 1
                    else:
                        counters["skipped"] += 1
                except Exception as exc:
                    logger.error("Error indexing %s: %s", file_path, exc)
                    counters["errors"] += 1

        # Scan LWC bundles
        lwc_root = search_root / "main" / "default" / "lwc"
        if lwc_root.exists():
            for bundle_dir in lwc_root.iterdir():
                if bundle_dir.is_dir():
                    try:
                        result = self._index_lwc_bundle(bundle_dir)
                        if result:
                            counters["indexed"] += 1
                        else:
                            counters["skipped"] += 1
                    except Exception as exc:
                        logger.error("Error indexing LWC bundle %s: %s", bundle_dir, exc)
                        counters["errors"] += 1

        # Scan Aura bundles
        aura_root = search_root / "main" / "default" / "aura"
        if aura_root.exists():
            for bundle_dir in aura_root.iterdir():
                if bundle_dir.is_dir():
                    try:
                        result = self._index_aura_bundle(bundle_dir)
                        if result:
                            counters["indexed"] += 1
                        else:
                            counters["skipped"] += 1
                    except Exception as exc:
                        logger.error("Error indexing Aura bundle %s: %s", bundle_dir, exc)
                        counters["errors"] += 1

        self._build_relations()
        self._save_relations()
        self.registry.save()

        logger.info(
            "Indexing complete. Indexed: %d, Skipped: %d, Errors: %d",
            counters["indexed"],
            counters["skipped"],
            counters["errors"],
        )
        return counters

    def get_component_names(self) -> list[str]:
        """Return list of all indexed component names."""
        return list(self._components.keys())
