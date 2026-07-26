"""Task 7: ClaudeDiffJudge — transport/semantic split, provider binding, and
durable finalize (design §6 steps 3-5, §6.5).

The scheduler (Task 8) owns the envelope preflight and the atomic
`VALIDATING -> VERIFYING` CAS that mints the placeholder execution handle;
these tests simulate that by calling `Database.start_execution` directly
before invoking `ClaudeDiffJudge.verify`, exactly as the scheduler would.
"""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from maestro.database import Database
from maestro.domain.verdict import VerdictValue
from maestro.execution.models import (
    BackendHealth,
    CapabilityResult,
    CollectResult,
    ExecutionHandleRef,
    ExecutionRequest,
    ExecutionResult,
    ProbeResult,
)
from maestro.models import Task, TaskStatus
from maestro.verifier.judge import ClaudeDiffJudge, TaskVerificationContext


pytestmark = pytest.mark.anyio


ARTIFACT_SHA = "a" * 64
CRITERIA_SHA = "c" * 64
PROFILE_SHA = "p" * 64
SOURCE_COMMIT = "s" * 40
SCOPE_SHA = ARTIFACT_SHA  # both hash the same envelope in this slice (by design)


class FakeTaskHandle:
    """TaskHandle double with call-tracking for wait/collect/cleanup."""

    def __init__(
        self,
        *,
        stdout_tail: str = "",
        exit_code: int | None = 0,
        timed_out: bool = False,
        transport_ref: str = "fake:handle-1",
        output_log_path: Path = Path("/tmp/fake-judge.log"),
    ) -> None:
        self._stdout_tail = stdout_tail
        self._exit_code = exit_code
        self._timed_out = timed_out
        self._output_log_path = output_log_path
        self.wait_calls = 0
        self.collect_calls = 0
        self.cleanup_calls = 0
        self.ref = ExecutionHandleRef(
            backend_id="fake",
            run_id="verify-run-1",
            transport_ref=transport_ref,
            started_at=datetime.now(UTC),
        )

    @property
    def os_pid(self) -> int | None:
        return None

    def poll(self) -> int | None:
        return self._exit_code

    async def wait(self) -> ExecutionResult:
        self.wait_calls += 1
        return ExecutionResult(
            exit_code=self._exit_code,
            stdout_tail=self._stdout_tail,
            output_log_path=self._output_log_path,
            timed_out=self._timed_out,
        )

    async def terminate(self, grace_seconds: float) -> None:
        del grace_seconds

    async def kill(self) -> None:
        pass

    async def collect(self) -> CollectResult:
        self.collect_calls += 1
        return CollectResult(applied=False, detail="fake: no collect needed")

    async def cleanup(self) -> None:
        self.cleanup_calls += 1


class FakeJudgeBackend:
    """ExecutionBackend double: returns a scripted FakeTaskHandle, or raises."""

    id = "fake"

    def __init__(
        self,
        handle: FakeTaskHandle | None = None,
        *,
        raise_on_run: Exception | None = None,
    ) -> None:
        self._handle = handle
        self._raise_on_run = raise_on_run
        self.run_calls = 0
        self.last_request: ExecutionRequest | None = None

    async def healthcheck(self) -> BackendHealth:
        return BackendHealth(reachable=True)

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        del req
        return CapabilityResult(ok=True)

    async def run(self, req: ExecutionRequest) -> FakeTaskHandle:
        self.run_calls += 1
        self.last_request = req
        if self._raise_on_run is not None:
            raise self._raise_on_run
        assert self._handle is not None
        return self._handle

    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        raise NotImplementedError("not exercised by these tests")

    def accepts_ref(self, ref: ExecutionHandleRef) -> bool:
        raise NotImplementedError("not exercised by these tests")


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[Database, None]:
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    d = Database(db_dir / "m.db")
    await d.connect()
    await d.initialize_schema()
    try:
        yield d
    finally:
        await d.close()


def _ctx(tmp_path: Path, *, task_id: str = "task-1") -> TaskVerificationContext:
    return TaskVerificationContext(
        task_id=task_id,
        run_id="run-1",
        attempt=1,
        execution_id="exec-1",
        worktree=tmp_path / "work",
        out_json=tmp_path / "verdict.json",
        envelope='{"task":{}}',
        artifact_sha256=ARTIFACT_SHA,
        criteria_sha256=CRITERIA_SHA,
        profile_sha256=PROFILE_SHA,
        verified_source_commit=SOURCE_COMMIT,
        verified_scope_sha256=SCOPE_SHA,
    )


