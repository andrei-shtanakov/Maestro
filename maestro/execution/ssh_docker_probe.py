"""Drive `DockerCli` against a remote daemon over SSH, plus ownership-verified
container lifecycle for the SSH+Docker path (Phase 2c).

`ssh_docker_run_cmd` adapts `SshCli` to `DockerCli.RunCmd`, enforcing the op
timeout locally (SshCli.run has none) and raising TimeoutError on expiry so
DockerCli's probe/inspect paths fail closed on a hung remote daemon.
"""

import asyncio

from maestro.execution.docker_cli import RunCmd
from maestro.execution.ssh_cli import SshCli


def ssh_docker_run_cmd(ssh: SshCli) -> RunCmd:
    """Adapt an `SshCli` into a `DockerCli` run_cmd that tunnels over SSH."""

    async def run_cmd(
        argv: list[str],
        timeout: float | None,  # noqa: ASYNC109
    ) -> tuple[int, str, str]:
        async with asyncio.timeout(timeout):
            res = await ssh.run(argv)
        return res.returncode, res.stdout, res.stderr

    return run_cmd
