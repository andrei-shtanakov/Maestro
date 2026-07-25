"""WorkCorrelation v1 — reference implementation of the contract.

Contract: `contracts/work-correlation/schema.json` (+ rationale.md).
Maestro mints `work_item_id` (its own task/workstream id); `status` is a
surjective, deliberately lossy projection of each source vocabulary onto a
minimal common enum, with `source_status` kept verbatim. arbiter's
`decisions.action` vocabulary is out of scope (PolicyDecisionRef, phase 2).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from maestro.models import ExecutorTaskStatus, TaskStatus, WorkstreamStatus


SCHEMA_VERSION = "1"


class CommonStatus(StrEnum):
    """Minimal common status enum of the WorkCorrelation contract."""

    PENDING = "pending"
    RUNNING = "running"
    NEEDS_REVIEW = "needs_review"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Reachable from any non-terminal state.
UNIVERSAL_EXITS = frozenset({CommonStatus.FAILED, CommonStatus.CANCELLED})
#: Where recovery (retry) re-enters.
RECOVERY = CommonStatus.PENDING
#: No transitions out.
TERMINAL = frozenset({CommonStatus.DONE, CommonStatus.CANCELLED})

_TRANSITIONS: dict[CommonStatus, frozenset[CommonStatus]] = {
    CommonStatus.PENDING: frozenset({CommonStatus.RUNNING, CommonStatus.NEEDS_REVIEW}),
    CommonStatus.RUNNING: frozenset({CommonStatus.DONE, CommonStatus.NEEDS_REVIEW}),
    CommonStatus.NEEDS_REVIEW: frozenset({RECOVERY}),
    CommonStatus.FAILED: frozenset({RECOVERY}),
    CommonStatus.DONE: frozenset(),
    CommonStatus.CANCELLED: frozenset(),
}

PROJECTIONS: dict[str, dict[str, CommonStatus]] = {
    "maestro.task": {
        TaskStatus.PENDING: CommonStatus.PENDING,
        TaskStatus.READY: CommonStatus.PENDING,
        TaskStatus.AWAITING_APPROVAL: CommonStatus.NEEDS_REVIEW,
        TaskStatus.RUNNING: CommonStatus.RUNNING,
        TaskStatus.VALIDATING: CommonStatus.RUNNING,
        TaskStatus.DONE: CommonStatus.DONE,
        TaskStatus.FAILED: CommonStatus.FAILED,
        TaskStatus.NEEDS_REVIEW: CommonStatus.NEEDS_REVIEW,
        TaskStatus.ABANDONED: CommonStatus.CANCELLED,
    },
    "maestro.workstream": {
        WorkstreamStatus.PENDING: CommonStatus.PENDING,
        WorkstreamStatus.READY: CommonStatus.PENDING,
        WorkstreamStatus.DECOMPOSING: CommonStatus.RUNNING,
        WorkstreamStatus.RUNNING: CommonStatus.RUNNING,
        WorkstreamStatus.VERIFYING: CommonStatus.RUNNING,
        WorkstreamStatus.MERGING: CommonStatus.RUNNING,
        WorkstreamStatus.PR_CREATED: CommonStatus.NEEDS_REVIEW,
        WorkstreamStatus.DONE: CommonStatus.DONE,
        WorkstreamStatus.FAILED: CommonStatus.FAILED,
        WorkstreamStatus.NEEDS_REVIEW: CommonStatus.NEEDS_REVIEW,
        WorkstreamStatus.ABANDONED: CommonStatus.CANCELLED,
    },
    "spec-runner.task": {
        ExecutorTaskStatus.PENDING: CommonStatus.PENDING,
        ExecutorTaskStatus.RUNNING: CommonStatus.RUNNING,
        ExecutorTaskStatus.SUCCESS: CommonStatus.DONE,
        ExecutorTaskStatus.FAILED: CommonStatus.FAILED,
        ExecutorTaskStatus.SKIPPED: CommonStatus.CANCELLED,
    },
    "arbiter.outcome": {
        "success": CommonStatus.DONE,
        "failure": CommonStatus.FAILED,
        "timeout": CommonStatus.FAILED,
        "cancelled": CommonStatus.CANCELLED,
    },
}


#: Which keys each EvidenceRef kind requires (mirrors the schema allOf).
_EVIDENCE_REQUIRED: dict[str, tuple[str, ...]] = {
    "trace": ("trace_id",),
    "log": ("pipeline_id",),
    "benchmark": ("run_id",),
    "decision": ("decision_id",),
    "artifact": ("project", "path"),
    "gate-verdict": ("pipeline_id", "gate_id", "sha"),
}


class EvidenceRef(BaseModel):
    """Typed pointer to one piece of evidence (EvidenceRef v1).

    Contract: `contracts/observability/evidence-ref.schema.json` (+ .md).
    Kind-conditional requirements are enforced here exactly as in the
    schema's allOf, so a ref valid here is valid against the schema.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["trace", "log", "benchmark", "decision", "artifact", "gate-verdict"]
    trace_id: str | None = Field(None, pattern=r"^[0-9a-f]{32}$")
    span_id: str | None = Field(None, pattern=r"^[0-9a-f]{16}$")
    pipeline_id: str | None = None
    run_id: str | None = None
    decision_id: int | None = Field(None, ge=1)
    project: str | None = None
    path: str | None = None
    gate_id: str | None = None
    sha: str | None = Field(None, pattern=r"^[0-9a-f]{40}$")
    note: str | None = None

    @model_validator(mode="after")
    def _kind_requirements(self) -> EvidenceRef:
        missing = [
            key for key in _EVIDENCE_REQUIRED[self.kind] if getattr(self, key) is None
        ]
        if missing:
            msg = f"kind={self.kind!r} requires {missing}"
            raise ValueError(msg)
        if self.path is not None:
            parts = self.path.split("/")
            if self.path.startswith("/") or ".." in parts:
                msg = f"path must be project-relative without '..': {self.path!r}"
                raise ValueError(msg)
        return self


