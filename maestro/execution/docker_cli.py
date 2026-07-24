"""Async wrapper over the `docker` CLI. All ops shell out via an injected
run_cmd so unit tests need no daemon. inspect reads `--format '{{json .}}'`
only — never scraped human text.
"""

import asyncio
import contextlib
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any


RunCmd = Callable[[list[str], "float | None"], Awaitable[tuple[int, str, str]]]


def _default_run_cmd() -> RunCmd:
    async def run_cmd(
        argv: list[str],
        timeout: float | None,  # noqa: ASYNC109
    ) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        try:
            async with asyncio.timeout(timeout):
                out, err = await proc.communicate()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise
        return (
            proc.returncode or 0,
            out.decode(errors="replace"),
            err.decode(errors="replace"),
        )

    return run_cmd


class DockerCli:
    """Minimal docker operations Maestro needs."""

    def __init__(
        self,
        binary: str = "docker",
        run_cmd: RunCmd | None = None,
        op_timeout: float = 30.0,
    ) -> None:
        self._binary = binary
        self._run = run_cmd or _default_run_cmd()
        self._op_timeout = op_timeout

    async def version_ok(self) -> bool:
        """Check if docker version command succeeds."""
        rc, _, _ = await self._run([self._binary, "version"], self._op_timeout)
        return rc == 0

    async def image_exists(self, image: str) -> bool:
        """Check if an image exists."""
        rc, _, _ = await self._run(
            [self._binary, "image", "inspect", image], self._op_timeout
        )
        return rc == 0

    async def inspect(self, name: str) -> dict[str, Any] | None:
        """Inspect a container by name.

        Returns None only when docker itself reports the object as absent
        (stderr contains "no such", case-insensitively — covers "No such
        object/container/image"). Any other non-zero return code (daemon
        unreachable, permission error, etc.) is a genuine failure, not
        "absent" — it raises RuntimeError so callers never conflate a
        connectivity/daemon fault with "container doesn't exist". A JSON
        decode error on an rc=0 response is treated as absent/unknown
        (None) rather than raised, since docker itself reported success.
        """
        rc, out, err = await self._run(
            [self._binary, "inspect", "--format", "{{json .}}", name],
            self._op_timeout,
        )
        if rc != 0:
            if "no such" in err.lower():
                return None
            raise RuntimeError(f"docker inspect {name} failed: {err or out}")
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None

    async def ps_ids_by_label(self, key: str, value: str) -> list[str]:
        """Get container IDs by label. Returns list of container IDs.
        Uses -a to include running, exited, dead, paused, restarting.
        """
        # -a: include running, exited, dead, paused, restarting.
        rc, out, err = await self._run(
            [self._binary, "ps", "-a", "-q", "--filter", f"label={key}={value}"],
            self._op_timeout,
        )
        if rc != 0:
            raise RuntimeError(f"docker ps failed: {err.strip()}")
        return [line for line in out.splitlines() if line.strip()]

    async def stop(self, name: str, timeout: float) -> None:  # noqa: ASYNC109
        """Stop a container by name with a timeout. Raises on non-zero rc."""
        secs = max(1, int(timeout))
        rc, out, err = await self._run(
            [self._binary, "stop", "-t", str(secs), name], secs + 10.0
        )
        if rc != 0:
            raise RuntimeError(
                f"docker stop {name} failed: {err.strip() or out.strip()}"
            )

    async def kill(self, name: str) -> None:
        """Kill a container by name. Raises on non-zero rc."""
        rc, out, err = await self._run([self._binary, "kill", name], self._op_timeout)
        if rc != 0:
            raise RuntimeError(
                f"docker kill {name} failed: {err.strip() or out.strip()}"
            )

    async def rm(self, name: str) -> None:
        """Remove a container by name with -f. Raises on non-zero rc."""
        rc, out, err = await self._run(
            [self._binary, "rm", "-f", name], self._op_timeout
        )
        if rc != 0:
            raise RuntimeError(f"docker rm {name} failed: {err.strip() or out.strip()}")
