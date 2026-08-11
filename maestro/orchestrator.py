"""Multi-process orchestrator for Maestro.

This module provides the Orchestrator class that coordinates
multiple spec-runner processes, each running in its own git
worktree. It handles the full lifecycle: decomposition, workspace
setup, process spawning, monitoring, and PR creation.
"""

import asyncio
import contextlib
import functools
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

import ulid

from maestro._vendor.obs import child_env, current_pipeline_id, span
from maestro.approver import (
    AuthorInfo,
    BlockContext,
    EchoFields,
    build_request_envelope,
    run_approver_cmd,
    validate_verdict,
)
from maestro.changed_paths import changed_paths_since
from maestro.completeness import (
    COMPLETENESS_PHASE,
    CompletenessVerdict,
    build_completeness_block_reason,
    classify_completeness,
    completeness_approval_is_fresh,
)
from maestro.database import ConcurrentModificationError, Database
from maestro.decomposer import ProjectDecomposer
from maestro.domain import (
    KNOWN_RESUME_REASONS,
    RESUME_ACCEPT_PARTIAL,
    RESUME_OPERATOR_REWORK,
    RESUME_RECAPTURE,
    RESUME_REVERIFY,
    RESUME_REWORK,
    CommandVerifier,
    EvidenceLedger,
    IngestedAttempt,
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
    gc_terminal_handle,
)
from maestro.execution.finalize import EvidenceCaptureFailed, ensure_finalize_task
from maestro.execution.handle_ref import handle_ref_from_row
from maestro.execution.local import LocalBackend
from maestro.execution.models import (
    CollectPolicy,
    ExecutionRequest,
    ProgressMirrorPolicy,
)
from maestro.execution.resolver import BackendResolver, ExecutionConfigError
from maestro.execution.ssh_backend import SshBackend
from maestro.execution.ssh_launch import decode_transport_ref
from maestro.execution.ssh_recovery import gc_ssh_terminal
from maestro.gates import (
    BLOCK_REASON_PREFIX,
    ApprovalMarker,
    GateDecision,
    GateKeeper,
    GateVerdictRecord,
    parse_approval_marker,
    pipeline_log_dir,
    preserve_approval_marker,
)
from maestro.git import GitError, MergeConflictError
from maestro.merge_logs import merge_logs_dir
from maestro.models import (
    SPEC_PREFIX,
    ApproverConfig,
    OrchestratorConfig,
    TransitionSubject,
    Workstream,
    WorkstreamConfig,
    WorkstreamStatus,
)
from maestro.notifications.manager import NotificationManager
from maestro.postmortem import (
    PostmortemCaptureError,
    archive_is_committed,
    build_recapture_marker,
    capture_archive,
    parse_recapture_marker,
    prune_archives,
    read_manifest,
)
from maestro.pr_manager import PRManager, PRManagerError
from maestro.retry_policy import describe_retry_decision, retry_is_unproductive
from maestro.rework import build_operator_rework_addendum
from maestro.scope_gate import build_scope_escape_reason, find_escapes, normalize
from maestro.spec_runner import read_executor_state, read_planned_total
from maestro.tasks_spec import (
    SELF_CONTAINED_DEPENDENCIES_INSTRUCTION,
    build_dangling_dependency_error,
    find_dangling_dependencies,
)
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


def _read_verified_source_commit(pass_row: IngestedAttempt) -> str | None:
    """Parse the verified source commit from a PASS attempt's verdict JSON.

    A PASS row is always backed by a real `VerdictDocument` (synthetic JSONs
    exist only for protocol-ERROR outcomes), so its identity records the exact
    commit that was verified — never re-derived from a possibly-advanced HEAD.
    Returns None when the JSON is missing or fails validation, so the caller
    can fail closed to NEEDS_REVIEW.
    """
    try:
        document = VerdictDocument.model_validate_json(pass_row.json_path.read_text())
    except (OSError, ValueError):
        return None
    return document.identity.verified_source_commit


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


def _ambiguity_marker(kind: str, pid: int | None) -> str:
    """Durable recovery-ambiguity marker JSON (#124).

    kind: 'live_orphan' | 'spawn_uncertain' | 'live_handle'. ``pid`` is the
    preserved probeable evidence; None means "no probeable evidence" — such
    a marker is resolvable ONLY via `maestro workstream-resolve-ambiguity`,
    never by an automatic probe.
    """
    return json.dumps(
        {
            "kind": kind,
            "pid": None if pid == _SPAWNING_SENTINEL else pid,
            "parked_at": datetime.now(UTC).isoformat(),
        }
    )


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


