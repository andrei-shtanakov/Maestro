"""Multi-process orchestrator for Maestro.

This module provides the Orchestrator class that coordinates
multiple spec-runner processes, each running in its own git
worktree. It handles the full lifecycle: decomposition, workspace
setup, process spawning, monitoring, and PR creation.
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from maestro._vendor.obs import child_env, current_pipeline_id, span
from maestro.changed_paths import changed_paths_since
from maestro.database import Database
from maestro.decomposer import ProjectDecomposer
from maestro.domain import (
    RESUME_REVERIFY,
    RESUME_REWORK,
    CommandVerifier,
    EvidenceLedger,
    LedgerCollisionError,
    VerdictDocument,
    VerdictValue,
    VerificationContext,
    VerificationSection,
    build_rework_addendum,
    profile_sha256,
)
from maestro.event_log import get_event_logger
from maestro.execution.backend import TaskHandle
from maestro.execution.docker_cli import DockerCli
from maestro.execution.docker_recovery import (
    GC_CLEAN_OUTCOMES,
    DockerProbe,
    RecoveryVerdict,
    gc_terminal_handle,
    probe_execution,
)
from maestro.execution.finalize import ensure_finalize_task
from maestro.execution.models import (
    CollectPolicy,
    ExecutionHandleRef,
    ExecutionRequest,
    ProgressMirrorPolicy,
)
from maestro.execution.resolver import BackendResolver
from maestro.execution.ssh_backend import SshBackend
from maestro.execution.ssh_launch import decode_transport_ref
from maestro.execution.ssh_recovery import gc_ssh_terminal, probe_ssh
from maestro.gates import (
    BLOCK_REASON_PREFIX,
    ApprovalMarker,
    GateDecision,
    GateKeeper,
    parse_approval_marker,
    pipeline_log_dir,
    preserve_approval_marker,
)
from maestro.git import GitError, MergeConflictError
from maestro.merge_logs import merge_logs_dir
from maestro.models import (
    SPEC_PREFIX,
    OrchestratorConfig,
    TransitionSubject,
    Workstream,
    WorkstreamConfig,
    WorkstreamStatus,
)
from maestro.notifications.manager import NotificationManager
from maestro.pr_manager import PRManager, PRManagerError
from maestro.scope_gate import build_scope_escape_reason, find_escapes, normalize
from maestro.spec_runner import read_executor_state
from maestro.transitions import TransitionDispatcher
from maestro.workspace import WorkspaceManager, ensure_harness_excludes


class OrchestratorError(Exception):
    """Base exception for orchestrator errors."""


class _EvidenceContainmentError(Exception):
    """Raised inside PASS finalization for a fail-closed containment breach.

    Distinct from a delivery-preparation IO error: this routes the workstream
    to NEEDS_REVIEW (evidence escaping verifier.write, or a stale PASS), never
    a silent stay-in-VERIFYING.
    """


StatusChangeCallback = Callable[[str, str, str], None]


def _subject(workstream: Workstream) -> TransitionSubject:
    """Build the dispatcher's entity-agnostic view of a workstream."""
    return TransitionSubject(
        "workstream", workstream.id, workstream.title, workstream.status
    )


_SPAWNING_SENTINEL = -1
"""Placeholder pid written into ``process_pid`` / ``generation_pid`` BEFORE a
subprocess spawn and overwritten with the real pid after. A recovery that finds
it treats the workstream as a possible live orphan (a spawn was in progress at
the crash). Never passed to ``os.kill`` — see ``_maybe_live_orphan`` and the
``pid <= 0`` guard in ``_is_pid_alive``."""


def _is_pid_alive(pid: int) -> bool:
    """True if a process with this pid exists (signal 0 probes without killing).

    ProcessLookupError means it is gone; PermissionError means it exists but
    we may not signal it (still alive).
    """
    if pid <= 0:
        # Never signal a non-positive pid: os.kill(0/-1, …) would hit the
        # caller's process group / every process. A real pid is always > 0.
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _maybe_live_orphan(pid: int | None) -> bool:
    """True if the recorded pid indicates a possibly-live orphan: the spawning
    sentinel (a spawn was in progress at the crash) or a still-alive real pid.

    Checks the sentinel FIRST so it is never passed to os.kill.
    """
    if pid == _SPAWNING_SENTINEL:
        return True
    return pid is not None and _is_pid_alive(pid)


_STRANDED_INFLIGHT = (
    WorkstreamStatus.DECOMPOSING,
    WorkstreamStatus.RUNNING,
    WorkstreamStatus.MERGING,
    WorkstreamStatus.PR_CREATED,
)

_SSH_GC_CLEAN_OUTCOMES = frozenset({"removed", "no owner marker; skipped"})
"""`gc_ssh_terminal` outcomes after which a `collected` handle is safe to
mark `cleaned` — nothing remote is left to account for."""

_VerifyProbeVerdict = Literal["alive", "dead", "ambiguous"]
"""Classification of a VERIFYING workstream's latest verification-phase
execution handle at startup recovery (Task 9, §4/§10):

- ``"alive"``: the verifier subprocess is still running — leave the
  workstream in VERIFYING untouched (never a duplicate spawn).
- ``"dead"``: the handle is gone/collected, or no handle was ever persisted
  for this attempt (the crash landed before the verifier spawned) — safe to
  re-enter ``_run_verification``, which mints a NEW attempt under the SAME
  run_id (append-only evidence).
- ``"ambiguous"``: liveness cannot be determined (the handle still carries
  its pre-spawn placeholder transport_ref — the verification-loop analogue
  of ``_SPAWNING_SENTINEL`` — or the probe itself failed) — fail closed to
  NEEDS_REVIEW, mirroring the existing live-orphan rule for RUNNING.
"""


def _decode_local_verify_pid(transport_ref: str) -> int | None:
    """Extract the pid from a local verification handle's `transport_ref`.

    `CommandVerifier._pre_spawn_persist` seeds a placeholder
    (`"<backend_id>:verify-<execution_id>"`) BEFORE the subprocess spawns;
    `update_execution_handle_launch` overwrites it with the real
    `"local_pid:<pid>"` value (`BareIsolator.transport_ref`) once
    `backend.run()` returns. Returns `None` for the placeholder (or any
    other non-matching string) — the caller treats that as the ambiguous,
    spawn-in-flight window, never as a decodable pid.
    """
    if not transport_ref.startswith("local_pid:"):
        return None
    try:
        return int(transport_ref.split(":", 1)[1])
    except ValueError:
        return None


def build_ssh_execution_request(
    *,
    workstream_id: str,
    workspace: str,
    log_file: str,
    cmd: list[str],
    execution_id: str,
    attempt: int,
    mirror_dir: str,
) -> ExecutionRequest:
    """ExecutionRequest for a remote (ssh) Mode-2 workstream: whole-worktree
    collect + WAL-safe progress mirror. Secrets flow via the backend's
    secret_env allowlist (env-file), never inherit_env."""
    return ExecutionRequest(
        run_id=workstream_id,
        argv=cmd,
        workdir=Path(workspace),
        log_path=Path(log_file),
        inherit_env=False,
        collect=CollectPolicy(
            mode="whole_worktree", conflict_policy="fail", on_failure="collect"
        ),
        progress_mirror=ProgressMirrorPolicy(
            kind="spec_runner_sqlite",
            remote_globs=[f".executor-{SPEC_PREFIX}state.db"],
            local_dir=Path(mirror_dir),
            interval_seconds=2.0,
        ),
        required_tools=["spec-runner"],
        execution_id=execution_id,
        entity_kind="workstream",
        attempt=attempt,
        backend_id="",  # set by the caller via model_copy (existing pattern)
    )


def _handle_ref_from_row(row: dict[str, Any]) -> ExecutionHandleRef:
    """Rebuild an ExecutionHandleRef from an execution_handles DB row."""
    return ExecutionHandleRef(
        backend_id=row["backend_id"],
        run_id=row["entity_id"],
        transport_ref=row["transport_ref"],
        status_marker=row.get("status_marker"),
        started_at=datetime.fromisoformat(row["created_at"]),
        workdir_mirror_path=None,
        state_mirror_path=None,
    )


@dataclass
class RunningWorkstream:
    """Represents a currently running workstream execution.

    ``finalize_task`` holds the single-owner finalization task (reap + collect
    + cleanup) so a second monitor/shutdown caller awaits it rather than
    starting a duplicate. ``execution_id`` is the durable execution-handle id
    for a non-local backend (``None`` for the local path, which persists no
    handle). ``backend_id`` is the resolved execution backend id (e.g.
    "local", "docker") this workstream was spawned on. ``mirror_dir`` is the
    local WAL-mirror directory `_update_progress` reads from for an ssh
    execution (``None`` for local/docker, which read the live workspace
    ``spec`` dir directly).
    """

    workstream: Workstream
    handle: TaskHandle
    started_at: datetime
    workspace_path: Path
    log_file: Path
    finalize_task: "asyncio.Task | None" = None
    execution_id: str | None = None
    backend_id: str = "local"
    mirror_dir: Path | None = None


@dataclass
class OrchestratorStats:
    """Statistics for an orchestration run."""

    total_workstreams: int = 0
    completed: int = 0
    failed: int = 0
    prs_created: int = 0
    start_time: datetime = field(default_factory=lambda: datetime.now(UTC))


