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
from maestro.coordination.rest_api import (
    AvailableTaskItem,
    AvailableTasksResponse,
    ClaimRequest,
    HealthResponse,
    RESTServer,
    StatusUpdateRequest,
    TaskListResponse,
    create_app_with_lifespan,
    create_rest_server,
)


__all__ = [
    "AvailableTaskItem",
    "AvailableTasksResponse",
    "ClaimRequest",
    "ClaimResult",
    "HealthResponse",
    "MCPServer",
    "RESTServer",
    "StatusUpdateRequest",
    "StatusUpdateResult",
    "TaskListResponse",
    "TaskResponse",
    "TaskResultResponse",
    "create_app_with_lifespan",
    "create_mcp_server",
    "create_rest_server",
    "get_server",
    "shutdown_server",
]
