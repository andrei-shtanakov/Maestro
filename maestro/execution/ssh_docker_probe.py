"""Drive `DockerCli` against a remote daemon over SSH, plus ownership-verified
container lifecycle for the SSH+Docker path (Phase 2c).

`ssh_docker_run_cmd` adapts `SshCli` to `DockerCli.RunCmd`, enforcing the op
timeout locally (SshCli.run has none) and raising TimeoutError on expiry so
DockerCli's probe/inspect paths fail closed on a hung remote daemon.
"""

import asyncio
import contextlib
from collections.abc import Mapping

from maestro.execution.docker_cli import DockerCli, RunCmd
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


def labels_match(
    actual: Mapping[str, str | None], expected: Mapping[str, str | None]
) -> bool:
    """True iff every expected label is present on `actual` with an equal,
    non-None value. Used for full-set ownership verification (Phase 2c)."""
    for key, value in expected.items():
        if value is None or actual.get(key) != value:
            return False
    return True


class ContainerOps:
    """Ownership-verified container lifecycle over an (ssh-backed) DockerCli."""

    def __init__(
        self,
        *,
        docker: DockerCli,
        container_name: str,
        expected_labels: dict[str, str],
    ) -> None:
        """Bind the ops to one container name + its full expected label set."""
        self._docker = docker
        self._name = container_name
        self._expected = expected_labels

    async def _verify(self) -> bool:
        """Inspect by name; True iff present AND full labels match. None → absent."""
        info = await self._docker.inspect(self._name)
        if info is None:
            return False
        labels = (info.get("Config") or {}).get("Labels") or {}
        if not labels_match(labels, self._expected):
            raise RuntimeError(
                f"refusing to act on {self._name}: label mismatch "
                f"(expected {self._expected}, got {labels})"
            )
        return True

    async def stop(self, grace: float) -> None:
        """Best-effort ownership-verified stop→kill (a channel/daemon blip
        must not wedge terminate/kill)."""
        with contextlib.suppress(Exception):
            if await self._verify():
                with contextlib.suppress(Exception):
                    await self._docker.stop(self._name, grace)
                with contextlib.suppress(Exception):
                    await self._docker.kill(self._name)

    async def remove(self) -> None:
        """Ownership-verified `docker rm -f`. Raises on a label mismatch;
        a no-op when the container is already absent."""
        if await self._verify():
            await self._docker.rm(self._name)
