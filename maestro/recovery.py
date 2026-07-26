"""State recovery module for Maestro orchestrator.

This module provides recovery mechanisms for handling scheduler crashes
and restarts. It can detect orphaned tasks (stuck in RUNNING, VALIDATING,
or VERIFYING state from a crashed scheduler) and transition them back to
READY (RUNNING/VALIDATING) or to NEEDS_REVIEW (VERIFYING — fail-closed,
no auto re-run; see `_recover_verifying_tasks`) for re-execution/review.
"""

import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from maestro.coordination.arbiter_errors import ArbiterUnavailable
from maestro.coordination.routing import (
    RoutingStrategy,
    interrupted_error_code,
    task_status_to_outcome_status,
)
from maestro.database import Database
from maestro.event_log import Event, EventType, get_event_logger
from maestro.execution.backend import ExecutionBackend
from maestro.execution.docker_cli import DockerCli
from maestro.execution.docker_recovery import (
    GC_CLEAN_OUTCOMES,
    DockerProbe,
    gc_terminal_handle,
)
from maestro.execution.exec_config import ExecutionConfig
from maestro.execution.handle_ref import handle_ref_from_row
from maestro.execution.resolver import BackendResolver, ExecutionConfigError
from maestro.execution.ssh_backend import SshBackend
from maestro.execution.ssh_launch import decode_transport_ref
from maestro.execution.ssh_recovery import gc_ssh_terminal
from maestro.models import (
    Task,
    TaskOutcome,
    TaskOutcomeStatus,
    TaskStatus,
    VerifierConfig,
)
from maestro.verifier.docker_backend import build_verifier_backend


logger = logging.getLogger(__name__)

_SSH_GC_CLEAN_OUTCOMES = frozenset({"removed", "no owner marker; skipped"})
"""`gc_ssh_terminal` outcomes after which a `collected` handle is safe to
mark `cleaned` — mirrors `orchestrator.py`'s constant of the same name."""


@dataclass(frozen=True)
class RecoveryStatistics:
    """Statistics about the state recovery process.

    Attributes:
        running_recovered: Number of tasks recovered from RUNNING state.
        validating_recovered: Number of tasks recovered from VALIDATING state.
        total_recovered: Total number of tasks recovered.
        tasks_done: Number of tasks already completed (not re-executed).
        tasks_pending: Number of tasks still pending execution.
        recovery_time: Timestamp when recovery was performed.
        verifying_recovered: Number of tasks routed NEEDS_REVIEW from a
            stranded VERIFYING state (Task 11, spec §8 — fail-closed, no
            auto re-run). Defaults to 0 so existing callers/tests that
            construct `RecoveryStatistics` without this field keep working.
    """

    running_recovered: int
    validating_recovered: int
    total_recovered: int
    tasks_done: int
    tasks_pending: int
    recovery_time: datetime
    verifying_recovered: int = 0

    def __str__(self) -> str:
        """Return human-readable summary of recovery statistics."""
        lines = [
            f"Recovery completed at {self.recovery_time.isoformat()}",
            f"  RUNNING → READY: {self.running_recovered} task(s)",
            f"  VALIDATING → READY: {self.validating_recovered} task(s)",
            f"  VERIFYING → NEEDS_REVIEW: {self.verifying_recovered} task(s)",
            f"  Total recovered: {self.total_recovered} task(s)",
            f"  Already done: {self.tasks_done} task(s)",
            f"  Pending: {self.tasks_pending} task(s)",
        ]
        return "\n".join(lines)


