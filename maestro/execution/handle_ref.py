"""Rebuild an ExecutionHandleRef from an execution_handles DB row.

Shared by Mode-1 StateRecovery and Mode-2 orchestrator recovery so the row->ref
translation has one definition.
"""

from datetime import datetime
from typing import Any

from maestro.execution.models import ExecutionHandleRef


def handle_ref_from_row(row: dict[str, Any]) -> ExecutionHandleRef:
    """Reconstruct a persisted execution ref from an `execution_handles` row."""
    return ExecutionHandleRef(
        backend_id=row["backend_id"],
        run_id=row["entity_id"],
        execution_id=row.get("execution_id"),
        transport_ref=row["transport_ref"],
        status_marker=row.get("status_marker"),
        started_at=datetime.fromisoformat(row["created_at"]),
        workdir_mirror_path=None,
        state_mirror_path=None,
    )
