"""
data_tools.py — Token-efficient data creation helpers.

Provides two functions that prevent Salesforce data operations from flooding
Claude's context window with thousands of tokens of schema JSON or SOQL dumps.

get_object_schema(object_name, rtk_dir)
    Reads the local YAML spec instead of hitting `sf sobject describe`.
    Returns a compact "data-generation profile": field names, types,
    required flags, and picklist values only.
    Token impact: 5,000-token live schema → ~150-token clean dictionary.

run_soql(query, target_org, sample_size)
    Runs `sf data query --json`, enforces a LIMIT cap, and truncates the
    result array before returning it to Claude.
    Token impact: 500-record dump → 3 sample records + truncation notice.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# On Windows, sf is installed as sf.cmd; shell=True is required to resolve it.
_SHELL = sys.platform == "win32"

_SAMPLE_SIZE_DEFAULT = 3
_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)

# Field attributes that are noise for data generation — omitted from profile
_OMIT_FIELD_ATTRS = {
    "calculated", "calculatedFormula", "caseSensitive", "controllerName",
    "defaultedOnCreate", "deprecatedAndHidden", "externalId", "filterable",
    "groupable", "htmlFormatted", "idLookup", "nameField", "namePointing",
    "permissionable", "queryByDistance", "searchPrefilterable", "sortable",
    "unique", "writeRequiresMasterRead",
}


# ---------------------------------------------------------------------------
# Feature 1: Mock Schema Injection
# ---------------------------------------------------------------------------

def get_object_schema(object_name: str, rtk_dir: Path) -> str:
    """
    Return a compact data-generation profile for a Salesforce object.

    Reads from the local .rtk-sf/specs/ YAML index — no org call needed.
    Output includes only what Claude needs to generate or modify records:
    field API name, data type, required flag, and picklist values.

    Args:
        object_name: e.g. "Order__c" or "Account"
        rtk_dir:     Path to .rtk-sf directory

    Returns:
        YAML string — the data-generation profile, or an error message.
    """
    spec_file = rtk_dir / "specs" / f"{object_name}.yaml"
    if not spec_file.exists():
        # Try case-insensitive search
        candidates = [
            f for f in (rtk_dir / "specs").glob("*.yaml")
            if f.stem.lower() == object_name.lower()
        ]
        if candidates:
            spec_file = candidates[0]
            object_name = spec_file.stem
        else:
            return (
                f"Object '{object_name}' not found in local index.\n"
                "Run `rtk-sf index` to index your Salesforce project."
            )

    raw = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return f"Could not parse spec for '{object_name}'."

    obj_type = raw.get("type", "")
    if "object" not in obj_type.lower() and "field" not in obj_type.lower():
        return f"'{object_name}' is a {obj_type}, not a CustomObject."

    # Build compact field profiles
    field_rows: list[dict[str, Any]] = []

    # Fields embedded in the object spec
    for field in raw.get("fields", []):
        row = _compact_field(field)
        if row:
            field_rows.append(row)

    # Also load sibling field specs (Object.FieldName.yaml)
    specs_dir = rtk_dir / "specs"
    prefix = object_name + "."
    for fspec in sorted(specs_dir.glob(f"{prefix}*.yaml")):
        fraw = yaml.safe_load(fspec.read_text(encoding="utf-8"))
        if isinstance(fraw, dict) and "field" in fraw.get("type", "").lower():
            row = _compact_field_from_spec(fraw)
            if row:
                field_rows.append(row)

    if not field_rows:
        return f"No field data found for '{object_name}' in local index."

    profile: dict[str, Any] = {
        "object": object_name,
        "label": raw.get("label", object_name),
        "source": "rtk-sf local index (no org call)",
        "fields": field_rows,
    }

    lines = [
        f"# Data-Generation Profile: {object_name}",
        f"# {len(field_rows)} fields · from local index · no org tokens used",
        "",
        yaml.dump(profile, allow_unicode=True, default_flow_style=False, sort_keys=False),
    ]
    return "\n".join(lines)


def _compact_field(field: dict[str, Any]) -> dict[str, Any] | None:
    name = field.get("name") or field.get("api_name") or field.get("fullName")
    if not name:
        return None
    row: dict[str, Any] = {"name": name, "type": field.get("type", "Unknown")}
    if field.get("required") or field.get("nillable") is False:
        row["required"] = True
    if field.get("referenceTo"):
        row["referenceTo"] = field["referenceTo"]
    picklist = field.get("picklistValues") or field.get("picklist_values") or []
    if picklist:
        row["picklist"] = [
            v.get("value", v) if isinstance(v, dict) else v
            for v in picklist
            if (v.get("active", True) if isinstance(v, dict) else True)
        ]
    return row


def _compact_field_from_spec(spec: dict[str, Any]) -> dict[str, Any] | None:
    name = spec.get("component", "").split(".")[-1] or spec.get("fullName", "")
    if not name:
        return None
    row: dict[str, Any] = {"name": name, "type": spec.get("field_type") or spec.get("type", "Unknown")}
    if spec.get("required"):
        row["required"] = True
    if spec.get("referenceTo"):
        row["referenceTo"] = spec["referenceTo"]
    picklist = spec.get("picklist_values") or spec.get("picklistValues") or []
    if picklist:
        row["picklist"] = [
            v.get("value", v) if isinstance(v, dict) else v
            for v in picklist
            if (v.get("active", True) if isinstance(v, dict) else True)
        ]
    return row


# ---------------------------------------------------------------------------
# Feature 2: SOQL Output Truncation
# ---------------------------------------------------------------------------

def _inject_limit(query: str, sample_size: int) -> tuple[str, bool]:
    """
    Ensure the SOQL query has LIMIT <= sample_size.

    Returns (modified_query, was_changed).
    """
    m = _LIMIT_RE.search(query)
    if m:
        existing = int(m.group(1))
        if existing <= sample_size:
            return query, False
        # Replace existing LIMIT with sample_size
        new_query = query[: m.start()] + f"LIMIT {sample_size}" + query[m.end():]
        return new_query.strip(), True

    # No LIMIT clause — append one (before any ORDER BY tail is fine)
    return query.rstrip().rstrip(";") + f" LIMIT {sample_size}", True


def run_soql(
    query: str,
    target_org: str | None = None,
    sample_size: int = _SAMPLE_SIZE_DEFAULT,
) -> str:
    """
    Run a SOQL query with an enforced row cap and return a condensed result.

    Args:
        query:       SOQL query string (SELECT ... FROM ... WHERE ...)
        target_org:  Org alias or username
        sample_size: Maximum rows to return (default 3)

    Returns:
        Formatted result string with truncation notice if rows were clipped.
    """
    if not query.strip():
        return "Error: query is required."

    capped_query, was_capped = _inject_limit(query, sample_size)

    cmd = ["sf", "data", "query", "--query", capped_query, "--json"]
    if target_org:
        cmd += ["--target-org", target_org]

    logger.info("soql: %s", capped_query)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, shell=_SHELL)
    except FileNotFoundError:
        return "❌ sf CLI not found. Install: https://developer.salesforce.com/tools/salesforcecli"
    except subprocess.TimeoutExpired:
        return "❌ SOQL query timed out after 60s."

    raw = proc.stdout.strip() or proc.stderr.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return f"❌ sf returned non-JSON: {raw[:300]}"

    if data.get("status", 0) != 0:
        return f"❌ Query error: {data.get('message', 'Unknown error')}"

    result = data.get("result", {})
    records = result.get("records", [])
    total_size = result.get("totalSize", len(records))

    # Clean up internal Salesforce attributes from each record
    clean_records = []
    for rec in records[:sample_size]:
        cleaned = {k: v for k, v in rec.items() if k != "attributes"}
        clean_records.append(cleaned)

    lines: list[str] = []

    if was_capped and total_size > sample_size:
        lines.append(
            f"[rtk-sf: Showing {len(clean_records)} of {total_size} records. "
            f"Truncated to save tokens. Use sample_size param to adjust.]"
        )
    else:
        lines.append(f"[rtk-sf: {len(clean_records)} record(s) returned]")

    lines.append("")
    for i, rec in enumerate(clean_records, 1):
        lines.append(f"── Record {i} ──")
        for key, val in rec.items():
            lines.append(f"  {key}: {val}")
        lines.append("")

    return "\n".join(lines)