class StateRecovery:
    """Handles state recovery after scheduler crashes or restarts.

    When the scheduler is killed (SIGKILL) or crashes unexpectedly, tasks
    may be left in RUNNING, VALIDATING, or VERIFYING state with no process
    actually executing them. This class provides methods to:

    1. Detect orphaned tasks (RUNNING/VALIDATING/VERIFYING with no active
       process)
    2. Transition them back to READY for re-execution (RUNNING/VALIDATING)
       or to NEEDS_REVIEW (VERIFYING — fail-closed, no auto re-run, spec §8)
    3. Report recovery statistics

    Usage:
        recovery = StateRecovery(database)
        stats = await recovery.recover()
        print(stats)
    """

    def __init__(
        self,
        db: Database,
        docker: DockerProbe | None = None,
        execution: ExecutionConfig | None = None,
        verifier: VerifierConfig | None = None,
    ) -> None:
        """Initialize state recovery.

        Args:
            db: Database connection for task state access.
            docker: Docker CLI wrapper used to probe execution_handles rows
                for local-docker-backed tasks before re-READYing them.
                Injectable for tests; defaults to a real `DockerCli()`. Also
                wired into the `BackendResolver` as `local_docker` so a
                resolved local-docker backend's `probe()` hits this same
                client instead of a fresh, un-injectable one. This same
                client is passed as the `docker_cli` for the verifier-docker
                backend factory (`_verifier_backend_for`), so verification
                recovery never spins up a second, un-injectable `DockerCli`.
            execution: Execution-backends config (the `execution:` block)
                used to resolve each open handle's persisted `backend_id`
                to its `ExecutionBackend` for probing. `None` keeps the
                zero-config local/bare path (only `backends["local"]`
                resolvable).
            verifier: Opt-in adversarial-verifier-gate config (the
                `verifier:` block). `None` keeps `_reconcile_verification_
                handles` fail-closed for any non-local verification
                handle it finds (no config to build a backend from —
                preserve, never guess). Required to reconcile a
                `verifier-docker` `prepared`/`running` handle.
        """
        self._db = db
        self._docker = docker or DockerCli()
        self._backends = BackendResolver(
            execution, mode="scheduler", local_docker=cast("DockerCli", self._docker)
        )
        self._verifier = verifier
        # Dedicated root for the verifier judge's execution scratch state,
        # derived identically to the scheduler (`Scheduler._verifier_exec_
        # root`) from the DB's own directory — so dispatch and recovery are
        # guaranteed to agree on the same path without either side
        # hard-coding or independently passing it in.
        self._verifier_exec_root = Path(self._db.db_path).parent / "verifier-exec"

    async def recover(
        self, routing: RoutingStrategy | None = None
    ) -> RecoveryStatistics:
        """Perform full state recovery.

        Finds all tasks in RUNNING or VALIDATING state and transitions
        them back to READY for re-execution — unless an open, non-local
        `execution_handles` row exists for the task and its resolved
        backend's `probe()` says review is needed (fail-closed: a
        docker-backed task is never silently re-run over a container that
        might still be alive, and any SSH-backed task is never silently
        re-run at all — collect may be unconfirmed). Tasks in terminal
        states (DONE, ABANDONED) are not affected. `terminal`-state handles
        (any entity) are swept for ownership-checked GC as a side effect —
        see `_gc_terminal_handles`.

        Tasks found in VERIFYING are handled differently (Task 11, spec
        §8): ALWAYS routed to NEEDS_REVIEW, unconditionally — a stranded
        verifier gate has no resumable checkpoint and the judge subprocess
        may still be alive, so there is no auto re-run in this slice. See
        `_recover_verifying_tasks`.

        Args:
            routing: Optional RoutingStrategy. When supplied, arbiter
                decisions left dangling by the crash are closed via
                `recover_arbiter_outcomes` as the final recovery step.

        Returns:
            RecoveryStatistics with details about recovered tasks.
        """
        open_handles = await self._db.get_open_execution_handles()

        # Split off verification-phase handles ONCE, right up front: they
        # are owned end-to-end by `_reconcile_verification_handles` (spec
        # §7) and must never reach the general per-phase loop below NOR
        # `_gc_terminal_handles` — a `verifier-docker` handle's `backend_id`
        # is non-local, so it WOULD otherwise flow through both (the bug
        # this task fixes: the general GC sweep doesn't know the
        # verifier-docker container-naming/label convention and could
        # mis-clean it, or leave it stranded outside the phase-specific
        # state matrix).
        general_handles = [
            h
            for h in open_handles
            if h.get("execution_phase", "task") != "verification"
        ]

        # Filter to prepared/running/terminal/collected, and split by
        # execution_phase: a task can have both a stale terminal row (prior
        # attempt, cleanup unconfirmed) and a fresh running row (current
        # attempt) open at once, AND — once validation runs on a durable
        # backend — a still-open task-phase handle alongside a live
        # validation-phase handle for the same entity_id. Keying by
        # entity_id alone would let one shadow the other; splitting by phase
        # lets RUNNING recovery probe the task handle and VALIDATING
        # recovery probe the validation handle. `terminal`/`collected` are
        # included (PR2 §4c) so a handle stranded past `running` — including
        # one whose outcome was lost after `collected` — still reaches the
        # backend-probe router instead of being silently skipped. Within one
        # (entity_id, phase), a prepared/running row always wins over a
        # terminal/collected one — the in-flight current attempt must never
        # be shadowed by stale bookkeeping left over from a prior attempt
        # whose cleanup confirmation never landed; the terminal/collected
        # row is only surfaced when it is the *sole* open handle for that
        # (entity_id, phase).
        def _by_phase(phase: str) -> dict[str, dict[str, Any]]:
            candidates = [
                h
                for h in general_handles
                if h["entity_kind"] == "task"
                and h["state"] in ("prepared", "running", "terminal", "collected")
                and h.get("execution_phase", "task") == phase
            ]
            result: dict[str, dict[str, Any]] = {}
            for h in candidates:
                if h["state"] in ("prepared", "running"):
                    result[h["entity_id"]] = h
            for h in candidates:
                if h["state"] in ("terminal", "collected"):
                    result.setdefault(h["entity_id"], h)
            return result

        task_phase = _by_phase("task")
        validation_phase = _by_phase("validation")

        # Recover RUNNING tasks: the task-phase handle is the live execution.
        running_recovered = await self._recover_running_tasks(task_phase)

        # Recover VALIDATING tasks: prefer the validation-phase handle (the
        # validation container), but fall back to a still-open task-phase
        # handle (a primary container whose finalize never completed is an
        # equal live-container hazard). validation wins on key collision, so a
        # stale task handle never shadows a live validation handle (detail 4).
        validating_handles = {**task_phase, **validation_phase}
        validating_recovered = await self._recover_validating_tasks(validating_handles)

        # Phase-specific owner of ALL open verification handles (any task
        # status, any backend) — see `_reconcile_verification_handles`.
        # Runs independently of task-status routing below: a verification
        # handle can be open whether its task is still VERIFYING, already
        # NEEDS_REVIEW, or even DONE/ABANDONED (finalize ran but the handle
        # cleanup confirmation never landed).
        await self._reconcile_verification_handles()

        # Recover VERIFYING tasks: fail-closed, no auto re-run (spec §8) —
        # every task found here always routes to NEEDS_REVIEW. Pure FSM
        # routing only; handle reconciliation is owned entirely by
        # `_reconcile_verification_handles` above.
        verifying_recovered = await self._recover_verifying_tasks()

        # Best-effort GC of leftover containers for settled entities.
        # `general_handles` excludes verification-phase rows (owned above).
        await self._gc_terminal_handles(general_handles)

        if routing is not None:
            await recover_arbiter_outcomes(self._db, routing)

        # Get counts for statistics
        all_tasks = await self._db.get_all_tasks()
        done_count = sum(1 for t in all_tasks if t.status == TaskStatus.DONE)
        pending_count = sum(
            1 for t in all_tasks if t.status in (TaskStatus.PENDING, TaskStatus.READY)
        )

        return RecoveryStatistics(
            running_recovered=running_recovered,
            validating_recovered=validating_recovered,
            verifying_recovered=verifying_recovered,
            total_recovered=(
                running_recovered + validating_recovered + verifying_recovered
            ),
            tasks_done=done_count,
            tasks_pending=pending_count,
            recovery_time=datetime.now(UTC),
        )

    async def _recover_running_tasks(
        self, task_handles: dict[str, dict[str, Any]]
    ) -> int:
        """Recover tasks stuck in RUNNING state.

        Transitions RUNNING → FAILED → READY to allow re-execution — unless
        the task has an open, non-local execution handle and its resolved
        backend's `probe()` says review is needed, in which case it goes
        RUNNING → NEEDS_REVIEW instead (a direct edge, valid per the
        `TaskStatus` state diagram).

        Args:
            task_handles: Map of `entity_id` -> open `execution_handles`
                row, filtered to `entity_kind == "task"`.

        Returns:
            Number of tasks recovered (READY or routed to NEEDS_REVIEW).
        """
        running_tasks = await self._db.get_tasks_by_status(TaskStatus.RUNNING)
        recovered = 0

        for task in running_tasks:
            if await self._route_open_handle_to_review(task, task_handles):
                recovered += 1
                continue
            await self._transition_to_ready(task, "Recovered after scheduler restart")
            recovered += 1

        return recovered

    async def _recover_validating_tasks(
        self, task_handles: dict[str, dict[str, Any]]
    ) -> int:
        """Recover tasks stuck in VALIDATING state.

        Transitions VALIDATING → FAILED → READY to allow re-execution —
        unless the task has an open, non-local execution handle and its
        resolved backend's `probe()` says review is needed, in which case
        it goes VALIDATING → FAILED → NEEDS_REVIEW instead (VALIDATING has
        no direct edge to NEEDS_REVIEW; see the `TaskStatus` state diagram).

        Args:
            task_handles: Map of `entity_id` -> open `execution_handles`
                row, filtered to `entity_kind == "task"`.

        Returns:
            Number of tasks recovered (READY or routed to NEEDS_REVIEW).
        """
        validating_tasks = await self._db.get_tasks_by_status(TaskStatus.VALIDATING)
        recovered = 0

        for task in validating_tasks:
            if await self._route_open_handle_to_review(task, task_handles):
                recovered += 1
                continue
            await self._transition_to_ready(
                task, "Recovered from validation after scheduler restart"
            )
            recovered += 1

        return recovered

    async def _recover_verifying_tasks(self) -> int:
        """Recover tasks stuck in VERIFYING state (Task 11, spec §8).

        Fail-closed, no auto re-run: every task found here is routed
        straight to NEEDS_REVIEW (VERIFYING has a direct edge to
        NEEDS_REVIEW; see the `TaskStatus` state diagram) — a stranded
        verifier gate has no resumable checkpoint, and the judge subprocess
        may still be alive.

        Pure FSM routing only — this method never looks up or touches the
        task's verification handle; that ownership belongs entirely to
        `_reconcile_verification_handles` (spec §7), called independently
        by `recover()` regardless of task status.

        Returns:
            Number of tasks routed to NEEDS_REVIEW.
        """
        verifying_tasks = await self._db.get_tasks_by_status(TaskStatus.VERIFYING)

        for task in verifying_tasks:
            message = (
                "recovery: task stranded in VERIFYING at scheduler restart "
                "— fail-closed, no auto re-run (spec §8)"
            )
            logger.warning(
                "recovery: task '%s' stranded in VERIFYING — routing to "
                "NEEDS_REVIEW (fail-closed, no auto re-run)",
                task.id,
            )
            await self._db.update_task_status(
                task.id, TaskStatus.NEEDS_REVIEW, error_message=message
            )

        return len(verifying_tasks)

    async def _reconcile_verification_handles(self) -> None:
        """Own ALL open verification handles regardless of task status/backend.

        The single reconciliation owner for `execution_phase='verification'`
        rows (spec §7.2 state matrix) — split out of `open_handles` and
        never touched by the general per-phase loop or `_gc_terminal_
        handles` (see `recover()`). Queries `Database.get_open_verification_
        handles` (the plural, all-backend query — NOT the singular
        per-task requeue-fence lookup) so a `local` verifier handle is
        reconciled here too, not just `verifier-docker`.

        - `prepared`/`running`: build the matching verifier backend via
          `_verifier_backend_for`, `accepts_ref()` BEFORE `probe()`; any
          unresolved backend, rejected ref, or probe error, or a live
          result, PRESERVES the handle open. Only a confirmed-dead probe
          closes it.
        - `terminal`/`collected`, `local` backend: `finalize_handle`'s
          `wait()` already confirmed the judge subprocess exited before
          either state is reached, so mark `cleaned` directly — no Docker
          GC, no container/credential artifact.
        - `terminal`/`collected`, `verifier-docker` backend:
          ownership-checked `gc_terminal_handle`, then credential-artifact
          cleanup (spec §7.3), then `cleaned`. A non-clean GC outcome or
          any exception PRESERVES the handle (fail-closed) for the next
          sweep.
        """
        rows = await self._db.get_open_verification_handles()
        for row in rows:
            await self._reconcile_one_verification_handle(row)

    async def _reconcile_one_verification_handle(self, row: dict[str, Any]) -> None:
        """Reconcile a single open verification-phase handle (see
        `_reconcile_verification_handles` for the full state matrix)."""
        state = row["state"]
        backend_id = row["backend_id"]

        if state in ("prepared", "running"):
            backend = self._verifier_backend_for(row)
            if backend is None:
                return  # unknown/mismatch/no-config -> preserve
            ref = handle_ref_from_row(row)
            if not backend.accepts_ref(ref):
                return
            try:
                result = await backend.probe(ref)
            except Exception:
                return
            if not result.needs_review:
                await self._close_handle(row["execution_id"])
            return

        if state in ("terminal", "collected"):
            if backend_id == "verifier-docker":
                try:
                    outcome = await gc_terminal_handle(row, self._docker)
                except Exception:
                    return
                if outcome not in GC_CLEAN_OUTCOMES:
                    return  # preserve for next sweep
                self._cleanup_credential_artifacts(row["execution_id"])
            await self._db.mark_execution_state(
                row["execution_id"], "cleaned", allowed_from=[state]
            )

    def _verifier_backend_for(self, row: dict[str, Any]) -> ExecutionBackend | None:
        """Resolve the verifier backend for a persisted row.

        `backend_id == "local"` resolves via the plain `BackendResolver`
        unconditionally — the verifier gate's `"local"` backend IS the
        general local execution backend, with no verifier-specific sandbox
        config to drift, so this needs no `VerifierConfig` at all (mirrors
        pre-Task-8 behavior byte-for-byte: a dead-pid local verification
        handle is still reconcilable even when the caller passed no
        `verifier=` — e.g. `maestro run --resume` recovering a task whose
        project config since dropped its `verifier:` block).

        `backend_id == "verifier-docker"` requires the shared
        `build_verifier_backend` factory AND a `VerifierConfig` whose
        `backend` is `"docker"` — config drift (missing config, or config
        now says `"local"`) means `None` (caller preserves, fail-closed).

        Any other `backend_id`, or a factory/resolve failure, is `None`.
        """
        backend_id = row["backend_id"]
        if backend_id == "local":
            try:
                return self._backends.resolve("local")
            except ExecutionConfigError:
                return None
        if backend_id != "verifier-docker":
            return None
        if self._verifier is None or self._verifier.backend != "docker":
            return None
        try:
            return build_verifier_backend(
                self._verifier,
                local_backend=self._backends.resolve("local"),
                exec_root=self._verifier_exec_root,
                docker_cli=cast("DockerCli", self._docker),
            )
        except Exception:
            return None

    def _cleanup_credential_artifacts(self, execution_id: str) -> None:
        """Delete the deterministic verifier temp-dir (env-file/cidfile/dir).

        Path-safety (spec §7.3): `execution_id` must parse as a UUID, and
        the recomputed canonical temp-dir must resolve inside the
        dedicated verifier exec root, before any destructive operation. A
        malformed UUID or an out-of-root path is a no-op — this must never
        delete outside `self._verifier_exec_root`.
        """
        try:
            uuid.UUID(execution_id)
        except ValueError:
            return

        root = self._verifier_exec_root.resolve()
        target = (root / f"maestro-verify-{execution_id}").resolve()
        if target != root and root not in target.parents:
            return
        shutil.rmtree(target, ignore_errors=True)

    async def _route_open_handle_to_review(
        self, task: Task, task_handles: dict[str, dict[str, Any]]
    ) -> bool:
        """Probe a task's open handle via its resolved backend; route to
        NEEDS_REVIEW unless the backend proves it safe to reclaim.

        No-op (returns False) for tasks with no open, non-cleaned handle
        row — a plain local task (no durable handle row at all) is always
        unaffected, preserving the pre-Task-18 recovery behavior exactly.

        Per the PR2 §4c state matrix, classification is by the persisted
        `backend_id` — resolved through `BackendResolver` — never by a
        nullable coordinate or hand-composed transport check:

        - a `collected` handle (any backend) always routes to review: the
          scope reservation is already GC-eligible and the handle itself is
          safe to leave alone, but the crashed task's outcome was never
          recorded, so it cannot be silently re-READYed or treated as done.
        - otherwise, the row is probed via `backend.probe()` — the single,
          transport-correct recovery boundary. A local bare/docker backend
          can prove "no live PID/container" (`needs_review=False`) and is
          safely reclaimed; an SSH backend (bare or docker) always answers
          `needs_review=True` for an open handle (collect unconfirmed).
        - an unresolvable `backend_id`, or any exception raised while
          probing (e.g. a placeholder SSH row with no real coordinates yet,
          crashed before `update_execution_handle_launch`), fails closed to
          NEEDS_REVIEW rather than falling through to a default backend.
        - a resolved backend whose `accepts_ref()` rejects the persisted ref
          — the persisted transport/isolation identity no longer matches
          the resolved backend (config drift after the handle was minted),
          or the ref is a placeholder/unknown — also fails closed to
          NEEDS_REVIEW, WITHOUT ever calling `probe()`. Probing across
          identities (e.g. a local-docker probe of an SSH run's
          `execution_id`) would fail-OPEN: it could report "no such
          container" and silently reclaim a task whose real remote run is
          still unconfirmed. (spec decision #5)

        Args:
            task: The RUNNING or VALIDATING task being recovered.
            task_handles: Map of `entity_id` -> open `execution_handles`
                row, filtered to `entity_kind == "task"`.

        Returns:
            True if the task was routed to NEEDS_REVIEW (caller must not
            also re-READY it); False if there is nothing to probe or the
            backend confirmed it is safe to reclaim.
        """
        row = task_handles.get(task.id)
        if row is None:
            return False

        if row["state"] == "collected":
            await self._route_to_review(
                task,
                f"execution {row['execution_id']!r} was collected but its "
                "outcome was never recorded before the crash",
            )
            return True

        try:
            backend = self._backends.resolve(row["backend_id"])
        except ExecutionConfigError as exc:
            await self._route_to_review(
                task, f"unresolvable backend {row['backend_id']!r}: {exc}"
            )
            return True

        ref = handle_ref_from_row(row)
        if not backend.accepts_ref(ref):
            # Persisted transport/isolation identity no longer matches the
            # resolved backend (config drift), or the ref is a placeholder/
            # unknown (crash before launch write-back). Never probe across
            # identities — a local-docker probe of an SSH run's execution_id
            # would fail-OPEN. Fail-closed. (spec decision #5)
            await self._route_to_review(
                task, "persisted execution identity does not match current config"
            )
            return True

        try:
            result = await backend.probe(ref)
        except Exception as exc:
            # failure (e.g. a placeholder ref with no real coordinates yet)
            # means review, never a silent reclaim.
            await self._route_to_review(task, f"probe failed: {exc}")
            return True

        if not result.needs_review:
            await self._close_handle(row["execution_id"])
            return False

        await self._route_to_review(task, result.detail or "needs review")
        return True

    async def _close_handle(self, execution_id: str) -> None:
        """Confirmed safe to reclaim: close the open handle row.

        Marks the handle `terminal` then `cleaned` (both are no-ops if the
        row is already past that point) so it doesn't linger open and
        shadow the fresh attempt's own handle after the task is re-READYed.
        """
        await self._db.mark_execution_state(
            execution_id, "terminal", allowed_from=["prepared", "running"]
        )
        await self._db.mark_execution_state(
            execution_id, "cleaned", allowed_from=["terminal"]
        )

    async def _route_to_review(self, task: Task, reason: str) -> None:
        """Route a RUNNING/VALIDATING task to NEEDS_REVIEW.

        RUNNING has a direct edge to NEEDS_REVIEW; VALIDATING does not, so
        it goes through FAILED first (both valid per the `TaskStatus` state
        diagram).
        """
        message = f"recovery: {reason}"
        logger.warning(
            "recovery: task '%s' has a possibly-live/unresolved execution "
            "(%s) — routing to NEEDS_REVIEW instead of READY",
            task.id,
            reason,
        )
        if task.status == TaskStatus.VALIDATING:
            await self._db.update_task_status(
                task.id, TaskStatus.FAILED, error_message=message
            )
            await self._db.update_task_status(
                task.id,
                TaskStatus.NEEDS_REVIEW,
                expected_status=TaskStatus.FAILED,
            )
        else:
            await self._db.update_task_status(
                task.id, TaskStatus.NEEDS_REVIEW, error_message=message
            )

    async def _gc_terminal_handles(self, handles: list[dict[str, Any]]) -> int:
        """Best-effort, ownership-checked GC sweep — transport/isolation-aware.

        A `terminal`/`collected` handle means the entity behind it already
        reached a settled status (finalize ran) but resource cleanup was
        never confirmed. This only removes leftover containers/remote
        artifacts and marks the handle `cleaned` — it never touches entity
        status. Swept across all entity kinds (task and workstream), since
        the handle table is shared.

        Classification is by the persisted `backend_id`, resolved through
        `BackendResolver` (never a hand-composed transport/isolation check):

        - resolved `SshBackend`, `terminal` state -> **never GC'd**. A
          terminal marker does not prove `collect` ran, so the remote tmp
          must be preserved for the operator (this is the bug this task
          fixes: docker-GC'ing an SSH `terminal` row destroyed the record
          of an uncollected remote run).
        - resolved `SshBackend`, `collected` bare -> guarded remote-root GC
          (`gc_ssh_terminal`) -> `cleaned` on a clean outcome.
        - resolved `SshBackend`, `collected` docker (persisted isolation) ->
          remote **container** GC first; only on a clean outcome does the
          remote-root GC run -> `cleaned` (container-first: never delete
          the remote root while a container may still reference it).
        - any other resolved backend (local bare/docker) -> docker GC on
          both `terminal` and `collected` states -> `cleaned` on a clean
          outcome (spec §5: local-docker rows left in `collected` state
          after a finalize crash have containers that must be removed).
        - an unresolvable `backend_id`, or a resolved backend whose
          `accepts_ref()` rejects the persisted ref (config drift after the
          handle was minted, or an unknown/placeholder ref), is left in
          place (fail-closed) for the next sweep or a human to resolve —
          never docker-GC an SSH run's `execution_id` (or vice versa) just
          because the backend *name* still resolves to something.

        A row whose outcome is ambiguous (multiple container matches /
        label mismatch / probe error / no owner marker / resolve failure)
        is left in place for the next sweep or a human to resolve.

        Args:
            handles: Open `execution_handles` rows (any state) from
                `Database.get_open_execution_handles()`.

        Returns:
            Number of handles marked `cleaned`.
        """
        swept = 0
        for row in handles:
            state = row["state"]
            if state not in ("terminal", "collected"):
                continue

            try:
                backend = self._backends.resolve(row["backend_id"])
            except ExecutionConfigError as exc:
                logger.warning(
                    "recovery: GC skipping handle %s (%s %s): "
                    "unresolvable backend %r: %s",
                    row["execution_id"],
                    row["entity_kind"],
                    row["entity_id"],
                    row["backend_id"],
                    exc,
                )
                continue

            if not backend.accepts_ref(handle_ref_from_row(row)):
                logger.warning(
                    "recovery: GC skipping handle %s (%s %s): persisted "
                    "execution identity does not match resolved backend "
                    "%r (config drift)",
                    row["execution_id"],
                    row["entity_kind"],
                    row["entity_id"],
                    row["backend_id"],
                )
                continue

            if isinstance(backend, SshBackend):
                cleaned = await self._gc_ssh_row(row, backend, state)
            else:
                outcome = await gc_terminal_handle(row, self._docker)
                cleaned = outcome in GC_CLEAN_OUTCOMES
                if not cleaned:
                    logger.warning(
                        "recovery: GC left handle %s (%s %s) as %s: %s",
                        row["execution_id"],
                        row["entity_kind"],
                        row["entity_id"],
                        state,
                        outcome,
                    )

            if cleaned:
                await self._db.mark_execution_state(
                    row["execution_id"], "cleaned", allowed_from=[state]
                )
                swept += 1
        return swept

    async def _gc_ssh_row(
        self, row: dict[str, Any], backend: SshBackend, state: str
    ) -> bool:
        """GC a single SSH-backed handle row.

        `terminal` is never GC'd here (collect unconfirmed; the remote tmp
        must be preserved). `collected` runs a guarded remote-root GC,
        container-first when the persisted `transport_ref` carries a
        docker isolation. Never raises — any failure is logged and treated
        as "not cleaned", leaving the row for the next sweep.
        """
        if state != "collected":
            return False

        ref = handle_ref_from_row(row)
        try:
            decoded = decode_transport_ref(ref.transport_ref)
        except Exception as exc:
            logger.warning(
                "recovery: GC could not decode transport_ref for handle %s (%s %s): %s",
                row["execution_id"],
                row["entity_kind"],
                row["entity_id"],
                exc,
            )
            return False

        try:
            if decoded.get("isolation") == "docker":
                # Container-first ordering: never delete the remote root
                # (which may hold the only evidence of what the container
                # touched) before the container itself is confirmed gone.
                if backend.docker is None:
                    logger.warning(
                        "recovery: GC left handle %s (%s %s) as collected: "
                        "persisted docker isolation but backend has no "
                        "docker client",
                        row["execution_id"],
                        row["entity_kind"],
                        row["entity_id"],
                    )
                    return False
                dk_outcome = await gc_terminal_handle(
                    {"execution_id": row["execution_id"]},
                    backend.docker,
                    expected_labels=decoded.get("expected_labels"),
                )
                if dk_outcome not in GC_CLEAN_OUTCOMES:
                    logger.warning(
                        "recovery: container GC not clean for handle %s "
                        "(%s %s): %s — leaving remote root intact",
                        row["execution_id"],
                        row["entity_kind"],
                        row["entity_id"],
                        dk_outcome,
                    )
                    return False
            outcome = await gc_ssh_terminal(backend._ssh, ref)
        except Exception as exc:
            logger.warning(
                "recovery: ssh GC failed for handle %s (%s %s): %s",
                row["execution_id"],
                row["entity_kind"],
                row["entity_id"],
                exc,
            )
            return False

        if outcome in _SSH_GC_CLEAN_OUTCOMES:
            return True
        logger.warning(
            "recovery: GC left handle %s (%s %s) as collected: %s",
            row["execution_id"],
            row["entity_kind"],
            row["entity_id"],
            outcome,
        )
        return False

    async def _transition_to_ready(self, task: Task, reason: str) -> None:
        """Transition a task back to READY state for re-execution.

        Follows the state machine: RUNNING/VALIDATING → FAILED → READY

        Note: If the second transition fails after FAILED is set, the task
        remains in FAILED state. This is acceptable because FAILED → READY
        is a valid transition that will be retried on the next recovery cycle.

        Args:
            task: The task to recover.
            reason: Description of why the task is being recovered.
        """
        # First transition to FAILED (valid from both RUNNING and VALIDATING)
        await self._db.update_task_status(
            task.id,
            TaskStatus.FAILED,
            error_message=reason,
        )

        # Then transition to READY
        await self._db.update_task_status(
            task.id,
            TaskStatus.READY,
            expected_status=TaskStatus.FAILED,
        )

    async def get_orphaned_task_count(self) -> int:
        """Get count of tasks that need recovery.

        Returns:
            Number of tasks in RUNNING, VALIDATING, or VERIFYING state.
        """
        running = await self._db.get_tasks_by_status(TaskStatus.RUNNING)
        validating = await self._db.get_tasks_by_status(TaskStatus.VALIDATING)
        verifying = await self._db.get_tasks_by_status(TaskStatus.VERIFYING)
        return len(running) + len(validating) + len(verifying)

    async def needs_recovery(self) -> bool:
        """Check if any tasks need recovery.

        Returns:
            True if there are tasks in RUNNING, VALIDATING, or VERIFYING
            state.
        """
        return await self.get_orphaned_task_count() > 0


