"""
ui_generator.py — Generates a self-contained architecture map HTML file.

Reads `.rtk-sf/relations.json` and YAML specs, then produces
`dist/architecture_map.html` — a single-file SPA powered by Cytoscape.js
that visualizes the Salesforce component graph with interactive node selection,
path highlighting, and a YAML spec sidebar.

Usage:
    python -m rtk_sf ui [--project-root .] [--output dist/architecture_map.html]
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
CYTOSCAPE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"

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


def _build_cytoscape_elements(relations: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert relations graph to Cytoscape.js elements array."""
    elements: list[dict[str, Any]] = []

    for node in relations.get("nodes", []):
        node_id = node.get("id", "")
        node_type = node.get("type", "Unknown")
        elements.append(
            {
                "data": {
                    "id": node_id,
                    "label": node_id,
                    "type": node_type,
                    "file": node.get("file", ""),
                },
                "classes": node_type.lower().replace("custom", "").replace("class", "apex"),
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
    elements = _build_cytoscape_elements(relations)

    node_count = len(relations.get("nodes", []))
    edge_count = len(relations.get("edges", []))
    logger.info(
        "Generating architecture map: %d nodes, %d edges, %d specs",
        node_count, edge_count, len(specs),
    )

    html = _render_html(elements, specs, node_count, edge_count)
    output_path.write_text(html, encoding="utf-8")
    logger.info("Architecture map written to: %s", output_path)
    return output_path


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
      background: {COLOR_BG};
      color: {COLOR_TEXT};
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}

    header {{
      background: {COLOR_SURFACE};
      border-bottom: 1px solid {COLOR_SURFACE2};
      padding: 12px 20px;
      display: flex;
      align-items: center;
      gap: 16px;
      flex-shrink: 0;
    }}

    header h1 {{
      font-size: 18px;
      font-weight: 700;
      color: {COLOR_PRIMARY};
      letter-spacing: -0.5px;
    }}

    header .brand {{
      font-size: 12px;
      color: {COLOR_TEXT_DIM};
      margin-left: auto;
    }}

    header .stats {{
      font-size: 12px;
      color: {COLOR_TEXT_DIM};
      background: {COLOR_SURFACE2};
      padding: 4px 10px;
      border-radius: 12px;
    }}

    .search-bar {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}

    #searchInput {{
      background: {COLOR_SURFACE2};
      border: 1px solid #333;
      border-radius: 6px;
      color: {COLOR_TEXT};
      padding: 6px 12px;
      font-size: 13px;
      width: 220px;
      outline: none;
      transition: border-color 0.2s;
    }}

    #searchInput:focus {{ border-color: {COLOR_PRIMARY}; }}
    #searchInput::placeholder {{ color: {COLOR_TEXT_DIM}; }}

    #clearSearch {{
      background: transparent;
      border: 1px solid #444;
      border-radius: 6px;
      color: {COLOR_TEXT_DIM};
      padding: 6px 10px;
      cursor: pointer;
      font-size: 12px;
    }}
    #clearSearch:hover {{ background: {COLOR_SURFACE2}; }}

    .main {{
      display: flex;
      flex: 1;
      overflow: hidden;
    }}

    #cy {{
      flex: 1;
      height: 100%;
      background: {COLOR_BG};
    }}

    #sidebar {{
      width: 380px;
      background: {COLOR_SURFACE};
      border-left: 1px solid {COLOR_SURFACE2};
      display: flex;
      flex-direction: column;
      overflow: hidden;
      transition: width 0.2s;
    }}

    #sidebar.collapsed {{ width: 0; }}

    .sidebar-header {{
      padding: 16px;
      border-bottom: 1px solid {COLOR_SURFACE2};
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-shrink: 0;
    }}

    .sidebar-header h2 {{
      font-size: 14px;
      font-weight: 600;
      color: {COLOR_TEXT};
    }}

    #closeSidebar {{
      background: transparent;
      border: none;
      color: {COLOR_TEXT_DIM};
      cursor: pointer;
      font-size: 18px;
      line-height: 1;
    }}
    #closeSidebar:hover {{ color: {COLOR_TEXT}; }}

    .component-badge {{
      font-size: 11px;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 10px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}

    .badge-apexclass {{ background: {COLOR_NODE_APEX}22; color: {COLOR_NODE_APEX}; border: 1px solid {COLOR_NODE_APEX}44; }}
    .badge-customobject {{ background: {COLOR_NODE_OBJECT}22; color: {COLOR_NODE_OBJECT}; border: 1px solid {COLOR_NODE_OBJECT}44; }}
    .badge-customfield {{ background: {COLOR_NODE_FIELD}22; color: {COLOR_NODE_FIELD}; border: 1px solid {COLOR_NODE_FIELD}44; }}
    .badge-flow {{ background: {COLOR_NODE_FLOW}22; color: {COLOR_NODE_FLOW}; border: 1px solid {COLOR_NODE_FLOW}44; }}

    #specContent {{
      flex: 1;
      overflow-y: auto;
      padding: 16px;
    }}

    #specContent pre {{
      background: {COLOR_BG};
      border: 1px solid {COLOR_SURFACE2};
      border-radius: 6px;
      padding: 14px;
      font-size: 12px;
      line-height: 1.6;
      font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
      white-space: pre-wrap;
      word-break: break-word;
      color: #c9d1d9;
      overflow-x: auto;
    }}

    .relations-section {{
      margin-top: 16px;
    }}

    .relations-section h3 {{
      font-size: 12px;
      font-weight: 600;
      color: {COLOR_TEXT_DIM};
      text-transform: uppercase;
      letter-spacing: 0.8px;
      margin-bottom: 8px;
    }}

    .relations-section ul {{
      list-style: none;
      padding: 0;
    }}

    .relations-section li {{
      font-size: 12px;
      padding: 4px 0;
      color: {COLOR_TEXT};
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
    }}

    .relations-section li:hover {{ color: {COLOR_PRIMARY}; }}
    .relations-section li::before {{ content: '→'; color: {COLOR_TEXT_DIM}; }}

    .empty-state {{
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: 100%;
      gap: 12px;
      color: {COLOR_TEXT_DIM};
      text-align: center;
      padding: 32px;
    }}

    .empty-state .icon {{ font-size: 40px; }}
    .empty-state p {{ font-size: 13px; line-height: 1.6; }}

    .legend {{
      display: flex;
      gap: 12px;
      padding: 8px 20px;
      background: {COLOR_SURFACE};
      border-top: 1px solid {COLOR_SURFACE2};
      flex-shrink: 0;
      flex-wrap: wrap;
    }}

    .legend-item {{
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 11px;
      color: {COLOR_TEXT_DIM};
    }}

    .legend-dot {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      flex-shrink: 0;
    }}

    #layoutBtn {{
      background: {COLOR_SURFACE2};
      border: 1px solid #444;
      border-radius: 6px;
      color: {COLOR_TEXT};
      padding: 6px 10px;
      cursor: pointer;
      font-size: 12px;
    }}
    #layoutBtn:hover {{ background: #1a3a60; }}

    ::-webkit-scrollbar {{ width: 6px; }}
    ::-webkit-scrollbar-track {{ background: transparent; }}
    ::-webkit-scrollbar-thumb {{ background: {COLOR_SURFACE2}; border-radius: 3px; }}
  </style>