async def _seed_placeholder_handle(
    db: Database, *, task_id: str, execution_id: str
) -> None:
    """Simulate the scheduler's (Task 8) atomic VALIDATING->VERIFYING CAS +
    placeholder handle mint, which happens BEFORE ClaudeDiffJudge.verify()
    is ever called.
    """
    await db.create_task(
        Task(
            id=task_id,
            title="t",
            prompt="p",
            workdir="/tmp/does-not-matter",
            status=TaskStatus.VERIFYING,
        )
    )
    await db.start_execution(
        entity_kind="task",
        entity_id=task_id,
        expected_status="verifying",
        running_status="verifying",
        execution_id=execution_id,
        backend_id="fake",
        transport_ref=f"fake:verify-{execution_id}",
        attempt=1,
        execution_phase="verification",
    )


async def _handle_state(db: Database, execution_id: str) -> str:
    assert db._connection is not None
    cursor = await db._connection.execute(
        "SELECT state FROM execution_handles WHERE execution_id = ?",
        (execution_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    return row["state"]


def _cli_envelope(result_text: str) -> str:
    """A realistic `claude -p --output-format json` CLI result envelope.

    The CLI does NOT print the model's answer directly — it wraps it in an
    envelope shaped like this (top-level `type`/`subtype`/`result`/`usage`),
    with the model's own text answer as the `result` STRING (see
    `maestro/cost_tracker.py`'s `_extract_usage_from_dict`, which reads this
    exact shape for token/cost parsing).
    """
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "result": result_text,
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "total_cost_usd": 0.001,
        }
    )


def _raw_pass_payload() -> str:
    """The model's own raw verdict text (unwrapped, `{verdict, findings}`)."""
    return json.dumps({"verdict": "pass", "findings": []})


def _raw_fail_payload() -> str:
    return json.dumps(
        {
            "verdict": "fail",
            "findings": [
                {
                    "criterion_id": "stub_implementation",
                    "severity": "major",
                    "evidence": "raise NotImplementedError",
                    "author_feedback": "implement the real logic",
                }
            ],
        }
    )


def _pass_payload() -> str:
    """Realistic CLI stdout for a `pass` verdict — envelope-wrapped."""
    return _cli_envelope(_raw_pass_payload())


def _fail_payload() -> str:
    """Realistic CLI stdout for a `fail` verdict — envelope-wrapped."""
    return _cli_envelope(_raw_fail_payload())


def _large_raw_fail_payload(num_findings: int = 30) -> str:
    """A verbose multi-finding FAIL payload — the "several fake-done
    problems" case the verifier gate exists to catch. Large enough that
    its CLI envelope exceeds `LocalBackend._TAIL_LIMIT` (4000 chars)."""
    findings = [
        {
            "criterion_id": f"criterion_{i}",
            "severity": "major",
            "evidence": "x" * 200,
            "author_feedback": f"finding {i}: " + "needs a real fix " * 15,
        }
        for i in range(num_findings)
    ]
    return json.dumps({"verdict": "fail", "findings": findings})


def _large_fail_payload(num_findings: int = 30) -> str:
    """Realistic CLI stdout for a verbose multi-finding `fail` verdict —
    envelope-wrapped, deliberately > 4000 chars overall."""
    return _cli_envelope(_large_raw_fail_payload(num_findings))


async def test_valid_pass_payload_returns_pass_with_provider_bound_identity(
    tmp_path: Path,
) -> None:
    handle = FakeTaskHandle(stdout_tail=_pass_payload())
    backend = FakeJudgeBackend(handle)
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30)
    ctx = _ctx(tmp_path)

    result = await judge.verify(ctx)

    assert result.outcome is VerdictValue.PASS
    assert result.protocol_error is None
    assert result.document is not None
    identity = result.document.identity
    assert identity.task_id == ctx.task_id
    assert identity.verification_run_id == ctx.run_id
    assert identity.verification_attempt == ctx.attempt
    assert identity.artifact == f"task-diff:{ctx.task_id}"
    assert identity.artifact_sha256 == ctx.artifact_sha256
    assert identity.criteria_sha256 == ctx.criteria_sha256
    assert identity.profile_sha256 == ctx.profile_sha256
    assert identity.verified_source_commit == ctx.verified_source_commit
    assert identity.verified_scope_sha256 == ctx.verified_scope_sha256
    assert ctx.out_json.is_file()


