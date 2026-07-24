import asyncio

import pytest

from maestro.execution.exec_config import SshTransport
from maestro.execution.ssh_cli import RunResult, SshCli
from maestro.execution.ssh_docker_probe import ssh_docker_run_cmd


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