</head>
<body>

<header>
  <h1>rtk-sf  Architecture Map</h1>
  <div class="stats">{node_count} components · {edge_count} relations</div>
  <div class="search-bar">
    <input type="text" id="searchInput" placeholder="Search components..." />
    <button id="clearSearch">Clear</button>
    <button id="layoutBtn">Re-layout</button>
  </div>
  <span class="brand">by furuCRM Inc.</span>
</header>

<div class="main">
  <div id="cy"></div>
  <div id="sidebar">
    <div class="sidebar-header">
      <h2 id="sidebarTitle">Component Details</h2>
      <button id="closeSidebar">×</button>
    </div>
    <div id="specContent">
      <div class="empty-state">
        <div class="icon">🗂️</div>
        <p>Click any node in the graph to view its compressed YAML specification.</p>
      </div>
    </div>
  </div>
</div>

<div class="legend">
  <div class="legend-item"><div class="legend-dot" style="background:{COLOR_NODE_APEX}"></div>Apex Class</div>
  <div class="legend-item"><div class="legend-dot" style="background:{COLOR_NODE_OBJECT}"></div>Custom Object</div>
  <div class="legend-item"><div class="legend-dot" style="background:{COLOR_NODE_FIELD}"></div>Custom Field</div>
  <div class="legend-item"><div class="legend-dot" style="background:{COLOR_NODE_FLOW}"></div>Flow</div>
  <div class="legend-item"><div class="legend-dot" style="background:{COLOR_SELECTED}"></div>Selected</div>
  <div class="legend-item"><div class="legend-dot" style="background:{COLOR_UPSTREAM}"></div>Upstream</div>
  <div class="legend-item"><div class="legend-dot" style="background:{COLOR_DOWNSTREAM}"></div>Downstream</div>
