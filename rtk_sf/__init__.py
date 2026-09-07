"""
rtk-sf: Zero-Token Knowledge & Visual Live-Mapping Layer for Salesforce AI Agents.

Indexes Salesforce metadata (Apex classes, custom objects, fields, flows) into
compressed YAML specs and serves them via MCP to AI agents like Claude Code and Cline,
dramatically reducing token consumption.
"""

__version__ = "0.4.1"
__author__ = "furuCRM Inc."
__email__ = "dev@furucrm.com"
__license__ = "MIT"

from rtk_sf.indexer import SalesforceIndexer
from rtk_sf.search import SearchEngine
from rtk_sf.mcp_server import MCPServer

__all__ = ["SalesforceIndexer", "SearchEngine", "MCPServer", "__version__"]
