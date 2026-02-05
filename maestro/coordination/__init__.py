"""Coordination APIs (MCP server and REST API).

This module provides MCP and REST APIs for agent coordination,
allowing AI agents to discover, claim, and execute tasks.
"""

from maestro.coordination.mcp_server import (
    ClaimResult,
    MCPServer,
    StatusUpdateResult,
    TaskResponse,
    TaskResultResponse,
    create_mcp_server,
    get_server,
    shutdown_server,
)


__all__ = [
    "ClaimResult",
    "MCPServer",
    "StatusUpdateResult",
    "TaskResponse",
    "TaskResultResponse",
    "create_mcp_server",
    "get_server",
    "shutdown_server",
]
