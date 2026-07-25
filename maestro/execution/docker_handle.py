"""TaskHandle for a container run: composes a LocalTaskHandle (the attached
`docker run` process) with targeted container stop/kill/rm.

The `docker run` CLI process being killed (e.g. on timeout) does NOT
guarantee the container itself stopped — `docker run` without `--rm` can
leave the container running/exited-but-present after its parent CLI dies.
This handle closes that gap by targeting the container by name directly.
"""

import contextlib
import shutil
from pathlib import Path

from maestro.execution.docker_cli import DockerCli
from maestro.execution.docker_recovery import labels_match
from maestro.execution.local import LocalTaskHandle
from maestro.execution.models import (
    CollectResult,
    ExecutionHandleRef,
    ExecutionResult,
)


class DockerTaskHandle:
    """Wraps a LocalTaskHandle; owns its container's lifecycle.

    `poll`/`os_pid` delegate to the wrapped local `docker run` process.
    `wait`/`terminate`/`kill` additionally target the container by name so
    that a killed/exited CLI process can never leave an orphaned container
    behind. `cleanup` is ownership-checked: it verifies the container's
    full expected label set before removing it, so a name collision with a
    foreign (or partially-matching) container is never silently rm'd.
    """

    def __init__(
        self,
        *,
        local: LocalTaskHandle,
        container_name: str,
        expected_labels: dict[str, str],
        cleanup_paths: list[Path],
        docker: DockerCli,
        ref: ExecutionHandleRef,
    ) -> None:
        """Initialize the handle around an already-spawned local process."""
        self._local = local
        self._name = container_name
        self._expected = expected_labels
        self._cleanup_paths = cleanup_paths
        self._docker = docker
        self.ref = ref

    @property
    def os_pid(self) -> int | None:
        """Local OS pid of the wrapped `docker run` CLI process."""
        return self._local.os_pid

    def poll(self) -> int | None:
        """Non-blocking, cached exit code of the wrapped CLI process."""
        return self._local.poll()

    async def wait(self) -> ExecutionResult:
        """Await the wrapped CLI process; stop the container if it timed out."""
        result = await self._local.wait()
        if result.timed_out:
            # docker run was killed; ensure the container itself is stopped.
            await self._stop_container(grace=10.0)
        return result

    async def terminate(self, grace_seconds: float) -> None:
        """Terminate the CLI process, then targeted-stop the container."""
        await self._local.terminate(grace_seconds)
        await self._stop_container(grace=grace_seconds)

    async def kill(self) -> None:
        """Force-kill the CLI process, then best-effort kill the container."""
        await self._local.kill()
        with contextlib.suppress(Exception):
            await self._docker.kill(self._name)

    async def collect(self) -> CollectResult:
        """No-op: the workspace is bind-mounted, so /work is already local."""
        return CollectResult(applied=False, detail="docker: bind-mounted /work")

    async def cleanup(self) -> None:
        """Ownership-checked `docker rm -f`, then unlink local artifacts.

        Raises RuntimeError if a container with this name exists but its
        labels don't fully match this handle's `expected_labels` — a
        foreign (or partially-matching) container is never removed. Local
        cleanup_paths (the env-file/cidfile/tmp dir) are always unlinked,
        whether or not the container is present, so this is idempotent and
        safe to call more than once (a second call sees an absent container
        and no files).
        """
        info = await self._docker.inspect(self._name)
        if info is not None:
            labels = (info.get("Config") or {}).get("Labels") or {}
            expected_id = self._expected.get("maestro.execution_id")
            # expected_id is None (handle built without the label) must
            # also fail the check — otherwise a foreign, unlabeled
            # container (whose labels vacuously satisfy an empty/id-less
            # expected set) would get rm'd.
            if expected_id is None or not labels_match(labels, self._expected):
                raise RuntimeError(
                    f"refusing to rm {self._name}: label mismatch "
                    f"(expected {self._expected}, got {labels})"
                )
            await self._docker.rm(self._name)
        # Always unlink local secret/cid/tmp artifacts, even if the
        # container is already gone.
        for path in self._cleanup_paths:
            with contextlib.suppress(OSError):
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)

    async def _stop_container(self, grace: float) -> None:
        """Best-effort targeted `docker stop` then `docker kill` by name."""
        with contextlib.suppress(Exception):
            await self._docker.stop(self._name, grace)
        with contextlib.suppress(Exception):
            await self._docker.kill(self._name)
