"""Execution backend and task-handle protocols.

`TaskHandle` replaces BOTH the scheduler's synchronous `subprocess.Popen`
and the orchestrator's `asyncio.subprocess.Process`. `poll()` is SYNC and
cached-only — it must never do network I/O (a remote backend updates the
cache from a local monitor task).
"""

from typing import Protocol, runtime_checkable

from maestro.execution.models import (
    BackendHealth,
    CapabilityResult,
    CollectResult,
    ExecutionHandleRef,
    ExecutionRequest,
    ExecutionResult,
    ProbeResult,
)


@runtime_checkable
class TaskHandle(Protocol):
    """Handle to a running (or finished) execution."""

    ref: ExecutionHandleRef

    @property
    def os_pid(self) -> int | None:
        """Local OS pid if the run is a local process, else None.

        Used by orchestrator recovery, which persists a pid and probes it
        with os.kill(pid, 0). Remote handles return None.
        """
        ...

    def poll(self) -> int | None:
        """Non-blocking, cached exit code (None while running). No I/O."""
        ...

    async def wait(self) -> ExecutionResult:
        """Await terminal completion and return the result."""
        ...

    async def terminate(self, grace_seconds: float) -> None:
        """Ask the process to stop; escalate after grace_seconds if needed."""
        ...

    async def kill(self) -> None:
        """Force-kill and reap."""
        ...

    async def collect(self) -> CollectResult:
        """Apply remote file changes back locally (no-op for LocalBackend)."""
        ...

    async def cleanup(self) -> None:
        """Release backend resources (remote tmp/container/env-file)."""
        ...


@runtime_checkable
class ExecutionBackend(Protocol):
    """Runs an ExecutionRequest and yields a TaskHandle."""

    @property
    def id(self) -> str:
        """Backend identifier (e.g. "local", "docker")."""
        ...

    async def healthcheck(self) -> BackendHealth:
        """Is the transport reachable (fail-fast before dispatch)?"""
        ...

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        """Are req.required_tools present on the target executor?"""
        ...

    async def run(self, req: ExecutionRequest) -> TaskHandle:
        """Start the run."""
        ...

    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        """Is a persisted run still alive (post-restart recovery)?"""
        ...

    def accepts_ref(self, ref: ExecutionHandleRef) -> bool:
        """Does this resolved backend's (transport, isolation) match the identity
        the persisted ref was minted with? False = config drift / unknown ref ->
        recovery must NEEDS_REVIEW without probing (never reclaim across
        identities)."""
        ...
