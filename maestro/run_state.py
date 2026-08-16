"""The run row and the single place that decides what a run's state means.

Liveness is *observed*, never inferred from a NULL: a running run and a killed
run both have `ended_at IS NULL` (spec §B.3). The stage lock proves that an
orchestration stage is live **in this repository**; the holder's run id is what
attributes that liveness to a particular run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal


if TYPE_CHECKING:
    from collections.abc import Mapping


RunStatus = Literal[
    "running",
    "interrupted",
    "suspended",
    "completed",
    "cancelled",
    "superseded",
    "failed",
    "legacy",
]


@dataclass(frozen=True)
class RunRow:
    run_id: str
    repo_key: str
    started_at: str
    outcome: str | None = None
    ended_at: str | None = None
    reason: str | None = None
    suspended_at: str | None = None
    suspend_reason: str | None = None


def run_row_from_mapping(mapping: Mapping[str, object]) -> RunRow:
    return RunRow(
        run_id=str(mapping["run_id"]),
        repo_key=str(mapping["repo_key"]),
        started_at=str(mapping["started_at"]),
        outcome=_opt(mapping.get("outcome")),
        ended_at=_opt(mapping.get("ended_at")),
        reason=_opt(mapping.get("reason")),
        suspended_at=_opt(mapping.get("suspended_at")),
        suspend_reason=_opt(mapping.get("suspend_reason")),
    )


def _opt(value: object) -> str | None:
    return None if value is None else str(value)


def classify_run(row: RunRow | None, *, lock_holder_run_id: str | None) -> RunStatus:
    """Spec §B.3. Order matters: terminal, then observed liveness, then pause."""
    if row is None:
        return "legacy"
    if row.outcome is not None:
        return row.outcome  # type: ignore[return-value]
    if lock_holder_run_id is not None and lock_holder_run_id == row.run_id:
        return "running"
    if row.suspended_at is not None:
        return "suspended"
    return "interrupted"
