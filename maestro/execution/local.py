"""Local execution backend: an asyncio subprocess wrapped as a TaskHandle."""

import asyncio
import contextlib
import os
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from maestro._vendor.obs import child_env
from maestro.execution.docker_recovery import probe_execution
from maestro.execution.env import build_local_env  # noqa: F401 (re-exported)
from maestro.execution.models import (
    BackendHealth,
    CapabilityResult,
    CollectResult,
    ExecutionHandleRef,
    ExecutionRequest,
    ExecutionResult,
    PreparedRun,
    ProbeResult,
)
from maestro.execution.ref_identity import RefIdentity, ref_identity


if TYPE_CHECKING:
    # Imported only for type hints: isolators.py imports this module, so a
    # runtime import here would create a circular import.
    from maestro.execution.backend import TaskHandle
    from maestro.execution.docker_cli import DockerCli
    from maestro.execution.isolators import DockerIsolator, Isolator


_TAIL_LIMIT = 4000


class VerifierLaunchError(RuntimeError):
    """Raised when a verifier isolator's post-spawn hook fails to confirm.

    Signals a fail-closed launch: `LocalBackend.run` kills the freshly
    spawned process and cleans up the isolator's materialized artifacts
    rather than returning a live handle. Task 3's `VerifierDockerIsolator`
    raises this (or lets the underlying error propagate) from
    `after_spawn` when the container's credential handoff never confirms.
    """


