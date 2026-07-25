"""Tests for DockerTaskHandle: honest lifecycle + ownership-checked cleanup.

All fakes — no docker daemon required.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from maestro.execution.docker_handle import DockerTaskHandle
from maestro.execution.models import ExecutionHandleRef, ExecutionResult


class _FakeLocal:
    def __init__(self, result: ExecutionResult) -> None:
        self._result = result
        self.terminated = False
        self.killed = False

    @property
    def os_pid(self) -> int:
        return 111

    def poll(self) -> int | None:
        return self._result.exit_code

    async def wait(self) -> ExecutionResult:
        return self._result

    async def terminate(self, grace_seconds: float) -> None:
        self.terminated = True

    async def kill(self) -> None:
        self.killed = True


class _FakeDockerCli:
    def __init__(self, inspect_labels: dict[str, str] | None = None) -> None:
        self._labels = inspect_labels
        self.stopped: list[str] = []
        self.killed: list[str] = []
        self.removed: list[str] = []

    async def inspect(self, name: str) -> dict | None:
        if self._labels is None:
            return None
        return {"Config": {"Labels": self._labels}}

    async def stop(self, name: str, timeout: float) -> None:  # noqa: ASYNC109
        self.stopped.append(name)

    async def kill(self, name: str) -> None:
        self.killed.append(name)

    async def rm(self, name: str) -> None:
        self.removed.append(name)

    def set_absent(self) -> None:
        """Flip a subsequent `inspect()` to report the container gone."""
        self._labels = None


def _handle(
    local,
    docker,
    cleanup_paths=None,
    expected_labels: dict[str, str] | None = None,
) -> DockerTaskHandle:
    ref = ExecutionHandleRef(
        backend_id="docker",
        run_id="t1",
        transport_ref="docker:maestro-e1",
        started_at=datetime.now(UTC),
    )
    return DockerTaskHandle(
        local=local,
        container_name="maestro-e1",
        expected_labels=(
            {"maestro.execution_id": "e1"}
            if expected_labels is None
            else expected_labels
        ),
        cleanup_paths=cleanup_paths or [],
        docker=docker,
        ref=ref,
    )


@pytest.mark.anyio
async def test_wait_timeout_stops_container() -> None:
    local = _FakeLocal(
        ExecutionResult(exit_code=None, output_log_path=Path("/l"), timed_out=True)
    )
    docker = _FakeDockerCli()
    h = _handle(local, docker)
    result = await h.wait()
    assert result.timed_out is True
    assert docker.stopped == ["maestro-e1"]  # container stopped on timeout


@pytest.mark.anyio
async def test_collect_is_noop() -> None:
    h = _handle(
        _FakeLocal(ExecutionResult(exit_code=0, output_log_path=Path("/l"))),
        _FakeDockerCli(),
    )
    res = await h.collect()
    assert res.applied is False


@pytest.mark.anyio
async def test_cleanup_removes_when_label_matches(tmp_path: Path) -> None:
    f = tmp_path / "env"
    f.write_text("X=1")
    docker = _FakeDockerCli(inspect_labels={"maestro.execution_id": "e1"})
    h = _handle(
        _FakeLocal(ExecutionResult(exit_code=0, output_log_path=Path("/l"))),
        docker,
        [f],
    )
    await h.cleanup()
    assert docker.removed == ["maestro-e1"]
    assert not f.exists()  # local secret file unlinked


@pytest.mark.anyio
async def test_cleanup_raises_on_label_mismatch(tmp_path: Path) -> None:
    docker = _FakeDockerCli(inspect_labels={"maestro.execution_id": "OTHER"})
    h = _handle(
        _FakeLocal(ExecutionResult(exit_code=0, output_log_path=Path("/l"))), docker
    )
    with pytest.raises(RuntimeError):
        await h.cleanup()
    assert docker.removed == []  # never removes a foreign container


@pytest.mark.anyio
async def test_cleanup_absent_container_still_unlinks(tmp_path: Path) -> None:
    f = tmp_path / "env"
    f.write_text("X=1")
    docker = _FakeDockerCli(inspect_labels=None)  # inspect -> None (absent)
    h = _handle(
        _FakeLocal(ExecutionResult(exit_code=0, output_log_path=Path("/l"))),
        docker,
        [f],
    )
    await h.cleanup()
    assert docker.removed == []
    assert not f.exists()


@pytest.mark.anyio
async def test_cleanup_raises_when_no_expected_label_but_present() -> None:
    # Handle built without maestro.execution_id in expected_labels (e.g. a
    # caller-assembled plan missing the label). A present, unlabeled
    # container must NOT be treated as owned just because `None == None`
    # would otherwise pass the equality check.
    docker = _FakeDockerCli(inspect_labels={})
    h = _handle(
        _FakeLocal(ExecutionResult(exit_code=0, output_log_path=Path("/l"))),
        docker,
        expected_labels={},
    )
    with pytest.raises(RuntimeError):
        await h.cleanup()
    assert docker.removed == []


@pytest.mark.anyio
async def test_cleanup_refuses_on_full_label_mismatch() -> None:
    # id matches but backend_id differs -> full-set check must refuse.
    expected = {"maestro.execution_id": "e1", "maestro.backend_id": "rs"}
    docker = _FakeDockerCli(
        inspect_labels={"maestro.execution_id": "e1", "maestro.backend_id": "OTHER"}
    )
    h = _handle(
        _FakeLocal(ExecutionResult(exit_code=0, output_log_path=Path("/l"))),
        docker,
        expected_labels=expected,
    )
    with pytest.raises(RuntimeError, match="label mismatch"):
        await h.cleanup()
    assert docker.removed == []


@pytest.mark.anyio
async def test_terminate_targets_this_container_by_name() -> None:
    local = _FakeLocal(ExecutionResult(exit_code=0, output_log_path=Path("/l")))
    docker = _FakeDockerCli()
    h = _handle(local, docker)
    await h.terminate(5.0)
    assert local.terminated is True
    assert docker.stopped == ["maestro-e1"]
    assert docker.killed == ["maestro-e1"]  # stop then kill, both targeted


@pytest.mark.anyio
async def test_kill_targets_this_container_by_name_kill_only() -> None:
    local = _FakeLocal(ExecutionResult(exit_code=0, output_log_path=Path("/l")))
    docker = _FakeDockerCli()
    h = _handle(local, docker)
    await h.kill()
    assert local.killed is True
    assert docker.killed == ["maestro-e1"]
    assert docker.stopped == []  # kill path never calls stop


@pytest.mark.anyio
async def test_cleanup_is_idempotent_present_then_absent(tmp_path: Path) -> None:
    f = tmp_path / "env"
    f.write_text("X=1")
    docker = _FakeDockerCli(inspect_labels={"maestro.execution_id": "e1"})
    h = _handle(
        _FakeLocal(ExecutionResult(exit_code=0, output_log_path=Path("/l"))),
        docker,
        [f],
    )
    await h.cleanup()
    assert docker.removed == ["maestro-e1"]
    assert not f.exists()

    # Second call: the container is already gone and the files are already
    # unlinked. Must not raise, and must not re-remove or re-touch anything.
    docker.set_absent()
    await h.cleanup()
    assert docker.removed == ["maestro-e1"]
    assert not f.exists()