</div>

<script>
// -----------------------------------------------------------------------
// Data embedded at generation time
// -----------------------------------------------------------------------
const ELEMENTS = {elements_json};
const SPECS = {specs_json};

// -----------------------------------------------------------------------
// Color helpers
// -----------------------------------------------------------------------
const TYPE_COLORS = {{
  apexclass:    '{COLOR_NODE_APEX}',
  apex:         '{COLOR_NODE_APEX}',
  customobject: '{COLOR_NODE_OBJECT}',
  customfield:  '{COLOR_NODE_FIELD}',
  flow:         '{COLOR_NODE_FLOW}',
}};

function nodeColor(type) {{
  return TYPE_COLORS[(type || '').toLowerCase()] || '#666';
}}

// -----------------------------------------------------------------------
// Cytoscape initialization
// -----------------------------------------------------------------------
const cy = cytoscape({{
  container: document.getElementById('cy'),
  elements: ELEMENTS,
  style: [
    {{
      selector: 'node',
      style: {{
        'background-color': (ele) => nodeColor(ele.data('type')),
        'label': 'data(label)',
        'color': '{COLOR_TEXT}',
        'font-size': '11px',
        'text-valign': 'bottom',
        'text-halign': 'center',
        'text-margin-y': '4px',
        'text-outline-color': '{COLOR_BG}',
        'text-outline-width': '2px',
        'width': '32px',
        'height': '32px',
        'border-width': '2px',
        'border-color': (ele) => nodeColor(ele.data('type')) + '88',
        'transition-property': 'background-color, border-color, width, height',
        'transition-duration': '0.15s',
      }}
    }},
    {{
      selector: 'node.selected',
      style: {{
        'background-color': '{COLOR_SELECTED}',
        'border-color': '{COLOR_SELECTED}',
        'width': '42px',
        'height': '42px',
        'z-index': 10,
      }}
    }},
    {{
      selector: 'node.upstream',
      style: {{
        'background-color': '{COLOR_UPSTREAM}',
        'border-color': '{COLOR_UPSTREAM}',
      }}
    }},
    {{
      selector: 'node.downstream',
      style: {{
        'background-color': '{COLOR_DOWNSTREAM}',
        'border-color': '{COLOR_DOWNSTREAM}',
      }}
    }},
    {{
      selector: 'node.dimmed',
      style: {{
        'opacity': 0.2,
      }}
    }},
    {{
      selector: 'edge',
      style: {{
        'width': 1.5,
        'line-color': '{COLOR_EDGE}',
        'target-arrow-color': '{COLOR_EDGE}',
        'target-arrow-shape': 'triangle',
        'curve-style': 'bezier',
        'opacity': 0.7,
        'transition-property': 'line-color, opacity',
        'transition-duration': '0.15s',
      }}
    }},
    {{
      selector: 'edge.highlighted',
      style: {{
        'line-color': '{COLOR_SELECTED}',
        'target-arrow-color': '{COLOR_SELECTED}',
        'opacity': 1,
        'width': 2.5,
      }}
    }},
    {{
      selector: 'edge.dimmed',
      style: {{ 'opacity': 0.05 }}
    }},
  ],
  layout: {{
    name: 'cose',
    animate: false,
    randomize: true,
    nodeRepulsion: () => 8000,
    idealEdgeLength: () => 100,
    gravity: 0.25,
    numIter: 1000,
  }},
  minZoom: 0.1,
  maxZoom: 4,
  wheelSensitivity: 0.3,
}});

// -----------------------------------------------------------------------
// Sidebar
// -----------------------------------------------------------------------
const sidebar = document.getElementById('sidebar');
const sidebarTitle = document.getElementById('sidebarTitle');
const specContent = document.getElementById('specContent');

function badgeHtml(type) {{
  const cls = (type || 'unknown').toLowerCase().replace(/\\s+/g, '');
  return `<span class="component-badge badge-${{cls}}">${{type || 'Unknown'}}</span>`;
}}

