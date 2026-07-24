import asyncio

import pytest

from maestro.execution.exec_config import SshTransport
from maestro.execution.ssh_cli import RunResult, SshCli
from maestro.execution.ssh_docker_probe import (
    ContainerOps,
    labels_match,
    ssh_docker_run_cmd,
)


def _ssh(runner):
    return SshCli(SshTransport(type="ssh", host="h", workdir_root="/r"), runner=runner)


@pytest.mark.anyio
async def test_run_cmd_tunnels_argv_and_returns_tuple():
    seen = {}

    async def runner(argv, stdin):
        seen["argv"] = argv
        return RunResult(0, "out", "err")

    run_cmd = ssh_docker_run_cmd(_ssh(runner))
    rc, out, err = await run_cmd(["docker", "version"], 30.0)
    assert (rc, out, err) == (0, "out", "err")
    # argv is shlex-joined into the ssh command tail
    assert seen["argv"][0] == "ssh"
    assert "docker version" in seen["argv"][-1]


@pytest.mark.anyio
async def test_run_cmd_timeout_raises():
    async def slow_runner(argv, stdin):
        await asyncio.sleep(1.0)
        return RunResult(0, "", "")

    run_cmd = ssh_docker_run_cmd(_ssh(slow_runner))
    with pytest.raises(TimeoutError):
        await run_cmd(["docker", "inspect", "x"], 0.01)


class _FakeDocker:
    def __init__(self, labels):
        self._labels = labels
        self.removed = []
        self.stopped = []

    async def inspect(self, name):
        return {"Config": {"Labels": self._labels}} if self._labels else None

    async def stop(self, name, timeout):  # noqa: ASYNC109
        self.stopped.append(name)

    async def kill(self, name):
        self.stopped.append(("kill", name))

    async def rm(self, name):
        self.removed.append(name)


def _ops(docker, container_name, expected_labels) -> ContainerOps:
    """Untyped-param wrapper: `docker` is a duck-typed fake, not a real
    `DockerCli` instance — matches the fake-injection pattern already used
    for `DockerTaskHandle` in `tests/test_docker_handle.py`."""
    return ContainerOps(
        docker=docker, container_name=container_name, expected_labels=expected_labels
    )


def test_labels_match_full_set():
    assert labels_match({"a": "1", "b": "2"}, {"a": "1", "b": "2"})
    assert not labels_match({"a": "1"}, {"a": "1", "b": "2"})  # missing key
    assert not labels_match({"a": "9"}, {"a": "1"})  # value mismatch
    assert not labels_match({"a": None}, {"a": None})  # None never matches


@pytest.mark.anyio
async def test_container_ops_remove_ownership_verified():
    exp = {"maestro.execution_id": "e1", "maestro.backend_id": "rs"}
    dk = _FakeDocker(exp)
    ops = _ops(dk, "maestro-e1", exp)
    await ops.remove()
    assert dk.removed == ["maestro-e1"]


@pytest.mark.anyio
async def test_container_ops_remove_refuses_on_mismatch():
    exp = {"maestro.execution_id": "e1", "maestro.backend_id": "rs"}
    dk = _FakeDocker({"maestro.execution_id": "OTHER"})
    ops = _ops(dk, "maestro-e1", exp)
    with pytest.raises(RuntimeError, match="label mismatch"):
        await ops.remove()
    assert dk.removed == []