def trace_evidence(trace_id: str, span_id: str | None = None) -> EvidenceRef:
    """Pointer to a W3C trace (optionally narrowed to one span)."""
    return EvidenceRef(kind="trace", trace_id=trace_id, span_id=span_id)


def log_evidence(pipeline_id: str) -> EvidenceRef:
    """Pointer to a Maestro session log directory (logs/<ULID>/)."""
    return EvidenceRef(kind="log", pipeline_id=pipeline_id)


def benchmark_evidence(run_id: str) -> EvidenceRef:
    """Pointer to an arbiter benchmark_runs row."""
    return EvidenceRef(kind="benchmark", run_id=run_id)


def decision_evidence(decision_id: int) -> EvidenceRef:
    """Pointer to an arbiter routing decision (PolicyDecisionRef id)."""
    return EvidenceRef(kind="decision", decision_id=decision_id)


def artifact_evidence(project: str, path: str, note: str | None = None) -> EvidenceRef:
    """Pointer to a project-relative file in the owning repo."""
    return EvidenceRef(kind="artifact", project=project, path=path, note=note)


def gate_verdict_evidence(
    pipeline_id: str, gate_id: str, sha: str, note: str | None = None
) -> EvidenceRef:
    """Pointer to one gate verdict-record in logs/<ULID>/gate_verdicts.jsonl.

    The record is addressed by (pipeline_id, gate_id, sha): the run's
    verdict log, the gate that was evaluated, and the commit the verdict
    is bound to (WS-006 DESIGN-607/608 — verdicts are SHA-bound).
    """
    return EvidenceRef(
        kind="gate-verdict",
        pipeline_id=pipeline_id,
        gate_id=gate_id,
        sha=sha,
        note=note,
    )


