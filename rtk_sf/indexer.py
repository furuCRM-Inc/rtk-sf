"""
indexer.py — Differential Salesforce metadata parser and YAML spec generator.

Parses Apex classes, custom object XML, and field XML into compressed YAML specs.
Uses mtime-based differential tracking so only changed files are re-indexed.

Usage:
    python -m rtk_sf index [--path ./force-app]
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
# XML parsers (field, object)
# ---------------------------------------------------------------------------


def _extract_xml_tag(xml: str, tag: str) -> str:
    """Extract the first occurrence of a simple XML tag value."""
    m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.DOTALL)
    return m.group(1).strip() if m else ""


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
# YAML spec builder
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
# Main indexer
# ---------------------------------------------------------------------------


class SalesforceIndexer:
    """
    Differential Salesforce metadata indexer.

    Scans a Salesforce DX project directory, parses Apex classes, custom objects,
    custom fields, and Flow metadata, then writes compressed YAML specs and a
    JSON relations graph.
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

    def _index_xml(self, file_path: Path, stem: str) -> bool:
        xml = file_path.read_text(encoding="utf-8", errors="replace")

        # Detect type from XML content or path
        if "<CustomField>" in xml or "<type>" in xml and "<fullName>" in xml and "fields" not in str(file_path.parent.stem):
            # Field files live in fields/ subdirectory
            if "fields" in str(file_path).lower() or file_path.parent.name == "fields":
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

        # Walk all .cls and .xml files
        extensions = {".cls", ".xml"}
        for root, dirs, files in os.walk(search_root):
            # Skip hidden directories and node_modules
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
            for fname in files:
                file_path = Path(root) / fname
                if file_path.suffix.lower() not in extensions:
                    continue
                # Skip meta.xml companion files for Apex (they carry no useful info)
                if fname.endswith(".cls-meta.xml"):
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
