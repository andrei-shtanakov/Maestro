"""VerificationProvider Protocol + CommandVerifier (§6, §7, §9).

`CommandVerifier` runs a workstream's configured verifier command through the
transport-agnostic execution layer (`maestro.execution`) and translates the
result through the §5 strict handshake (`evaluate_handshake`), adding two
protocol-level cross-checks the handshake alone cannot see: the recomputed
artifact hash, and worktree cleanliness before/after the run. Every protocol
violation degrades to a `HandshakeResult(outcome=ERROR, ...)` — this class
never raises for a contract violation; only genuine infrastructure failures
(DB errors, a missing git binary, ...) propagate as exceptions.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from maestro.domain.profile import CriteriaConfig, VerifierSpec, render_argv
from maestro.domain.verdict import (
    EchoExpectations,
    HandshakeResult,
    VerdictValue,
    evaluate_handshake,
)
from maestro.execution.models import CollectPolicy, ExecutionRequest


if TYPE_CHECKING:
    # Deferred: maestro.database imports maestro.models, which imports
    # maestro.domain.profile — a module-level import of Database here would
    # be circular (same rationale as maestro.domain.ledger). The
    # execution_phase="verification" CAS target is passed as a plain string,
    # never the WorkstreamStatus enum, for the same reason.
    from maestro.database import Database
    from maestro.execution.backend import ExecutionBackend


class VerificationContext(BaseModel):
    """Everything one `CommandVerifier.verify()` call needs (§9 invocation).

    `out_json` is Maestro-assigned (ledger staging, Task 5's
    `EvidenceLedger.staging_dir`); the verifier never chooses its own output
    address. `workstream_id`/`rework_attempt`/`profile_sha256`/
    `verified_source_commit`/`verified_source_tree` are computed by Maestro
    and must be echoed back unchanged (§5) — conveyed to the verifier
    subprocess via `MAESTRO_*` env vars, never argv (the argv placeholder
    set is a small, profile-shared template).
    """

    model_config = ConfigDict(frozen=True)

    workstream_id: str
    run_id: str
    attempt: int
    rework_attempt: int
    worktree: Path
    out_json: Path
    profile_sha256: str
    verified_source_commit: str
    verified_source_tree: str


@runtime_checkable
class VerificationProvider(Protocol):
    """Runs one verification attempt and returns its handshake outcome."""

    async def verify(self, ctx: VerificationContext) -> HandshakeResult:
        """Execute the verifier for `ctx` and evaluate its handshake."""
        ...


def _protocol_error(message: str) -> HandshakeResult:
    return HandshakeResult(
        outcome=VerdictValue.ERROR, protocol_error=message, document=None
    )


def _git_status_porcelain(worktree: Path) -> str:
    """Return `git status --porcelain` output for `worktree` (may raise)."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _is_clean(worktree: Path) -> bool:
    return _git_status_porcelain(worktree) == ""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class CommandVerifier:
    """Implements `VerificationProvider` by spawning an external process.

    Args:
        spec: Argv template, timeout, and error-retry budget (Task 2).
        criteria: Rubric reference — visibility, source, pinned sha256.
        artifact: Worktree-relative path to the artifact under verification.
        backend: Execution backend the verifier process runs on.
        db: Optional database handle. When provided, the spawned process's
            execution handle is persisted (`execution_phase="verification"`)
            via the same `Database.start_execution` CAS the durable
            validation path uses — a self-loop on the workstream's current
            (already-`VERIFYING`) status, since every attempt of a
            verification run happens entirely inside that one state.
    """

    def __init__(
        self,
        spec: VerifierSpec,
        criteria: CriteriaConfig,
        artifact: str,
        backend: ExecutionBackend,
        *,
        db: Database | None = None,
    ) -> None:
        self._spec = spec
        self._criteria = criteria
        self._artifact = artifact
        self._backend = backend
        self._db = db

    async def verify(self, ctx: VerificationContext) -> HandshakeResult:
        """Run one verifier attempt for `ctx` (binding 5-step order, §6)."""
        try:
            clean_before = _is_clean(ctx.worktree)
        except subprocess.CalledProcessError as exc:
            return _protocol_error(f"git status failed: {exc}")
        if not clean_before:
            return _protocol_error("worktree is not clean before verification")

        try:
            criteria_bytes = self._read_criteria_bytes(ctx.worktree)
        except OSError as exc:
            return _protocol_error(f"criteria unreadable: {exc}")
        actual_criteria_sha = _sha256_bytes(criteria_bytes)
        if actual_criteria_sha != self._criteria.sha256:
            return _protocol_error(
                "criteria sha256 mismatch: expected "
                f"{self._criteria.sha256}, got {actual_criteria_sha}"
            )

        artifact_path = ctx.worktree / self._artifact
        try:
            artifact_sha = _sha256_bytes(artifact_path.read_bytes())
        except OSError as exc:
            return _protocol_error(f"artifact unreadable: {exc}")

        staged_criteria = self._stage_criteria(ctx.out_json, criteria_bytes)
        execution_id = str(uuid.uuid4())
        try:
            # Gate BEFORE spawn (mirrors orchestrator.py's READY->RUNNING
            # CAS-before-spawn pattern, e.g. its non-local branch around
            # `start_execution`): if the workstream left VERIFYING between
            # scheduling and here, this raises and NOTHING gets spawned —
            # there is no window where a live subprocess exists untracked.
            if self._db is not None:
                await self._pre_spawn_persist(ctx, execution_id)
            request = self._build_request(ctx, staged_criteria, execution_id)
            handle = await self._backend.run(request)
            if self._db is not None:
                # Patch in the transport_ref the backend actually minted
                # (placeholder above was seeded pre-spawn) — same two-step
                # `start_execution` + `update_execution_handle_launch` the
                # SSH path uses to record real launch coordinates.
                await self._db.update_execution_handle_launch(
                    execution_id,
                    transport_ref=handle.ref.transport_ref,
                    remote_host=None,
                    remote_dir=None,
                    status_marker=None,
                )
            result = await handle.wait()
        finally:
            staged_criteria.unlink(missing_ok=True)

        expected = EchoExpectations(
            run_id=ctx.run_id,
            attempt=ctx.attempt,
            rework_attempt=ctx.rework_attempt,
            workstream_id=ctx.workstream_id,
            artifact=self._artifact,
            profile_sha256=ctx.profile_sha256,
            verified_source_commit=ctx.verified_source_commit,
            verified_source_tree=ctx.verified_source_tree,
        )
        handshake = evaluate_handshake(
            ctx.out_json, result.exit_code, result.timed_out, expected
        )

        if (
            handshake.document is not None
            and handshake.document.identity.artifact_sha256 != artifact_sha
        ):
            return _protocol_error(
                "artifact sha256 changed: recomputed "
                f"{artifact_sha}, verifier reported "
                f"{handshake.document.identity.artifact_sha256}"
            )
        try:
            clean_after = _is_clean(ctx.worktree)
        except subprocess.CalledProcessError as exc:
            return _protocol_error(f"git status failed: {exc}")
        if not clean_after:
            return _protocol_error("worktree was modified during verification")

        return handshake

    def _read_criteria_bytes(self, worktree: Path) -> bytes:
        """Read the pinned rubric bytes (§7): worktree-relative for
        `shared`, absolute operator-side path for `verifier_only`.
        """
        if self._criteria.visibility == "shared":
            path = worktree / self._criteria.source
        else:
            path = Path(self._criteria.source)
        return path.read_bytes()

    @staticmethod
    def _stage_criteria(out_json: Path, data: bytes) -> Path:
        """Write an ephemeral 0600 copy of the criteria bytes beside
        `out_json`; the caller deletes it in a `finally` after the attempt.
        """
        path = out_json.parent / f"{out_json.stem}.criteria"
        path.write_bytes(data)
        path.chmod(0o600)
        return path

    def _build_request(
        self, ctx: VerificationContext, staged_criteria: Path, execution_id: str
    ) -> ExecutionRequest:
        values = {
            "artifact": self._artifact,
            "criteria": str(staged_criteria),
            "out": str(ctx.out_json),
            "run_id": ctx.run_id,
            "attempt": str(ctx.attempt),
        }
        argv = render_argv(self._spec.argv, values)
        return ExecutionRequest(
            run_id=f"verify-{ctx.run_id}-{ctx.attempt}",
            argv=argv,
            workdir=ctx.worktree,
            log_path=ctx.out_json.with_suffix(".log"),
            env={
                # `inherit_env=False` below means `req.env` is the *entire*
                # child environment (plus tracing vars) — PATH must be
                # passed explicitly or a non-absolute argv[0] (e.g. `uv`,
                # per the §9 example invocation) can't be resolved.
                "PATH": os.environ.get("PATH", ""),
                "MAESTRO_PROFILE_SHA256": ctx.profile_sha256,
                "MAESTRO_VERIFIED_SOURCE_COMMIT": ctx.verified_source_commit,
                "MAESTRO_VERIFIED_SOURCE_TREE": ctx.verified_source_tree,
                "MAESTRO_WORKSTREAM_ID": ctx.workstream_id,
                "MAESTRO_REWORK_ATTEMPT": str(ctx.rework_attempt),
            },
            # Deliberately NOT inherit_env=True: `build_local_env` (Phase 0)
            # drops `req.env` entirely whenever `inherit_env` is True, which
            # would silently swallow the five echo-field env vars above.
            inherit_env=False,
            timeout_seconds=self._spec.timeout_seconds,
            collect=CollectPolicy(mode="none"),
            labels={"execution_phase": "verification"},
            execution_id=execution_id,
            entity_kind="workstream",
            attempt=ctx.attempt,
            backend_id=self._backend.id,
        )

    async def _pre_spawn_persist(
        self, ctx: VerificationContext, execution_id: str
    ) -> None:
        """CAS-gate + persist a placeholder handle BEFORE the process spawns.

        Every attempt inside a VERIFYING run happens while the workstream
        stays in `WorkstreamStatus.VERIFYING` — so this CAS is a same-status
        self-loop (`expected_status == running_status == "verifying"`),
        reusing `Database.start_execution` (Task 3's durable-handle path)
        rather than inventing a second persistence mechanism. If the CAS
        fails (the workstream left `VERIFYING`, e.g. an operator action
        raced this call), the raised exception propagates to the caller
        BEFORE `backend.run()` is ever reached — a genuine orchestration
        bug, not a verifier-contract violation, so this deliberately does
        not degrade to a protocol-error `HandshakeResult`. The seeded
        `transport_ref` is a placeholder (mirrors orchestrator.py's
        pre-spawn seed for non-local backends); `verify()` overwrites it
        with the backend-minted value via `update_execution_handle_launch`
        once `backend.run()` returns.
        """
        assert self._db is not None
        await self._db.start_execution(
            entity_kind="workstream",
            entity_id=ctx.workstream_id,
            expected_status="verifying",
            running_status="verifying",
            execution_id=execution_id,
            backend_id=self._backend.id,
            transport_ref=f"{self._backend.id}:verify-{execution_id}",
            attempt=ctx.attempt,
            execution_phase="verification",
        )