async def test_pass_payload_surfaces_raw_envelope_for_cost_tracking(
    tmp_path: Path,
) -> None:
    """Task 10: the CLI result envelope (carrying `usage`/`total_cost_usd`)
    is surfaced on the result so the scheduler can write a verifier cost
    row, without changing the PASS/document behavior tested above."""
    handle = FakeTaskHandle(stdout_tail=_pass_payload())
    backend = FakeJudgeBackend(handle)
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30)
    ctx = _ctx(tmp_path)

    result = await judge.verify(ctx)

    assert result.raw_result_envelope == _pass_payload()


async def test_valid_fail_payload_returns_fail_not_error(tmp_path: Path) -> None:
    handle = FakeTaskHandle(stdout_tail=_fail_payload())
    backend = FakeJudgeBackend(handle)
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30)
    ctx = _ctx(tmp_path)

    result = await judge.verify(ctx)

    assert result.outcome is VerdictValue.FAIL
    assert result.document is not None
    assert len(result.document.findings) == 1
    assert result.document.findings[0].criterion_id == "stub_implementation"


async def test_large_fail_envelope_parses_from_full_log_not_truncated_tail(
    tmp_path: Path,
) -> None:
    """A verbose multi-finding FAIL envelope that exceeds the backend's
    4000-char `stdout_tail` truncation limit must still parse as FAIL, not
    degrade to ERROR.

    `LocalBackend._TAIL_LIMIT` truncates `stdout_tail` (and, historically,
    even its own log-file mirror) to the *last* 4000 chars — which drops
    the FRONT of the CLI's `--output-format json` envelope
    (`{"type":"result","result":"...`). Parsing that truncated prefix-loss
    is not valid JSON, so the pre-fix judge always turned a big FAIL into
    an ERROR, diverting exactly the case the verifier gate exists to catch
    ("judge found several fake-done problems") into manual review instead
    of a retry.

    This simulates the real fix (`capture_output=False` on the judge's own
    `ExecutionRequest`): the backend's `stdout_tail` is the truncated tail
    (as a real backend would still report it), but `output_log_path` is a
    REAL file holding the full, untruncated stdout — the judge must read
    and parse THAT.
    """
    full_payload = _large_fail_payload()
    assert len(full_payload) > 4000  # the scenario only matters if it's big enough

    log_path = tmp_path / "judge-full-stdout.log"
    log_path.write_text(full_payload, encoding="utf-8")

    handle = FakeTaskHandle(
        stdout_tail=full_payload[-4000:],
        output_log_path=log_path,
    )
    backend = FakeJudgeBackend(handle)
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30)
    ctx = _ctx(tmp_path)

    result = await judge.verify(ctx)

    assert result.outcome is VerdictValue.FAIL
    assert result.protocol_error is None
    assert result.document is not None
    assert len(result.document.findings) == 30


async def test_model_never_supplies_identity_provider_binds_it(tmp_path: Path) -> None:
    """The raw payload schema is strictly `{verdict, findings}` — it has no
    room for identity fields at all. Confirm the sealed document's identity
    is entirely provider-computed from `ctx`, never influenced by the model.
    """
    handle = FakeTaskHandle(stdout_tail=_pass_payload())
    backend = FakeJudgeBackend(handle)
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30)
    ctx = _ctx(tmp_path, task_id="task-xyz")

    result = await judge.verify(ctx)

    assert result.document is not None
    raw_payload = json.loads(_raw_pass_payload())
    assert "task_id" not in raw_payload
    assert "artifact_sha256" not in raw_payload
    assert result.document.identity.task_id == "task-xyz"


async def test_cli_exit_nonzero_is_error(tmp_path: Path) -> None:
    handle = FakeTaskHandle(stdout_tail=_pass_payload(), exit_code=1)
    backend = FakeJudgeBackend(handle)
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30)
    ctx = _ctx(tmp_path)

    result = await judge.verify(ctx)

    assert result.outcome is VerdictValue.ERROR
    assert result.protocol_error is not None
    assert not ctx.out_json.exists()