class Orchestrator:
    """Coordinates multiple spec-runner processes.

    Main loop:
    1. Decompose project into workstreams (if needed)
    2. Resolve ready workstreams from DAG
    3. Create workspace + spawn spec-runner for each
    4. Monitor processes, read progress
    5. On completion: push + create PR + cleanup
    """

    def __init__(
        self,
        db: Database,
        workspace_mgr: WorkspaceManager,
        decomposer: ProjectDecomposer,
        pr_manager: PRManager,
        config: OrchestratorConfig,
        log_dir: Path | None = None,
        notifier: NotificationManager | None = None,
        on_status_change: StatusChangeCallback | None = None,
        docker: DockerProbe | None = None,
    ) -> None:
        """Initialize orchestrator.

        Args:
            db: Database for state persistence.
            workspace_mgr: Manager for worktree workspaces.
            decomposer: Project decomposer for spec gen.
            pr_manager: PR creation manager.
            config: Orchestrator configuration.
            log_dir: Directory for log files.
            notifier: Optional notification manager for workstream
                lifecycle notifications.
            on_status_change: Optional callback for workstream status changes.
            docker: Docker CLI wrapper used by startup recovery to probe
                execution_handles rows for docker-backed workstreams before
                re-READYing them. Injectable for tests; defaults to a real
                `DockerCli()`.
        """
        self._db = db
        self._docker = docker or DockerCli()
        self._workspace_mgr = workspace_mgr
        self._decomposer = decomposer
        self._pr_manager = pr_manager
        self._config = config
        self._on_status_change = on_status_change
        self._dispatcher = TransitionDispatcher(
            notifier=notifier,
            event_logger_getter=get_event_logger,
            status_change_cb=on_status_change,
        )
        self._log_dir = log_dir or Path(config.repo_path).expanduser() / "logs"
        self._gates: GateKeeper | None = None
        if config.gates is not None:
            self._gates = GateKeeper(
                config.gates,
                project=config.project,
                repo_path=Path(config.repo_path).expanduser(),
                base_branch=config.base_branch,
                log_dir=pipeline_log_dir(),
            )

        self._backends = BackendResolver(self._config.execution)
        # Stage B evidence ledger: built only when a domain profile is active,
        # so legacy (domain=None) runs stay byte-identical with no evidence
        # machinery. Root lives beside the DB file, never inside a worktree.
        self._ledger: EvidenceLedger | None = None
        if config.domain is not None:
            evidence_root = Path(db.db_path).parent / "evidence"
            self._ledger = EvidenceLedger(db, evidence_root)
        self._running: dict[str, RunningWorkstream] = {}
        self._generating: dict[str, asyncio.Task[None]] = {}
        # Task 9 fix-up: VERIFYING workstreams whose verifier process was
        # found alive at startup recovery (`_recover_one_verifying`'s
        # "alive" branch) — maps workstream_id -> the verification_attempt
        # being watched. `_poll_verifying_orphans` (called every main-loop
        # tick) is what makes "leave in place, resume monitoring" actually
        # resume something; without it a recovered-alive orphan would sit
        # in VERIFYING forever with nothing ever re-checking it.
        self._verifying_orphans: dict[str, int] = {}
        self._shutdown_grace_seconds: float = 5.0
        self._shutdown_requested = False
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._logger = logging.getLogger(__name__)
        self._stats = OrchestratorStats()

    @property
    def is_running(self) -> bool:
        """Check if orchestrator is running."""
        return self._loop is not None and not self._shutdown_requested

    async def _transition(
        self,
        workstream_id: str,
        to_status: WorkstreamStatus,
        *,
        expected_status: WorkstreamStatus,
        details: dict[str, object] | None = None,
        message: str | None = None,
        **fields: object,
    ) -> Workstream:
        """Write a workstream status transition and dispatch its effects.

        Mirrors `Scheduler._transition` (spec §4.1): `expected_status` is a
        CAS guard on the write; on success it *is* the true `frm` for the
        dispatcher (a plain re-`get` would be unreliable under concurrent
        writes). `details`/`message` feed the event/notification only;
        `**fields` are DB columns (error_message, pr_url, ...) and never
        leak into the effect.
        """
        workstream = await self._db.update_workstream_status(
            workstream_id, to_status, expected_status=expected_status, **fields
        )
        await self._dispatcher.fire(
            _subject(workstream),
            frm=expected_status,
            details=details,
            message=message,
        )
        return workstream

    async def _update_fields(self, workstream_id: str, **fields: object) -> Workstream:
        """Patch workstream columns without a status transition (no dispatch).

        For same-state writes that reuse the status API to update columns
        (pid tracking, progress text, ...) — see spec §4.2.
        """
        workstream = await self._db.get_workstream(workstream_id)
        return await self._db.update_workstream_status(
            workstream_id, workstream.status, expected_status=None, **fields
        )

    async def run(self) -> OrchestratorStats:
        """Run the orchestrator main loop.

        Returns:
            Statistics for the orchestration run.

        Raises:
            OrchestratorError: If database not connected.
        """
        if not self._db.is_connected:
            msg = "Database must be connected"
            raise OrchestratorError(msg)

        self._loop = asyncio.get_running_loop()
        self._setup_signal_handlers()
        self._log_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Step 1: Ensure workstreams exist
            await self._ensure_workstreams()

            # Step 1b: Reconcile workstreams stranded by a prior hard crash
            # (resume path) so the main loop can advance them.
            await self._recover_stranded_workstreams()

            # Step 2: Main loop
            await self._main_loop()
        finally:
            await self._cleanup()
            _pipeline_id = current_pipeline_id()
            if _pipeline_id:
                _log_dir = Path(
                    os.environ.get("ORCHESTRA_LOG_DIR") or f"logs/{_pipeline_id}"
                )
                if _log_dir.exists():  # noqa: ASYNC240
                    with contextlib.suppress(Exception):
                        merge_logs_dir(_log_dir)

        return self._stats

    async def _recover_stranded_workstreams(self) -> int:
        """Reconcile workstreams stranded by a hard crash so the resume loop
        can advance them. In-flight strands reset to READY (no retry, no
        error_message); a RUNNING workstream whose recorded process is still
        alive goes to NEEDS_REVIEW instead (never re-run over a live orphan);
        a RUNNING workstream whose process looks dead but whose non-local
        execution_handles row can't rule out a live/leftover execution
        (Task 18: docker `probe_execution`; Task 16: ssh `probe_ssh`, both
        fail-closed) also goes to NEEDS_REVIEW; FAILED workstreams reconcile
        by the retry rule. Best-effort per workstream; never raises.
        `terminal`/`collected`-state handles (any entity) are swept for
        ownership-checked GC as a side effect — see `_gc_terminal_handles`."""
        recovered = 0
        open_handles = await self._db.get_open_execution_handles()
        workstream_handles = {
            h["entity_id"]: h
            for h in open_handles
            if h["entity_kind"] == "workstream"
            and h["state"] in ("prepared", "running")
        }
        # Parallel lookup for handles that already reached the terminal/collected
        # window: finalize's `on_terminal` persisted the marker, but the center
        # crashed before `collect` confirmed the remote diff was applied. For SSH
        # these must NOT be silently re-run (spec §G/§J) — see the pre-empt check
        # at the top of the loop. (Rows in these states are returned by
        # `get_open_execution_handles` alongside prepared/running.)
        workstream_terminal_handles = {
            h["entity_id"]: h
            for h in open_handles
            if h["entity_kind"] == "workstream"
            and h["state"] in ("terminal", "collected")
        }

        for state in _STRANDED_INFLIGHT:
            for w in await self._db.get_workstreams_by_status(state):
                try:
                    # SSH terminal/collect window (spec §G/§J): an ssh execution
                    # stranded after its terminal marker but before collect must
                    # never re-run over uncollected remote changes — park it for
                    # review with the remote tmp preserved, BEFORE the generic
                    # pid/reset logic (SSH os_pid is None, so live-orphan detection
                    # would otherwise let it fall through to FAILED->READY).
                    term_row = workstream_terminal_handles.get(w.id)
                    if term_row is not None and self._is_ssh_terminal_strand(term_row):
                        await self._route_ssh_terminal_strand(w.id, state, term_row)
                        recovered += 1
                        continue
                    orphan_pid = (
                        w.process_pid
                        if state is WorkstreamStatus.RUNNING
                        else w.generation_pid
                        if state is WorkstreamStatus.DECOMPOSING
                        else None
                    )
                    live_orphan = _maybe_live_orphan(orphan_pid)
                    handle_needs_review = False
                    if not live_orphan and state is WorkstreamStatus.RUNNING:
                        handle_needs_review = await self._probe_open_handle(
                            w.id, workstream_handles
                        )
                    if handle_needs_review:
                        self._logger.warning(
                            "Workstream '%s' stranded in RUNNING with a "
                            "possibly-live execution after restart — sending "
                            "to NEEDS_REVIEW; verify and clean it up before "
                            "resume",
                            w.id,
                        )
                        await self._transition(
                            w.id, WorkstreamStatus.FAILED, expected_status=state
                        )
                        await self._transition(
                            w.id,
                            WorkstreamStatus.NEEDS_REVIEW,
                            expected_status=WorkstreamStatus.FAILED,
                            process_pid=None,
                            generation_pid=None,
                        )
                        self._stats.failed += 1
                    elif live_orphan:
                        if orphan_pid == _SPAWNING_SENTINEL:
                            self._logger.warning(
                                "Workstream '%s' stranded in %s with a spawn in "
                                "progress at the crash — state uncertain (a "
                                "subprocess may or may not be running); sending "
                                "to NEEDS_REVIEW, verify before resuming",
                                w.id,
                                state.value,
                            )
                        else:
                            self._logger.warning(
                                "Workstream '%s' stranded in %s with a live "
                                "process (pid %s) after restart — sending to "
                                "NEEDS_REVIEW; verify and clean it up before resume",
                                w.id,
                                state.value,
                                orphan_pid,
                            )
                        await self._transition(
                            w.id, WorkstreamStatus.FAILED, expected_status=state
                        )
                        await self._transition(
                            w.id,
                            WorkstreamStatus.NEEDS_REVIEW,
                            expected_status=WorkstreamStatus.FAILED,
                            process_pid=None,
                            generation_pid=None,
                        )
                        # Parked for review — signal via exit code + summary,
                        # matching _handle_failure's NEEDS_REVIEW accounting.
                        self._stats.failed += 1
                    elif state is WorkstreamStatus.DECOMPOSING:
                        self._logger.info(
                            "Recovering workstream '%s' from stranded "
                            "DECOMPOSING -> READY",
                            w.id,
                        )
                        await self._transition(
                            w.id, WorkstreamStatus.READY, expected_status=state
                        )
                    else:
                        # RUNNING (dead) / MERGING / PR_CREATED: cannot go
                        # directly to READY, reset via FAILED.
                        self._logger.info(
                            "Recovering workstream '%s' from stranded %s -> READY",
                            w.id,
                            state.value,
                        )
                        await self._transition(
                            w.id, WorkstreamStatus.FAILED, expected_status=state
                        )
                        await self._transition(
                            w.id,
                            WorkstreamStatus.READY,
                            expected_status=WorkstreamStatus.FAILED,
                        )
                    recovered += 1
                except Exception as e:
                    self._logger.error("Failed to recover workstream '%s': %s", w.id, e)

        # FAILED reconciliation (genuine failures resting mid-_handle_failure).
        # Runs after the in-flight loop, so in-flight resets that pass through
        # FAILED have already reached their final state.
        for w in await self._db.get_workstreams_by_status(WorkstreamStatus.FAILED):
            try:
                if _maybe_live_orphan(w.process_pid) or _maybe_live_orphan(
                    w.generation_pid
                ):
                    # A FAILED row can be an in-flight reset interrupted mid
                    # two-write (X->FAILED committed, target write lost). If its
                    # recorded process_pid (RUNNING orphan) or generation_pid
                    # (DECOMPOSING orphan) is alive OR the spawning sentinel, it
                    # may be a live orphan — never reset to READY. Park for
                    # review. The NEEDS_REVIEW write below clears both pids.
                    target = WorkstreamStatus.NEEDS_REVIEW
                elif w.error_message is not None and w.error_message.startswith(
                    BLOCK_REASON_PREFIX
                ):
                    # `_gate_ex_post` blocks with TWO writes: RUNNING ->
                    # FAILED-with-block-reason, then FAILED -> NEEDS_REVIEW.
                    # A crash between them strands a FAILED row whose
                    # error_message IS the gate-block reason. Finish the
                    # interrupted write as NEEDS_REVIEW. NOTE (v1.3): the
                    # predicate is the BLOCK_REASON_PREFIX, not marker
                    # presence — `_handle_failure` now APPENDS markers to
                    # ordinary failure messages (H-6 position retention),
                    # and those must follow the normal retry rule.
                    target = WorkstreamStatus.NEEDS_REVIEW
                else:
                    target = (
                        WorkstreamStatus.READY
                        if w.can_retry()
                        else WorkstreamStatus.NEEDS_REVIEW
                    )
                self._logger.info(
                    "Reconciling FAILED workstream '%s' -> %s",
                    w.id,
                    target.value,
                )
                if target is WorkstreamStatus.NEEDS_REVIEW:
                    await self._transition(
                        w.id,
                        WorkstreamStatus.NEEDS_REVIEW,
                        expected_status=WorkstreamStatus.FAILED,
                        process_pid=None,
                        generation_pid=None,
                    )
                    # Parked for review — signal via exit code + summary.
                    self._stats.failed += 1
                else:
                    await self._transition(
                        w.id,
                        WorkstreamStatus.READY,
                        expected_status=WorkstreamStatus.FAILED,
                    )
                recovered += 1
            except Exception as e:
                self._logger.error(
                    "Failed to reconcile FAILED workstream '%s': %s", w.id, e
                )

        # Stage B (Task 9): VERIFYING reconcile. Deliberately NOT part of
        # `_STRANDED_INFLIGHT` — the generic table's "reset via FAILED ->
        # READY" pattern would discard in-flight verification state (the
        # run_id/attempt counters, and an unmaterialized PASS already in the
        # ledger); VERIFYING needs its own rules (see the method docstring).
        recovered += await self._recover_verifying_workstreams()

        if recovered:
            self._logger.info(
                "Recovered %d stranded workstream(s) on startup", recovered
            )

        # Best-effort GC of leftover containers for settled entities (any
        # entity kind — the handle table is shared with the scheduler).
        await self._gc_terminal_handles(open_handles)

        return recovered

    async def _recover_verifying_workstreams(self) -> int:
        """Reconcile workstreams stranded in VERIFYING by a hard crash
        (Task 9, §4/§10).

        Rules (each mirrors an existing stranded-state convention, adapted
        so a crash never discards in-flight verification state or duplicate-
        spawns a verifier over a possibly-live one):

        - A PASS attempt already in the ledger but not yet materialized
          (crash inside `_finalize_verification`) re-enters finalization
          ONLY — idempotent by the evidence-commit trailer, never a new
          verifier run. Checked FIRST, before probing any handle.
        - Otherwise, the latest verification-phase execution handle is
          probed (`_probe_verification_handle`): alive -> leave in VERIFYING
          untouched (no duplicate spawn); dead/never-spawned -> re-enter
          `_run_verification` (mints a NEW `verification_attempt` under the
          SAME run_id — append-only evidence, safe to retry); ambiguous
          (spawn in flight at the crash, or a probe error) -> fail closed to
          NEEDS_REVIEW, the same accounting as the existing live-orphan rule
          for RUNNING.

        `rework_attempt` is untouched by every branch here — only
        `_route_fail` (a genuine FAIL verdict) ever increments it.

        Best-effort per workstream; never raises. No-op when no domain
        profile is configured (`self._ledger is None`): VERIFYING is
        unreachable on the legacy zero-change path.
        """
        if self._ledger is None:
            return 0
        recovered = 0
        for w in await self._db.get_workstreams_by_status(WorkstreamStatus.VERIFYING):
            try:
                recovered += await self._recover_one_verifying(w)
            except Exception as e:
                self._logger.error(
                    "Failed to recover VERIFYING workstream '%s': %s", w.id, e
                )
        return recovered

    async def _recover_one_verifying(self, w: Workstream) -> int:
        """Reconcile a single VERIFYING workstream; returns 1 if an action
        was taken (re-entered finalization/verification, or parked for
        review), 0 if left untouched (a live verifier)."""
        assert self._ledger is not None
        run_id = w.verification_run_id
        if run_id is not None:
            bundle = await self._ledger.list_bundle(run_id)
            pass_row = next(
                (r for r in reversed(bundle) if r.verdict is VerdictValue.PASS),
                None,
            )
            if pass_row is not None and not pass_row.materialized:
                workspace = self._workspace_mgr.get_workspace_path(w.id)
                commit = await self._workspace_head(workspace)
                if commit is None:
                    reason = (
                        "verification recovery: cannot read worktree HEAD "
                        "for stranded finalization"
                    )
                    self._logger.warning("%s for '%s'", reason, w.id)
                    await self._route_verifying_needs_review(w.id, reason)
                    return 1
                self._logger.info(
                    "Recovering workstream '%s' from stranded VERIFYING: "
                    "PASS attempt %d already ledgered but not materialized — "
                    "re-entering finalization only",
                    w.id,
                    pass_row.attempt,
                )
                await self._finalize_verification(
                    w.id, workspace, run_id=run_id, verified_source_commit=commit
                )
                return 1

        probe = await self._probe_verification_handle(w.id, w.verification_attempt)
        if probe == "alive":
            self._logger.info(
                "Workstream '%s' stranded in VERIFYING with a live verifier "
                "process — leaving in place, registered for main-loop "
                "re-poll (no duplicate spawn)",
                w.id,
            )
            # `_poll_verifying_orphans` is what makes "leave in place" a
            # real resume rather than a dead end: startup recovery has no
            # asyncio Task to attach to (the orchestrator that spawned this
            # process is gone), so re-checking it happens on the main-loop
            # tick instead.
            self._verifying_orphans[w.id] = w.verification_attempt
            return 0
        if probe == "ambiguous":
            reason = (
                "verification handle probe is ambiguous after restart (a "
                "spawn may have been in progress at the crash) — state "
                "uncertain, sending to NEEDS_REVIEW"
            )
            self._logger.warning("%s for '%s'", reason, w.id)
            await self._route_verifying_needs_review(w.id, reason)
            return 1

        self._logger.info(
            "Recovering workstream '%s' from stranded VERIFYING: verifier "
            "handle is dead/never-spawned — re-entering verification",
            w.id,
        )
        workspace = self._workspace_mgr.get_workspace_path(w.id)
        # Reset the per-session ERROR budget, mirroring the READY -> VERIFYING
        # reverify resume dispatch (`_spawn_workstream`'s RESUME_REVERIFY
        # branch) — a crash-recovery re-entry is a fresh session too.
        await self._update_fields(w.id, verification_error_attempt=0)
        await self._run_verification(w.id, workspace)
        return 1

    async def _route_verifying_needs_review(
        self, workstream_id: str, reason: str
    ) -> None:
        """Fail-closed VERIFYING -> NEEDS_REVIEW (direct edge — VERIFYING's
        transition table allows it, unlike RUNNING which must go via
        FAILED). Same accounting as the existing live-orphan rule."""
        await self._transition(
            workstream_id,
            WorkstreamStatus.NEEDS_REVIEW,
            expected_status=WorkstreamStatus.VERIFYING,
            message=reason,
            error_message=reason,
        )
        self._stats.failed += 1

    async def _probe_verification_handle(
        self, workstream_id: str, attempt: int
    ) -> _VerifyProbeVerdict:
        """Classify the verification-phase execution handle for `attempt`
        (the workstream's persisted `verification_attempt` counter — the
        attempt `_run_verification` was driving when the crash happened).

        No handle row at all for that attempt means the crash landed before
        `CommandVerifier._pre_spawn_persist` ever ran — nothing was spawned,
        definitively "dead". A row whose `transport_ref` is still the
        pre-spawn placeholder is the verification-loop's parallel of
        `_SPAWNING_SENTINEL`: a spawn may have been in flight at the crash,
        so liveness cannot be determined — "ambiguous", fail closed.
        Otherwise the handle carries a real backend ref: local is probed by
        pid directly (`_is_pid_alive`); non-local backends reuse the same
        fail-closed verdict machinery `_probe_open_handle` uses for RUNNING
        recovery (`probe_execution`/`probe_ssh`).
        """
        if attempt <= 0:
            return "dead"
        row = await self._db.get_execution_handle(
            entity_kind="workstream",
            entity_id=workstream_id,
            execution_phase="verification",
            attempt=attempt,
        )
        if row is None:
            return "dead"
        backend_id = row["backend_id"]
        if backend_id == "local":
            pid = _decode_local_verify_pid(row["transport_ref"])
            if pid is None:
                return "ambiguous"
            return "alive" if _is_pid_alive(pid) else "dead"
        if row["state"] not in ("prepared", "running"):
            # Already resolved elsewhere (verification handles normally never
            # advance past "prepared", but treat any settled state as safe).
            return "dead"
        try:
            backend = self._backends.resolve(backend_id)
        except Exception:
            return "ambiguous"
        if isinstance(backend, SshBackend):
            # TODO(reviewer Important #2): this does not mirror
            # `_probe_open_handle`'s isolation dispatch — an ssh-launched
            # verifier whose remote harness itself runs inside a container
            # (Phase 2c, `decoded["isolation"] == "docker"`) should also
            # probe that container (`probe_execution` with
            # `expected_labels`) and fail closed if EITHER probe says so,
            # exactly like `_probe_open_handle` does for RUNNING recovery.
            # Currently masked because `probe_ssh` is unconditionally
            # fail-closed (`needs_review=True` always) regardless of
            # isolation, so the missing dual-probe never changes the
            # outcome today — but it must be added before any non-local
            # verifier backend combined with docker isolation is wired up.
            verdict = await probe_ssh(backend._ssh, _handle_ref_from_row(row))
        elif backend_id == "docker":
            verdict = await probe_execution(row["execution_id"], self._docker)
        else:
            verdict = RecoveryVerdict(
                True, f"unsupported backend {backend_id!r} for recovery probe"
            )
        return "ambiguous" if verdict.needs_review else "dead"

    async def _poll_verifying_orphans(self) -> None:
        """Re-check every VERIFYING workstream recovered with a possibly-live
        verifier process (`_recover_one_verifying`'s "alive" branch, tracked
        in `self._verifying_orphans`).

        This is what makes "leave in place, resume monitoring" a real
        resume: `_run_verification`'s `await verifier.verify(ctx)` for that
        process died with the crashed coroutine, and there is no live
        asyncio Task to re-attach to — so the only way to ever notice the
        orphan finishing is to re-probe it on a schedule. Called once per
        main-loop tick.

        - Still alive -> keep waiting; never a duplicate spawn (the
          design's explicit invariant for this branch).
        - Dead -> the orphaned process's own exit code/handshake was never
          awaited by any Maestro code, so it can never be validated or
          replayed; the only sound path is a FRESH verification attempt
          under the SAME run_id (append-only evidence tolerates this,
          exactly like the startup-recovery "dead" rule).
        - Probe error/ambiguous -> fail closed to NEEDS_REVIEW, the same
          accounting as the startup-time ambiguous branch.

        Best-effort: a per-workstream failure is logged and skipped rather
        than raised, so one bad probe never stalls the main loop.
        """
        if not self._verifying_orphans:
            return
        for workstream_id, attempt in list(self._verifying_orphans.items()):
            try:
                await self._poll_one_verifying_orphan(workstream_id, attempt)
            except Exception as e:
                self._logger.error(
                    "Failed to re-poll VERIFYING orphan '%s': %s", workstream_id, e
                )

    async def _poll_one_verifying_orphan(
        self, workstream_id: str, attempt: int
    ) -> None:
        workstream = await self._db.get_workstream(workstream_id)
        if workstream.status is not WorkstreamStatus.VERIFYING:
            # Left VERIFYING through some other path (e.g. an operator
            # action) -- stop tracking it.
            self._verifying_orphans.pop(workstream_id, None)
            return

        try:
            probe = await self._probe_verification_handle(workstream_id, attempt)
        except Exception as e:
            self._logger.error(
                "Probe failed while re-polling VERIFYING orphan '%s': %s",
                workstream_id,
                e,
            )
            probe = "ambiguous"

        if probe == "alive":
            return  # keep waiting, no duplicate spawn

        self._verifying_orphans.pop(workstream_id, None)

        if probe == "ambiguous":
            reason = (
                "verification handle probe turned ambiguous while "
                "re-polling a recovered orphan — state uncertain, sending "
                "to NEEDS_REVIEW"
            )
            self._logger.warning("%s for '%s'", reason, workstream_id)
            await self._route_verifying_needs_review(workstream_id, reason)
            return

        self._logger.info(
            "VERIFYING orphan '%s' is no longer alive — its handshake was "
            "never awaited, so re-entering verification with a FRESH "
            "attempt under the same run_id",
            workstream_id,
        )
        workspace = self._workspace_mgr.get_workspace_path(workstream_id)
        # Reset the per-session ERROR budget, mirroring the READY ->
        # VERIFYING reverify resume dispatch and the startup-recovery "dead"
        # branch — this re-poll re-entry is a fresh session too.
        await self._update_fields(workstream_id, verification_error_attempt=0)
        await self._run_verification(workstream_id, workspace)

    def _is_ssh_terminal_strand(self, handle_row: dict[str, Any]) -> bool:
        """True if `handle_row` is an SSH-resolved execution stranded in the
        terminal/collected window — the case that must route to NEEDS_REVIEW
        rather than silently re-run (spec §G/§J). Docker rows return False
        (docker `collect` is a no-op, so its reset-and-rerun stays safe)."""
        if handle_row["state"] not in ("terminal", "collected"):
            return False
        try:
            backend = self._backends.resolve(handle_row["backend_id"])
        except Exception:
            return False
        return isinstance(backend, SshBackend)

    async def _route_ssh_terminal_strand(
        self,
        workstream_id: str,
        state: WorkstreamStatus,
        handle_row: dict[str, Any],
    ) -> None:
        """Park an SSH workstream stranded in the terminal/collect window for
        review, preserving the remote tmp. A crash between the terminal marker
        and collect cannot prove the remote diff was applied, so re-running
        would discard uncollected remote changes — route to NEEDS_REVIEW
        (the `collected`-row remote tmp is GC'd separately by the ownership-
        checked `_gc_terminal_handles` sweep; a `terminal` row is left intact)."""
        remote_dir = handle_row.get("remote_dir") or "<unknown>"
        reason = (
            f"ssh execution stranded in '{handle_row['state']}' after restart "
            f"(crash between terminal marker and collect); remote workspace "
            f"preserved at {remote_dir} — verify/collect before resuming"
        )
        self._logger.warning(
            "Workstream '%s' stranded in %s with an ssh execution in the "
            "terminal/collect window — sending to NEEDS_REVIEW; %s",
            workstream_id,
            state.value,
            reason,
        )
        await self._transition(
            workstream_id, WorkstreamStatus.FAILED, expected_status=state
        )
        await self._transition(
            workstream_id,
            WorkstreamStatus.NEEDS_REVIEW,
            expected_status=WorkstreamStatus.FAILED,
            process_pid=None,
            generation_pid=None,
            message=reason,
            error_message=reason,
        )
        self._stats.failed += 1

    async def _probe_open_handle(
        self, workstream_id: str, workstream_handles: dict[str, dict[str, Any]]
    ) -> bool:
        """Probe a non-local workstream's open handle for a possibly-live
        execution (Task 18: docker `probe_execution`; Task 16: ssh
        `probe_ssh`, both fail-closed).

        No-op (returns False) when there is no open, non-cleaned handle row
        for this workstream — a local-backed workstream (or one whose
        handle already reached `terminal`/`collected`/`cleaned`) is always
        unaffected, preserving pre-Task-18 recovery behavior exactly.

        SSH is fail-closed by design: `probe_ssh` always returns
        `needs_review=True` (a remote terminal marker cannot prove collect
        already applied), so an SSH-backed row here is never silently
        reclaimed. When the docker verdict confirms no container is left,
        the open handle row is closed (terminal -> cleaned) here so it
        doesn't linger open and shadow the workstream's next attempt after
        it's recovered to READY.
        """
        row = workstream_handles.get(workstream_id)
        if row is None:
            return False
        backend_id = row["backend_id"]
        if backend_id == "docker":
            verdict = await probe_execution(row["execution_id"], self._docker)
        else:
            backend = self._backends.resolve(backend_id)
            if isinstance(backend, SshBackend):
                ref = _handle_ref_from_row(row)
                decoded = decode_transport_ref(ref.transport_ref)
                ssh_verdict = await probe_ssh(backend._ssh, ref)
                if decoded["isolation"] == "docker":
                    # Phase 2c: an ssh-launched execution that itself runs the
                    # harness inside a container can strand TWO resources — the
                    # remote supervisor/process-group AND the container. Probe
                    # both and route to NEEDS_REVIEW if either says so; never
                    # trust ssh_verdict alone to rule out a leftover container.
                    if backend.isolation_kind != "docker" or backend.docker is None:
                        verdict = RecoveryVerdict(
                            True, "persisted docker isolation but backend is bare"
                        )
                    else:
                        cont = await probe_execution(
                            row["execution_id"],
                            backend.docker,
                            expected_labels=decoded["expected_labels"],
                        )
                        verdict = RecoveryVerdict(
                            ssh_verdict.needs_review or cont.needs_review,
                            f"ssh={ssh_verdict.reason}; container={cont.reason}",
                        )
                else:
                    verdict = ssh_verdict
            else:
                verdict = RecoveryVerdict(
                    True, f"unsupported backend {backend_id!r} for recovery probe"
                )
        if not verdict.needs_review:
            await self._db.mark_execution_state(
                row["execution_id"], "terminal", allowed_from=["prepared", "running"]
            )
            await self._db.mark_execution_state(
                row["execution_id"], "cleaned", allowed_from=["terminal"]
            )
        return verdict.needs_review

    async def _gc_terminal_handles(self, handles: list[dict[str, Any]]) -> int:
        """Best-effort, ownership-checked GC sweep for settled handles.

        Mirrors `StateRecovery._gc_terminal_handles` (Mode 1): a `terminal`
        (docker) or `collected` (ssh) handle means the entity behind it
        already reached — or passed through — a settled point (finalize
        ran) but the resource-cleanup confirmation was never persisted.
        This only removes the leftover container/remote artifacts and marks
        the handle `cleaned` — it never touches entity status. Swept across
        all entity kinds since the handle table is shared.

        Docker accepts either `terminal` or `collected`: `collect()` is a
        no-op for docker, so both states are equally safe to sweep (the
        phased finalize wiring, Task 16, can leave a docker row at either
        depending on exactly when a crash lands). SSH only ever sweeps
        `collected` — a `terminal`-but-not-`collected` SSH row means the
        remote diff was never confirmed applied, so it is left for human
        review (`ssh_recovery.probe_ssh` is fail-closed) rather than
        silently removed here — this is the SSH parallel of docker's
        terminal -> cleaned GC sweep.

        A row whose outcome is ambiguous (multiple container matches /
        label mismatch / probe error / no owner marker / resolve failure)
        is left in place for the next sweep or a human to resolve.
        """
        swept = 0
        for row in handles:
            state = row["state"]
            backend_id = row["backend_id"]
            if backend_id == "docker":
                if state not in ("terminal", "collected"):
                    continue
                outcome = await gc_terminal_handle(row, self._docker)
                if outcome in GC_CLEAN_OUTCOMES:
                    await self._db.mark_execution_state(
                        row["execution_id"], "cleaned", allowed_from=[state]
                    )
                    swept += 1
                else:
                    self._logger.warning(
                        "recovery: GC left handle %s (%s %s) as %s: %s",
                        row["execution_id"],
                        row["entity_kind"],
                        row["entity_id"],
                        state,
                        outcome,
                    )
                continue
            if state != "collected":
                continue
            try:
                backend = self._backends.resolve(backend_id)
                if not isinstance(backend, SshBackend):
                    continue
                ref = _handle_ref_from_row(row)
                decoded = decode_transport_ref(ref.transport_ref)
                if decoded["isolation"] == "docker":
                    # Container-first ordering: never delete the remote root
                    # (which may hold the only evidence of what the container
                    # touched) before the container itself is confirmed gone.
                    if backend.docker is None:
                        continue  # config no longer docker; leave for a human
                    dk_outcome = await gc_terminal_handle(
                        {"execution_id": row["execution_id"]},
                        backend.docker,
                        expected_labels=decoded["expected_labels"],
                    )
                    if dk_outcome not in GC_CLEAN_OUTCOMES:
                        self._logger.warning(
                            "recovery: container GC not clean for %s: %s — "
                            "leaving remote root intact",
                            row["execution_id"],
                            dk_outcome,
                        )
                        continue
                outcome = await gc_ssh_terminal(backend._ssh, ref)
            except Exception as e:
                self._logger.warning(
                    "recovery: ssh GC failed for handle %s (%s %s): %s",
                    row["execution_id"],
                    row["entity_kind"],
                    row["entity_id"],
                    e,
                )
                continue
            if outcome in _SSH_GC_CLEAN_OUTCOMES:
                await self._db.mark_execution_state(
                    row["execution_id"], "cleaned", allowed_from=["collected"]
                )
                swept += 1
            else:
                self._logger.warning(
                    "recovery: GC left handle %s (%s %s) as collected: %s",
                    row["execution_id"],
                    row["entity_kind"],
                    row["entity_id"],
                    outcome,
                )
        return swept

    async def _ensure_workstreams(self) -> None:
        """Ensure workstreams are in the database.

        If no workstreams exist, run decomposition.
        """
        existing = await self._db.get_all_workstreams()

        if existing:
            self._logger.info("Found %d existing workstreams", len(existing))
            self._stats.total_workstreams = len(existing)
            return

        # Use manually specified workstreams from config
        if self._config.workstreams:
            self._logger.info(
                "Creating %d workstreams from config",
                len(self._config.workstreams),
            )
            await self._create_workstreams_from_configs(self._config.workstreams)
            return

        # Auto-decompose
        if not self._config.description:
            msg = "No workstreams in config and no project description for auto-decomposition"
            raise OrchestratorError(msg)

        self._logger.info("Auto-decomposing project")
        configs = self._decomposer.decompose(self._config.description)
        await self._create_workstreams_from_configs(configs)

    async def _create_workstreams_from_configs(
        self, configs: list[WorkstreamConfig]
    ) -> None:
        """Create Workstream records in DB from configs."""
        for config in configs:
            workstream = Workstream.from_config(
                config,
                branch_prefix=self._config.branch_prefix,
            )
            await self._db.create_workstream(workstream)

        self._stats.total_workstreams = len(configs)
        self._logger.info("Created %d workstreams in database", len(configs))

    async def _main_loop(self) -> None:
        """Main orchestration loop."""
        poll_interval = 2.0

        while not self._shutdown_requested:
            # Get completed workstream IDs
            completed_ids = await self._get_completed_ids()

            # Check if all done
            if await self._all_workstreams_complete():
                self._logger.info("All workstreams complete")
                break

            # Resolve ready workstreams
            ready_ids = await self._resolve_ready(completed_ids)

            # Spawn up to max_concurrent
            await self._spawn_ready(ready_ids)

            # Monitor running processes
            await self._monitor_running()

            # Re-check any VERIFYING orphans recovered alive at startup
            # (Task 9 fix-up) — see `_poll_verifying_orphans`.
            await self._poll_verifying_orphans()

            # Wait before next iteration
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=poll_interval,
                )

    async def _get_completed_ids(self) -> set[str]:
        """Get IDs of completed workstreams."""
        done = await self._db.get_workstreams_by_status(WorkstreamStatus.DONE)
        return {z.id for z in done}

    async def _all_workstreams_complete(self) -> bool:
        """Check if all workstreams are in terminal states."""
        all_z = await self._db.get_all_workstreams()
        terminal = {
            WorkstreamStatus.DONE,
            WorkstreamStatus.ABANDONED,
        }

        for z in all_z:
            if z.status not in terminal:
                if z.status == WorkstreamStatus.NEEDS_REVIEW:
                    continue
                return False

        return True

    async def _resolve_ready(self, completed_ids: set[str]) -> list[str]:
        """Resolve workstreams that are ready to run.

        A workstream is ready when:
        - Status is PENDING or READY
        - All dependencies are completed
        - Not already running
        """
        all_z = await self._db.get_all_workstreams()
        ready: list[str] = []

        for z in all_z:
            if z.id in self._running:
                continue
            if z.id in self._generating:
                continue
            if z.status not in (
                WorkstreamStatus.PENDING,
                WorkstreamStatus.READY,
            ):
                continue

            # Check all dependencies completed
            if z.depends_on and not set(z.depends_on).issubset(completed_ids):
                continue

            ready.append(z.id)

        # Sort by priority (descending)
        all_by_id = {z.id: z for z in all_z}
        ready.sort(
            key=lambda zid: all_by_id[zid].priority,
            reverse=True,
        )

        return ready

    async def _spawn_ready(self, ready_ids: list[str]) -> None:
        """Launch background spec generation for ready workstreams up to the
        concurrency limit. Generation runs off the main loop so monitoring
        and shutdown stay responsive."""
        available = max(
            0,
            self._config.max_concurrent - len(self._running) - len(self._generating),
        )
        for zid in ready_ids[:available]:
            if self._shutdown_requested:
                break
            if zid in self._generating or zid in self._running:
                continue
            self._generating[zid] = asyncio.create_task(self._generate_and_launch(zid))

    async def _generate_and_launch(self, workstream_id: str) -> None:
        """Background task: generate the spec, then spawn `run --all`.

        - Cancellation (shutdown) → return the workstream to READY, no retry
          consumed, and propagate the cancel.
        - Any other error → _handle_failure (retry accounting).
        - The _generating slot is always freed in `finally`.
        """
        try:
            await self._spawn_workstream(workstream_id)
        except asyncio.CancelledError:
            # Clear generation_pid atomically in the same READY write: on the
            # cancel path the `finally` clear can itself be interrupted by a
            # re-raised CancelledError before its awaits complete, so cleanup
            # must not depend on it here. Re-read the current status right
            # before writing so the CAS reflects the actual pre-cancel state
            # (typically DECOMPOSING) rather than a hardcoded guess.
            with contextlib.suppress(Exception):
                current = await self._db.get_workstream(workstream_id)
                await self._transition(
                    workstream_id,
                    WorkstreamStatus.READY,
                    expected_status=current.status,
                    generation_pid=None,
                )
            raise
        except Exception as e:
            self._logger.error(
                "Failed to generate spec or launch workstream '%s': %s",
                workstream_id,
                e,
            )
            await self._handle_failure(workstream_id, str(e))
        finally:
            self._generating.pop(workstream_id, None)
            # Clear the generation pid on every exit (success/cancel/failure);
            # a stale pid only pollutes REST/dashboard, but keep it clean.
            # Same-state field patch (spec §4.2) — no dispatch.
            with contextlib.suppress(Exception):
                w = await self._db.get_workstream(workstream_id)
                if w.generation_pid is not None:
                    await self._update_fields(workstream_id, generation_pid=None)

    async def _spawn_workstream(self, workstream_id: str) -> None:
        """Spawn a spec-runner process for a workstream."""
        workstream = await self._db.get_workstream(workstream_id)

        # Gates v1.2 (H-6): an operator-approved ex-post block resumes at
        # the ex-post edge over the untouched worktree. Replaying the
        # pipeline would regenerate the spec, mint a new sha, and void the
        # approval (DESIGN-608). Must run before DECOMPOSING and before the
        # spawning sentinel — a resume never spawns anything.
        marker = parse_approval_marker(workstream.error_message)
        if (
            marker is not None
            and marker.phase == "ex_post"
            and await self._try_resume_ex_post(workstream, marker)
        ):
            return

        # Stage B resume dispatch (before DECOMPOSING — a resume never
        # re-decomposes). REVERIFY re-enters the verification loop over the
        # untouched worktree; REWORK is the ONLY path that respawns the author,
        # and it does so with the FAIL findings appended to the spec-gen
        # description (§7 declassification channel).
        rework_addendum: str | None = None
        if workstream.resume_reason == RESUME_REVERIFY:
            workspace = self._workspace_mgr.get_workspace_path(workstream_id)
            await self._transition(
                workstream_id,
                WorkstreamStatus.VERIFYING,
                expected_status=WorkstreamStatus.READY,
                resume_reason=None,
                verification_error_attempt=0,
            )
            await self._run_verification(workstream_id, workspace)
            return
        is_rework_resume = workstream.resume_reason == RESUME_REWORK
        if is_rework_resume:
            rework_addendum = await self._load_rework_addendum(workstream)

        # Transition to DECOMPOSING; write the spawning sentinel up front — it
        # marks a spawn-in-progress AND overwrites any stale prior generation
        # pid (re-decompose).
        await self._transition(
            workstream_id,
            WorkstreamStatus.DECOMPOSING,
            expected_status=workstream.status,
            generation_pid=_SPAWNING_SENTINEL,
        )

        # Create workspace
        if not self._workspace_mgr.workspace_exists(workstream_id):
            workspace = self._workspace_mgr.create_workspace(
                workstream_id, workstream.branch
            )
        else:
            workspace = self._workspace_mgr.get_workspace_path(workstream_id)

        # Update workspace path in DB (same-state field patch, no dispatch).
        await self._update_fields(workstream_id, workspace_path=str(workspace))

        # H-7: keep every harness artifact untracked in the target repo —
        # repo-local ignore block, shared by all linked worktrees.
        await asyncio.get_running_loop().run_in_executor(
            None, ensure_harness_excludes, workspace
        )

        # Setup spec-runner config BEFORE generation so `plan --full`
        # writes prefix-namespaced spec files from the start (H-7).
        executor_config = self._config.spec_runner.to_executor_config()
        # Set main_branch to the workstream branch (so spec-runner
        # merges subtask branches back to it)
        executor_config.setdefault("executor", {})["main_branch"] = workstream.branch
        self._workspace_mgr.setup_spec_runner(workspace, executor_config)

        # Generate spec for this workstream
        # Always regenerate: the repo may already have spec/maestro-tasks.md
        # from a previous run or different project phase.
        # On a Stage B rework respawn, the verifier's FAIL feedback is appended
        # to the description so spec generation can incorporate it (§7).
        description = workstream.description
        if rework_addendum is not None:
            description = f"{workstream.description}\n\n{rework_addendum}"
        workstream_config = WorkstreamConfig(
            id=workstream.id,
            title=workstream.title,
            description=description,
            scope=workstream.scope,
            depends_on=workstream.depends_on,
            priority=workstream.priority,
        )

        async def _on_gen_pid(pid: int) -> None:
            # Same-state field patch (still DECOMPOSING) — no dispatch.
            await self._update_fields(workstream_id, generation_pid=pid)

        await self._decomposer.generate_spec(
            workstream_config, workspace, on_pid=_on_gen_pid
        )

        # Transition to READY then RUNNING
        await self._transition(
            workstream_id,
            WorkstreamStatus.READY,
            expected_status=WorkstreamStatus.DECOMPOSING,
        )

        # Gates (WS-006): ex-ante guard over the declared scope. A block
        # routes to NEEDS_REVIEW; the operator re-queueing it approves the
        # gate for this exact repo SHA (see maestro/gates.py).
        if not await self._gate_ex_ante(workstream_id, workstream):
            return

        # Resolve the execution backend for this workstream (per-entity,
        # falling back to the configured/default backend) BEFORE the
        # READY->RUNNING transition, so the transition can branch on whether
        # this is a durable (non-local) execution.
        backend = self._backends.resolve(workstream.backend)

        # Local-only fail-fast (spec §8): non-local backends must prove the
        # target daemon is reachable — and not a remote DOCKER_HOST — before
        # the READY->RUNNING transition/spawn. A block routes READY ->
        # NEEDS_REVIEW instead, mirroring _route_gate_block, so a docker
        # workstream never CAS's to RUNNING against an unreachable/remote
        # daemon. The local path stays a true no-op — the call is skipped
        # entirely for backend.id == "local".
        if backend.id != "local":
            health = await backend.healthcheck()
            if not health.reachable:
                reason = f"backend {backend.id} not reachable: {health.detail}"
                self._logger.warning(
                    "Workstream '%s' backend healthcheck failed: %s",
                    workstream_id,
                    reason,
                )
                await self._transition(
                    workstream_id,
                    WorkstreamStatus.NEEDS_REVIEW,
                    expected_status=WorkstreamStatus.READY,
                    message=reason,
                    error_message=reason,
                )
                return

        # SSH capability gate (spec C.4): reachability (healthcheck) does not
        # prove the remote host actually has `spec-runner` on PATH. Probe it
        # BEFORE the READY->RUNNING CAS so a missing remote tool routes READY
        # -> NEEDS_REVIEW (mirroring the healthcheck-failure block) instead of
        # spawning a supervisor whose workload can never exec. SSH-only;
        # local/docker are unaffected.
        if isinstance(backend, SshBackend):
            cap = await backend.can_run(
                ExecutionRequest(
                    run_id=workstream_id,
                    argv=["spec-runner"],
                    workdir=workspace,
                    log_path=self._log_dir / f"{workstream_id}.log",
                    collect=CollectPolicy(mode="none"),
                    required_tools=["spec-runner"],
                )
            )
            if not cap.ok:
                reason = (
                    f"backend {backend.id} missing required tools on remote: "
                    f"{cap.missing_tools}"
                )
                self._logger.warning(
                    "Workstream '%s' backend can_run gate failed: %s",
                    workstream_id,
                    reason,
                )
                await self._transition(
                    workstream_id,
                    WorkstreamStatus.NEEDS_REVIEW,
                    expected_status=WorkstreamStatus.READY,
                    message=reason,
                    error_message=reason,
                )
                return

        execution_id: str | None = None
        attempt: int = 1
        request_launch_fields: dict[str, object] = {}

        if backend.id != "local":
            # Non-local backends mint a durable execution identity: the
            # READY->RUNNING CAS and the execution_handles insert are one
            # atomic DB transaction (start_execution), so the already-
            # committed transition is dispatched directly afterward rather
            # than through the plain _transition helper (mirrors
            # Scheduler._spawn_task's docker branch, Task 16). The local
            # path below is unchanged.
            execution_id = str(uuid.uuid4())
            attempt = workstream.retry_count + 1
            await self._db.start_execution(
                entity_kind="workstream",
                entity_id=workstream_id,
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id=execution_id,
                backend_id=backend.id,
                transport_ref=f"{backend.id}:maestro-{execution_id}",
                attempt=attempt,
            )
            request_launch_fields = {
                "execution_id": execution_id,
                "entity_kind": "workstream",
                "attempt": attempt,
            }
            refreshed = await self._db.get_workstream(workstream_id)
            await self._dispatcher.fire(_subject(refreshed), frm=WorkstreamStatus.READY)
        else:
            await self._transition(
                workstream_id,
                WorkstreamStatus.RUNNING,
                expected_status=WorkstreamStatus.READY,
                process_pid=_SPAWNING_SENTINEL,
            )

        # Spawn spec-runner
        log_file = self._log_dir / f"{workstream_id}.log"

        cmd = ["spec-runner", "run", "--all", "--spec-prefix", SPEC_PREFIX]

        # Add callback URL if REST API is running
        # (optional — we also poll state files)
        if self._config.callback_url:
            cmd.extend(["--callback-url", self._config.callback_url])

        mirror_dir: Path | None = None
        if isinstance(backend, SshBackend):
            # Remote (ssh) execution: whole-worktree collect on completion,
            # plus a live progress mirror — spec-runner's sqlite state lives
            # only on the remote host while the run is in flight, so
            # `_update_progress` reads from the mirror instead (Task 16).
            mirror_dir = self._log_dir / f"{workstream_id}.mirror"
            request = build_ssh_execution_request(
                workstream_id=workstream_id,
                workspace=str(workspace),
                log_file=str(log_file),
                cmd=cmd,
                execution_id=cast("str", execution_id),
                attempt=attempt,
                mirror_dir=str(mirror_dir),
            )
        else:
            request = ExecutionRequest(
                run_id=workstream_id,
                argv=cmd,
                workdir=workspace,
                log_path=log_file,
                inherit_env=True,
                collect=CollectPolicy(mode="none"),
                required_tools=["spec-runner"],
            )

        request = request.model_copy(
            update={"backend_id": backend.id, **request_launch_fields}
        )

        with span("task.execute", task_id=workstream_id):
            handle = await backend.run(request)

        # Register in _running BEFORE any further await, so a shutdown
        # cancellation can never orphan the spawned process: once it's
        # here, _cleanup's termination loop will reach it regardless of
        # where a later cancel lands.
        self._running[workstream_id] = RunningWorkstream(
            workstream=workstream.model_copy(
                update={
                    "status": WorkstreamStatus.RUNNING,
                    "workspace_path": str(workspace),
                }
            ),
            handle=handle,
            started_at=datetime.now(UTC),
            workspace_path=workspace,
            log_file=log_file,
            execution_id=execution_id,
            backend_id=backend.id,
            mirror_dir=mirror_dir,
        )

        if isinstance(backend, SshBackend):
            # Persist the coordinates SshBackend.run() actually minted (the
            # JSON transport_ref, remote_dir, status_marker) — start_execution
            # only seeded a plain-string placeholder before the launch, so
            # without this write ssh_recovery can never locate the remote
            # workspace (Task 16b).
            info = decode_transport_ref(handle.ref.transport_ref)
            await self._db.update_execution_handle_launch(
                cast("str", execution_id),
                transport_ref=handle.ref.transport_ref,
                remote_host=info.get("host"),
                remote_dir=info.get("remote_dir"),
                status_marker=handle.ref.status_marker,
            )

        # Update PID in DB (same-state field patch, no dispatch). Docker
        # recovery uses execution_handles, not pid (Task 18) — leaving the
        # real pid here for non-local backends too is harmless.
        await self._update_fields(workstream_id, process_pid=handle.os_pid)

        # Stage B: clear the rework marker ONLY after a successful respawn, so
        # a crash before the process is live re-picks the rework path (and its
        # addendum) rather than a plain re-run.
        if is_rework_resume:
            await self._update_fields(workstream_id, resume_reason=None)

        self._logger.info(
            "Spawned spec-runner for '%s' (PID %s) in %s",
            workstream_id,
            handle.os_pid,
            workspace,
        )

    def _merge_into_base(self, feature_branch: str) -> None:
        """Merge feature branch into base branch in the main repo.

        Prevents accumulation of unmerged branches that diverge and cause
        conflicts. Each workstream is merged immediately after completion so
        the next workstream sees all prior work.

        Verifies the main repo is on ``base_branch`` before merging (the
        Mode-2 worktree topology keeps it there); a wrong or detached branch
        raises rather than silently merging into the wrong place. On a merge
        failure the partial merge is aborted and the error raised so the
        caller can route the workstream to review instead of DONE.

        Raises:
            GitError: If the repo is not on ``base_branch``, or the merge
                fails for a non-conflict reason.
            MergeConflictError: If the merge has conflicts.
        """
        repo = Path(self._config.repo_path).expanduser()
        base = self._config.base_branch
        merge_env = {**os.environ, **child_env()}

        with span("task.execute", task_id=feature_branch):
            head = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo,
                env=merge_env,
                capture_output=True,
                text=True,
                check=False,
            )
            current_branch = head.stdout.strip()
            if head.returncode != 0 or current_branch != base:
                msg = (
                    f"Refusing to merge '{feature_branch}': main repo is on "
                    f"'{current_branch or '(unknown)'}', not base '{base}'. "
                    "The main repo must be checked out on the base branch."
                )
                raise GitError(msg)

            result = subprocess.run(
                ["git", "merge", feature_branch, "--no-edit"],
                cwd=repo,
                env=merge_env,
                capture_output=True,
                text=True,
                check=False,
            )

        if result.returncode == 0:
            self._logger.info("Merged '%s' into '%s'", feature_branch, base)
            return

        # Abort the partial/conflicted merge so the base repo is left clean,
        # then raise so the caller routes the workstream to review, not DONE.
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=repo,
            env=merge_env,
            capture_output=True,
            text=True,
            check=False,
        )
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        # git writes "CONFLICT (...)" to stdout, not stderr; combine both so
        # the conflict marker is detected regardless of which stream git
        # chooses, and so the log and the raised error carry the same detail.
        detail = "\n".join(part for part in (stderr, stdout) if part)
        self._logger.warning(
            "Failed to merge '%s' into '%s': %s", feature_branch, base, detail
        )
        if "conflict" in detail.lower():
            msg = f"Merge conflicts merging '{feature_branch}' into '{base}':\n{detail}"
            raise MergeConflictError(msg)
        msg = f"Failed to merge '{feature_branch}' into '{base}':\n{detail}"
        raise GitError(msg)

    async def _monitor_running(self) -> None:
        """Monitor running spec-runner processes."""
        completed: list[str] = []

        for zid, running in self._running.items():
            # Read progress from state file
            await self._update_progress(zid, running)

            # Check if process finished (returncode is None while running)
            return_code = running.handle.poll()

            if return_code is not None:
                # Process finished — finalize (reap/collect/cleanup) exactly
                # once. Callbacks persist the "terminal"/"collected" phases
                # BETWEEN wait/collect/cleanup (Task 16), so a crash mid-
                # finalize can never leave durable state that lies. Docker/
                # local: collect() no-ops and always succeeds, so this is
                # functionally unchanged — only the intermediate DB phase
                # now persists earlier.
                eid = running.execution_id

                async def _mark_terminal(eid: str | None = eid) -> None:
                    if eid is not None:
                        await self._db.mark_execution_state(
                            eid, "terminal", allowed_from=["prepared", "running"]
                        )

                async def _mark_collected(eid: str | None = eid) -> None:
                    if eid is not None:
                        await self._db.mark_execution_state(
                            eid, "collected", allowed_from=["terminal"]
                        )

                fin = await asyncio.shield(
                    ensure_finalize_task(
                        running,
                        on_terminal=_mark_terminal,
                        on_collected=_mark_collected,
                    )
                )
                if eid is not None and fin.cleaned:
                    await self._db.mark_execution_state(
                        eid, "cleaned", allowed_from=["collected"]
                    )
                if fin.collect_error or fin.cleanup_error:
                    self._logger.warning(
                        "execution.finalize.resource_fault workstream=%s "
                        "collect_error=%s cleanup_error=%s",
                        zid,
                        fin.collect_error,
                        fin.cleanup_error,
                    )
                if not fin.collect_succeeded:
                    # Remote tmp + staging preserved; do NOT enter PR/gate
                    # flow — a human must inspect/resolve the collect
                    # conflict before this workstream can proceed.
                    await self._transition(
                        zid,
                        WorkstreamStatus.NEEDS_REVIEW,
                        expected_status=WorkstreamStatus.RUNNING,
                        message="collect failed/conflict; remote workspace preserved",
                        error_message=(fin.collect_error or "collect failed"),
                    )
                    completed.append(zid)
                    continue
                await self._handle_completion(zid, running, fin.execution.exit_code)
                completed.append(zid)

        for zid in completed:
            del self._running[zid]

    async def _update_progress(
        self,
        workstream_id: str,
        running: RunningWorkstream,
    ) -> None:
        """Read spec-runner state file for progress.

        Delegates to `maestro.spec_runner.read_executor_state()` so SQLite
        (spec-runner 2.0) and JSON (legacy) are handled uniformly. Runs the
        blocking read in a thread so the orchestrator loop stays responsive.

        For an ssh execution, `running.mirror_dir` points at the local
        WAL-mirror of the remote spec dir (Task 16) — the live remote spec
        dir isn't locally readable while the run is in flight. Local/docker
        runs have no mirror and read the live workspace `spec` dir directly,
        unchanged.
        """
        spec_dir = (
            running.mirror_dir
            if running.mirror_dir is not None
            else running.workspace_path / "spec"
        )
        loop = asyncio.get_running_loop()
        state = await loop.run_in_executor(
            None, read_executor_state, spec_dir, SPEC_PREFIX
        )

        if state is None:
            return

        # Same-state field patch (still RUNNING) — no dispatch.
        await self._update_fields(
            workstream_id, subtask_progress=state.progress_label()
        )

    async def _gate_ex_ante(self, workstream_id: str, workstream: Workstream) -> bool:
        """Evaluate the ex-ante gate; on block route READY -> NEEDS_REVIEW."""
        if self._gates is None:
            return True
        approvals = await self._db.list_gate_approvals(workstream_id)
        decision = await self._gates.evaluate_ex_ante(
            workstream_id, workstream.scope, approvals=approvals
        )
        if decision.allow:
            return True
        await self._route_gate_block(
            workstream_id, decision, expected=WorkstreamStatus.READY
        )
        return False

    @staticmethod
    async def _workspace_head(workspace: Path) -> str | None:
        """HEAD sha of a worktree, or None when unreadable."""
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(workspace),
            "rev-parse",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            return None
        return stdout.decode().strip()

    async def _try_resume_ex_post(
        self, workstream: Workstream, marker: ApprovalMarker
    ) -> bool:
        """Resume an ex-post-approved workstream at the ex-post edge (H-6).

        True only when the worktree still exists AND sits exactly at the
        approved sha; then the workstream goes READY -> RUNNING (explicitly
        clearing both pids so recovery never mistakes it for a live orphan)
        and re-enters the success continuation — ex-post re-evaluates,
        passes via the approval memory, and the normal MERGING -> PR ->
        merge -> DONE tail runs. False = caller proceeds with the full
        respawn (the approval is genuinely void, DESIGN-608).
        """
        if not self._workspace_mgr.workspace_exists(workstream.id):
            self._logger.warning(
                "Workstream '%s' has an ex-post approval for sha %s but no "
                "workspace; falling back to a full respawn",
                workstream.id,
                marker.sha[:12],
            )
            return False
        workspace = self._workspace_mgr.get_workspace_path(workstream.id)
        head = await self._workspace_head(workspace)
        if head != marker.sha:
            self._logger.warning(
                "Workstream '%s' worktree HEAD %s != approved sha %s; the "
                "approval is void (DESIGN-608), falling back to a full respawn",
                workstream.id,
                (head or "unreadable")[:12],
                marker.sha[:12],
            )
            return False
        await self._transition(
            workstream.id,
            WorkstreamStatus.RUNNING,
            expected_status=workstream.status,
            process_pid=None,
            generation_pid=None,
            workspace_path=str(workspace),
        )
        self._logger.info(
            "Resuming workstream '%s' at the ex-post gate (approved sha %s)",
            workstream.id,
            marker.sha[:12],
        )
        await self._handle_success(workstream.id, workspace)
        return True

    async def _route_scope_block(self, workstream_id: str, reason: str) -> bool:
        """Fail-closed RUNNING -> FAILED -> NEEDS_REVIEW for the scope gate.

        The worktree is left intact for inspection. Returns False (blocked).
        """
        await self._transition(
            workstream_id,
            WorkstreamStatus.FAILED,
            expected_status=WorkstreamStatus.RUNNING,
            message=reason,
            error_message=reason,
        )
        await self._transition(
            workstream_id,
            WorkstreamStatus.NEEDS_REVIEW,
            expected_status=WorkstreamStatus.FAILED,
            message=reason,
            error_message=reason,
        )
        self._stats.failed += 1
        return False

    async def _gate_scope(
        self,
        workstream_id: str,
        workstream: Workstream,
        workspace_path: Path,
    ) -> bool:
        """Deterministic always-on scope containment (ex-post edge).

        The workstream's own committed diff must touch only paths matched by
        its declared scope. Escapes block RUNNING -> FAILED -> NEEDS_REVIEW
        with a marker-bearing reason. Empty scope skips. Infrastructure
        failures (unreadable HEAD, a git error computing the diff) fail closed
        to NEEDS_REVIEW with a marker-less reason -- containment could not be
        evaluated, so the operator must fix the worktree rather than approve.
        """
        scope = workstream.scope
        if not scope:
            return True
        head = await self._workspace_head(workspace_path)
        if head is None:
            reason = "scope-gate: cannot read worktree HEAD"
            self._logger.warning("%s for '%s'", reason, workstream_id)
            return await self._route_scope_block(workstream_id, reason)
        approvals = await self._db.list_gate_approvals(workstream_id)
        if ("ex_post", head) in approvals:
            return True
        try:
            paths = await changed_paths_since(
                self._config.base_branch, "HEAD", workspace_path
            )
        except RuntimeError as exc:
            reason = f"scope-gate: cannot compute changed paths: {exc}"
            self._logger.warning("%s for '%s'", reason, workstream_id)
            return await self._route_scope_block(workstream_id, reason)
        # changed_paths_since already returns normalized paths; only the
        # declared scope patterns still need normalizing.
        escapes = find_escapes(paths, normalize(scope))
        if not escapes:
            return True
        self._logger.warning(
            "scope escape in '%s' (%d paths): %s",
            workstream_id,
            len(escapes),
            escapes,  # FULL list to structured log
        )
        reason = build_scope_escape_reason(escapes, head)
        return await self._route_scope_block(workstream_id, reason)

    async def _gate_ex_post(
        self,
        workstream_id: str,
        workstream: Workstream,
        workspace_path: Path,
    ) -> bool:
        """Evaluate the ex-post gate; on block route RUNNING -> FAILED -> NEEDS_REVIEW."""
        if self._gates is None:
            return True
        approvals = await self._db.list_gate_approvals(workstream_id)
        decision = await self._gates.evaluate_ex_post(
            workstream_id,
            workstream.scope,
            workspace=workspace_path,
            approvals=approvals,
        )
        if decision.allow:
            return True
        await self._transition(
            workstream_id,
            WorkstreamStatus.FAILED,
            expected_status=WorkstreamStatus.RUNNING,
            message=decision.reason,
            error_message=decision.reason,
        )
        await self._route_gate_block(
            workstream_id, decision, expected=WorkstreamStatus.FAILED
        )
        self._stats.failed += 1
        # Leave the workspace intact so a human can inspect the diff.
        return False

    async def _route_gate_block(
        self,
        workstream_id: str,
        decision: GateDecision,
        *,
        expected: WorkstreamStatus,
    ) -> None:
        self._logger.warning(
            "Gates blocked workstream '%s': %s", workstream_id, decision.reason
        )
        await self._transition(
            workstream_id,
            WorkstreamStatus.NEEDS_REVIEW,
            expected_status=expected,
            message=decision.reason,
            error_message=decision.reason,
        )

    async def _handle_completion(
        self,
        workstream_id: str,
        running: RunningWorkstream,
        return_code: int | None,
    ) -> None:
        """Handle spec-runner process completion.

        Args:
            workstream_id: ID of the completed workstream.
            running: The running workstream info.
            return_code: Process exit code (``None`` is treated as failure —
                it never compares equal to 0).
        """
        if return_code == 0:
            self._logger.info(
                "Workstream '%s' completed successfully (backend=%s)",
                workstream_id,
                running.backend_id,
            )
            await self._handle_success(workstream_id, running.workspace_path)
        else:
            self._logger.warning(
                "Workstream '%s' failed (code %s)",
                workstream_id,
                return_code,
            )
            await self._handle_failure(
                workstream_id,
                f"spec-runner exited with code {return_code}",
            )

    async def _handle_success(
        self,
        workstream_id: str,
        workspace_path: Path,
    ) -> None:
        """Handle successful workstream completion.

        Push branch, create PR, cleanup workspace.
        """
        workstream = await self._db.get_workstream(workstream_id)

        # Deterministic scope containment (always-on, before the optional
        # steward risk gate). Fail-fast on out-of-scope commits.
        if not await self._gate_scope(workstream_id, workstream, workspace_path):
            return

        # Gates (WS-006): ex-post guard over the actual diff (catches scope
        # violations and tier escalations the declared scope did not show).
        if not await self._gate_ex_post(workstream_id, workstream, workspace_path):
            return

        # Stage B: with a domain profile active, the workstream must pass its
        # final verification before delivery. Enter VERIFYING and hand off to
        # the verification loop; the loop owns every terminal transition from
        # here (MERGING on PASS via finalization, READY/NEEDS_REVIEW/FAILED
        # otherwise). Legacy (domain=None) falls straight through to the
        # merge/PR tail, byte-identical to before.
        profile = self._config.domain
        if profile is not None:
            await self._transition(
                workstream_id,
                WorkstreamStatus.VERIFYING,
                expected_status=WorkstreamStatus.RUNNING,
                verification_error_attempt=0,
            )
            await self._run_verification(workstream_id, workspace_path)
            return

        await self._merge_and_pr(
            workstream_id, workstream, expected_status=WorkstreamStatus.RUNNING
        )

    async def _merge_and_pr(
        self,
        workstream_id: str,
        workstream: Workstream,
        *,
        expected_status: WorkstreamStatus,
    ) -> None:
        """Delivery tail: MERGING -> (PR) -> merge-into-base -> DONE + cleanup.

        Shared by the legacy success path (`expected_status=RUNNING`) and
        Stage B finalization (`expected_status=VERIFYING`); the only
        difference is the state the workstream transitions into MERGING from.
        """
        # Transition to MERGING
        await self._transition(
            workstream_id,
            WorkstreamStatus.MERGING,
            expected_status=expected_status,
        )

        # Push branch and create PR
        if self._config.auto_pr:
            try:
                pr_url = self._pr_manager.push_and_create_pr(
                    branch=workstream.branch,
                    title=f"[Maestro] {workstream.title}",
                    body=self._build_pr_body(workstream),
                    base_branch=self._config.base_branch,
                )

                await self._transition(
                    workstream_id,
                    WorkstreamStatus.PR_CREATED,
                    expected_status=WorkstreamStatus.MERGING,
                    pr_url=pr_url,
                )

                self._stats.prs_created += 1
                self._logger.info(
                    "Created PR for '%s': %s",
                    workstream_id,
                    pr_url,
                )
            except PRManagerError as e:
                self._logger.warning(
                    "Failed to create PR for '%s': %s",
                    workstream_id,
                    e,
                )
                # Still mark as PR_CREATED (PR may exist). If error_message
                # currently carries an ex-post approval marker (H-6 resume
                # tail), APPEND the note instead of overwriting it: an
                # overwrite would destroy the marker mid-resume, and a
                # later crash before DONE would then full-respawn instead
                # of resuming (the marker clears ONLY on the DONE write).
                note = f"PR creation note: {e}"
                await self._transition(
                    workstream_id,
                    WorkstreamStatus.PR_CREATED,
                    expected_status=WorkstreamStatus.MERGING,
                    error_message=preserve_approval_marker(
                        note, workstream.error_message
                    ),
                )

        # Ensure the workstream is at PR_CREATED (both auto_pr paths converge
        # here); auto_pr=False creates no PR, so pass MERGING -> PR_CREATED.
        current = await self._db.get_workstream(workstream_id)
        if current.status == WorkstreamStatus.MERGING:
            await self._transition(
                workstream_id,
                WorkstreamStatus.PR_CREATED,
                expected_status=WorkstreamStatus.MERGING,
            )

        # Merge the feature branch into base BEFORE marking DONE, so DONE is
        # gated on a successful merge. A conflict/failure routes to
        # NEEDS_REVIEW (a human resolves it; re-running run --all cannot), and
        # a crash mid-merge leaves the workstream pre-DONE for startup recovery.
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                self._merge_into_base,
                workstream.branch,
            )
        except GitError as e:
            self._logger.warning(
                "Base merge failed for '%s'; routing to NEEDS_REVIEW: %s",
                workstream_id,
                e,
            )
            merge_failed_msg = f"Base merge failed: {e}"
            await self._transition(
                workstream_id,
                WorkstreamStatus.FAILED,
                expected_status=WorkstreamStatus.PR_CREATED,
                message=merge_failed_msg,
                error_message=merge_failed_msg,
            )
            await self._transition(
                workstream_id,
                WorkstreamStatus.NEEDS_REVIEW,
                expected_status=WorkstreamStatus.FAILED,
                message=merge_failed_msg,
            )
            self._stats.failed += 1
            # Leave the workspace intact so a human can resolve the conflict.
            return

        # Merge succeeded -> DONE. Clear error_message here ONLY: it may
        # still hold an ex-post approval marker (H-6) that recovery needs
        # to survive a crash-tail through MERGING/PR_CREATED (both reset to
        # READY on recovery, and the next run must still see the marker to
        # resume instead of full-respawning).
        await self._transition(
            workstream_id,
            WorkstreamStatus.DONE,
            expected_status=WorkstreamStatus.PR_CREATED,
            error_message=None,
        )
        self._stats.completed += 1

        # Cleanup workspace
        self._workspace_mgr.cleanup_workspace(workstream_id)

    async def _run_verification(self, workstream_id: str, workspace_path: Path) -> None:
        """Drive the VERIFYING loop for one workstream (Stage B, §4/§6).

        The workstream is already in VERIFYING. Runs the configured verifier
        with a per-session ERROR-retry budget; owns every terminal transition:
        PASS -> finalization (MERGING), FAIL -> READY (rework) or NEEDS_REVIEW
        (rework budget exhausted), ERROR budget exhausted -> FAILED (reverify).
        """
        profile = self._config.domain
        assert profile is not None and self._ledger is not None
        ledger = self._ledger
        verification = profile.verification

        workstream = await self._db.get_workstream(workstream_id)
        run_id = workstream.verification_run_id
        if not run_id:
            run_id = str(uuid.uuid4())
            await self._update_fields(workstream_id, verification_run_id=run_id)

        commit = await self._workspace_head(workspace_path)
        tree = await self._workspace_tree(workspace_path)
        if commit is None or tree is None:
            reason = "verification: cannot read worktree HEAD/tree"
            self._logger.warning("%s for '%s'", reason, workstream_id)
            await self._transition(
                workstream_id,
                WorkstreamStatus.NEEDS_REVIEW,
                expected_status=WorkstreamStatus.VERIFYING,
                message=reason,
                error_message=reason,
            )
            self._stats.failed += 1
            return

        backend = self._backends.resolve(workstream.backend)
        verifier = CommandVerifier(
            verification.verifier,
            verification.criteria,
            verification.artifact,
            backend,
            db=self._db,
        )
        prof_sha = profile_sha256(profile)
        error_budget = verification.verifier.error_retry_budget

        error_attempt = 0
        last_error = "verification error budget exhausted"
        while error_attempt <= error_budget:
            attempt = workstream.verification_attempt + 1
            await self._update_fields(workstream_id, verification_attempt=attempt)
            workstream = await self._db.get_workstream(workstream_id)

            staging = ledger.staging_dir(workstream_id, run_id, attempt)
            out_json = staging / f"attempt-{attempt:03d}.json"
            ctx = VerificationContext(
                workstream_id=workstream_id,
                run_id=run_id,
                attempt=attempt,
                rework_attempt=workstream.rework_attempt,
                worktree=workspace_path,
                out_json=out_json,
                profile_sha256=prof_sha,
                verified_source_commit=commit,
                verified_source_tree=tree,
            )
            result = await verifier.verify(ctx)
            # Every outcome is evidence. A protocol ERROR that never produced a
            # verdict file (pre-spawn violation, or a verifier that wrote
            # nothing) still gets a forensic bundle so ingest has something to
            # store — the DB row's verdict/protocol_error carry the meaning.
            if not out_json.exists():
                out_json.parent.mkdir(parents=True, exist_ok=True)
                out_json.write_text(
                    json.dumps(
                        {
                            "outcome": result.outcome.value,
                            "protocol_error": result.protocol_error,
                        }
                    )
                )
            await ledger.ingest_attempt(
                workstream_id=workstream_id,
                run_id=run_id,
                attempt=attempt,
                result=result,
                staging=staging,
            )

            if result.outcome is VerdictValue.PASS:
                await self._finalize_verification(
                    workstream_id,
                    workspace_path,
                    run_id=run_id,
                    verified_source_commit=commit,
                )
                return
            if result.outcome is VerdictValue.FAIL:
                await self._route_fail(workstream_id, workstream, verification)
                return

            # ERROR: consume one unit of the per-session retry budget.
            last_error = result.protocol_error or "verification protocol error"
            error_attempt += 1
            await self._update_fields(
                workstream_id, verification_error_attempt=error_attempt
            )

        # Error budget exhausted: fail-closed to FAILED, tagged for reverify so
        # an operator re-queue routes back to VERIFYING (never an author
        # respawn, §4 invariant).
        self._logger.warning(
            "Verification error budget exhausted for '%s': %s",
            workstream_id,
            last_error,
        )
        await self._transition(
            workstream_id,
            WorkstreamStatus.FAILED,
            expected_status=WorkstreamStatus.VERIFYING,
            resume_reason=RESUME_REVERIFY,
            message=last_error,
            error_message=last_error,
        )
        self._stats.failed += 1

    async def _route_fail(
        self,
        workstream_id: str,
        workstream: Workstream,
        verification: VerificationSection,
    ) -> None:
        """Route a genuine FAIL verdict: rework respawn or review.

        Under budget -> READY tagged RESUME_REWORK (the ONLY path that sets it,
        §4 invariant), pids cleared; budget exhausted -> NEEDS_REVIEW.
        """
        if workstream.rework_attempt < verification.rework_budget:
            await self._transition(
                workstream_id,
                WorkstreamStatus.READY,
                expected_status=WorkstreamStatus.VERIFYING,
                resume_reason=RESUME_REWORK,
                rework_attempt=workstream.rework_attempt + 1,
                process_pid=None,
                generation_pid=None,
            )
            return
        reason = (
            f"verification FAILED and rework budget "
            f"({verification.rework_budget}) is exhausted"
        )
        self._logger.warning("%s for '%s'", reason, workstream_id)
        await self._transition(
            workstream_id,
            WorkstreamStatus.NEEDS_REVIEW,
            expected_status=WorkstreamStatus.VERIFYING,
            message=reason,
            error_message=reason,
        )
        self._stats.failed += 1

    async def _finalize_verification(
        self,
        workstream_id: str,
        workspace_path: Path,
        *,
        run_id: str,
        verified_source_commit: str,
    ) -> None:
        """PASS finalization (§8): materialize evidence, commit it, deliver.

        The workstream stays in VERIFYING throughout preparation. A
        delivery-preparation error (unwritable evidence root, git/IO failure)
        leaves it in VERIFYING so recovery re-enters idempotently; a
        containment violation (evidence escaping verifier.write, or a stale
        PASS whose parent no longer matches the verified commit) fails closed
        to NEEDS_REVIEW. Only a clean evidence commit proceeds to the delivery
        tail (MERGING -> PR -> merge -> DONE).
        """
        profile = self._config.domain
        assert profile is not None and self._ledger is not None
        workstream = await self._db.get_workstream(workstream_id)
        try:
            materialized = await self._ledger.materialize(
                run_id=run_id,
                worktree=workspace_path,
                evidence_root=profile.workspace.evidence_root,
            )
            await self._commit_evidence(
                workspace_path,
                run_id=run_id,
                verified_source_commit=verified_source_commit,
                materialized=materialized,
            )
            await self._ledger.mark_materialized(run_id)
        except _EvidenceContainmentError as exc:
            self._logger.warning(
                "Verification finalization blocked for '%s': %s",
                workstream_id,
                exc,
            )
            await self._transition(
                workstream_id,
                WorkstreamStatus.NEEDS_REVIEW,
                expected_status=WorkstreamStatus.VERIFYING,
                message=str(exc),
                error_message=str(exc),
            )
            self._stats.failed += 1
            return
        except (OSError, RuntimeError, LedgerCollisionError) as exc:
            # Delivery-preparation error: STAY in VERIFYING, recovery re-enters.
            self._logger.warning(
                "Verification finalization deferred for '%s' (stays VERIFYING): %s",
                workstream_id,
                exc,
            )
            return

        await self._merge_and_pr(
            workstream_id, workstream, expected_status=WorkstreamStatus.VERIFYING
        )

    async def _commit_evidence(
        self,
        workspace_path: Path,
        *,
        run_id: str,
        verified_source_commit: str,
        materialized: list[Path],
    ) -> None:
        """Stage the materialized evidence and commit it onto the branch.

        Idempotent: a commit already carrying this run's trailer (a recovery
        re-entry after a crash between commit and mark) is left as-is. Raises
        `_EvidenceContainmentError` when the staged set escapes verifier.write
        or when HEAD no longer equals the verified source commit (stale PASS).
        """
        profile = self._config.domain
        assert profile is not None
        trailer = f"Maestro-Verification-Run: {run_id}"
        if await self._evidence_commit_exists(workspace_path, trailer):
            return

        rels = [str(Path(p).relative_to(workspace_path)) for p in materialized]
        await self._git_check(workspace_path, "add", "--", *rels)
        staged = await self._git_check(
            workspace_path, "diff", "--cached", "--name-only"
        )
        staged_paths = normalize([line for line in staged.splitlines() if line])
        write_globs = normalize(profile.workspace.roles["verifier"].write)
        escapes = find_escapes(staged_paths, write_globs)
        if escapes:
            msg = (
                "verification evidence escapes verifier.write scope: "
                f"{', '.join(sorted(escapes))}"
            )
            raise _EvidenceContainmentError(msg)

        head = await self._workspace_head(workspace_path)
        if head != verified_source_commit:
            msg = (
                "stale PASS: worktree HEAD "
                f"{(head or 'unreadable')[:12]} no longer matches the verified "
                f"source commit {verified_source_commit[:12]}"
            )
            raise _EvidenceContainmentError(msg)

        message = f"verification evidence {run_id}\n\n{trailer}"
        await self._git_check(workspace_path, "commit", "-m", message)

    async def _evidence_commit_exists(self, workspace_path: Path, trailer: str) -> bool:
        """True when a commit carrying `trailer` already exists on the branch."""
        out = await self._git_check(
            workspace_path, "log", "-F", "--grep", trailer, "--format=%H"
        )
        return bool(out.strip())

    async def _load_rework_addendum(self, workstream: Workstream) -> str | None:
        """Build the author-facing rework addendum from the latest FAIL.

        Reads the highest-attempt FAIL of the run from the ledger and renders
        the §7 declassification channel (severity + author_feedback only). No
        FAIL evidence -> None (nothing to append).
        """
        if self._ledger is None or not workstream.verification_run_id:
            return None
        bundle = await self._ledger.list_bundle(workstream.verification_run_id)
        fail = self._ledger.latest_fail(bundle)
        if fail is None:
            return None
        document = VerdictDocument.model_validate_json(fail.json_path.read_text())
        return build_rework_addendum(document)

    @staticmethod
    async def _workspace_tree(workspace: Path) -> str | None:
        """HEAD tree sha of a worktree, or None when unreadable."""
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(workspace),
            "rev-parse",
            "HEAD^{tree}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            return None
        return stdout.decode().strip()

    @staticmethod
    async def _git_check(workspace: Path, *args: str) -> str:
        """Run a git command in `workspace`; return stdout, raise on failure."""
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(workspace),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode().strip()
            raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
        return stdout.decode()

    async def _handle_failure(
        self,
        workstream_id: str,
        error_message: str,
    ) -> None:
        """Handle workstream failure with retry logic."""
        workstream = await self._db.get_workstream(workstream_id)

        # H-6 position retention (not authority — that lives in
        # gate_approvals): preserve any approval marker already carried in
        # error_message so an ordinary failure overwrite doesn't force a
        # full respawn on the next approved retry.
        preserved = preserve_approval_marker(error_message, workstream.error_message)

        if workstream.can_retry():
            new_count = workstream.retry_count + 1
            self._logger.info(
                "Retrying workstream '%s' (%d/%d)",
                workstream_id,
                new_count,
                workstream.max_retries,
            )
            # `workstream.status` (fresh, above) is the real `frm`: unlike
            # the scheduler's analogous failure handler, this is reached
            # from both a DECOMPOSING failure (spec gen) and a RUNNING
            # failure (process exit), so the prior status can't be
            # hardcoded.
            await self._transition(
                workstream_id,
                WorkstreamStatus.FAILED,
                expected_status=workstream.status,
                message=preserved,
                error_message=preserved,
                retry_count=new_count,
            )
            await self._transition(
                workstream_id,
                WorkstreamStatus.READY,
                expected_status=WorkstreamStatus.FAILED,
            )
        else:
            self._logger.warning(
                "Workstream '%s' exhausted retries",
                workstream_id,
            )
            await self._transition(
                workstream_id,
                WorkstreamStatus.FAILED,
                expected_status=workstream.status,
                message=preserved,
                error_message=preserved,
            )
            await self._transition(
                workstream_id,
                WorkstreamStatus.NEEDS_REVIEW,
                expected_status=WorkstreamStatus.FAILED,
                message=preserved,
            )
            self._stats.failed += 1

    def _build_pr_body(self, workstream: Workstream) -> str:
        """Build PR body from workstream info."""
        scope_str = "\n".join(f"- `{s}`" for s in workstream.scope)
        return (
            f"## Summary\n\n"
            f"{workstream.description}\n\n"
            f"## Scope\n\n"
            f"{scope_str}\n\n"
            f"## Progress\n\n"
            f"{workstream.subtask_progress or 'N/A'}\n\n"
            f"---\n"
            f"🤖 Generated by Maestro Orchestrator"
        )

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        if self._loop is None:
            return

        for sig in (signal.SIGTERM, signal.SIGINT):
            self._loop.add_signal_handler(sig, self._handle_shutdown_signal)

    def _handle_shutdown_signal(self) -> None:
        """Handle shutdown signal."""
        self._shutdown_requested = True
        self._shutdown_event.set()

    async def shutdown(self) -> None:
        """Request graceful shutdown."""
        self._shutdown_requested = True
        self._shutdown_event.set()

    async def _cleanup(self) -> None:
        """Cleanup running processes and in-flight generations on shutdown."""
        for _zid, task in list(self._generating.items()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._generating.clear()

        for zid, running in list(self._running.items()):
            try:
                await running.handle.terminate(self._shutdown_grace_seconds)
            except OSError as e:
                self._logger.debug(
                    "Failed to terminate process for workstream %s during cleanup: %s",
                    zid,
                    e,
                )

            try:
                # `running.workstream.status` is RUNNING (set at registration
                # in _spawn_workstream, and _update_progress only patches
                # subtask_progress, never status), matching the scheduler's
                # analogous shutdown-cleanup transition (expected=RUNNING).
                await self._transition(
                    zid,
                    WorkstreamStatus.FAILED,
                    expected_status=WorkstreamStatus.RUNNING,
                    message="Orchestrator shutdown",
                    error_message="Orchestrator shutdown",
                )
                await self._transition(
                    zid,
                    WorkstreamStatus.READY,
                    expected_status=WorkstreamStatus.FAILED,
                )
            except Exception as e:
                self._logger.warning(
                    "Failed to update workstream '%s' during cleanup: %s",
                    zid,
                    e,
                )

        self._running.clear()

        if self._loop:
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(ValueError):
                    self._loop.remove_signal_handler(sig)

        self._loop = None
