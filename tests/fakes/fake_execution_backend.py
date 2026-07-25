"""In-memory ExecutionBackend/TaskHandle doubles for scheduler tests.

Task 5 wires the scheduler onto `LocalBackend`/`TaskHandle` instead of raw
`subprocess.Popen`. These fakes let scheduler tests keep faking process
lifecycles (never spawning a real subprocess) while satisfying the new
`ExecutionBackend`/`TaskHandle` protocols.

Behavior is carried entirely inside `ExecutionRequest.labels` (all-string
fields) so a `FakeExecutionBackend` never needs to be the *same* object a
spawner double was constructed with — any fake spawner that encodes its
intended return code/delay/pid into `labels` works with any
`FakeExecutionBackend` instance, including ones a test never touches
directly (e.g. one a `Scheduler` builds for itself via a patched
`LocalBackend`).
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from maestro.execution.models import (
    BackendHealth,
    CapabilityResult,
    CollectResult,
    ExecutionHandleRef,
    ExecutionRequest,
    ExecutionResult,
    ProbeResult,
)


class FakeTaskHandle:
    """TaskHandle double mimicking a mocked Popen's lifecycle.

    `poll()` mirrors the old MockSpawner behavior: with no delay it reports
    the exit code immediately; with a delay it reports `None` (running) for
    a number of calls proportional to `delay_seconds` before finishing, so
    tests that manually pump `_monitor_running_tasks()` multiple times can
    still observe a "still running" tick before completion.
    """

    def __init__(
        self,
        exit_code: int = 0,
        delay_seconds: float = 0.0,
        pid: int = 1,
    ) -> None:
        self._exit_code = exit_code
        self._pid = pid
        self._poll_calls = 0
        self._max_poll_calls = int(delay_seconds / 0.1) + 1 if delay_seconds > 0 else 0
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.ref = ExecutionHandleRef(
            backend_id="fake",
            run_id=f"fake-{pid}",
            transport_ref=f"local_pid:{pid}",
            started_at=datetime.now(UTC),
        )

    @property
    def os_pid(self) -> int | None:
        return self._pid

    @property
    def terminate_called(self) -> bool:
        return self.terminate_calls > 0

    @property
    def kill_called(self) -> bool:
        return self.kill_calls > 0

    def poll(self) -> int | None:
        if self._max_poll_calls:
            self._poll_calls += 1
            if self._poll_calls < self._max_poll_calls:
                return None
        return self._exit_code

    async def wait(self) -> ExecutionResult:
        self.wait_calls += 1
        return ExecutionResult(
            exit_code=self._exit_code, output_log_path=Path("/tmp/fake-handle.log")
        )

    async def terminate(self, grace_seconds: float) -> None:
        del grace_seconds
        self.terminate_calls += 1

    async def kill(self) -> None:
        self.kill_calls += 1

    async def collect(self) -> CollectResult:
        return CollectResult(applied=False, detail="fake: no collect needed")

    async def cleanup(self) -> None:
        pass


class FakeExecutionBackend:
    """ExecutionBackend double: builds a FakeTaskHandle from request labels.

    A spawner double encodes the intended fake process behavior into
    `ExecutionRequest.labels` (see `fake_return_code`/`fake_delay_seconds`/
    `fake_pid`); `run()` decodes it back into a `FakeTaskHandle`. `can_run`
    reuses the exact `shutil.which` check `LocalBackend` uses, so a
    spawner double that sets a bogus `required_tools` entry reproduces the
    old `is_available() == False` behavior without any backend-specific
    test wiring.
    """

    id = "fake"

    def __init__(
        self,
        isolator: object | None = None,
        *,
        backend_id: str = "local",
        docker: object | None = None,
    ) -> None:
        # Accepts (and ignores) the same construction kwargs the registry
        # resolver now passes to `LocalBackend` (see `_build_local` in
        # `maestro/execution/resolver.py`), so monkeypatching this class in
        # for `LocalBackend` tolerates the resolver's call signature.
        del isolator, backend_id, docker
        # Kept alive for the scheduler's whole lifetime (unlike
        # `_running_tasks`, which is cleared on completion/cleanup), so
        # tests can still inspect terminate/kill calls after a run finishes.
        self.created_handles: list[FakeTaskHandle] = []

    async def healthcheck(self) -> BackendHealth:
        return BackendHealth(reachable=True)

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        missing = [t for t in req.required_tools if shutil.which(t) is None]
        return CapabilityResult(ok=not missing, missing_tools=missing)

    async def run(self, req: ExecutionRequest) -> FakeTaskHandle:
        exit_code = int(req.labels.get("fake_return_code", "0"))
        delay_seconds = float(req.labels.get("fake_delay_seconds", "0"))
        pid = int(req.labels.get("fake_pid", "1"))
        handle = FakeTaskHandle(
            exit_code=exit_code, delay_seconds=delay_seconds, pid=pid
        )
        self.created_handles.append(handle)
        return handle

    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        raise NotImplementedError("not exercised by scheduler tests")

    def accepts_ref(self, ref: ExecutionHandleRef) -> bool:
        raise NotImplementedError("not exercised by scheduler tests")
