"""R-06b M2 — tests for SpawnerResponder.

Mock-only: no real CLI subprocess. We exercise the adapter's three
shapes (happy path / timeout / non-zero exit) with a fake spawner that
builds an ``ExecutionRequest`` encoding the desired outcome into
``labels``, and a fake ``ExecutionBackend``/``TaskHandle`` pair that
decodes those labels instead of actually running a process — mirroring
the scheduler's ``FakeExecutionBackend`` (``tests/fakes/`` ), but with
``timed_out`` support since this responder's timeout is now handled
entirely inside ``TaskHandle.wait()``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from maestro.benchmark import SpawnerResponder
from maestro.execution.models import (
    CollectPolicy,
    CollectResult,
    ExecutionHandleRef,
    ExecutionRequest,
    ExecutionResult,
)
from maestro.spawners.base import AgentSpawner


if TYPE_CHECKING:
    from pathlib import Path

    from maestro.execution.models import (
        BackendHealth,
        CapabilityResult,
        ProbeResult,
    )
    from maestro.models import Task


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeTaskHandle:
    """TaskHandle double: decodes the desired outcome from request labels.

    ``wait()`` writes ``fake_log_content`` (if any) to the request's log
    path and returns an ``ExecutionResult`` built straight from labels —
    no subprocess, no real timing.
    """

    def __init__(self, req: ExecutionRequest) -> None:
        self._req = req
        self.killed = False
        self.ref = ExecutionHandleRef(
            backend_id="fake",
            run_id=req.run_id,
            transport_ref="fake:1",
            started_at=datetime.now(UTC),
        )

    @property
    def os_pid(self) -> int | None:
        return 1

    def poll(self) -> int | None:
        return None

    async def wait(self) -> ExecutionResult:
        labels = self._req.labels
        timed_out = labels.get("fake_timed_out") == "1"
        log_content = labels.get("fake_log_content", "")
        if log_content:
            self._req.log_path.write_text(log_content)
        exit_code = None if timed_out else int(labels.get("fake_exit_code", "0"))
        if timed_out:
            self.killed = True
        return ExecutionResult(
            exit_code=exit_code,
            output_log_path=self._req.log_path,
            timed_out=timed_out,
        )

    async def terminate(self, grace_seconds: float) -> None:
        del grace_seconds

    async def kill(self) -> None:
        self.killed = True

    async def collect(self) -> CollectResult:
        raise NotImplementedError("not exercised by responder tests")

    async def cleanup(self) -> None:
        pass


class FakeBackend:
    """ExecutionBackend double: builds a FakeTaskHandle from the request."""

    id = "fake"

    def __init__(self) -> None:
        self.created_handles: list[FakeTaskHandle] = []

    async def healthcheck(self) -> BackendHealth:
        raise NotImplementedError("not exercised by responder tests")

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        raise NotImplementedError("not exercised by responder tests")

    async def run(self, req: ExecutionRequest) -> FakeTaskHandle:
        handle = FakeTaskHandle(req)
        self.created_handles.append(handle)
        return handle

    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        raise NotImplementedError("not exercised by responder tests")

    def accepts_ref(self, ref: ExecutionHandleRef) -> bool:
        raise NotImplementedError("not exercised by responder tests")


class FakeSpawner(AgentSpawner):
    """Real subclass of the ABC so we don't need to mock isinstance checks.

    ``build_request`` encodes the intended fake outcome (exit code /
    timeout / log content) into ``ExecutionRequest.labels``, which
    ``FakeTaskHandle`` decodes back at ``wait()`` time.
    """

    def __init__(
        self,
        *,
        agent_type_str: str = "claude_code",
        exit_code: int = 0,
        timed_out: bool = False,
        log_content: str = "",
    ) -> None:
        self._agent_type = agent_type_str
        self._exit_code = exit_code
        self._timed_out = timed_out
        self._log_content = log_content
        self.build_request_calls: list[tuple[str, str]] = []

    @property
    def agent_type(self) -> str:
        return self._agent_type

    def is_available(self) -> bool:
        return True

    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        retry_context: str = "",
        *,
        model: str | None = None,
    ):
        raise NotImplementedError("legacy path unused by the responder")

    def build_request(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        run_id: str,
        retry_context: str = "",
        *,
        model: str | None = None,
    ) -> ExecutionRequest:
        self.build_request_calls.append((task.id, task.prompt))
        return ExecutionRequest(
            run_id=run_id,
            argv=["true"],
            workdir=workdir,
            log_path=log_file,
            collect=CollectPolicy(mode="none"),
            labels={
                "fake_exit_code": str(self._exit_code),
                "fake_timed_out": "1" if self._timed_out else "",
                "fake_log_content": self._log_content,
            },
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_responder_happy_path_parses_tokens_and_cost(tmp_path) -> None:
    """Exit 0 + log with parseable usage → AgentResponse with text=log,
    tokens summed across input+output, cost computed via cost_tracker."""
    log_content = '{"result": "done", "input_tokens": 1500, "output_tokens": 500}'
    spawner = FakeSpawner(log_content=log_content)
    backend = FakeBackend()
    responder = SpawnerResponder(
        spawner=spawner,
        workdir=tmp_path,
        log_dir=tmp_path,
        timeout_seconds=5.0,
        backend=backend,
    )

    response = await responder.respond("write hello world")

    assert response.error is None
    assert response.text == log_content
    assert response.tokens_used == 2000  # 1500 + 500
    assert response.cost_usd is not None
    assert response.cost_usd > 0
    assert spawner.build_request_calls == [("benchmark-1", "write hello world")]
    assert responder.agent_id == "claude_code"


@pytest.mark.anyio
async def test_responder_timeout_kills_process(tmp_path) -> None:
    """Backend reports timed_out → response has empty text + error='timeout'."""
    spawner = FakeSpawner(timed_out=True)
    backend = FakeBackend()
    responder = SpawnerResponder(
        spawner=spawner,
        workdir=tmp_path,
        log_dir=tmp_path,
        timeout_seconds=0.05,
        backend=backend,
    )

    response = await responder.respond("hangs forever")

    assert response.error == "timeout"
    assert response.text == ""
    assert response.tokens_used is None
    assert response.cost_usd is None
    assert backend.created_handles[0].killed is True


@pytest.mark.anyio
async def test_responder_nonzero_exit_reports_error(tmp_path) -> None:
    """Non-zero exit → text empty, error='exit code N'. Tokens/cost are
    still parsed from whatever log was captured before the crash (may be
    None when the agent didn't get far enough to emit usage)."""
    spawner = FakeSpawner(
        exit_code=2,
        log_content="",  # crash before usage was emitted
    )
    backend = FakeBackend()
    responder = SpawnerResponder(
        spawner=spawner,
        workdir=tmp_path,
        log_dir=tmp_path,
        timeout_seconds=5.0,
        backend=backend,
    )

    response = await responder.respond("breaks")

    assert response.error == "exit code 2"
    assert response.text == ""
    assert response.tokens_used is None
    assert response.cost_usd is None


@pytest.mark.anyio
async def test_reported_cost_preferred_over_pricing(tmp_path) -> None:
    """When the parsed usage carries agent-reported cost, the responder
    forwards it instead of the PRICING estimate (0.0 for opencode)."""
    log_content = (
        '{"type": "step_finish", "part": {"cost": 0.02, "tokens": '
        '{"input": 100, "output": 20, "reasoning": 0}}}'
    )
    spawner = FakeSpawner(agent_type_str="opencode", log_content=log_content)
    responder = SpawnerResponder(
        spawner=spawner,
        workdir=tmp_path,
        log_dir=tmp_path,
        timeout_seconds=5.0,
        backend=FakeBackend(),
    )

    response = await responder.respond("write hello world")

    assert response.error is None
    assert response.cost_usd == pytest.approx(0.02)
    assert response.tokens_used == 120


@pytest.mark.anyio
async def test_reported_zero_cost_is_preserved(tmp_path) -> None:
    """A genuinely-reported 0.0 (e.g. a free model) must survive to the
    wire format — it is an observation, not an absence of one."""
    log_content = json.dumps(
        {
            "type": "step_finish",
            "part": {"cost": 0.0, "tokens": {"input": 10, "output": 5}},
        }
    )
    spawner = FakeSpawner(agent_type_str="opencode", log_content=log_content)
    responder = SpawnerResponder(
        spawner=spawner,
        workdir=tmp_path,
        log_dir=tmp_path,
        timeout_seconds=5.0,
        backend=FakeBackend(),
    )

    response = await responder.respond("free run")

    assert response.error is None
    assert response.cost_usd == 0.0  # reported 0.0, NOT collapsed to None


@pytest.mark.anyio
async def test_reported_zero_cost_preserved_on_error_exit(tmp_path) -> None:
    """A reported 0.0 must survive even on a non-zero exit — both return
    sites share the same cost_wire, so the error path cannot re-collapse it."""
    log_content = json.dumps(
        {
            "type": "step_finish",
            "part": {"cost": 0.0, "tokens": {"input": 10, "output": 5}},
        }
    )
    spawner = FakeSpawner(
        agent_type_str="opencode",
        exit_code=2,
        log_content=log_content,
    )
    responder = SpawnerResponder(
        spawner=spawner,
        workdir=tmp_path,
        log_dir=tmp_path,
        timeout_seconds=5.0,
        backend=FakeBackend(),
    )

    response = await responder.respond("free but failed")

    assert response.cost_usd == 0.0  # reported 0.0 preserved on error path
    assert response.error == "exit code 2"  # error is still reported


@pytest.mark.anyio
async def test_estimated_zero_cost_stays_unknown(tmp_path) -> None:
    """No parseable usage → no reported cost, zero tokens → estimate 0.0
    → collapses to None (an estimate of zero is not an observation)."""
    spawner = FakeSpawner(agent_type_str="claude_code", log_content="{}")
    responder = SpawnerResponder(
        spawner=spawner,
        workdir=tmp_path,
        log_dir=tmp_path,
        timeout_seconds=5.0,
        backend=FakeBackend(),
    )

    response = await responder.respond("no usage")

    assert response.cost_usd is None


@pytest.mark.anyio
async def test_responder_unknown_agent_type_short_circuits(tmp_path) -> None:
    """If a wrapped spawner reports an agent_type not in AgentType enum,
    the adapter must surface the error rather than crash mid-spawn."""
    spawner = FakeSpawner(agent_type_str="not_a_real_agent")
    responder = SpawnerResponder(
        spawner=spawner,
        workdir=tmp_path,
        log_dir=tmp_path,
        backend=FakeBackend(),
    )

    response = await responder.respond("doesn't matter")

    assert response.error is not None
    assert "unknown agent_type" in response.error
    assert spawner.build_request_calls == []  # never reached build_request


class _RealArgvSpawner(AgentSpawner):
    """Builds a request with a real (non-labels-encoded) argv, so it can be
    driven by the real ``LocalBackend`` instead of ``FakeBackend`` — needed
    to exercise the genuine ``asyncio.wait_for`` timeout path.
    """

    @property
    def agent_type(self) -> str:
        return "claude_code"

    def build_request(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
        run_id: str,
        retry_context: str = "",
        *,
        model: str | None = None,
    ) -> ExecutionRequest:
        return ExecutionRequest(
            run_id=run_id,
            argv=["sh", "-c", "sleep 0.05; echo done"],
            workdir=workdir,
            log_path=log_file,
            collect=CollectPolicy(mode="none"),
        )


@pytest.mark.anyio
async def test_responder_preserves_fractional_timeout_end_to_end(tmp_path) -> None:
    """A sub-second fractional ``--timeout`` must survive to
    ``ExecutionRequest.timeout_seconds`` without truncating to 0.

    Regression for a bug where the responder did
    ``model_copy(update={"timeout_seconds": int(self._timeout)})``: a
    fractional timeout like 0.3s truncated to ``0``, and
    ``asyncio.wait_for(coro, timeout=0)`` fires (almost) immediately —
    so a task that actually finishes in ~50ms would incorrectly come
    back as ``error="timeout"``. With the fix (no truncation), the task
    finishes well within the 0.3s budget and reports success.
    """
    from maestro.execution.local import LocalBackend

    responder = SpawnerResponder(
        spawner=_RealArgvSpawner(),
        workdir=tmp_path,
        log_dir=tmp_path,
        timeout_seconds=0.3,
        backend=LocalBackend(),
    )

    response = await responder.respond("irrelevant prompt")

    assert response.error is None
    assert "done" in response.text
