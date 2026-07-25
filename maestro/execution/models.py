"""Transport-agnostic execution contract models.

These describe a run request and its lifecycle independently of *where* it
runs (local process, remote SSH host, container). Phase 0 uses only the
subset needed by LocalBackend; the remaining fields (progress_mirror,
secret_env, status_marker, mirror paths) are part of the frozen contract and
are consumed by later phases.
"""

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class CollectPolicy(BaseModel):
    """Terminal file-application policy (apply remote changes back locally)."""

    mode: Literal["none", "whole_worktree", "scope_paths", "patch"]
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=lambda: [".git/**", ".maestro/**"])
    conflict_policy: Literal["fail", "overwrite"] = "fail"
    on_failure: Literal["collect", "skip"] = "collect"


class ProgressMirrorPolicy(BaseModel):
    """Live, during-run mirror of executor state (orthogonal to collect)."""

    kind: Literal["spec_runner_sqlite"]
    remote_globs: list[str]
    local_dir: Path
    interval_seconds: float


class ExecutionRequest(BaseModel):
    """A transport-agnostic run request and its lifecycle description."""

    run_id: str
    argv: list[str]
    workdir: Path
    log_path: Path
    stdin: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: list[str] = Field(default_factory=list)
    inherit_env: bool = False
    timeout_seconds: float | None = None
    capture_output: bool = False
    collect: CollectPolicy
    progress_mirror: ProgressMirrorPolicy | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    required_tools: list[str] = Field(default_factory=list)
    execution_id: str | None = None
    entity_kind: Literal["task", "workstream"] | None = None
    attempt: int = 1
    backend_id: str = "local"


class PreparedRunPlan(BaseModel):
    """Deterministic launch plan (no I/O performed to build it)."""

    argv: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    container_name: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    env_file_keys: list[str] = Field(default_factory=list)
    cidfile_path: Path | None = None
    tmp_dir: Path | None = None


class PreparedRun(BaseModel):
    """A plan after its filesystem side effects have been materialized."""

    plan: PreparedRunPlan
    env_file: Path | None = None
    cleanup_paths: list[Path] = Field(default_factory=list)


class ExecutionResult(BaseModel):
    """Terminal outcome of a run."""

    exit_code: int | None
    stdout_tail: str = ""
    stderr_tail: str = ""
    output_log_path: Path
    timed_out: bool = False
    error_message: str | None = None


class ExecutionHandleRef(BaseModel):
    """Persisted identity of a run; survives a center restart."""

    backend_id: str
    run_id: str
    transport_ref: str
    execution_id: str | None = None
    status_marker: str | None = None
    started_at: datetime
    workdir_mirror_path: Path | None = None
    state_mirror_path: Path | None = None


class CollectResult(BaseModel):
    applied: bool
    files_changed: int = 0
    detail: str = ""


class BackendHealth(BaseModel):
    reachable: bool
    detail: str = ""


class CapabilityResult(BaseModel):
    ok: bool
    missing_tools: list[str] = Field(default_factory=list)


class ProbeResult(BaseModel):
    """Recovery decision signal for a possibly-orphaned execution.

    `needs_review` is the decision boundary (True -> route to NEEDS_REVIEW);
    `alive`/`exit_code` are diagnostic only and must not be used to decide.
    """

    needs_review: bool
    alive: bool | None = None
    exit_code: int | None = None
    detail: str = ""