def _execution_id_of_archive(path: Path) -> str:
    """Recover the execution id from an archive directory name.

    The name is `<utc-compact>-<execution_id>` and the id may itself contain
    hyphens, so the split is on the FIRST separator only.
    """
    return path.name.split("-", 1)[1] if "-" in path.name else path.name


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

        self._backends = BackendResolver(
            self._config.execution, local_docker=cast("DockerCli", self._docker)
        )
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
        # #166: the first signal drains (stop dispatching, terminate nothing);
        # a second one forces. Without the escalation the only way out of a
        # long drain would be SIGKILL — the very hammer this removes.
        self._force_shutdown = False
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._logger = logging.getLogger(__name__)
        self._stats = OrchestratorStats()
        # approver_cmd hook (#137): in-flight evaluation tasks (dedup by
        # workstream), per-workstream finalize locks (the in-process half
        # of the §7.2 TOCTOU narrowing), and the observation dedup set
        # ((workstream, sha, reason) — §6 skips are logged once).
        self._approver_tasks: dict[str, asyncio.Task[None]] = {}
        self._approver_locks: dict[str, asyncio.Lock] = {}
        self._approver_observed: set[tuple[str, str, str]] = set()

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
        url: str | None = None,
        **fields: object,
    ) -> Workstream:
        """Write a workstream status transition and dispatch its effects.

        Mirrors `Scheduler._transition` (spec §4.1): `expected_status` is a
        CAS guard on the write; on success it *is* the true `frm` for the
        dispatcher (a plain re-`get` would be unreliable under concurrent
        writes). `details`/`message`/`url` feed the event/notification only;
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
            url=url,
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

            # Step 1c: approver sentinels from a prior crash — fail-closed
            # to the human, never auto-re-run (#137 §8.2).
            await self._finalize_interrupted_approver_runs()

            # Step 2: Main loop
            await self._main_loop()
            await self._drain_approver_tasks()
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
                            recovery_ambiguity=_ambiguity_marker("live_handle", None),
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
                            recovery_ambiguity=_ambiguity_marker(
                                "spawn_uncertain"
                                if orphan_pid == _SPAWNING_SENTINEL
                                else "live_orphan",
                                orphan_pid,
                            ),
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
                # The commit that PASSed is recorded in the verdict JSON — read
                # it from there, never from the current worktree HEAD (a manual
                # commit after the crash would make the stale-PASS guard in
                # `_commit_evidence` compare HEAD to itself, vacuously).
                verified_commit = _read_verified_source_commit(pass_row)
                if verified_commit is None:
                    reason = (
                        "verification recovery: PASS verdict JSON is "
                        "unreadable/invalid — cannot confirm the verified "
                        "source commit for stranded finalization"
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
                    w.id,
                    workspace,
                    run_id=run_id,
                    verified_source_commit=verified_commit,
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
        FAILED). Same accounting as the existing live-orphan rule.

        Tagged `RESUME_REVERIFY` so an operator re-queue (NEEDS_REVIEW ->
        READY) re-enters the verification loop over the untouched worktree
        rather than respawning the author — author respawn fires ONLY on a
        genuine FAIL with rework budget left (§4 invariant)."""
        await self._transition(
            workstream_id,
            WorkstreamStatus.NEEDS_REVIEW,
            expected_status=WorkstreamStatus.VERIFYING,
            resume_reason=RESUME_REVERIFY,
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
        centralized `backend.probe()` boundary `_probe_open_handle` uses for
        RUNNING recovery (PR2 Task 7 centralization).
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
        # Non-local verifier backends reuse the same centralized probe
        # boundary as RUNNING recovery (`_probe_open_handle`): resolve ->
        # accepts_ref -> `backend.probe(ref)` (PR2 Task 7 centralization,
        # replacing the old hand-composed `probe_ssh`/`probe_execution`).
        # NOTE: the verifier is pinned to LocalBackend today (Stage B scope,
        # commit 299e1a3), so this branch is defensive/future-proofing only.
        # TODO(reviewer Important #2): `SshBackend.probe` is unconditionally
        # fail-closed regardless of isolation, so it does not yet mirror
        # `_probe_open_handle`'s docker-isolation dual-probe (Phase 2c: an
        # ssh-launched verifier whose remote harness runs in a container).
        # Harmless today (fail-closed masks it), but add the dual-probe
        # before any non-local verifier backend is wired up.
        ref = handle_ref_from_row(row)
        if not backend.accepts_ref(ref):
            return "ambiguous"
        try:
            verdict = await backend.probe(ref)
        except Exception:
            return "ambiguous"
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
        """True if `handle_row` is stranded in the terminal/collected window
        and must route to NEEDS_REVIEW rather than silently re-run (spec
        §G/§J, decision #5): either the resolved backend is `SshBackend`, or
        the persisted ref identity no longer matches the resolved backend
        (config drift — e.g. the backend NAME was reconfigured from
        `transport: ssh` to `transport: local, isolation: docker` between
        the crash and this restart). An unresolvable backend is likewise
        fail-closed to True — never assume it's safe to reset. Docker rows
        whose identity still matches return False (docker `collect` is a
        no-op, so its reset-and-rerun stays safe).

        This mirrors the `accepts_ref()` gate already used by
        `_probe_open_handle` / `_gc_terminal_handles` for the
        prepared/running and GC paths — terminal/collected handles don't
        flow through either of those, so this is their only guard against
        the fail-open case: a config change alone must never be enough to
        turn a stranded SSH run back into a silent re-run."""
        if handle_row["state"] not in ("terminal", "collected"):
            return False
        try:
            backend = self._backends.resolve(handle_row["backend_id"])
        except Exception:
            return True  # unresolvable backend -> fail-closed to review
        ref = handle_ref_from_row(handle_row)
        return isinstance(backend, SshBackend) or not backend.accepts_ref(ref)

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
        execution, via the resolved backend's own `probe()` (Task 7: this
        used to hand-compose `probe_ssh`/`probe_execution`/`decode_transport_ref`
        here; that composition now lives once, in `SshBackend.probe` /
        `LocalBackend.probe`, Task 6).

        No-op (returns False) when there is no open, non-cleaned handle row
        for this workstream — a local-backed workstream (or one whose
        handle already reached `terminal`/`collected`/`cleaned`) is always
        unaffected, preserving pre-Task-18 recovery behavior exactly.

        Every backend_id — `docker` included (Task 7b) — is resolved via
        `self._backends.resolve()` and probed via `backend.probe()`: the
        resolver is constructed with `local_docker=self._docker` (`__init__`),
        so `resolve("docker")` returns a `LocalBackend` wired to the very
        same, test-injectable `DockerCli` startup recovery has always probed
        with (`Orchestrator(docker=...)`) — not a fresh, un-injectable one.
        An unresolvable `backend_id` is treated as needs_review (fail-closed)
        rather than raising. Likewise, a resolved backend whose
        `accepts_ref()` rejects the persisted ref — the persisted
        transport/isolation identity no longer matches the resolved backend
        (config drift after the handle was minted), or the ref is a
        placeholder/unknown — is treated as needs_review WITHOUT ever
        calling `probe()`: probing across identities (e.g. a local-docker
        probe of an SSH run's `execution_id`) would fail-OPEN. (spec
        decision #5; kept consistent with `StateRecovery`'s Mode-1 gate.)

        SSH is fail-closed by design: `SshBackend.probe` always returns
        `needs_review=True` for a bare handle (a remote terminal marker
        cannot prove collect already applied), so an SSH-backed row here is
        never silently reclaimed. When the verdict confirms no
        execution is left, the open handle row is closed (terminal ->
        cleaned) here so it doesn't linger open and shadow the workstream's
        next attempt after it's recovered to READY.
        """
        row = workstream_handles.get(workstream_id)
        if row is None:
            return False
        backend_id = row["backend_id"]
        try:
            backend = self._backends.resolve(backend_id)
        except ExecutionConfigError:
            needs_review = True
        else:
            ref = handle_ref_from_row(row)
            if not backend.accepts_ref(ref):
                needs_review = True
            else:
                try:
                    result = await backend.probe(ref)
                except Exception as exc:
                    # A probe raise (e.g. transient transport I/O) must not
                    # abort recovery and strand the workstream in RUNNING:
                    # fail closed to NEEDS_REVIEW, never a silent reclaim.
                    # Mirrors StateRecovery's Mode-1 probe guard (recovery.py).
                    self._logger.warning(
                        "Workstream '%s' execution probe failed during "
                        "recovery (%s) — sending to NEEDS_REVIEW; verify "
                        "and clean it up before resume",
                        workstream_id,
                        exc,
                    )
                    needs_review = True
                else:
                    needs_review = result.needs_review
        if not needs_review:
            await self._db.mark_execution_state(
                row["execution_id"], "terminal", allowed_from=["prepared", "running"]
            )
            await self._db.mark_execution_state(
                row["execution_id"], "cleaned", allowed_from=["terminal"]
            )
        return needs_review

    async def _gc_terminal_handles(self, handles: list[dict[str, Any]]) -> int:
        """Best-effort, ownership-checked GC sweep for settled handles.

        Mirrors `StateRecovery._gc_terminal_handles` (Mode 1): a `terminal`
        (docker) or `collected` (ssh) handle means the entity behind it
        already reached — or passed through — a settled point (finalize
        ran) but the resource-cleanup confirmation was never persisted.
        This only removes the leftover container/remote artifacts and marks
        the handle `cleaned` — it never touches entity status. Swept across
        all entity kinds since the handle table is shared.

        Classification is by the persisted `backend_id`, resolved through
        `BackendResolver` (never a hand-composed/literal `backend_id ==
        "docker"` check — Task 13c: that literal used to route an
        SSH-transport backend a user happened to *name* "docker" through
        local-docker GC, which found no local container and wrongly marked
        an uncollected remote run `cleaned`):

        - resolved `SshBackend` -> sweeps only `collected` (`terminal`-but-
          not-`collected` means the remote diff was never confirmed
          applied, so it is left for human review — `ssh_recovery.probe_ssh`
          is fail-closed); docker-isolation rows GC the remote container
          first, then the remote root, only on a clean container outcome.
        - any other resolved backend (local bare/docker) -> docker GC on
          both `terminal` and `collected` (`collect()` is a no-op for
          docker, so both states are equally safe to sweep — the phased
          finalize wiring, Task 16, can leave a docker row at either
          depending on exactly when a crash lands).
        - an unresolvable `backend_id`, or a resolved backend whose
          `accepts_ref()` rejects the persisted ref (config drift after the
          handle was minted), is left in place (fail-closed) for the next
          sweep or a human to resolve — never docker-GC an SSH run's
          `execution_id` (or vice versa) just because the backend *name*
          still resolves to something.

        A row whose outcome is ambiguous (multiple container matches /
        label mismatch / probe error / no owner marker) is likewise left in
        place for the next sweep or a human to resolve.
        """
        swept = 0
        for row in handles:
            state = row["state"]
            if state not in ("terminal", "collected"):
                continue
            backend_id = row["backend_id"]
            try:
                backend = self._backends.resolve(backend_id)
            except ExecutionConfigError as exc:
                self._logger.warning(
                    "recovery: GC skipping handle %s (%s %s): "
                    "unresolvable backend %r: %s",
                    row["execution_id"],
                    row["entity_kind"],
                    row["entity_id"],
                    backend_id,
                    exc,
                )
                continue

            ref = handle_ref_from_row(row)
            if not backend.accepts_ref(ref):
                # Persisted identity no longer matches the resolved backend
                # (e.g. isolation reconfigured bare<->docker under the same
                # name, or "docker" renamed to an ssh transport) — never GC
                # across identities.
                self._logger.warning(
                    "recovery: GC skipping handle %s (%s %s): persisted "
                    "execution identity does not match resolved backend "
                    "%r (config drift)",
                    row["execution_id"],
                    row["entity_kind"],
                    row["entity_id"],
                    backend_id,
                )
                continue

            if isinstance(backend, SshBackend):
                if state != "collected":
                    continue
                try:
                    decoded = decode_transport_ref(ref.transport_ref)
                    if decoded["isolation"] == "docker":
                        # Container-first ordering: never delete the remote
                        # root (which may hold the only evidence of what the
                        # container touched) before the container itself is
                        # confirmed gone.
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

        # Drain (#166): a shutdown request stops dispatch but keeps the loop
        # monitoring live executions until they finalize; only a forced
        # shutdown leaves the loop with work still running.
        while not self._force_shutdown and (
            not self._shutdown_requested or self._running
        ):
            # Get completed workstream IDs
            completed_ids = await self._get_completed_ids()

            # approver_cmd (#137): schedule eligible NEEDS_REVIEW blocks
            # BEFORE the completeness check, so an eligible block becomes
            # tracked in-flight work instead of letting the run exit.
            await self._schedule_approver()

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
        if self._approver_tasks:
            return False  # in-flight approver evaluations are active work
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
            if z.quarantined_at is not None:
                # #166: quarantine forbids progression, and dispatch is
                # progression. Checked here rather than at spawn time so a
                # quarantined workstream never even counts as ready — it must
                # not occupy a concurrency slot or look dispatchable in logs.
                self._logger.debug(
                    "workstream.quarantined.skipped workstream=%s reason=%s",
                    z.id,
                    z.quarantine_reason,
                )
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

        # Resume dispatch (before DECOMPOSING). Exhaustive over
        # KNOWN_RESUME_REASONS (#124): an unknown value is an error routed
        # fail-closed to NEEDS_REVIEW, never a silent plain resume.
        # REVERIFY re-enters the verification loop over the untouched
        # worktree; verification REWORK and operator rework respawn the
        # author via re-decomposition, each with its own addendum channel
        # (§7 declassification for FAIL findings; the audit row's
        # `instructions`, keyed by operator_rework_seq, for the operator).
        if (
            workstream.resume_reason is not None
            and workstream.resume_reason not in KNOWN_RESUME_REASONS
        ):
            self._logger.error(
                "Workstream '%s' carries unknown resume_reason %r — "
                "failing closed to NEEDS_REVIEW",
                workstream_id,
                workstream.resume_reason,
            )
            await self._transition(
                workstream_id,
                WorkstreamStatus.NEEDS_REVIEW,
                expected_status=workstream.status,
                message=(f"unknown resume_reason {workstream.resume_reason!r}"),
            )
            return
        rework_addendum: str | None = None
        if workstream.resume_reason == RESUME_RECAPTURE:
            # #164: retry ONLY the evidence capture for the same execution.
            await self._resume_recapture(workstream)
            return
        if workstream.resume_reason == RESUME_ACCEPT_PARTIAL:
            # #164: the operator accepted an incomplete result. Continue the
            # existing success pipeline over the untouched worktree; nothing
            # is executed and nothing is regenerated.
            await self._resume_accept_partial(workstream)
            return
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
        is_rework_resume = workstream.resume_reason in (
            RESUME_REWORK,
            RESUME_OPERATOR_REWORK,
        )
        if workstream.resume_reason == RESUME_REWORK:
            rework_addendum = await self._load_rework_addendum(workstream)
        elif workstream.resume_reason == RESUME_OPERATOR_REWORK:
            rework_addendum = await self._load_operator_rework_addendum(workstream)

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
        parts = [workstream.description]
        if rework_addendum is not None:
            parts.append(rework_addendum)
        if is_rework_resume:
            # Keyed on the rework RESUME itself, not on whether an addendum
            # text exists: the risk comes from regenerating tasks.md over a
            # revision the model remembers, and an operator rework with no
            # instructions regenerates just the same. The instruction is
            # prevention; `_validate_generated_tasks` is the guarantee and runs
            # regardless of whether it was honoured (#165).
            parts.append(SELF_CONTAINED_DEPENDENCIES_INSTRUCTION)
        description = "\n\n".join(parts)
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

        # Honest progress denominator (#123): the state DB registers tasks
        # lazily, so capture the planned total once from spec-runner's own
        # tasks.md parser right after generation. Display-only — None on
        # any failure keeps the lazy label.
        subtask_total = await asyncio.get_running_loop().run_in_executor(
            None, read_planned_total, workspace
        )
        if subtask_total is not None:
            await self._update_fields(workstream_id, subtask_total=subtask_total)

        # #165: every dependency must resolve inside THIS revision of
        # tasks.md. spec-runner validates the same thing when it runs and
        # exits 1 — correct, but only after we have paid for generation and
        # spawned a process. Checking here, before the READY transition and
        # any spawner, turns that into a cheap block naming the exact ids.
        if not await self._validate_generated_tasks(workstream_id, workspace):
            return

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
                    "subtask_total": (
                        subtask_total
                        if subtask_total is not None
                        else workstream.subtask_total
                    ),
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

                async def _mark_collected(
                    eid: str | None = eid, running: RunningWorkstream = running
                ) -> None:
                    if eid is not None:
                        await self._db.mark_execution_state(
                            eid, "collected", allowed_from=["terminal"]
                        )
                    # #164: capture the evidence HERE — the one moment every
                    # transport agrees on. Collect has applied, so the state
                    # db is local; nothing has been destroyed yet, so the ssh
                    # logs (excluded from the collect rsync) are still on the
                    # remote. The durable-phase write above stays first, and
                    # keeps its own crash semantics.
                    await self._capture_postmortem(running)

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
                if fin.archive_error is not None:
                    # Evidence capture failed, so cleanup was skipped and the
                    # workspace is intact (#164, spec §6.5). Do NOT enter the
                    # gate flow: the gate's own input is the archive that does
                    # not exist. No approval marker — there is nothing to
                    # approve here, the operator has to fix the archive root.
                    # No approval marker: there is no result to approve, only
                    # an archive root to fix. A recapture token instead, so
                    # this is not a dead end — `maestro workstream-recapture`
                    # retries the capture for THIS execution and nothing else,
                    # where a plain requeue would fall through to a respawn
                    # and re-run the very work we are trying to preserve.
                    reason = (
                        f"post-mortem capture failed: {fin.archive_error}; "
                        f"{build_recapture_marker(self._evidence_key(running))}"
                    )
                    self._logger.error("workstream '%s' %s", zid, reason)
                    await self._transition(
                        zid,
                        WorkstreamStatus.NEEDS_REVIEW,
                        expected_status=WorkstreamStatus.RUNNING,
                        message="post-mortem capture failed; workspace preserved",
                        error_message=reason,
                        process_pid=None,
                        generation_pid=None,
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

        # Same-state field patch (still RUNNING) — no dispatch. The honest
        # planned total (#123) supplies the denominator when known.
        await self._update_fields(
            workstream_id,
            subtask_progress=state.progress_label(
                total=running.workstream.subtask_total
            ),
        )

    async def _final_progress_refresh(
        self, workstream_id: str, workspace_path: Path
    ) -> None:
        """One last progress read after the run finished (#123).

        The mid-run label can be stale (written before the final task
        completed); refreshing here makes "DONE 4/5" for a no-op success
        impossible — the label lands as e.g. "5/5 done (1 no-op)". A
        missing/unreadable state file leaves the last label untouched
        (display concern, never blocks completion).
        """
        loop = asyncio.get_running_loop()
        try:
            state = await loop.run_in_executor(
                None, read_executor_state, workspace_path / "spec", SPEC_PREFIX
            )
        except Exception as exc:
            self._logger.warning(
                "final progress refresh for '%s' failed: %s", workstream_id, exc
            )
            return
        if state is None:
            return
        workstream = await self._db.get_workstream(workstream_id)
        await self._update_fields(
            workstream_id,
            subtask_progress=state.progress_label(total=workstream.subtask_total),
        )

    def _postmortem_root(self) -> Path:
        """`<db_dir>/postmortem` — anchored to the DB, never to the cwd.

        Archives then travel with the database that describes them, and
        recovery does not depend on where the incident run was launched from
        (spec §6.1). A cwd-relative root would make one database resolve
        different archive sets from different directories.
        """
        return Path(self._db.db_path).parent / "postmortem"

    @staticmethod
    def _evidence_key(running: RunningWorkstream) -> str:
        """Stable per-execution archive key.

        `execution_id` is None on the bare-local path, which persists no
        handle, so that case falls back to the start timestamp — still unique
        per execution, which is all the key has to be.
        """
        if running.execution_id is not None:
            return running.execution_id
        return f"local-{running.started_at.strftime('%Y%m%dT%H%M%S%f')}"

    async def _capture_postmortem(self, running: RunningWorkstream) -> None:
        """Archive this execution's evidence before anything is destroyed.

        Runs inside finalization's `on_collected` (#164, spec §3) — after
        collect has applied and before `cleanup()`. Raises
        `EvidenceCaptureFailed` so finalization skips cleanup and preserves
        the workspace; every other failure mode of this method is a genuine
        capture failure too, so nothing here is swallowed.
        """
        try:
            await self._capture_evidence(
                running.workstream.id,
                running.workspace_path,
                evidence_key=self._evidence_key(running),
                backend_id=running.backend_id,
                transport=running.handle.ref.backend_id,
                exit_code=running.handle.poll(),
            )
        except PostmortemCaptureError as exc:
            raise EvidenceCaptureFailed(str(exc)) from exc

    async def _capture_evidence(
        self,
        workstream_id: str,
        workspace_path: Path,
        *,
        evidence_key: str,
        backend_id: str,
        transport: str,
        exit_code: int | None,
    ) -> None:
        """Capture one execution's evidence and record it.

        Shared by finalization and `maestro workstream-recapture`, which
        retries exactly this step for the same execution after a capture
        failure — no executor, no decomposition. Raises
        `PostmortemCaptureError`; each caller decides what that means for the
        workspace it owns.
        """
        spec_dir = workspace_path / "spec"
        loop = asyncio.get_running_loop()
        state = await loop.run_in_executor(
            None, read_executor_state, spec_dir, SPEC_PREFIX
        )
        persisted = await self._db.get_workstream(workstream_id)
        counters: dict[str, int | None] = {
            "done": state.done if state else None,
            "noop_done": state.noop_done if state else None,
            "state_total": state.total if state else None,
            "planned": persisted.subtask_total,
        }
        identity: dict[str, Any] = {
            "workstream_id": workstream_id,
            "execution_id": evidence_key,
            "attempt": persisted.retry_count,
            "backend_id": backend_id,
            "transport": transport,
            "exit_code": exit_code,
            "branch": persisted.branch,
            "head_sha": await self._workspace_head(workspace_path),
            "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_run_stop_reason": state.last_run_stop_reason if state else None,
            "last_run_stop_detail": state.last_run_stop_detail if state else None,
        }
        archive = await loop.run_in_executor(
            None,
            functools.partial(
                capture_archive,
                spec_dir=spec_dir,
                root=self._postmortem_root(),
                identity=identity,
                counters=counters,
                config=self._config.postmortem,
            ),
        )
        # The row goes in only after the directory is committed, so its
        # existence implies evidence on disk. Retention runs strictly after
        # that — a prune fault must never cost the archive just captured.
        await self._db.record_postmortem_archive(
            workstream_id,
            identity["execution_id"],
            path=str(archive.path),
            bytes_written=archive.bytes_written,
            truncated=archive.truncated,
        )
        self._logger.info(
            "postmortem.captured workstream=%s execution=%s path=%s bytes=%d",
            workstream_id,
            identity["execution_id"],
            archive.path,
            archive.bytes_written,
        )
        await self._prune_postmortem(workstream_id)

    async def _prune_postmortem(self, workstream_id: str) -> None:
        """Apply the retention policy; never let its failure escape.

        A retention fault is not a capture failure: the fresh evidence is
        already committed, and raising here would preserve a workspace over a
        housekeeping problem.
        """
        keep = self._config.postmortem.keep_per_workstream
        loop = asyncio.get_running_loop()
        try:
            removed = await loop.run_in_executor(
                None,
                functools.partial(
                    prune_archives,
                    self._postmortem_root(),
                    workstream_id,
                    keep=keep,
                ),
            )
        except OSError as exc:
            self._logger.warning(
                "postmortem retention failed for '%s': %s", workstream_id, exc
            )
            return
        for path in removed:
            await self._db.delete_postmortem_archive(
                workstream_id, _execution_id_of_archive(path)
            )

    async def _route_quarantined_completion(
        self, workstream_id: str, frm: WorkstreamStatus
    ) -> None:
        """A finished quarantined workstream parks instead of delivering (#166).

        Reached either from the pre-gate check or from the MERGING CAS losing
        to a concurrent quarantine. The work is not lost and nothing is
        reverted — the branch, the worktree and the commits stay exactly as the
        author left them; only their progression stops.
        """
        workstream = await self._db.get_workstream(workstream_id)
        reason = (
            f"quarantined: {workstream.quarantine_reason or 'no reason recorded'}; "
            f"delivery withheld. Lift with `maestro workstream-unquarantine "
            f"{workstream_id}` once resolved."
        )
        self._logger.warning(
            "workstream.quarantined.withheld workstream=%s frm=%s",
            workstream_id,
            frm.value,
        )
        await self._transition(
            workstream_id,
            WorkstreamStatus.NEEDS_REVIEW,
            expected_status=frm,
            message="quarantined; delivery withheld",
            error_message=reason,
            process_pid=None,
            generation_pid=None,
        )
        self._stats.failed += 1

    async def _gate_completeness(
        self,
        workstream_id: str,
        workstream: Workstream,
        workspace_path: Path,
    ) -> bool:
        """Always-on completeness gate (#164, spec §4).

        DONE used to mean "spec-runner exited 0 and the merge applied" — a
        true statement about the process and a false one about the work. This
        reads the counters the post-mortem capture recorded and refuses to
        deliver a run that did not finish its planned tasks.

        Reads the ARCHIVE rather than the live worktree, which is what keeps
        local and ssh on one path (spec §3). Fail-closed throughout: a missing
        archive, an unreadable manifest or an uncaptured denominator all
        block.
        """
        head = await self._workspace_head(workspace_path)
        archive_row = await self._newest_archive(workstream_id)
        verdict = await self._completeness_verdict(archive_row)

        if verdict.all_no_op:
            # Passes the gate; visibility only (§4.3/§10.2). Judging whether
            # the work was substantively useful belongs to verification.
            self._logger.warning(
                "workstream.completeness.all_no_op workstream=%s execution=%s "
                "done=%s planned=%s head_sha=%s",
                workstream_id,
                (archive_row or {}).get("execution_id"),
                verdict.message,
                workstream.subtask_total,
                (head or "unknown")[:12],
            )
        if verdict.ok:
            return True

        evidence = (archive_row or {}).get("execution_id")
        if head is not None and await self._completeness_approved(
            workstream_id, workstream, head, evidence
        ):
            self._logger.info(
                "workstream '%s' completeness block approved by operator "
                "(%s); accepting the partial result",
                workstream_id,
                verdict.reason,
            )
            return True

        reason = build_completeness_block_reason(
            verdict,
            head or "0" * 40,
            evidence=evidence,
            stop_reason=(archive_row or {}).get("stop_reason"),
        )
        self._logger.warning("workstream '%s' blocked: %s", workstream_id, reason)
        return await self._route_scope_block(workstream_id, reason)

    async def _last_stop_reason(self, workstream_id: str) -> str | None:
        """spec-runner's typed stop reason for the run that just finished.

        Read from the post-mortem archive's manifest — the same structured
        source the completeness gate uses, and the reason #169a exists
        (`executor_meta` string values used to be dropped by an int cast). NOT
        parsed out of logs: the classification must not depend on wording.

        None when there is no archive or no recorded reason, which keeps the
        existing retry policy.
        """
        row = await self._newest_archive(workstream_id)
        if row is None:
            return None
        manifest = await asyncio.get_running_loop().run_in_executor(
            None, read_manifest, row["path"]
        )
        if manifest is None:
            return None
        reason = manifest.get("last_run_stop_reason")
        return str(reason) if reason else None

    async def _newest_archive(self, workstream_id: str) -> dict[str, Any] | None:
        """This run's archive — the newest row, and only if it is committed.

        Deliberately does NOT search past the newest row for an older archive
        that happens to still be on disk. Falling back would be wrong twice
        over: the gate would judge completeness from a different run's
        counters, and the cleanup guard would see "evidence exists" and
        destroy the only remaining logs of the run that actually just
        finished. A missing newest archive means no archive for this run.

        "Committed" is decided by the filesystem, not the row: a row can
        outlive its directory (hand-pruned archive, volume restored from an
        older snapshot), and this answer gates both delivery and the
        destruction of the last copy of the logs.
        """
        rows = await self._db.list_postmortem_archives(workstream_id)
        if not rows:
            return None
        newest = rows[0]
        if archive_is_committed(newest["path"]):
            return newest
        self._logger.warning(
            "workstream '%s': newest post-mortem archive %s is recorded but "
            "not on disk; refusing to fall back to an older run's evidence",
            workstream_id,
            newest["path"],
        )
        return None

    async def _completeness_verdict(
        self, archive_row: dict[str, Any] | None
    ) -> CompletenessVerdict:
        """Classify the archived counters (fail-closed on anything missing)."""
        if archive_row is None:
            return CompletenessVerdict.unreadable(
                "no committed post-mortem archive for this run"
            )
        manifest = await asyncio.get_running_loop().run_in_executor(
            None, read_manifest, archive_row["path"]
        )
        if manifest is None:
            return CompletenessVerdict.unreadable(
                f"unreadable manifest in {archive_row['path']}"
            )
        done = manifest.get("done")
        noop_done = manifest.get("noop_done")
        if done is None:
            return CompletenessVerdict.unreadable(
                "archive recorded no executor state "
                f"(state_missing={manifest.get('state_missing')})"
            )
        archive_row["stop_reason"] = manifest.get("last_run_stop_reason")
        return classify_completeness(
            done=int(done),
            planned=manifest.get("planned"),
            noop_done=int(noop_done or 0),
        )

    async def _completeness_approved(
        self,
        workstream_id: str,
        workstream: Workstream,
        head: str,
        evidence: str | None,
    ) -> bool:
        """True when the operator approved THIS result at THIS snapshot.

        Two independent conditions, both required. The `gate_approvals` table
        is the authority on "was (workstream, completeness, sha) approved".
        The marker adds freshness: it names the evidence snapshot the operator
        saw, so an approval cannot silently accept a later partial result that
        happens to sit at the same sha.
        """
        approvals = await self._db.list_gate_approvals(workstream_id)
        if (COMPLETENESS_PHASE, head) not in approvals:
            return False
        marker = parse_approval_marker(workstream.error_message)
        if marker is None:
            return False
        return completeness_approval_is_fresh(marker, current_evidence=evidence)

    async def _validate_generated_tasks(
        self, workstream_id: str, workspace: Path
    ) -> bool:
        """Reject a generated tasks.md whose dependencies leave the revision.

        A rework rewrites `spec/maestro-tasks.md` wholesale, and the decomposer
        — told it is continuing after TASK-021 — emits a dependency on a task
        that exists only in the file it replaced. The addendum asks it not to;
        this makes it not matter (#165).

        Blocks straight to NEEDS_REVIEW without consuming a retry: a retry
        would re-decompose, which is exactly the spend the finding says is
        wasted.

        An unreadable file logs and PASSES. spec-runner still validates at run
        time, so the guarantee survives — only the earliness is lost — whereas
        blocking on our own inability to find a file would turn a path
        assumption into an outage.
        """
        tasks_path = workspace / "spec" / f"{SPEC_PREFIX}tasks.md"
        try:
            # Explicit utf-8: the generated file carries emoji status markers
            # (`🔴 P0 | ⬜ TODO`) on every task, so the platform default
            # encoding is not a safe assumption. A decode failure is a
            # ValueError, not an OSError, and must reach the skip path rather
            # than escape into the orchestrator loop.
            text = await asyncio.get_running_loop().run_in_executor(
                None, functools.partial(tasks_path.read_text, encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError) as exc:
            # SKIPPED, not passed. Three distinct events on purpose: a
            # fail-open skip must never read as a statement about the file's
            # correctness, or an operator scanning the log will believe the
            # dependencies were checked when they were not.
            self._logger.warning(
                "workstream.tasks_validation.skipped workstream=%s path=%s "
                "error=%s note=unreadable; spec-runner still validates at "
                "run time",
                workstream_id,
                tasks_path,
                exc,
            )
            return True

        dangling = find_dangling_dependencies(text)
        if not dangling:
            self._logger.info(
                "workstream.tasks_validation.passed workstream=%s path=%s",
                workstream_id,
                tasks_path,
            )
            return True

        reason = build_dangling_dependency_error(dangling)
        self._logger.error(
            "workstream.tasks_validation.blocked workstream=%s dangling=%d %s",
            workstream_id,
            len(dangling),
            reason,
        )
        await self._transition(
            workstream_id,
            WorkstreamStatus.NEEDS_REVIEW,
            expected_status=WorkstreamStatus.DECOMPOSING,
            message="generated tasks.md has dangling dependencies",
            error_message=reason,
            process_pid=None,
            generation_pid=None,
        )
        self._stats.failed += 1
        return False

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
        approved sha. What happens next forks on whether domain verification
        is active (`self._config.domain is not None`, same activation check
        `_handle_success` uses):

        - Legacy (no domain profile): READY -> RUNNING (explicitly clearing
          both pids so recovery never mistakes it for a live orphan) and
          re-enters the success continuation — ex-post re-evaluates, passes
          via the approval memory, and the normal MERGING -> PR -> merge ->
          DONE tail runs.
        - Domain profile active: the operator only approved the *ex-post
          scope/tier gate*, not the domain verifier — jumping straight to
          the merge tail would deliver an artifact that was never verified
          (the §4 invariant is MERGING reachable only through VERIFYING).
          So instead: READY -> VERIFYING, and `_run_verification` takes over
          from there (PASS -> finalization -> merge tail; FAIL/ERROR -> their
          normal rework/reverify/NEEDS_REVIEW routes), exactly as for any
          other VERIFYING entry.

        False = caller proceeds with the full respawn (the approval is
        genuinely void, DESIGN-608).
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

        if self._config.domain is not None:
            # The marker's job — skip re-gating the already-approved ex-post
            # escape — is done now that we're resuming past the gate into
            # VERIFYING. Clear it HERE, atomically with the status write,
            # rather than preserving it to DONE the way the legacy tail
            # does: unlike the legacy tail (a straight shot to MERGING), the
            # VERIFYING loop can cycle this workstream back through READY
            # multiple times (FAIL-with-budget rework, ERROR-budget-exhausted
            # reverify), and `_spawn_workstream` checks THIS SAME marker+sha
            # before it ever looks at `resume_reason`. A separate follow-up
            # write would leave a crash window between the two: transition
            # lands, process dies before the marker clears, and the next
            # rework/reverify READY dispatch is hijacked straight back into
            # this ex-post path — silently skipping the author respawn /
            # reverify bookkeeping those paths exist for. A single CAS write
            # closes that window. Also clear `process_pid`/`generation_pid`
            # here: a gate-blocked workstream can carry stale pids through
            # NEEDS_REVIEW -> READY (`approve_workstream_with_gate_record`
            # doesn't touch them), and leaving them set risks recovery
            # mistaking this VERIFYING entry for a live orphan.
            await self._transition(
                workstream.id,
                WorkstreamStatus.VERIFYING,
                expected_status=WorkstreamStatus.READY,
                error_message=None,
                process_pid=None,
                generation_pid=None,
            )
            self._logger.info(
                "Resuming workstream '%s' at the ex-post gate into VERIFYING "
                "(domain profile active, approved sha %s)",
                workstream.id,
                marker.sha[:12],
            )
            await self._run_verification(workstream.id, workspace)
            return True

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

    async def _resume_recapture(self, workstream: Workstream) -> None:
        """Retry evidence capture for the same execution, then continue.

        The only thing that failed was the archive write, so this repeats
        exactly that step over the untouched worktree and re-enters the
        success continuation. Nothing is executed and nothing is regenerated:
        the alternative — a plain requeue into the full respawn — would re-run
        the work whose evidence the operator was trying to preserve.

        A second failure returns to NEEDS_REVIEW carrying the same token, so
        the operator can fix the archive root and try again rather than
        landing in a state with no way forward.
        """
        execution_id = parse_recapture_marker(workstream.error_message)
        workspace_exists = self._workspace_mgr.workspace_exists(workstream.id)
        if execution_id is None or not workspace_exists:
            detail = (
                "no recapture token in the block reason"
                if execution_id is None
                else "the worktree is gone, so there is nothing left to capture"
            )
            reason = f"recapture cannot proceed: {detail}"
            self._logger.error("workstream '%s' %s", workstream.id, reason)
            await self._transition(
                workstream.id,
                WorkstreamStatus.NEEDS_REVIEW,
                expected_status=WorkstreamStatus.READY,
                message=reason,
                error_message=reason,
                resume_reason=None,
                process_pid=None,
                generation_pid=None,
            )
            return

        workspace = self._workspace_mgr.get_workspace_path(workstream.id)
        try:
            await self._capture_evidence(
                workstream.id,
                workspace,
                evidence_key=execution_id,
                backend_id="recapture",
                transport="recapture",
                exit_code=None,
            )
        except PostmortemCaptureError as exc:
            reason = (
                f"post-mortem recapture failed: {exc}; "
                f"{build_recapture_marker(execution_id)}"
            )
            self._logger.error("workstream '%s' %s", workstream.id, reason)
            await self._transition(
                workstream.id,
                WorkstreamStatus.NEEDS_REVIEW,
                expected_status=WorkstreamStatus.READY,
                message="post-mortem recapture failed; workspace preserved",
                error_message=reason,
                resume_reason=None,
                process_pid=None,
                generation_pid=None,
            )
            return

        # Clear the token with the status write: the evidence now exists, and
        # leaving it would re-enter this path on any later READY.
        await self._transition(
            workstream.id,
            WorkstreamStatus.RUNNING,
            expected_status=WorkstreamStatus.READY,
            error_message=None,
            resume_reason=None,
            process_pid=None,
            generation_pid=None,
            workspace_path=str(workspace),
        )
        self._logger.info(
            "Workstream '%s' evidence recaptured for execution %s; "
            "continuing the success pipeline (no executor, no decomposition)",
            workstream.id,
            execution_id,
        )
        await self._handle_success(workstream.id, workspace)

    async def _resume_accept_partial(self, workstream: Workstream) -> None:
        """Continue the success pipeline over an approved incomplete result.

        The operator's decision was "accept what exists", so this executes
        nothing: no author respawn, no re-decomposition, and no attempt at the
        tasks that are missing (catching up is #166's concern and has no
        mechanism here). READY -> RUNNING over the untouched worktree, then
        the ordinary success continuation — the completeness gate re-evaluates
        and passes through the recorded approval, and scope/ex-post/verification
        still apply. Approving incompleteness does not approve anything else.

        Refuses rather than falling back to a respawn: a respawn would
        regenerate the spec and mint a new sha, voiding the very approval that
        brought us here, so an operator who asked to accept a result would
        silently get a fresh run instead.
        """
        if not self._workspace_mgr.workspace_exists(workstream.id):
            reason = (
                "completeness approval cannot be honoured: the worktree is "
                "gone, so the accepted result no longer exists"
            )
            self._logger.error("workstream '%s' %s", workstream.id, reason)
            await self._transition(
                workstream.id,
                WorkstreamStatus.NEEDS_REVIEW,
                expected_status=WorkstreamStatus.READY,
                message=reason,
                error_message=reason,
                resume_reason=None,
                process_pid=None,
                generation_pid=None,
            )
            return
        workspace = self._workspace_mgr.get_workspace_path(workstream.id)
        # Clear `resume_reason` atomically with the status write: the gate
        # reads the approval from `gate_approvals` plus the marker, so the
        # reason has done its job here, and leaving it set would re-enter this
        # path on any later READY.
        await self._transition(
            workstream.id,
            WorkstreamStatus.RUNNING,
            expected_status=WorkstreamStatus.READY,
            resume_reason=None,
            process_pid=None,
            generation_pid=None,
            workspace_path=str(workspace),
        )
        self._logger.info(
            "Resuming workstream '%s' with an accepted partial result "
            "(no executor, no decomposition)",
            workstream.id,
        )
        await self._handle_success(workstream.id, workspace)

    async def _route_scope_block(self, workstream_id: str, reason: str) -> bool:
        """Fail-closed RUNNING -> FAILED -> NEEDS_REVIEW for the scope gate.

        The worktree is left intact for inspection. Returns False (blocked).
        """
        # Both writes clear the pids (#162): the RUNNING -> FAILED step is
        # where the stale pid stops being meaningful, and the FAILED ->
        # NEEDS_REVIEW step must not reintroduce one if a concurrent writer
        # set it in between. See `_route_gate_block` for why a leftover pid
        # wedges `workstream-rework`.
        await self._transition(
            workstream_id,
            WorkstreamStatus.FAILED,
            expected_status=WorkstreamStatus.RUNNING,
            message=reason,
            error_message=reason,
            process_pid=None,
            generation_pid=None,
        )
        await self._transition(
            workstream_id,
            WorkstreamStatus.NEEDS_REVIEW,
            expected_status=WorkstreamStatus.FAILED,
            message=reason,
            error_message=reason,
            process_pid=None,
            generation_pid=None,
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
        await self._persist_block_context(workstream_id, workstream, decision)
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
        # Clear both pids atomically with the status write (#162). By the time
        # a gate blocks, the spec-runner process has exited and its exit was
        # already handled — the recorded pid is stale by construction. Leaving
        # it set wedges the documented operator path: `workstream-rework`
        # refuses fail-closed on any recorded pid (`rework.py`), and
        # NEEDS_REVIEW is a stable state, so startup reconciliation never
        # revisits it. The only remaining exit was a manual
        # `UPDATE workstreams SET process_pid=NULL`, which the disputatio pilot
        # had to run four times in one wave.
        await self._transition(
            workstream_id,
            WorkstreamStatus.NEEDS_REVIEW,
            expected_status=expected,
            message=decision.reason,
            error_message=decision.reason,
            process_pid=None,
            generation_pid=None,
        )

    # ------------------------------------------------------------------
    # approver_cmd hook (#137) — automated operator over the approve API
    # ------------------------------------------------------------------

    async def _persist_block_context(
        self,
        workstream_id: str,
        workstream: Workstream,
        decision: GateDecision,
    ) -> None:
        """Persist-at-block snapshot (spec §7.1) — immutable, first write wins.

        Written for every ex-post block regardless of whether the
        approver is currently enabled, so enabling it later still works.
        Display-only failure domain: a persist error is logged, never
        raised (the block itself already routed to NEEDS_REVIEW).
        """
        marker = parse_approval_marker(decision.reason)
        if marker is None or marker.phase != "ex_post":
            return  # classification-error blocks carry no marker/sha
        paths = decision.paths or []
        try:
            context = BlockContext(
                tier=decision.tier,
                flags=list(decision.flags),
                block_reason=decision.reason or "",
                declared_scope=list(workstream.scope),
                changed_paths=paths,
                escaped_paths=find_escapes(paths, normalize(workstream.scope)),
                # Mode-2 v1: no per-workstream model is recorded; a null
                # model passes the independence comparison vacuously
                # (spec §5.3, documented limitation).
                author=AuthorInfo(harness="spec-runner", model=None),
            )
            await self._db.record_gate_block_context(
                workstream_id, "ex_post", marker.sha, context.model_dump_json()
            )
        except Exception:
            self._logger.exception(
                "Failed to persist gate block context for '%s'", workstream_id
            )

    def _approver_config(self) -> ApproverConfig | None:
        gates = self._config.gates
        if gates is None:
            return None
        return gates.approver

    @staticmethod
    def _approver_disabled(cfg: ApproverConfig) -> bool:
        return not cfg.enabled or os.environ.get("MAESTRO_APPROVER_DISABLED") == "1"

    def _approver_observe(self, workstream_id: str, sha: str, reason: str) -> None:
        """Record a §6 skip: `not_run` evidence + log, no attempt slot.

        Deduplicated per (workstream, sha, reason) within this process so
        main-loop passes don't spam.
        """
        key = (workstream_id, sha, reason)
        if key in self._approver_observed:
            return
        self._approver_observed.add(key)
        self._logger.info(
            "approver: not run for '%s' (sha=%s): %s", workstream_id, sha, reason
        )
        self._append_approver_record(workstream_id, sha or None, "not_run", note=reason)

    def _append_approver_record(
        self,
        workstream_id: str,
        sha: str | None,
        verdict: Literal["pass", "fail", "error", "not_run"],
        *,
        note: str | None,
    ) -> None:
        if self._gates is None:
            return
        try:
            self._gates.append_records(
                [
                    GateVerdictRecord(
                        gate_id="agent.approver",
                        obligation="advisory",
                        verdict=verdict,
                        phase="ex_post",
                        sha=sha,
                        ts=datetime.now(UTC).isoformat(),
                        workstream_id=workstream_id,
                        note=note,
                    )
                ]
            )
        except Exception:
            self._logger.exception("Failed to append approver evidence record")

    async def _schedule_approver(self) -> None:
        """Main-loop pass: evaluate §6 guards, spawn eligible evaluations.

        Covers both the immediate trigger (a block parked this run) and
        the restart trigger (blocks from a previous process) — the
        request is built from the persisted context either way.
        """
        cfg = self._approver_config()
        if cfg is None:
            return
        blocked = await self._db.get_workstreams_by_status(
            WorkstreamStatus.NEEDS_REVIEW
        )
        for ws in blocked:
            if ws.id in self._approver_tasks:
                continue
            await self._consider_approver_evaluation(ws, cfg)

    async def _consider_approver_evaluation(
        self, ws: Workstream, cfg: ApproverConfig
    ) -> None:
        """Apply the §6 guard chain for one workstream; maybe spawn."""
        marker = parse_approval_marker(ws.error_message)
        # Guard 1: kill-switches (reversible — observation, never a slot).
        if self._approver_disabled(cfg):
            self._approver_observe(ws.id, marker.sha if marker else "", "disabled")
            return
        # Guard 2: only marker-carrying ex-post blocks are ever evaluated.
        if marker is None or marker.phase != "ex_post":
            self._approver_observe(ws.id, "", "not_gate_block")
            return
        sha = marker.sha
        # Short-circuit: `already_attempted` is permanent for this SHA by
        # construction — once observed, later loop passes must not re-run
        # the guard chain (notably the git diff) for it.
        if (ws.id, sha, "already_attempted") in self._approver_observed:
            return
        # Guard 3: the envelope is built ONLY from the persisted context.
        context_json = await self._db.get_gate_block_context(ws.id, "ex_post", sha)
        context: BlockContext | None = None
        if context_json is not None:
            try:
                context = BlockContext.model_validate_json(context_json)
            except ValueError:
                context = None
        if context is None:
            self._approver_observe(ws.id, sha, "no_block_context")
            return
        # Guard 4: stale SHA / missing worktree.
        if not ws.workspace_path:
            self._approver_observe(ws.id, sha, "stale_sha")
            return
        workspace = Path(ws.workspace_path)
        head = await self._workspace_head(workspace)
        if head is None or head != sha:
            self._approver_observe(ws.id, sha, "stale_sha")
            return
        # Guard 5: authority budget.
        if await self._db.count_agent_approvals(ws.id) >= cfg.max_auto_approvals:
            self._approver_observe(ws.id, sha, "approval_budget_exhausted")
            return
        # Guard 6: execution budget (every attempt, any SHA, any outcome).
        if await self._db.count_approver_runs(ws.id) >= cfg.max_evaluations:
            self._approver_observe(ws.id, sha, "evaluation_budget_exhausted")
            return
        # Guard 7: cost budget — fail-closed on unproven remainder.
        if cfg.max_cost_usd is not None:
            known, has_unknown = await self._db.approver_cost_stats(ws.id)
            if has_unknown:
                self._approver_observe(ws.id, sha, "cost_budget_unknown")
                return
            if known >= cfg.max_cost_usd:
                self._approver_observe(ws.id, sha, "cost_budget_exhausted")
                return
        # Guard 8: escape size.
        if len(context.escaped_paths) > cfg.max_escaped_paths:
            self._approver_observe(ws.id, sha, "too_many_escapes")
            return
        # Guard 9: the diff must be producible and within bounds.
        diff, diff_skip = await self._produce_approver_diff(
            workspace, max_bytes=cfg.max_diff_bytes
        )
        if diff is None:
            self._approver_observe(ws.id, sha, diff_skip or "diff_error")
            return
        # Guard 10: one paid evaluation per (ws, phase, sha) — the
        # sentinel INSERT is the cross-process arbiter.
        approval_run_id = str(ulid.new())
        started = await self._db.insert_approver_run_started(
            approval_run_id, ws.id, "ex_post", sha
        )
        if not started:
            self._approver_observe(ws.id, sha, "already_attempted")
            return
        # Sentinel committed BEFORE create_task (spec §8.1) — no window
        # where an evaluation is scheduled but unrecorded.
        self._approver_tasks[ws.id] = asyncio.create_task(
            self._run_approver_evaluation(
                approval_run_id=approval_run_id,
                workstream_id=ws.id,
                sha=sha,
                context=context,
                diff=diff,
                workspace=workspace,
                cfg=cfg,
            )
        )

    async def _produce_approver_diff(
        self, workspace: Path, *, max_bytes: int
    ) -> tuple[str | None, str | None]:
        """Scope-relevant diff base...HEAD, or (None, skip_reason)."""
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(workspace),
                "diff",
                "--no-color",
                f"{self._config.base_branch}...HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await process.communicate()
        except OSError:
            return None, "diff_error"
        if process.returncode != 0:
            return None, "diff_error"
        if len(stdout) > max_bytes:
            return None, "oversize_diff"
        try:
            return stdout.decode(), None
        except UnicodeDecodeError:  # binary content — never reaches a critic
            return None, "diff_error"

    @staticmethod
    def _approver_env(approval_run_id: str, workstream_id: str, sha: str) -> dict:
        """Subprocess env (§5.2): explicit passthrough + echo identity."""
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "USER")
            if key in os.environ
        }
        env.update(
            {
                "MAESTRO_APPROVAL_RUN_ID": approval_run_id,
                "MAESTRO_WORKSTREAM_ID": workstream_id,
                "MAESTRO_GATE_PHASE": "ex_post",
                "MAESTRO_GATE_SHA": sha,
            }
        )
        return env

    async def _run_approver_evaluation(
        self,
        *,
        approval_run_id: str,
        workstream_id: str,
        sha: str,
        context: BlockContext,
        diff: str,
        workspace: Path,
        cfg: ApproverConfig,
    ) -> None:
        """One evaluation attempt: run the command, apply §7.2 on PASS.

        Every exit path finalizes the sentinel row; the task slot is
        always released in `finally`.
        """
        try:
            await self._evaluate_approver_once(
                approval_run_id=approval_run_id,
                workstream_id=workstream_id,
                sha=sha,
                context=context,
                diff=diff,
                workspace=workspace,
                cfg=cfg,
            )
        except asyncio.CancelledError:
            await self._db.finalize_approver_run(
                approval_run_id, "error", reason="interrupted"
            )
            self._append_approver_record(
                workstream_id, sha, "error", note="interrupted"
            )
            raise
        except Exception:
            self._logger.exception(
                "approver evaluation failed internally for '%s'", workstream_id
            )
            await self._db.finalize_approver_run(
                approval_run_id, "error", reason="internal_error"
            )
            self._append_approver_record(
                workstream_id, sha, "error", note="internal_error"
            )
        finally:
            self._approver_tasks.pop(workstream_id, None)

    async def _evaluate_approver_once(
        self,
        *,
        approval_run_id: str,
        workstream_id: str,
        sha: str,
        context: BlockContext,
        diff: str,
        workspace: Path,
        cfg: ApproverConfig,
    ) -> None:
        envelope = build_request_envelope(
            context,
            approval_run_id=approval_run_id,
            workstream_id=workstream_id,
            phase="ex_post",
            sha=sha,
            base_branch=self._config.base_branch,
            diff=diff,
            worktree=str(workspace),
            auto_approvals_used=await self._db.count_agent_approvals(workstream_id),
            evaluations_used=await self._db.count_approver_runs(workstream_id),
        )
        outcome = await run_approver_cmd(
            list(cfg.cmd),
            json.dumps(envelope).encode(),
            timeout_seconds=cfg.timeout_seconds,
            max_stdout_bytes=cfg.max_stdout_bytes,
            max_stderr_bytes=cfg.max_stderr_bytes,
            env=self._approver_env(approval_run_id, workstream_id, sha),
        )
        if outcome.error is not None or outcome.stdout is None:
            reason = outcome.error or "no_output"
            await self._db.finalize_approver_run(
                approval_run_id, "error", reason=reason
            )
            note = reason
            if outcome.stderr_tail:
                note = f"{reason}; stderr tail: {outcome.stderr_tail}"
            self._append_approver_record(workstream_id, sha, "error", note=note)
            return
        expected = EchoFields(
            approval_run_id=approval_run_id,
            workstream_id=workstream_id,
            phase="ex_post",
            sha=sha,
        )
        verdict = validate_verdict(
            outcome.stdout, expected, author_model=context.author.model
        )
        if isinstance(verdict, str):  # protocol ERROR — never softened
            await self._db.finalize_approver_run(
                approval_run_id, "error", reason=verdict[:500]
            )
            self._append_approver_record(
                workstream_id, sha, "error", note=verdict[:500]
            )
            return
        canonical = verdict.model_dump_json(by_alias=True)
        if verdict.verdict == "FAIL":
            await self._db.finalize_approver_run(
                approval_run_id,
                "fail",
                verdict_json=canonical,
                cost_usd=verdict.cost_usd,
            )
            self._append_approver_record(
                workstream_id, sha, "fail", note=verdict.summary or None
            )
            return
        if verdict.verdict == "ERROR":
            await self._db.finalize_approver_run(
                approval_run_id,
                "error",
                reason="command reported ERROR",
                verdict_json=canonical,
                cost_usd=verdict.cost_usd,
            )
            self._append_approver_record(
                workstream_id, sha, "error", note=verdict.summary or None
            )
            return
        await self._approve_on_pass(
            approval_run_id=approval_run_id,
            workstream_id=workstream_id,
            sha=sha,
            workspace=workspace,
            cfg=cfg,
            verdict_canonical=canonical,
            cost_usd=verdict.cost_usd,
        )

    async def _approve_on_pass(
        self,
        *,
        approval_run_id: str,
        workstream_id: str,
        sha: str,
        workspace: Path,
        cfg: ApproverConfig,
        verdict_canonical: str,
        cost_usd: float | None,
    ) -> None:
        """Spec §7.2: cost authority check → rechecks → CAS transaction."""

        async def _stale(reason: str) -> None:
            await self._db.finalize_approver_run(
                approval_run_id,
                "error",
                reason=reason,
                verdict_json=verdict_canonical,
                cost_usd=cost_usd,
            )
            self._append_approver_record(workstream_id, sha, "error", note=reason)

        # Step 1 (revision 4): the current evaluation's own cost must pass
        # the bar BEFORE any authority decision.
        if cfg.max_cost_usd is not None:
            if cost_usd is None:
                await _stale("cost_unknown_after_evaluation")
                return
            prior_sum, _ = await self._db.approver_cost_stats(workstream_id)
            if prior_sum + cost_usd > cfg.max_cost_usd:
                await _stale("cost_budget_exceeded_after_evaluation")
                return
        lock = self._approver_locks.setdefault(workstream_id, asyncio.Lock())
        async with lock:
            current = await self._db.get_workstream(workstream_id)
            if current.status != WorkstreamStatus.NEEDS_REVIEW:
                await _stale("stale_after_evaluation")
                return
            marker = parse_approval_marker(current.error_message)
            if marker is None or marker.phase != "ex_post" or marker.sha != sha:
                await _stale("stale_after_evaluation")
                return
            head = await self._workspace_head(workspace)
            if head != sha:
                await _stale("stale_after_evaluation")
                return
            try:
                await self._db.approve_workstream_agent(
                    workstream_id,
                    "ex_post",
                    sha,
                    approval_run_id=approval_run_id,
                    verdict_json=verdict_canonical,
                    cost_usd=cost_usd,
                    expected_error_message=current.error_message,
                )
            except ValueError:
                await _stale("stale_after_evaluation")
                return
        # Post-transaction HEAD confirmation (§7.2 TOCTOU boundary): a
        # mismatch is harmless — the approval stays bound to the old SHA
        # and H-6 refuses the resume — but it deserves loud evidence.
        head_after = await self._workspace_head(workspace)
        if head_after != sha:
            self._logger.warning(
                "approver: worktree of '%s' moved right after approval "
                "(%s != %s); H-6 will refuse the resume",
                workstream_id,
                head_after,
                sha,
            )
        self._logger.info(
            "approver: PASS for '%s' (sha=%s, run=%s) — re-queued",
            workstream_id,
            sha,
            approval_run_id,
        )
        self._append_approver_record(workstream_id, sha, "pass", note=None)

    async def _finalize_interrupted_approver_runs(self) -> None:
        """Startup (§8.2): started sentinels without a terminal state are
        finalized fail-closed to the human — never auto-re-run."""
        for row in await self._db.list_started_approver_runs():
            await self._db.finalize_approver_run(
                row["approval_run_id"], "error", reason="interrupted"
            )
            self._append_approver_record(
                row["workstream_id"],
                row["sha"],
                "error",
                note="interrupted (finalized at startup)",
            )
            self._logger.warning(
                "approver: finalized interrupted evaluation '%s' for '%s' "
                "(fail-closed to human)",
                row["approval_run_id"],
                row["workstream_id"],
            )

    async def _drain_approver_tasks(self) -> None:
        """Shutdown (§8.1): graceful exit waits for in-flight evaluations
        (they self-bound via timeout_seconds); a requested shutdown
        cancels them — the task handler finalizes error/interrupted."""
        tasks = list(self._approver_tasks.values())
        if not tasks:
            return
        if self._shutdown_requested:
            for task in tasks:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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
                stop_reason=await self._last_stop_reason(workstream_id),
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

        # Honest final label BEFORE any terminal transition (#123): a no-op
        # last task must read "N/N done (1 no-op)", never "DONE N-1/N".
        await self._final_progress_refresh(workstream_id, workspace_path)

        # #166: a quarantine forbids this result from progressing, so the
        # delivery tail is not entered at all. Checked before the gates because
        # classifying a diff nobody will deliver spends a steward call for
        # nothing; the MERGING CAS remains the atomic backstop for a quarantine
        # that lands after this point.
        if workstream.quarantined_at is not None:
            await self._route_quarantined_completion(
                workstream_id, WorkstreamStatus.RUNNING
            )
            return

        # Completeness (#164, always-on, first): an incomplete run's diff is
        # not worth classifying, so this precedes both diff gates. DONE must
        # be a statement about the work, not just about the process.
        if not await self._gate_completeness(workstream_id, workstream, workspace_path):
            return

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
        # Transition to MERGING — the first step that touches the base branch
        # and the git host, and therefore the point where a quarantine must
        # provably win (#166 §3.3). The guard rides inside this CAS rather than
        # being a preceding read, so a quarantine landing concurrently either
        # stops delivery here or arrives too late and is refused at its own
        # write. Exactly one of the two succeeds.
        try:
            await self._transition(
                workstream_id,
                WorkstreamStatus.MERGING,
                expected_status=expected_status,
                require_not_quarantined=True,
            )
        except ConcurrentModificationError:
            await self._route_quarantined_completion(workstream_id, expected_status)
            return

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
                    url=pr_url,  # notification payload (gate: requires_url)
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

        # Cleanup destroys the last copy of the executor logs, so it only runs
        # once the evidence is provably elsewhere (#164, spec §6.5). The
        # workstream stays DONE either way: the merge did apply, and rewriting
        # a correct terminal state because a diagnostic copy is missing would
        # be a worse lie than a leftover directory.
        if await self._postmortem_secured(workstream_id):
            self._workspace_mgr.cleanup_workspace(workstream_id)

    async def _postmortem_secured(self, workstream_id: str) -> bool:
        """True when a committed archive exists for this workstream's run.

        Asks the filesystem, not the row: a `postmortem_archives` row can
        outlive its directory, and being wrong here means destroying evidence
        that no longer exists anywhere else.
        """
        if await self._newest_archive(workstream_id) is not None:
            return True
        self._logger.warning(
            "workstream '%s' is DONE but has no committed post-mortem "
            "archive; leaving the worktree in place so its executor logs "
            "survive for diagnosis",
            workstream_id,
        )
        return False

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
            await self._route_verifying_needs_review(workstream_id, reason)
            return

        # Stage B scope: the verifier ALWAYS runs on the local backend,
        # regardless of `workstream.backend`. The verifier is orchestrator-side
        # (its `{out}`/`{criteria}` live under `<db_dir>/evidence/`, outside any
        # author isolation); the backend gate governs the AUTHOR's isolation
        # only. A docker-isolated backend here would mount only `req.workdir`,
        # so the evidence paths would be invisible in-container and every
        # attempt would ERROR. Non-local verifier backends are explicitly
        # deferred (see the TODO in `_probe_verification_handle` about ssh +
        # docker isolation for verification recovery).
        backend = LocalBackend()
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
                            "synthetic": True,
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
        # Deliberate (§4): a re-queue re-verifies rather than respawning the
        # author — a further FAIL with the budget still exhausted parks it
        # again unless the operator raised rework_budget in the config.
        await self._route_verifying_needs_review(workstream_id, reason)

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
            await self._route_verifying_needs_review(workstream_id, str(exc))
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

    async def _load_operator_rework_addendum(
        self, workstream: Workstream
    ) -> str | None:
        """Addendum keyed explicitly by (id, operator_rework_seq) (#124).

        Loaded durably from the audit table at DECOMPOSING time — never a
        latest-row heuristic, never `reason` (audit-only), so a crash
        between the CAS and spec generation loses nothing.
        """
        if workstream.operator_rework_seq is None:
            return None
        row = await self._db.get_workstream_rework(
            workstream.id, workstream.operator_rework_seq
        )
        if row is None:
            return None
        return build_operator_rework_addendum(row)

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
        *,
        stop_reason: str | None = None,
    ) -> None:
        """Handle workstream failure with retry logic.

        `stop_reason` is spec-runner's typed reason for ending the run (#169a),
        supplied by callers that have it. Callers that cannot — a
        spec-generation failure never produced executor state — pass None,
        which keeps the existing retry policy: unclassified is not unfit.
        """
        workstream = await self._db.get_workstream(workstream_id)

        # H-6 position retention (not authority — that lives in
        # gate_approvals): preserve any approval marker already carried in
        # error_message so an ordinary failure overwrite doesn't force a
        # full respawn on the next approved retry.
        preserved = preserve_approval_marker(error_message, workstream.error_message)

        # #165: a retry that cannot change the outcome still pays a full
        # re-decomposition. The pilot burned three on one validation error.
        # Classified ONLY from the typed stop_reason — never from stop_detail
        # prose, never from how fast the run failed.
        if retry_is_unproductive(stop_reason):
            verdict = describe_retry_decision(stop_reason)
            self._logger.warning(
                "workstream.retry.skipped workstream=%s stop_reason=%s",
                workstream_id,
                stop_reason,
            )
            blocked = f"{preserved} — {verdict}"
            await self._transition(
                workstream_id,
                WorkstreamStatus.NEEDS_REVIEW,
                expected_status=workstream.status,
                message=verdict,
                error_message=blocked,
                process_pid=None,
                generation_pid=None,
            )
            self._stats.failed += 1
            return

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
        """First signal drains; a second one forces termination (#166).

        Draining means: dispatch nothing new, terminate nothing, and keep
        monitoring live executions until they finalize on their own. Stopping
        the orchestrator is not a licence to destroy work in progress — that
        was the defect. Forced termination stays available, but it is now an
        explicit second act rather than the default.
        """
        if self._shutdown_requested:
            self._force_shutdown = True
            self._logger.warning(
                "second shutdown signal — forcing termination of %d running "
                "execution(s); in-flight work may be lost",
                len(self._running),
            )
        else:
            self._logger.info(
                "shutdown signal — draining: no new dispatch, %d running "
                "execution(s) will be allowed to finish (signal again to force)",
                len(self._running),
            )
        self._shutdown_requested = True
        self._shutdown_event.set()

    async def shutdown(self) -> None:
        """Request graceful shutdown."""
        self._shutdown_requested = True
        self._shutdown_event.set()

    async def _cleanup(self) -> None:
        """Release resources on shutdown.

        **Terminates nothing unless the shutdown was forced** (#166). A drained
        shutdown reaches this point with `self._running` already empty, because
        the main loop kept monitoring until every execution finalized; the
        terminate-and-reset path below then has nothing to do. It runs only
        after a forced (second-signal) shutdown, where the operator has
        explicitly chosen to lose in-flight work.

        In-flight spec generations are still cancelled either way: they hold no
        committed work, so the cost is money rather than an author's progress,
        and draining them could block shutdown for the whole spec-gen timeout.
        """
        for _zid, task in list(self._generating.items()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._generating.clear()

        if self._running and not self._force_shutdown:
            # Reached only if the loop exited with work still running for a
            # reason other than a force — e.g. an unexpected break. Say so
            # loudly rather than quietly terminating: a drain that silently
            # kills is the defect #166 removes.
            self._logger.warning(
                "shutdown with %d execution(s) still running and no force "
                "requested — leaving them alone; they will be reconciled by "
                "recovery on the next start",
                len(self._running),
            )
            self._running.clear()

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