class WorkCorrelation(BaseModel):
    """One correlation record (see contract schema for field semantics).

    Mirrors the JSON schema strictly (extra fields forbidden, version
    pinned, trace_id pattern enforced) so a record that validates here
    also validates against `schema.json`.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = SCHEMA_VERSION
    work_item_id: str = Field(..., min_length=1)
    parent_work_item_id: str | None = None
    status: CommonStatus
    source_project: str = Field(..., min_length=1)
    source_local_id: str = Field(..., min_length=1)
    source_status: str = Field(..., min_length=1)
    source_locator: str | None = None
    pipeline_id: str | None = None
    trace_id: str | None = Field(None, pattern=r"^[0-9a-f]{32}$")
    ts: str | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


def project_status(vocabulary: str, source_status: str) -> CommonStatus:
    """Project a source-local status onto the common enum.

    Raises ValueError for an unknown vocabulary or status — vocabulary
    drift must fail loudly (cf. Maestro #65), not silently mis-project.
    """
    table = PROJECTIONS.get(vocabulary)
    if table is None:
        raise ValueError(f"unknown status vocabulary: {vocabulary!r}")
    common = table.get(source_status)
    if common is None:
        raise ValueError(f"unknown {vocabulary} status: {source_status!r}")
    return common


def is_valid_transition(current: CommonStatus, new: CommonStatus) -> bool:
    """Check the common-enum transition table (with universal exits)."""
    if current in TERMINAL:
        return False
    if new in UNIVERSAL_EXITS:
        return True
    return new in _TRANSITIONS[current]


def for_maestro_task(
    task_id: str,
    status: TaskStatus | str,
    *,
    pipeline_id: str | None = None,
    trace_id: str | None = None,
    ts: str | None = None,
) -> WorkCorrelation:
    """Correlation record for a Maestro task (work_item_id = task.id)."""
    return WorkCorrelation(
        work_item_id=task_id,
        status=project_status("maestro.task", str(status)),
        source_project="maestro",
        source_local_id=task_id,
        source_status=str(status),
        pipeline_id=pipeline_id,
        trace_id=trace_id,
        ts=ts,
    )


def for_workstream(
    workstream_id: str,
    status: WorkstreamStatus | str,
    *,
    pipeline_id: str | None = None,
    ts: str | None = None,
) -> WorkCorrelation:
    """Correlation record for a Maestro workstream."""
    return WorkCorrelation(
        work_item_id=workstream_id,
        status=project_status("maestro.workstream", str(status)),
        source_project="maestro",
        source_local_id=workstream_id,
        source_status=str(status),
        pipeline_id=pipeline_id,
        ts=ts,
    )


def for_spec_task(
    parent_work_item_id: str,
    spec_dir: str,
    task_id: str,
    status: ExecutorTaskStatus | str,
    *,
    ts: str | None = None,
) -> WorkCorrelation:
    """Spec↔DAG bridge: spec-runner TASK-nnn under its owning workstream.

    spec-runner ids are only unique per spec dir, so the child key is
    derived deterministically from the parent and the locator is kept.
    """
    return WorkCorrelation(
        work_item_id=f"{parent_work_item_id}/{task_id}",
        parent_work_item_id=parent_work_item_id,
        status=project_status("spec-runner.task", str(status)),
        source_project="spec-runner",
        source_local_id=task_id,
        source_status=str(status),
        source_locator=spec_dir,
        ts=ts,
    )


def for_arbiter_outcome(
    task_id: str,
    status: str,
    *,
    pipeline_id: str | None = None,
    ts: str | None = None,
) -> WorkCorrelation:
    """Correlation record for an arbiter outcome (same join key)."""
    return WorkCorrelation(
        work_item_id=task_id,
        status=project_status("arbiter.outcome", status),
        source_project="arbiter",
        source_local_id=task_id,
        source_status=status,
        pipeline_id=pipeline_id,
        ts=ts,
    )