class LocalTaskHandle:
    """TaskHandle over a local asyncio.subprocess.Process."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        req: ExecutionRequest,
        log_fd: int | None,
        ref: ExecutionHandleRef,
    ) -> None:
        self._proc = proc
        self._req = req
        self._log_fd = log_fd
        self.ref = ref

    @property
    def os_pid(self) -> int | None:
        return self._proc.pid

    def poll(self) -> int | None:
        # asyncio sets returncode via the loop's child watcher; sync + no I/O.
        return self._proc.returncode

    async def wait(self) -> ExecutionResult:
        stdout_tail = ""
        stderr_tail = ""
        timed_out = False
        try:
            if self._req.capture_output:
                out, err = await self._await(self._proc.communicate())
                stdout_tail = _decode_tail(out)
                stderr_tail = _decode_tail(err)
                # Mirror captured output into the log for uniformity.
                self._req.log_path.write_text(
                    (stdout_tail + ("\n" + stderr_tail if stderr_tail else "")),
                    encoding="utf-8",
                )
            else:
                await self._await(self._proc.wait())
        except TimeoutError:
            timed_out = True
            self._proc.kill()
            await self._proc.wait()
        finally:
            self._close_log()
        return ExecutionResult(
            exit_code=None if timed_out else self._proc.returncode,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            output_log_path=self._req.log_path,
            timed_out=timed_out,
        )

    async def _await(self, coro):
        if self._req.timeout_seconds is not None:
            return await asyncio.wait_for(coro, timeout=self._req.timeout_seconds)
        return await coro

    async def terminate(self, grace_seconds: float) -> None:
        if self._proc.returncode is not None:
            return
        self._proc.terminate()
        await asyncio.sleep(grace_seconds)
        if self._proc.returncode is None:
            self._proc.kill()
        await self._proc.wait()
        self._close_log()

    async def kill(self) -> None:
        if self._proc.returncode is None:
            self._proc.kill()
        await self._proc.wait()
        self._close_log()

    async def collect(self) -> CollectResult:
        return CollectResult(applied=False, detail="local: no collect needed")

    async def cleanup(self) -> None:
        self._close_log()

    def _close_log(self) -> None:
        if self._log_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._log_fd)
            self._log_fd = None


def _decode_tail(data: bytes | None) -> str:
    if not data:
        return ""
    text = data.decode("utf-8", errors="replace")
    return text[-_TAIL_LIMIT:]


def _cleanup_prepared(prepared: PreparedRun) -> None:
    """Best-effort removal of files an isolator materialized (spawn-failure path)."""
    for path in prepared.cleanup_paths:
        with contextlib.suppress(OSError):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)


class LocalBackend:
    """Runs an ExecutionRequest as a local asyncio subprocess.

    Isolation-aware: with no `docker` client, `healthcheck`/`can_run` behave
    exactly as the plain local backend always has. Passing a `DockerCli`
    (paired with a `DockerIsolator`, see `resolver.py`) switches `healthcheck`
    to a daemon/DOCKER_HOST reachability check and `can_run` to an image
    presence gate — the composition the docker backend is built from.
    """

    def __init__(
        self,
        isolator: "Isolator | None" = None,
        *,
        backend_id: str = "local",
        docker: "DockerCli | None" = None,
    ) -> None:
        # Default BareIsolator; imported lazily to break the import cycle
        # (isolators.py imports LocalTaskHandle from this module).
        if isolator is None:
            from maestro.execution.isolators import BareIsolator

            isolator = BareIsolator()
        self._isolator = isolator
        self._backend_id = backend_id
        self._docker = docker

    @property
    def id(self) -> str:
        return self._backend_id

    async def healthcheck(self) -> BackendHealth:
        if self._docker is None:
            return BackendHealth(reachable=True)
        host = os.environ.get("DOCKER_HOST", "")
        if host.startswith("ssh://") or host.startswith("tcp://"):
            return BackendHealth(
                reachable=False,
                detail=f"DOCKER_HOST={host!r} is remote; Phase 1 is local only",
            )
        try:
            reachable = await self._docker.version_ok()
        except Exception as e:
            return BackendHealth(reachable=False, detail=f"docker unreachable: {e}")
        if not reachable:
            return BackendHealth(reachable=False, detail="docker daemon unreachable")
        return BackendHealth(reachable=True)

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        if self._docker is None:
            missing = [t for t in req.required_tools if shutil.which(t) is None]
            return CapabilityResult(ok=not missing, missing_tools=missing)
        # Docker: image presence is the Phase-1 capability gate; a full
        # in-image tool probe (a --rm helper container) is exercised by
        # integration tests. `self._docker is not None` here implies the
        # resolver paired this backend with a DockerIsolator (resolver.py).
        # `cast` only affects static typing: if that invariant is ever
        # violated (e.g. a BareIsolator passed alongside a non-None
        # `docker`), this raises AttributeError at call time — not a silent
        # type lie — since `_cfg` genuinely doesn't exist on other isolators.
        image = cast("DockerIsolator", self._isolator)._cfg.image
        if not await self._docker.image_exists(image):
            return CapabilityResult(ok=False, missing_tools=[f"image:{image}"])
        return CapabilityResult(ok=True)

    async def run(self, req: ExecutionRequest) -> "TaskHandle":
        plan = self._isolator.prepare(
            req, trace_env=child_env(), host_env=dict(os.environ)
        )
        prepared = self._isolator.materialize(plan)
        argv = prepared.plan.argv
        env = prepared.plan.env
        log_fd: int | None = None
        if req.capture_output:
            stdout = asyncio.subprocess.PIPE
            stderr = asyncio.subprocess.PIPE
        else:
            log_fd = os.open(str(req.log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            stdout = log_fd
            stderr = asyncio.subprocess.STDOUT
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=req.workdir,
                env=env,
                stdin=asyncio.subprocess.PIPE if req.stdin is not None else None,
                stdout=stdout,
                stderr=stderr,
            )
        except BaseException:
            if log_fd is not None:
                os.close(log_fd)
            # Clean any files the isolator created before the spawn failed.
            _cleanup_prepared(prepared)
            raise
        if log_fd is not None:
            # The child inherited its own dup of the fd when spawned above;
            # the parent's copy must be closed here or it leaks (the
            # scheduler's normal completion path never calls
            # wait()/terminate()/kill()/cleanup()). Matches the old
            # spawn()'s `finally: os.close(fd)` behavior.
            os.close(log_fd)
            log_fd = None
        if req.stdin is not None and proc.stdin is not None:
            proc.stdin.write(req.stdin.encode("utf-8"))
            proc.stdin.close()
        try:
            await self._isolator.after_spawn(prepared, proc)
        except BaseException:
            # Fail-closed: a post-spawn hook failure (e.g. the verifier
            # credential handoff never confirmed) must not hand back a live
            # handle. Kill the process and clean the isolator's artifacts.
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            _cleanup_prepared(prepared)
            raise
        ref = ExecutionHandleRef(
            backend_id=req.backend_id,
            run_id=req.run_id,
            execution_id=req.execution_id,
            transport_ref=self._isolator.transport_ref(prepared, proc.pid),
            started_at=datetime.now(UTC),
        )
        local_handle = LocalTaskHandle(proc, req, log_fd, ref)
        return self._isolator.wrap(local_handle, prepared, ref)

    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        """Isolation-aware recovery probe: bare -> PID liveness; docker ->
        container liveness by `execution_id` (fail-closed on either a
        missing daemon or a missing `execution_id`, since there is then no
        key to probe by)."""
        if ref.transport_ref.startswith("local_pid:"):
            pid = int(ref.transport_ref.split(":", 1)[1])
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return ProbeResult(needs_review=False, alive=False)
            except PermissionError:
                return ProbeResult(
                    needs_review=True, alive=True, detail="exists (EPERM)"
                )
            return ProbeResult(needs_review=True, alive=True)
        # Docker isolation: probe the container by execution_id (fail-closed
        # on a missing daemon or a missing execution_id — no key to probe).
        if self._docker is None or ref.execution_id is None:
            return ProbeResult(
                needs_review=True, detail="docker probe: no execution_id/daemon"
            )
        verdict = await probe_execution(ref.execution_id, self._docker)
        return ProbeResult(needs_review=verdict.needs_review, detail=verdict.reason)

    def accepts_ref(self, ref: ExecutionHandleRef) -> bool:
        """Own identity is `("local", "docker" if self._docker else "bare")`;
        a mismatched or unclassifiable (`("unknown","unknown")`) ref never
        matches -> caller fails closed rather than probing."""
        own = RefIdentity("local", "docker" if self._docker is not None else "bare")
        return ref_identity(ref.transport_ref) == own