def _reconstruct_outcome(task: Task, status: TaskOutcomeStatus) -> TaskOutcome:
    """Rebuild a TaskOutcome from persisted Task state for recovery delivery."""
    duration_min: float | None = None
    if task.started_at and task.completed_at:
        duration_min = (task.completed_at - task.started_at).total_seconds() / 60.0

    error_code = interrupted_error_code(task.status)
    if error_code is None and task.error_message:
        lines = task.error_message.splitlines()
        first = lines[0] if lines else task.error_message
        error_code = first[:200]

    return TaskOutcome(
        status=status,
        agent_used=task.routed_agent_type or task.agent_type.value,
        duration_min=duration_min,
        tokens_used=None,
        cost_usd=None,
        error_code=error_code,
    )


async def recover_arbiter_outcomes(db: Database, routing: RoutingStrategy) -> int:
    """R-03: Close dangling arbiter decisions after a Maestro crash.

    Iterates tasks with a persisted `arbiter_decision_id` but no
    `arbiter_outcome_reported_at`, reconstructs an outcome from persisted
    state (duration, error_code; status from `task_status_to_outcome_status`
    — e.g. RUNNING/VALIDATING map to INTERRUPTED), and reports it through
    the supplied routing strategy. StaticRouting's `report_outcome` is a
    no-op, so passing it keeps the static path safe.

    Tasks whose status can't yield a valid outcome (PENDING / READY /
    AWAITING_APPROVAL carrying a decision_id — an invariant violation) are
    logged and skipped. Delivery stops at the first `ArbiterUnavailable`;
    the scheduler's re-attempt pass picks up where we left off.

    Returns:
        Count of outcomes successfully re-delivered.
    """
    pending = await db.get_tasks_with_pending_outcome()
    now = datetime.now(UTC)
    count = 0

    for task in pending:
        outcome_status = task_status_to_outcome_status(task.status)
        if outcome_status is None:
            logger.error(
                "recovery: task %s has decision_id but status %s — skipping",
                task.id,
                task.status.value,
            )
            continue
        if task.arbiter_decision_id is None:
            continue

        outcome = _reconstruct_outcome(task, outcome_status)
        try:
            await routing.report_outcome(task, outcome)
        except ArbiterUnavailable:
            logger.info("recovery: arbiter unavailable — stopping at task %s", task.id)
            break
        await db.mark_outcome_reported(task.id, now, task.arbiter_decision_id)
        count += 1

    event_logger = get_event_logger()
    if event_logger is not None:
        event_logger.log(
            Event(
                event_type=EventType.RECOVERY_ARBITER_DECISIONS_CLOSED,
                details={"count": count},
            )
        )
    return count