async def test_timeout_is_error(tmp_path: Path) -> None:
    handle = FakeTaskHandle(stdout_tail="", exit_code=None, timed_out=True)
    backend = FakeJudgeBackend(handle)
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30)
    ctx = _ctx(tmp_path)

    result = await judge.verify(ctx)

    assert result.outcome is VerdictValue.ERROR
    assert result.protocol_error is not None
    assert result.raw_result_envelope is None


async def test_nonzero_exit_transport_failure_has_no_raw_envelope(
    tmp_path: Path,
) -> None:
    """A transport failure's stdout is untrusted and not surfaced — cost
    stays UNKNOWN rather than being parsed from an unreliable capture."""
    handle = FakeTaskHandle(stdout_tail=_pass_payload(), exit_code=1)
    backend = FakeJudgeBackend(handle)
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30)
    ctx = _ctx(tmp_path)

    result = await judge.verify(ctx)

    assert result.outcome is VerdictValue.ERROR
    assert result.raw_result_envelope is None


async def test_malformed_cli_envelope_is_error(tmp_path: Path) -> None:
    """The whole CLI stdout is not even valid JSON (envelope-level failure,
    before the inner raw-payload parse is ever reached).
    """
    handle = FakeTaskHandle(stdout_tail="not json at all")
    backend = FakeJudgeBackend(handle)
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30)
    ctx = _ctx(tmp_path)

    result = await judge.verify(ctx)

    assert result.outcome is VerdictValue.ERROR
    assert result.protocol_error is not None
    # Transport succeeded (exit 0) even though the envelope itself is junk —
    # the raw stdout is still surfaced (parse_claude_code_log degrades to
    # zero usage on invalid JSON; it never crashes on it).
    assert result.raw_result_envelope == "not json at all"


async def test_envelope_missing_result_field_is_error(tmp_path: Path) -> None:
    """Valid JSON envelope, but no `result` field at all (e.g. an error
    envelope) — fail-closed, never crash.
    """
    envelope = json.dumps({"type": "result", "subtype": "error_max_turns"})
    handle = FakeTaskHandle(stdout_tail=envelope)
    backend = FakeJudgeBackend(handle)
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30)
    ctx = _ctx(tmp_path)

    result = await judge.verify(ctx)

    assert result.outcome is VerdictValue.ERROR
    assert result.protocol_error is not None
    assert not ctx.out_json.exists()


async def test_envelope_result_field_not_a_string_is_error(tmp_path: Path) -> None:
    """`result` present but not a string (malformed/unexpected CLI shape)."""
    envelope = json.dumps({"type": "result", "result": {"verdict": "pass"}})
    handle = FakeTaskHandle(stdout_tail=envelope)
    backend = FakeJudgeBackend(handle)
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30)
    ctx = _ctx(tmp_path)

    result = await judge.verify(ctx)

    assert result.outcome is VerdictValue.ERROR
    assert result.protocol_error is not None


async def test_extra_key_raw_payload_is_error(tmp_path: Path) -> None:
    """The envelope unwraps fine; the INNER raw payload is what's strict."""
    inner = json.dumps({"verdict": "pass", "findings": [], "extra": "nope"})
    handle = FakeTaskHandle(stdout_tail=_cli_envelope(inner))
    backend = FakeJudgeBackend(handle)
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30)
    ctx = _ctx(tmp_path)

    result = await judge.verify(ctx)

    assert result.outcome is VerdictValue.ERROR
    assert result.protocol_error is not None


async def test_cli_envelope_is_unwrapped_before_parsing_pass(tmp_path: Path) -> None:
    """Regression: `claude -p --output-format json` wraps the model's
    answer in a CLI result envelope (`{"type": "result", "result": "<model
    text>", "usage": {...}}`) — the provider must unwrap `result` before
    calling `parse_raw_payload`. Pre-fix code fed the whole envelope
    straight in, which fails `RawVerdict`'s strict schema (unexpected
    `type`/`subtype`/`usage` keys, missing `verdict`) and always returned
    ERROR — this test fails on that code.
    """
    handle = FakeTaskHandle(stdout_tail=_cli_envelope(_raw_pass_payload()))
    backend = FakeJudgeBackend(handle)
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30)
    ctx = _ctx(tmp_path)

    result = await judge.verify(ctx)

    assert result.outcome is VerdictValue.PASS
    assert result.protocol_error is None
    assert result.document is not None


