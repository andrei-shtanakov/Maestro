"""`ClaudeDiffJudge`: the Mode-1 verifier provider (design §6 steps 3-5, §6.5).

Ownership split (authoritative — controller resolution of the T7/T8 split):
the scheduler (Task 8) owns the envelope preflight, the atomic
`VALIDATING -> VERIFYING` CAS that mints the placeholder execution handle
(`Database.start_execution`), the `VERIFIER_STARTED` emit, and the
PASS/FAIL/ERROR state routing. `ClaudeDiffJudge.verify` owns everything
AFTER the handle exists: run the judge process, record the real launch
coordinates, transport-check the result, parse + provider-bind the sealed
verdict document, and durably finalize the handle. It never transitions
task status and never emits events — it only returns a `TaskHandshakeResult`
for the scheduler to route.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from maestro.domain.verdict import (
    Finding,
    TaskHandshakeResult,
    TaskIdentityExpectations,
    TaskVerdictDocument,
    TaskVerdictIdentity,
    VerdictValue,
    evaluate_task_document,
)
from maestro.execution.finalize import finalize_handle
from maestro.execution.models import CollectPolicy, ExecutionRequest, ExecutionResult
from maestro.verifier.envelope import RawPayloadError, parse_raw_payload
from maestro.verifier.prompt import JUDGE_PROMPT


if TYPE_CHECKING:
    # Deferred for the same reason maestro/domain/verifier.py defers it: a
    # module-level import of Database is not needed at runtime (used only
    # as a type hint here) and keeping it under TYPE_CHECKING avoids
    # widening this module's real import surface.
    from maestro.database import Database
    from maestro.execution.backend import ExecutionBackend


class TaskVerificationContext(BaseModel):
    """Everything one `ClaudeDiffJudge.verify()` call needs (§5, §6 fields).

    Built by the scheduler (Task 8) AFTER the envelope preflight and the
    atomic CAS — `envelope` is the already-built stdin blob (Task 6's
    `build_envelope`) and the identity hashes are already computed (Task
    5's `compute_identity`). This context does NOT build the patch itself.
    """

    model_config = ConfigDict(frozen=True)

    task_id: str
    run_id: str
    attempt: int
    execution_id: str
    # Carried for context/audit only — intentionally not read by `verify`
    # (the diff/scope was already frozen into `envelope` upstream, Task 6).
    worktree: Path
    out_json: Path
    envelope: str
    artifact_sha256: str
    criteria_sha256: str
    profile_sha256: str
    verified_source_commit: str
    verified_scope_sha256: str


@runtime_checkable
class JudgeRunner(Protocol):
    """Runs one Mode-1 verification attempt and returns its outcome."""

    async def verify(self, ctx: TaskVerificationContext) -> TaskHandshakeResult:
        """Execute the judge for `ctx` and evaluate its sealed verdict."""
        ...


def _task_error(message: str) -> TaskHandshakeResult:
    return TaskHandshakeResult(
        outcome=VerdictValue.ERROR, protocol_error=message, document=None
    )


def _artifact_name(task_id: str) -> str:
    return f"task-diff:{task_id}"


class _CliEnvelopeError(ValueError):
    """The `claude -p --output-format json` CLI envelope is malformed.

    Maps to a fail-closed ERROR upstream — never raised past `_evaluate`.
    """


def _extract_result_text(stdout: str) -> str:
    """Unwrap the CLI's `--output-format json` result envelope.

    `claude -p ... --output-format json` does NOT print the model's answer
    directly: it prints a CLI result envelope shaped like
    `{"type": "result", "subtype": "success", "result": "<model text>",
    "usage": {...}}` (the same envelope `maestro.cost_tracker.
    parse_claude_code_log`/`_extract_usage_from_dict` reads `usage`/
    `total_cost_usd` from). The model's own raw verdict payload is the
    STRING held in the envelope's top-level `result` field — `--output-
    format json` is kept (rather than switching format) because a later
    task needs this same envelope's `usage` for judge cost tracking.

    Args:
        stdout: The captured CLI stdout (`ExecutionResult.stdout_tail`).

    Returns:
        The inner `result` string — the model's raw verdict text, still
        unparsed.

    Raises:
        _CliEnvelopeError: `stdout` is not valid JSON, not a JSON object,
            or has no string `result` field.
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise _CliEnvelopeError(f"claude CLI output is not valid JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise _CliEnvelopeError("claude CLI output is not a JSON object")
    result = envelope.get("result")
    if not isinstance(result, str):
        raise _CliEnvelopeError("claude CLI envelope has no string 'result' field")
    return result


class ClaudeDiffJudge:
    """Implements `JudgeRunner` by running the Claude CLI as an adversarial
    diff judge, through the transport-agnostic execution layer.

    Args:
        model: The resolved verifier model (`resolve_verifier_model`, a
            later slice) — never argv-templated by the caller, always the
            already-resolved string.
        backend: Execution backend the judge process runs on.
        timeout_seconds: Wall-clock timeout for the judge process.
        db: Optional database handle. When provided, the already-persisted
            placeholder execution handle (minted by the scheduler's
            `start_execution` CAS, §4/§6 step 2) is driven through the
            durable launch/terminal/collected/cleaned lifecycle.
    """

    def __init__(
        self,
        model: str,
        backend: ExecutionBackend,
        *,
        timeout_seconds: int,
        db: Database | None = None,
    ) -> None:
        self._model = model
        self._backend = backend
        self._timeout_seconds = timeout_seconds
        self._db = db

    async def verify(self, ctx: TaskVerificationContext) -> TaskHandshakeResult:
        """Run one judge attempt for `ctx` (§6 steps 3-5).

        Never raises: any exception anywhere in the run (launch failure,
        transport fault, an unwrapped subprocess error, ...) degrades to a
        fail-closed `TaskHandshakeResult(outcome=ERROR, ...)` — the judge
        must never crash its caller.
        """
        scratch_dir = Path(tempfile.mkdtemp(prefix="maestro-judge-"))
        try:
            return await self._run_and_evaluate(ctx, scratch_dir)
        except Exception as exc:
            return _task_error(f"verifier judge crashed unexpectedly: {exc!r}")
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)

    async def _run_and_evaluate(
        self, ctx: TaskVerificationContext, scratch_dir: Path
    ) -> TaskHandshakeResult:
        request = self._build_request(ctx, scratch_dir)
        try:
            handle = await self._backend.run(request)
        except Exception as exc:
            # Spawn-failure reconciliation (§6.5): the scheduler already
            # persisted the placeholder handle (step 2) before calling us —
            # if the process never actually launched, that placeholder must
            # be reconciled so no orphan OPEN handle lingers.
            if self._db is not None:
                await self._reconcile_failed_spawn(ctx.execution_id)
            return _task_error(f"claude judge failed to launch: {exc!r}")

        if self._db is not None:
            # Best-effort write-back (mirrors scheduler._persist_launch): a
            # failure here must not block finalize_handle from being the
            # sole wait() owner below.
            with contextlib.suppress(Exception):
                await self._db.update_execution_handle_launch(
                    ctx.execution_id,
                    transport_ref=handle.ref.transport_ref,
                    remote_host=None,
                    remote_dir=None,
                    status_marker=None,
                )

        finalization = await finalize_handle(
            handle,
            on_terminal=self._on_terminal(ctx.execution_id),
            on_collected=self._on_collected(ctx.execution_id),
        )
        if self._db is not None and finalization.cleaned:
            await self._db.mark_execution_state(
                ctx.execution_id, "cleaned", allowed_from=["collected"]
            )

        return self._evaluate(ctx, finalization.execution)

    def _on_terminal(self, execution_id: str):
        async def _callback() -> None:
            if self._db is not None:
                await self._db.mark_execution_state(
                    execution_id, "terminal", allowed_from=["prepared", "running"]
                )

        return _callback

    def _on_collected(self, execution_id: str):
        async def _callback() -> None:
            if self._db is not None:
                await self._db.mark_execution_state(
                    execution_id, "collected", allowed_from=["terminal"]
                )

        return _callback

    async def _reconcile_failed_spawn(self, execution_id: str) -> None:
        """Walk the placeholder handle straight to `cleaned` (§6.5).

        `backend.run()` never returned a handle, so there is nothing to
        `wait()`/`collect()`/`cleanup()` — the placeholder is driven through
        the same monotonic states finalize_handle would have produced, so
        recovery never sees it as an open (`prepared`/`running`/`terminal`/
        `collected`) handle.
        """
        assert self._db is not None
        await self._db.mark_execution_state(
            execution_id, "terminal", allowed_from=["prepared", "running"]
        )
        await self._db.mark_execution_state(
            execution_id, "collected", allowed_from=["terminal"]
        )
        await self._db.mark_execution_state(
            execution_id, "cleaned", allowed_from=["collected"]
        )

    def _build_request(
        self, ctx: TaskVerificationContext, scratch_dir: Path
    ) -> ExecutionRequest:
        return ExecutionRequest(
            run_id=f"verify-{ctx.run_id}-{ctx.attempt}",
            argv=[
                "claude",
                "-p",
                JUDGE_PROMPT,
                "--output-format",
                "json",
                "--model",
                self._model,
            ],
            workdir=scratch_dir,
            log_path=scratch_dir / "judge.log",
            stdin=ctx.envelope,
            env={"PATH": os.environ.get("PATH", "")},
            inherit_env=False,
            timeout_seconds=self._timeout_seconds,
            # capture_output=False: LocalBackend writes the process's raw
            # stdout directly to `log_path` in FULL, instead of piping it
            # through `communicate()` and truncating to a fixed tail length
            # (`LocalBackend._TAIL_LIMIT`, 4000 chars) — a verbose
            # multi-finding FAIL envelope routinely exceeds that limit, and
            # parsing the truncated tail drops the front of the JSON
            # envelope. See `_read_full_stdout`.
            capture_output=False,
            collect=CollectPolicy(mode="none"),
            labels={"execution_phase": "verification"},
            execution_id=ctx.execution_id,
            entity_kind="task",
            attempt=ctx.attempt,
            backend_id=self._backend.id,
        )

    @staticmethod
    def _read_full_stdout(execution: ExecutionResult) -> str:
        """The judge's full, untruncated stdout — not the 4000-char tail.

        `_build_request` sets `capture_output=False`, so the backend writes
        the process's raw stdout directly to `execution.output_log_path` in
        full (see `LocalBackend.run`/`LocalTaskHandle.wait`), unlike
        `execution.stdout_tail`, which every backend truncates to a fixed
        tail length. Falls back to `stdout_tail` only if the log file is
        missing/unreadable — this never forges a PASS on an unreadable
        output; the transport check upstream already gates exit_code/
        timeout, and an unparseable fallback still degrades to ERROR below.
        """
        try:
            return execution.output_log_path.read_text(encoding="utf-8")
        except OSError:
            return execution.stdout_tail

    def _evaluate(
        self, ctx: TaskVerificationContext, execution: ExecutionResult
    ) -> TaskHandshakeResult:
        # Transport check FIRST (§3 split): the Claude CLI exits 0 whenever
        # it successfully returns an answer, regardless of PASS/FAIL — so
        # only timeout/non-zero-exit is a transport ERROR; the verdict
        # itself is never derived from the exit code.
        if (
            execution.timed_out
            or execution.exit_code is None
            or execution.exit_code != 0
        ):
            return _task_error(
                "claude judge transport failure: "
                f"exit_code={execution.exit_code} timed_out={execution.timed_out}"
            )

        full_stdout = self._read_full_stdout(execution)

        # Captured as soon as the transport succeeded — even if the envelope
        # or payload later prove invalid, the CLI still ran and may carry a
        # billable `usage`/`total_cost_usd`, which the scheduler wants for
        # cost tracking regardless of the eventual verdict/ERROR outcome.
        raw_envelope = full_stdout

        try:
            result_text = _extract_result_text(full_stdout)
        except _CliEnvelopeError as exc:
            return _task_error(f"claude CLI envelope invalid: {exc}").model_copy(
                update={"raw_result_envelope": raw_envelope}
            )

        try:
            raw = parse_raw_payload(result_text)
        except RawPayloadError as exc:
            return _task_error(f"raw verifier payload invalid: {exc}").model_copy(
                update={"raw_result_envelope": raw_envelope}
            )

        # Provider binding (§6 step 4): identity is authored by Maestro from
        # `ctx`, never from the model's raw payload (which has no room for
        # identity fields at all — see RawVerdict's strict schema).
        identity = TaskVerdictIdentity(
            task_id=ctx.task_id,
            verification_run_id=ctx.run_id,
            verification_attempt=ctx.attempt,
            artifact=_artifact_name(ctx.task_id),
            artifact_sha256=ctx.artifact_sha256,
            criteria_sha256=ctx.criteria_sha256,
            profile_sha256=ctx.profile_sha256,
            verified_source_commit=ctx.verified_source_commit,
            verified_scope_sha256=ctx.verified_scope_sha256,
        )
        document = TaskVerdictDocument(
            schema_version=2,
            identity=identity,
            verdict=VerdictValue.PASS if raw.verdict == "pass" else VerdictValue.FAIL,
            findings=[
                Finding(
                    criterion_id=finding.criterion_id,
                    severity=finding.severity,
                    evidence=finding.evidence,
                    author_feedback=finding.author_feedback,
                )
                for finding in raw.findings
            ],
        )
        ctx.out_json.write_text(document.model_dump_json())

        expected = TaskIdentityExpectations(
            task_id=identity.task_id,
            verification_run_id=identity.verification_run_id,
            verification_attempt=identity.verification_attempt,
            artifact=identity.artifact,
            artifact_sha256=identity.artifact_sha256,
            criteria_sha256=identity.criteria_sha256,
            profile_sha256=identity.profile_sha256,
            verified_source_commit=identity.verified_source_commit,
            verified_scope_sha256=identity.verified_scope_sha256,
        )
        return evaluate_task_document(ctx.out_json, expected).model_copy(
            update={"raw_result_envelope": raw_envelope}
        )