function showSpec(nodeId, nodeType) {{
  const spec = SPECS[nodeId];
  sidebarTitle.innerHTML = `${{badgeHtml(nodeType)}} ${{nodeId}}`;

  // Build upstream/downstream lists
  const upstream = cy.edges(`[target = "${{nodeId}}"]`).map(e => e.source().id());
  const downstream = cy.edges(`[source = "${{nodeId}}"]`).map(e => e.target().id());

  let relHtml = '';
  if (upstream.length > 0) {{
    relHtml += `<div class="relations-section">
      <h3>Upstream callers (${{upstream.length}})</h3>
      <ul>${{upstream.map(n => `<li data-node="${{n}}" onclick="selectNode('${{n}}')">${{n}}</li>`).join('')}}</ul>
    </div>`;
  }}
  if (downstream.length > 0) {{
    relHtml += `<div class="relations-section">
      <h3>Downstream deps (${{downstream.length}})</h3>
      <ul>${{downstream.map(n => `<li data-node="${{n}}" onclick="selectNode('${{n}}')">${{n}}</li>`).join('')}}</ul>
    </div>`;
  }}

  if (spec) {{
    specContent.innerHTML = `<pre>${{escapeHtml(spec)}}</pre>${{relHtml}}`;
  }} else {{
    specContent.innerHTML = `
      <div class="empty-state">
        <p>No YAML spec found for <strong>${{nodeId}}</strong>.<br/>
        Run <code>rtk-sf index</code> to regenerate specs.</p>
      </div>${{relHtml}}`;
  }}

  sidebar.classList.remove('collapsed');
}}

function escapeHtml(str) {{
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}}

document.getElementById('closeSidebar').addEventListener('click', () => {{
  sidebar.classList.add('collapsed');
  clearHighlights();
}});

// -----------------------------------------------------------------------
// Node selection and path highlighting
// -----------------------------------------------------------------------
function clearHighlights() {{
  cy.elements().removeClass('selected upstream downstream dimmed highlighted');
}}

function selectNode(nodeId) {{
  const node = cy.$(`#${{nodeId}}`);
  if (node.empty()) return;

  clearHighlights();

  const upstreamNodes = node.predecessors('node');
  const downstreamNodes = node.successors('node');
  const relatedEdges = node.connectedEdges();

  cy.elements().addClass('dimmed');
  cy.elements().removeClass('selected upstream downstream highlighted');

  node.addClass('selected');
  node.removeClass('dimmed');
  upstreamNodes.addClass('upstream').removeClass('dimmed');
  downstreamNodes.addClass('downstream').removeClass('dimmed');
  relatedEdges.addClass('highlighted').removeClass('dimmed');

  showSpec(nodeId, node.data('type'));

  // Pan to node
  cy.animate({{
    center: {{ eles: node }},
    duration: 300,
  }});
}}

cy.on('tap', 'node', (evt) => {{
  selectNode(evt.target.id());
}});

cy.on('tap', (evt) => {{
  if (evt.target === cy) {{
    clearHighlights();
    sidebar.classList.add('collapsed');
  }}
}});

// -----------------------------------------------------------------------
// Search / filter
// -----------------------------------------------------------------------
const searchInput = document.getElementById('searchInput');
const clearSearch = document.getElementById('clearSearch');

searchInput.addEventListener('input', () => {{
  const q = searchInput.value.trim().toLowerCase();
  if (!q) {{
    cy.elements().removeClass('dimmed');
    return;
  }}
  cy.nodes().forEach(n => {{
    const matches = n.id().toLowerCase().includes(q);
    if (matches) n.removeClass('dimmed');
    else n.addClass('dimmed');
  }});
  cy.edges().addClass('dimmed');
}});

clearSearch.addEventListener('click', () => {{
  searchInput.value = '';
  cy.elements().removeClass('dimmed');
}});

// -----------------------------------------------------------------------
// Re-layout button
// -----------------------------------------------------------------------
document.getElementById('layoutBtn').addEventListener('click', () => {{
  cy.layout({{
    name: 'cose',
    animate: true,
    animationDuration: 800,
    randomize: true,
    nodeRepulsion: () => 8000,
    idealEdgeLength: () => 100,
    gravity: 0.25,
    numIter: 1000,
  }}).run();
}});

// -----------------------------------------------------------------------
// Keyboard shortcuts
// -----------------------------------------------------------------------
document.addEventListener('keydown', (e) => {{
  if (e.key === 'Escape') {{
    clearHighlights();
    sidebar.classList.add('collapsed');
  }}
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') {{
    e.preventDefault();
    searchInput.focus();
  }}
  if (e.key === 'f' && !e.ctrlKey && !e.metaKey && document.activeElement !== searchInput) {{
    searchInput.focus();
  }}
}});

// -----------------------------------------------------------------------
// Initial fit
// -----------------------------------------------------------------------
cy.ready(() => {{
  cy.fit(undefined, 40);
}});
</script>
</body>
</html>"""