async def test_cli_envelope_is_unwrapped_before_parsing_fail(tmp_path: Path) -> None:
    """Same regression as above, for a `fail` verdict inside the envelope."""
    handle = FakeTaskHandle(stdout_tail=_cli_envelope(_raw_fail_payload()))
    backend = FakeJudgeBackend(handle)
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30)
    ctx = _ctx(tmp_path)

    result = await judge.verify(ctx)

    assert result.outcome is VerdictValue.FAIL
    assert result.protocol_error is None
    assert result.document is not None
    assert len(result.document.findings) == 1


async def test_finalize_drives_handle_through_full_lifecycle(
    tmp_path: Path, db: Database
) -> None:
    """`finalize_handle` must be the sole `wait()` owner and must drive the
    handle terminal -> collected -> cleaned (design §6.5).
    """
    execution_id = "exec-lifecycle"
    await _seed_placeholder_handle(db, task_id="task-lc", execution_id=execution_id)
    handle = FakeTaskHandle(stdout_tail=_pass_payload())
    backend = FakeJudgeBackend(handle)
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30, db=db)
    ctx = _ctx(tmp_path, task_id="task-lc").model_copy(
        update={"execution_id": execution_id}
    )

    result = await judge.verify(ctx)

    assert result.outcome is VerdictValue.PASS
    assert handle.wait_calls == 1
    assert handle.collect_calls == 1
    assert handle.cleanup_calls == 1
    assert await _handle_state(db, execution_id) == "cleaned"


async def test_backend_run_raises_reconciles_placeholder_no_orphan(
    tmp_path: Path, db: Database
) -> None:
    """A `backend.run()` that raises AFTER the scheduler persisted the
    placeholder handle must not leave an orphan open handle (design §6.5).
    """
    execution_id = "exec-spawn-fail"
    await _seed_placeholder_handle(
        db, task_id="task-spawn-fail", execution_id=execution_id
    )
    backend = FakeJudgeBackend(raise_on_run=RuntimeError("boom: launch failed"))
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30, db=db)
    ctx = _ctx(tmp_path, task_id="task-spawn-fail").model_copy(
        update={"execution_id": execution_id}
    )

    result = await judge.verify(ctx)

    assert result.outcome is VerdictValue.ERROR
    assert result.protocol_error is not None
    state = await _handle_state(db, execution_id)
    assert state not in ("prepared", "running", "terminal", "collected")
    assert state == "cleaned"


async def test_never_crashes_on_unexpected_exception(tmp_path: Path) -> None:
    """A genuine exception anywhere in the run (e.g. an unwrapped
    CalledProcessError-like failure from the backend) must degrade to
    ERROR, never propagate to the caller.
    """
    backend = FakeJudgeBackend(raise_on_run=OSError("no such file or directory"))
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30)
    ctx = _ctx(tmp_path)

    result = await judge.verify(ctx)

    assert result.outcome is VerdictValue.ERROR
    assert result.protocol_error is not None


async def test_judge_request_shape_uses_stdin_and_scratch_cwd_outside_worktree(
    tmp_path: Path,
) -> None:
    """Argv/stdin/collect/workdir shape per design §6 step 3 and §7 (the
    judge must not receive the project worktree as its cwd).
    """
    handle = FakeTaskHandle(stdout_tail=_pass_payload())
    backend = FakeJudgeBackend(handle)
    judge = ClaudeDiffJudge("claude-haiku-4-5", backend, timeout_seconds=30)
    ctx = _ctx(tmp_path)
    ctx.worktree.mkdir(parents=True, exist_ok=True)

    await judge.verify(ctx)

    request = backend.last_request
    assert request is not None
    assert request.argv[:3] == ["claude", "-p", request.argv[2]]
    assert "--output-format" in request.argv
    assert "json" in request.argv
    assert "--model" in request.argv
    assert request.argv[-1] == "claude-haiku-4-5"
    assert request.stdin == ctx.envelope
    # capture_output=False: the backend writes the process's raw stdout
    # directly to `request.log_path` in FULL instead of piping it through
    # `communicate()` and truncating to a fixed tail length. See
    # `_read_full_stdout` / the large-FAIL-envelope test below.
    assert request.capture_output is False
    assert request.collect.mode == "none"
    assert request.workdir != ctx.worktree
    assert not str(request.workdir).startswith(str(ctx.worktree))
    assert request.labels.get("execution_phase") == "verification"
    assert request.execution_id == ctx.execution_id
    assert request.entity_kind == "task"
    assert request.backend_id == backend.id
