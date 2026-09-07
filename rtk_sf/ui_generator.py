"""
ui_generator.py — Generates a self-contained architecture map HTML file.

Reads `.rtk-sf/relations.json` and YAML specs, then produces
`dist/architecture_map.html` — a single-file SPA powered by Cytoscape.js
that visualizes the Salesforce component graph with interactive node selection,
path highlighting, and a YAML spec sidebar.

Usage:
    python3 -m rtk_sf ui [--project-root .] [--output dist/architecture_map.html]
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RTK_DIR = ".rtk-sf"
RELATIONS_FILE = "relations.json"
SPECS_DIR = "specs"

# Cytoscape.js CDN URL (loaded inline via CDN; no bundling required)
CYTOSCAPE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.34.2/cytoscape.min.js"

# furuCRM brand colors
COLOR_PRIMARY = "#0066cc"
COLOR_BG = "#1a1a2e"
COLOR_SURFACE = "#16213e"
COLOR_SURFACE2 = "#0f3460"
COLOR_TEXT = "#e0e0e0"
COLOR_TEXT_DIM = "#a0a0b0"
COLOR_NODE_APEX = "#3a86ff"
COLOR_NODE_OBJECT = "#ff006e"
COLOR_NODE_FIELD = "#8338ec"
COLOR_NODE_FLOW = "#fb5607"
COLOR_SELECTED = "#ff9f1c"
COLOR_UPSTREAM = "#ffbf69"
COLOR_DOWNSTREAM = "#ef233c"
COLOR_EDGE = "#4a4a6a"


def _load_relations(rtk_dir: Path) -> dict[str, Any]:
    """Load the relations graph from JSON."""
    relations_path = rtk_dir / RELATIONS_FILE
    if not relations_path.exists():
        logger.warning("Relations file not found: %s", relations_path)
        return {"nodes": [], "edges": []}
    with open(relations_path) as f:
        return json.load(f)


def _load_specs(specs_dir: Path) -> dict[str, str]:
    """Load all YAML specs into a dict: name → yaml_string."""
    specs: dict[str, str] = {}
    if not specs_dir.exists():
        return specs
    for yaml_file in specs_dir.glob("*.yaml"):
        name = yaml_file.stem
        try:
            specs[name] = yaml_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read spec %s: %s", yaml_file, exc)
    return specs


def _type_to_class(node_type: str) -> str:
    """Map Salesforce metadata type to a CSS class name for Cytoscape styling."""
    t = node_type.lower()
    if "apex" in t or t == "apexclass":
        return "apex"
    if "object" in t:
        return "object"
    if "field" in t:
        return "field"
    if "flow" in t:
        return "flow"
    if "lwc" in t or "lightning" in t or "aura" in t:
        return "lwc"
    if "profile" in t or "permission" in t:
        return "profile"
    if "prompt" in t or "genai" in t or "bot" in t or "ai" in t:
        return "agentforce"
    return "generic"


def _short_label(name: str) -> str:
    """Return a short display label: last segment after the last dot."""
    return name.rsplit(".", 1)[-1] if "." in name else name


def _nodes_from_specs(specs: dict[str, str]) -> list[dict[str, Any]]:
    """Build Cytoscape node elements directly from spec YAML when relations is empty.

    Nodes are sorted by type so the grid layout visually groups same-type nodes.
    """
    raw = []
    for name, yaml_text in specs.items():
        node_type = "Unknown"
        file_path = ""
        for line in yaml_text.split("\n")[:15]:
            if line.startswith("type:"):
                node_type = line.split(":", 1)[1].strip()
            elif line.startswith("file:"):
                file_path = line.split(":", 1)[1].strip()
            if node_type != "Unknown" and file_path:
                break
        raw.append((node_type, name, file_path))

    # Sort: ApexClass → CustomObject → Flow → LWC → CustomField → rest
    TYPE_ORDER = {
        "ApexClass": 0, "ApexTrigger": 1,
        "CustomObject": 2,
        "Flow": 3,
        "LightningComponentBundle": 4, "AuraDefinitionBundle": 5,
        "CustomField": 6,
    }
    raw.sort(key=lambda t: (TYPE_ORDER.get(t[0], 99), t[1].lower()))

    return [
        {
            "data": {
                "id": name,
                "label": _short_label(name),
                "fullName": name,
                "type": node_type,
                "file": file_path,
            },
            "classes": _type_to_class(node_type),
        }
        for node_type, name, file_path in raw
    ]


def _build_cytoscape_elements(
    relations: dict[str, Any],
    specs: dict[str, str],
) -> list[dict[str, Any]]:
    """Convert relations graph to Cytoscape.js elements array.

    Falls back to spec-based node list when relations.json has no nodes,
    so the architecture map is always populated even on first install.
    """
    elements: list[dict[str, Any]] = []

    nodes = relations.get("nodes", [])
    if not nodes and specs:
        # relations graph not yet built — render all specs as standalone nodes
        return _nodes_from_specs(specs)

    for node in nodes:
        node_id = node.get("id", "")
        node_type = node.get("type", "Unknown")
        elements.append(
            {
                "data": {
                    "id": node_id,
                    "label": _short_label(node_id),
                    "fullName": node_id,
                    "type": node_type,
                    "file": node.get("file", ""),
                },
                "classes": _type_to_class(node_type),
            }
        )

    for edge in relations.get("edges", []):
        source = edge.get("source", "")
        target = edge.get("target", "")
        relation = edge.get("relation", "references")
        edge_id = f"{source}__{target}__{relation}"
        elements.append(
            {
                "data": {
                    "id": edge_id,
                    "source": source,
                    "target": target,
                    "relation": relation,
                }
            }
        )

    return elements


def generate_html(
    project_root: str | Path = ".",
    output_path: str | Path | None = None,
) -> Path:
    """
    Generate the architecture map HTML file.

    Args:
        project_root: Root of the Salesforce project.
        output_path: Where to write the HTML. Defaults to dist/architecture_map.html.

    Returns:
        Path to the generated HTML file.
    """
    project_root = Path(project_root).resolve()
    rtk_dir = project_root / RTK_DIR
    specs_dir = rtk_dir / SPECS_DIR

    if output_path is None:
        dist_dir = project_root / "dist"
        dist_dir.mkdir(exist_ok=True)
        output_path = dist_dir / "architecture_map.html"
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    relations = _load_relations(rtk_dir)
    specs = _load_specs(specs_dir)
    elements = _build_cytoscape_elements(relations, specs)

    relation_nodes = len(relations.get("nodes", []))
    edge_count = len(relations.get("edges", []))
    node_count = relation_nodes if relation_nodes else len(specs)
    logger.info(
        "Generating architecture map: %d nodes, %d edges, %d specs%s",
        node_count, edge_count, len(specs),
        " (spec-fallback mode)" if not relation_nodes else "",
    )

    html = _render_html(elements, specs, node_count, edge_count)
    output_path.write_text(html, encoding="utf-8")
    logger.info("Architecture map written to: %s", output_path)
    return output_path


COLOR_NODE_LWC = "#06d6a0"
COLOR_NODE_PROFILE = "#adb5bd"
COLOR_NODE_AGENTFORCE = "#c77dff"


def _render_html(
    elements: list[dict[str, Any]],
    specs: dict[str, str],
    node_count: int,
    edge_count: int,
) -> str:
    """Render the complete self-contained HTML string."""
    elements_json = json.dumps(elements, indent=2, ensure_ascii=False)
    specs_json = json.dumps(specs, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>rtk-sf — Salesforce Architecture Map</title>
  <script src="{CYTOSCAPE_CDN}"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: {COLOR_BG}; color: {COLOR_TEXT};
      height: 100vh; display: flex; flex-direction: column; overflow: hidden;
    }}
    header {{
      background: {COLOR_SURFACE}; border-bottom: 1px solid {COLOR_SURFACE2};
      padding: 10px 16px; display: flex; align-items: center; gap: 12px; flex-shrink: 0;
    }}
    header h1 {{ font-size: 16px; font-weight: 700; color: {COLOR_PRIMARY}; white-space: nowrap; }}
    .stats {{ font-size: 11px; color: {COLOR_TEXT_DIM}; background: {COLOR_SURFACE2};
              padding: 3px 8px; border-radius: 10px; white-space: nowrap; }}
    .search-bar {{ display: flex; align-items: center; gap: 6px; flex: 1; }}
    #searchInput {{
      background: {COLOR_SURFACE2}; border: 1px solid #444; border-radius: 6px;
      color: {COLOR_TEXT}; padding: 5px 10px; font-size: 12px; flex: 1; max-width: 260px;
      outline: none; transition: border-color 0.2s;
    }}
    #searchInput:focus {{ border-color: {COLOR_PRIMARY}; }}
    #searchInput::placeholder {{ color: {COLOR_TEXT_DIM}; }}
    .hdr-btn {{
      background: {COLOR_SURFACE2}; border: 1px solid #444; border-radius: 6px;
      color: {COLOR_TEXT_DIM}; padding: 5px 9px; cursor: pointer; font-size: 11px;
      white-space: nowrap;
    }}
    .hdr-btn:hover {{ background: #1a3a60; color: {COLOR_TEXT}; }}
    .brand {{ font-size: 11px; color: {COLOR_TEXT_DIM}; margin-left: auto; white-space: nowrap; }}

    /* type filter bar */
    .filter-bar {{
      display: flex; gap: 6px; padding: 6px 16px;
      background: {COLOR_SURFACE}; border-bottom: 1px solid {COLOR_SURFACE2};
      flex-shrink: 0; flex-wrap: wrap;
    }}
    .filter-btn {{
      display: flex; align-items: center; gap: 5px;
      padding: 3px 10px; border-radius: 12px; border: 1px solid #444;
      background: transparent; color: {COLOR_TEXT_DIM}; cursor: pointer; font-size: 11px;
      transition: all 0.15s;
    }}
    .filter-btn .dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
    .filter-btn.active {{ border-color: currentColor; color: {COLOR_TEXT}; background: #ffffff12; }}
    .filter-btn:hover {{ color: {COLOR_TEXT}; }}

    .main {{ display: flex; flex: 1; overflow: hidden; position: relative; }}
    #cy {{ flex: 1; height: 100%; background: {COLOR_BG}; }}

    /* tooltip */
    #tooltip {{
      position: absolute; pointer-events: none; background: #0d1117ee;
      border: 1px solid {COLOR_SURFACE2}; border-radius: 6px;
      padding: 6px 10px; font-size: 11px; color: {COLOR_TEXT};
      max-width: 320px; word-break: break-all; z-index: 100;
      display: none; box-shadow: 0 4px 12px #0008;
    }}

    /* sidebar */
    #sidebar {{
      width: 380px; background: {COLOR_SURFACE};
      border-left: 1px solid {COLOR_SURFACE2};
      display: flex; flex-direction: column; overflow: hidden;
      transition: width 0.2s;
    }}
    #sidebar.collapsed {{ width: 0; overflow: hidden; }}
    .sidebar-header {{
      padding: 12px 16px; border-bottom: 1px solid {COLOR_SURFACE2};
      display: flex; align-items: center; justify-content: space-between; flex-shrink: 0;
    }}
    .sidebar-header h2 {{ font-size: 13px; font-weight: 600; color: {COLOR_TEXT}; flex: 1; min-width: 0; }}
    #closeSidebar {{
      background: transparent; border: none; color: {COLOR_TEXT_DIM};
      cursor: pointer; font-size: 18px; line-height: 1; padding: 0 4px; flex-shrink: 0;
    }}
    #closeSidebar:hover {{ color: {COLOR_TEXT}; }}
    .component-badge {{
      font-size: 10px; font-weight: 600; padding: 2px 7px;
      border-radius: 10px; text-transform: uppercase; letter-spacing: 0.4px;
    }}
    .badge-apex,.badge-apexclass,.badge-apextrigger {{ background:{COLOR_NODE_APEX}22; color:{COLOR_NODE_APEX}; border:1px solid {COLOR_NODE_APEX}44; }}
    .badge-object,.badge-customobject {{ background:{COLOR_NODE_OBJECT}22; color:{COLOR_NODE_OBJECT}; border:1px solid {COLOR_NODE_OBJECT}44; }}
    .badge-field,.badge-customfield {{ background:{COLOR_NODE_FIELD}22; color:{COLOR_NODE_FIELD}; border:1px solid {COLOR_NODE_FIELD}44; }}
    .badge-flow {{ background:{COLOR_NODE_FLOW}22; color:{COLOR_NODE_FLOW}; border:1px solid {COLOR_NODE_FLOW}44; }}
    .badge-lwc,.badge-lightningcomponentbundle,.badge-auradefinitionbundle {{ background:{COLOR_NODE_LWC}22; color:{COLOR_NODE_LWC}; border:1px solid {COLOR_NODE_LWC}44; }}
    .badge-agentforce,.badge-prompttemplate,.badge-genaiprompttemplate {{ background:{COLOR_NODE_AGENTFORCE}22; color:{COLOR_NODE_AGENTFORCE}; border:1px solid {COLOR_NODE_AGENTFORCE}44; }}

    #specContent {{ flex: 1; overflow-y: auto; padding: 14px; }}
    #specContent pre {{
      background: {COLOR_BG}; border: 1px solid {COLOR_SURFACE2}; border-radius: 6px;
      padding: 12px; font-size: 11px; line-height: 1.6;
      font-family: 'SF Mono','Fira Code','Cascadia Code',monospace;
      white-space: pre-wrap; word-break: break-word; color: #c9d1d9;
    }}
    .full-name-row {{
      font-size: 10px; color: {COLOR_TEXT_DIM}; margin-bottom: 10px;
      word-break: break-all; line-height: 1.5;
    }}
    .relations-section {{ margin-top: 14px; }}
    .relations-section h3 {{
      font-size: 11px; font-weight: 600; color: {COLOR_TEXT_DIM};
      text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 6px;
    }}
    .relations-section ul {{ list-style: none; }}
    .relations-section li {{
      font-size: 11px; padding: 3px 0; color: {COLOR_TEXT};
      cursor: pointer; display: flex; align-items: center; gap: 5px;
    }}
    .relations-section li:hover {{ color: {COLOR_PRIMARY}; }}
    .relations-section li::before {{ content: '→'; color: {COLOR_TEXT_DIM}; }}
    .empty-state {{
      display: flex; flex-direction: column; align-items: center;
      justify-content: center; height: 100%; gap: 10px;
      color: {COLOR_TEXT_DIM}; text-align: center; padding: 28px;
    }}
    .empty-state .icon {{ font-size: 36px; }}
    .empty-state p {{ font-size: 12px; line-height: 1.6; }}

    ::-webkit-scrollbar {{ width: 5px; }}
    ::-webkit-scrollbar-track {{ background: transparent; }}
    ::-webkit-scrollbar-thumb {{ background: {COLOR_SURFACE2}; border-radius: 3px; }}
  </style>
</head>
<body>

<header>
  <h1>rtk-sf  Architecture Map</h1>
  <div class="stats" id="statsEl">{node_count} components · {edge_count} relations</div>
  <div class="search-bar">
    <input type="text" id="searchInput" placeholder="Search components… (⌘K)" />
    <button class="hdr-btn" id="clearSearch">Clear</button>
    <button class="hdr-btn" id="layoutBtn">Re-layout</button>
    <button class="hdr-btn" id="fitBtn">Fit all</button>
  </div>
  <span class="brand">by furuCRM Inc.</span>
</header>

<div class="filter-bar" id="filterBar">
  <button class="filter-btn active" data-type="all">
    <span class="dot" style="background:#888"></span>All
    <span class="filter-count" id="cnt-all"></span>
  </button>
  <button class="filter-btn" data-type="ApexClass">
    <span class="dot" style="background:{COLOR_NODE_APEX}"></span>Apex
    <span class="filter-count" id="cnt-apex"></span>
  </button>
  <button class="filter-btn" data-type="CustomObject">
    <span class="dot" style="background:{COLOR_NODE_OBJECT}"></span>Object
    <span class="filter-count" id="cnt-object"></span>
  </button>
  <button class="filter-btn" data-type="Flow">
    <span class="dot" style="background:{COLOR_NODE_FLOW}"></span>Flow
    <span class="filter-count" id="cnt-flow"></span>
  </button>
  <button class="filter-btn" data-type="LightningComponentBundle">
    <span class="dot" style="background:{COLOR_NODE_LWC}"></span>LWC
    <span class="filter-count" id="cnt-lwc"></span>
  </button>
  <button class="filter-btn" data-type="CustomField">
    <span class="dot" style="background:{COLOR_NODE_FIELD}"></span>Field
    <span class="filter-count" id="cnt-field"></span>
  </button>
  <button class="filter-btn" data-type="other">
    <span class="dot" style="background:{COLOR_NODE_PROFILE}"></span>Other
    <span class="filter-count" id="cnt-other"></span>
  </button>
</div>

<div class="main">
  <div id="cy"></div>
  <div id="tooltip"></div>
  <div id="sidebar" class="collapsed">
    <div class="sidebar-header">
      <h2 id="sidebarTitle">Component Details</h2>
      <button id="closeSidebar">×</button>
    </div>
    <div id="specContent">
      <div class="empty-state">
        <div class="icon">🗂️</div>
        <p>Click any node to view its compressed YAML specification.</p>
      </div>
    </div>
  </div>
</div>

<script>
const ELEMENTS = {elements_json};
const SPECS = {specs_json};

// ── Color map ──────────────────────────────────────────────────────────
const TYPE_COLORS = {{
  apexclass:               '{COLOR_NODE_APEX}',
  apextrigger:             '{COLOR_NODE_APEX}',
  apex:                    '{COLOR_NODE_APEX}',
  customobject:            '{COLOR_NODE_OBJECT}',
  object:                  '{COLOR_NODE_OBJECT}',
  customfield:             '{COLOR_NODE_FIELD}',
  field:                   '{COLOR_NODE_FIELD}',
  flow:                    '{COLOR_NODE_FLOW}',
  lightningcomponentbundle:'{COLOR_NODE_LWC}',
  auradefinitionbundle:    '{COLOR_NODE_LWC}',
  lwc:                     '{COLOR_NODE_LWC}',
  prompttemplate:          '{COLOR_NODE_AGENTFORCE}',
  genaiprompttemplate:     '{COLOR_NODE_AGENTFORCE}',
  genaifunction:           '{COLOR_NODE_AGENTFORCE}',
  aiapplication:           '{COLOR_NODE_AGENTFORCE}',
  bot:                     '{COLOR_NODE_AGENTFORCE}',
  botversion:              '{COLOR_NODE_AGENTFORCE}',
  agentforce:              '{COLOR_NODE_AGENTFORCE}',
  profile:                 '{COLOR_NODE_PROFILE}',
  permissionset:           '{COLOR_NODE_PROFILE}',
}};
function nodeColor(type) {{
  return TYPE_COLORS[(type||'').toLowerCase().replace(/\\s+/g,'')] || '#607080';
}}

// ── Cytoscape ─────────────────────────────────────────────────────────
const cy = cytoscape({{
  container: document.getElementById('cy'),
  elements: ELEMENTS,
  style: [
    {{
      selector: 'node',
      style: {{
        'background-color': ele => nodeColor(ele.data('type')),
        'label': 'data(label)',
        'color': '{COLOR_TEXT}',
        'font-size': '10px',
        'text-valign': 'bottom',
        'text-halign': 'center',
        'text-margin-y': '3px',
        'text-outline-color': '{COLOR_BG}',
        'text-outline-width': '2px',
        'width': '28px',
        'height': '28px',
        'border-width': '2px',
        'border-color': ele => nodeColor(ele.data('type')) + '99',
        'transition-property': 'background-color, border-color, width, height, opacity',
        'transition-duration': '0.12s',
      }}
    }},
    {{ selector: 'node.selected', style: {{
      'background-color': '{COLOR_SELECTED}', 'border-color': '{COLOR_SELECTED}',
      'width': '38px', 'height': '38px', 'z-index': 10,
    }} }},
    {{ selector: 'node.upstream', style: {{
      'background-color': '{COLOR_UPSTREAM}', 'border-color': '{COLOR_UPSTREAM}',
    }} }},
    {{ selector: 'node.downstream', style: {{
      'background-color': '{COLOR_DOWNSTREAM}', 'border-color': '{COLOR_DOWNSTREAM}',
    }} }},
    {{ selector: 'node.dimmed', style: {{ 'opacity': 0.12 }} }},
    {{ selector: 'node.hidden',  style: {{ 'display': 'none' }} }},
    {{ selector: 'edge', style: {{
      'width': 1.5, 'line-color': '{COLOR_EDGE}',
      'target-arrow-color': '{COLOR_EDGE}', 'target-arrow-shape': 'triangle',
      'curve-style': 'bezier', 'opacity': 0.6,
    }} }},
    {{ selector: 'edge.highlighted', style: {{
      'line-color': '{COLOR_SELECTED}', 'target-arrow-color': '{COLOR_SELECTED}',
      'opacity': 1, 'width': 2.5,
    }} }},
    {{ selector: 'edge.dimmed', style: {{ 'opacity': 0.04 }} }},
  ],
  layout: {{ name: 'grid', animate: false, condense: false, avoidOverlapPadding: 8 }},
  minZoom: 0.05, maxZoom: 6, wheelSensitivity: 0.25,
}});

// ── Zoom-adaptive labels ───────────────────────────────────────────────
const LABEL_ZOOM_THRESHOLD = 0.5;
function updateLabels() {{
  const show = cy.zoom() >= LABEL_ZOOM_THRESHOLD;
  cy.style().selector('node').style('font-size', show ? '10px' : '0px').update();
}}
cy.on('zoom', updateLabels);
updateLabels();

// ── Tooltip ────────────────────────────────────────────────────────────
const tooltip = document.getElementById('tooltip');
cy.on('mouseover', 'node', evt => {{
  const n = evt.target;
  const pos = evt.renderedPosition;
  tooltip.textContent = n.data('fullName') || n.id();
  tooltip.style.display = 'block';
  tooltip.style.left = (pos.x + 14) + 'px';
  tooltip.style.top  = (pos.y - 10) + 'px';
}});
cy.on('mousemove', 'node', evt => {{
  const pos = evt.renderedPosition;
  tooltip.style.left = (pos.x + 14) + 'px';
  tooltip.style.top  = (pos.y - 10) + 'px';
}});
cy.on('mouseout', 'node', () => {{ tooltip.style.display = 'none'; }});

// ── Sidebar ────────────────────────────────────────────────────────────
const sidebar     = document.getElementById('sidebar');
const sidebarTitle = document.getElementById('sidebarTitle');
const specContent = document.getElementById('specContent');

function escapeHtml(s) {{
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}
function badgeHtml(type) {{
  const cls = (type||'unknown').toLowerCase().replace(/\\s+/g,'');
  return `<span class="component-badge badge-${{cls}}">${{type||'Unknown'}}</span>`;
}}

function showSpec(nodeId, nodeType) {{
  const spec = SPECS[nodeId];
  const fullName = cy.$(`#${{nodeId}}`).data('fullName') || nodeId;
  sidebarTitle.innerHTML = badgeHtml(nodeType);

  const upstream   = cy.edges(`[target = "${{nodeId}}"]`).map(e => e.source().id());
  const downstream = cy.edges(`[source = "${{nodeId}}"]`).map(e => e.target().id());
  let relHtml = '';
  if (upstream.length)   relHtml += `<div class="relations-section"><h3>Upstream (${{upstream.length}})</h3><ul>${{upstream.map(n=>`<li onclick="selectNode('${{n}}')">${{n}}</li>`).join('')}}</ul></div>`;
  if (downstream.length) relHtml += `<div class="relations-section"><h3>Downstream (${{downstream.length}})</h3><ul>${{downstream.map(n=>`<li onclick="selectNode('${{n}}')">${{n}}</li>`).join('')}}</ul></div>`;

  const specHtml = spec
    ? `<pre>${{escapeHtml(spec)}}</pre>`
    : `<div class="empty-state"><p>No YAML spec for <strong>${{nodeId}}</strong></p></div>`;

  specContent.innerHTML = `<div class="full-name-row">${{escapeHtml(fullName)}}</div>${{specHtml}}${{relHtml}}`;
  sidebar.classList.remove('collapsed');
}}

document.getElementById('closeSidebar').addEventListener('click', () => {{
  sidebar.classList.add('collapsed'); clearHighlights();
}});

// ── Selection ──────────────────────────────────────────────────────────
function clearHighlights() {{
  cy.elements().removeClass('selected upstream downstream dimmed highlighted');
}}
function selectNode(nodeId) {{
  const node = cy.$(`#${{CSS.escape(nodeId)}}`);
  if (node.empty()) return;
  clearHighlights();
  cy.elements().addClass('dimmed');
  node.addClass('selected').removeClass('dimmed');
  node.predecessors('node').addClass('upstream').removeClass('dimmed');
  node.successors('node').addClass('downstream').removeClass('dimmed');
  node.connectedEdges().addClass('highlighted').removeClass('dimmed');
  showSpec(nodeId, node.data('type'));
  cy.animate({{ center: {{ eles: node }}, duration: 250 }});
}}

cy.on('tap', 'node', evt => selectNode(evt.target.id()));
cy.on('tap', evt => {{
  if (evt.target === cy) {{ clearHighlights(); sidebar.classList.add('collapsed'); }}
}});

// ── Type filter buttons ────────────────────────────────────────────────
const KNOWN_TYPES = ['ApexClass','CustomObject','Flow','LightningComponentBundle','CustomField'];
const OTHER_BUCKET = new Set();
let activeFilter = 'all';

// Count per type and populate badges
const typeCounts = {{}};
cy.nodes().forEach(n => {{
  const t = n.data('type') || 'Unknown';
  typeCounts[t] = (typeCounts[t] || 0) + 1;
  if (!KNOWN_TYPES.includes(t)) OTHER_BUCKET.add(t);
}});

document.getElementById('cnt-all').textContent   = ` (${{cy.nodes().length}})`;
document.getElementById('cnt-apex').textContent   = ` (${{(typeCounts['ApexClass']||0)+(typeCounts['ApexTrigger']||0)}})`;
document.getElementById('cnt-object').textContent = ` (${{typeCounts['CustomObject']||0}})`;
document.getElementById('cnt-flow').textContent   = ` (${{typeCounts['Flow']||0}})`;
document.getElementById('cnt-lwc').textContent    = ` (${{(typeCounts['LightningComponentBundle']||0)+(typeCounts['AuraDefinitionBundle']||0)}})`;
document.getElementById('cnt-field').textContent  = ` (${{typeCounts['CustomField']||0}})`;
let otherCount = 0;
OTHER_BUCKET.forEach(t => {{ otherCount += typeCounts[t]||0; }});
document.getElementById('cnt-other').textContent  = ` (${{otherCount}})`;

document.getElementById('filterBar').addEventListener('click', e => {{
  const btn = e.target.closest('.filter-btn');
  if (!btn) return;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  activeFilter = btn.dataset.type;
  applyFilter();
}});

function applyFilter() {{
  cy.nodes().forEach(n => {{
    const t = n.data('type') || 'Unknown';
    let show = false;
    if (activeFilter === 'all') show = true;
    else if (activeFilter === 'ApexClass') show = (t === 'ApexClass' || t === 'ApexTrigger');
    else if (activeFilter === 'LightningComponentBundle') show = (t === 'LightningComponentBundle' || t === 'AuraDefinitionBundle');
    else if (activeFilter === 'other') show = OTHER_BUCKET.has(t);
    else show = (t === activeFilter);
    if (show) n.removeClass('hidden'); else n.addClass('hidden');
  }});
  // Update stats
  const visible = cy.nodes().not('.hidden').length;
  document.getElementById('statsEl').textContent = `${{visible}} shown · ${{cy.nodes().length}} total`;
}}

// ── Search ─────────────────────────────────────────────────────────────
const searchInput = document.getElementById('searchInput');
searchInput.addEventListener('input', () => {{
  const q = searchInput.value.trim().toLowerCase();
  if (!q) {{ cy.elements().removeClass('dimmed'); return; }}
  cy.nodes().forEach(n => {{
    const match = (n.data('fullName')||n.id()).toLowerCase().includes(q)
               || (n.data('label')||'').toLowerCase().includes(q);
    if (match) n.removeClass('dimmed'); else n.addClass('dimmed');
  }});
  cy.edges().addClass('dimmed');
}});
document.getElementById('clearSearch').addEventListener('click', () => {{
  searchInput.value = ''; cy.elements().removeClass('dimmed');
}});

// ── Layout ─────────────────────────────────────────────────────────────
function runLayout() {{
  const hasEdges = cy.edges().length > 0;
  cy.layout(hasEdges
    ? {{ name:'cose', animate:true, animationDuration:600, randomize:false, nodeRepulsion:()=>12000, idealEdgeLength:()=>80, gravity:0.2, numIter:600 }}
    : {{ name:'grid', animate:true, animationDuration:400, condense:false, avoidOverlapPadding:8 }}
  ).run();
}}
document.getElementById('layoutBtn').addEventListener('click', runLayout);
document.getElementById('fitBtn').addEventListener('click', () => cy.fit(undefined, 40));

// ── Keyboard ───────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {{
  if (e.key === 'Escape') {{ clearHighlights(); sidebar.classList.add('collapsed'); }}
  if ((e.metaKey||e.ctrlKey) && e.key==='k') {{ e.preventDefault(); searchInput.focus(); }}
}});

// ── Init ───────────────────────────────────────────────────────────────
cy.ready(() => {{ cy.fit(undefined, 40); }});
</script>
</body>
</html>"""
