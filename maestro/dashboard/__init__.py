"""Web dashboard for Maestro task visualization.

Provides a browser-based UI with DAG visualization (Mermaid.js),
real-time status updates (SSE), retry controls, and log viewing.
"""

from maestro.dashboard.app import DashboardServer, create_dashboard_app


__all__ = ["DashboardServer", "create_dashboard_app"]
