"""SshTaskHandle + local monitor. `poll()` is cached-only (never network I/O);
a background monitor task tails the remote log at a byte offset and polls the
atomic status marker to detect completion. Signals target the workload's
process GROUP (negative pgid), never a broad pkill.
"""

import asyncio
import contextlib
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from maestro.execution.models import (
    CollectResult,
    ExecutionHandleRef,
    ExecutionResult,
)
from maestro.execution.ssh_cli import SshCli
from maestro.execution.ssh_collect import apply_collect, plan_collect
from maestro.execution.ssh_launch import RSYNC_EXCLUDES_COLLECT, RemoteLayout
from maestro.execution.ssh_mirror import mirror_once


if TYPE_CHECKING:
    from maestro.execution.ssh_docker_probe import ContainerOps


logger = logging.getLogger(__name__)

_MAX_BACKOFF_S = 30.0


@dataclass
class CollectSpec:
    """Local paths + pre-run baseline needed to collect a remote run back."""

    worktree: Path
    staging_dir: Path
    journal_dir: Path
    baseline: dict[str, str]
    scope: list[str] | None = None


@dataclass
class MirrorSpec:
    """Remote/local paths needed to drive the WAL progress mirror each tick."""

    remote_db: str
    remote_snapshot: str
    local_target: Path


class SshTaskHandle:
    """`TaskHandle` for a workload running on a remote SSH host.

    `poll()` never performs I/O — it returns whatever exit code the
    background monitor task last cached. The monitor tails the remote log
    file at a byte offset (so a slow/partial read never duplicates bytes
    already written locally) and polls the remote status-marker file; once
    the marker parses, the monitor caches the exit code and flips the
    terminal event that `wait()` awaits. `terminate`/`kill` resolve the
    workload's process group (from the status or pid marker) and signal the
    whole group with a negative pgid — never a broad `pkill`. `cleanup` is
    ownership-checked: it refuses to `rm -rf` the remote root unless the
    owner marker matches the expected execution id.
    """

    def __init__(
        self,
        ssh: SshCli,
        layout: RemoteLayout,
        ref: ExecutionHandleRef,
        *,
        log_path: Path,
        timeout_seconds: float | None,
        collect_spec: CollectSpec,
        poll_interval: float = 1.0,
        expected_owner: str | None = None,
        mirror_spec: MirrorSpec | None = None,
        container_ops: "ContainerOps | None" = None,
    ) -> None:
        """Initialize the handle; call `start()` to spawn the monitor.

        `container_ops`, when set, is the docker-path lifecycle companion:
        `terminate`/`kill` also stop the container (killing the remote
        `docker run` client alone does not stop the container), and
        `cleanup` removes it before the remote `rm -rf`.
        """
        self._ssh = ssh
        self._layout = layout
        self.ref = ref
        self._log_path = log_path
        self._timeout = timeout_seconds
        self._collect = collect_spec
        self._interval = poll_interval
        self._expected_owner = expected_owner
        self._mirror = mirror_spec
        self._container_ops = container_ops
        self._exit_code: int | None = None
        self._terminal = asyncio.Event()
        self._timed_out = False
        self._monitor: asyncio.Task | None = None
        self._log_offset = 0

    @property
    def os_pid(self) -> int | None:
        """Remote executions have no local OS pid."""
        return None

    def poll(self) -> int | None:
        """Non-blocking, cached exit code (None while running). No I/O."""
        return self._exit_code

    def start(self) -> None:
        """Spawn the background monitor task (idempotent)."""
        if self._monitor is None:
            self._monitor = asyncio.create_task(self._run_monitor())

    async def _run_monitor(self) -> None:
        """Poll-loop: tail the log, check the status marker, honor timeout.

        Uses bounded exponential backoff (capped at `_MAX_BACKOFF_S`) after a
        transient failure so a flaky connection doesn't spin-loop the poll.
        """
        loop = asyncio.get_running_loop()
        deadline = None if self._timeout is None else loop.time() + self._timeout
        backoff = self._interval
        while not self._terminal.is_set():
            try:
                await self._tail_log()
                await self._mirror_progress()
                status = await self._read_status()
                if status is not None:
                    self._exit_code = int(status["exit_code"])
                    self._terminal.set()
                    return
                backoff = self._interval
            except Exception:
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
            if deadline is not None and loop.time() > deadline:
                self._timed_out = True
                await self._signal_group("KILL")
                self._exit_code = None
                self._terminal.set()
                return
            await asyncio.sleep(backoff)

    async def _tail_log(self) -> None:
        """Append newly-produced remote log bytes, tracked by byte offset."""
        res = await self._ssh.run(
            ["tail", "-c", f"+{self._log_offset + 1}", self._layout.log]
        )
        if res.returncode == 0 and res.stdout:
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(res.stdout)
            self._log_offset += len(res.stdout.encode("utf-8"))

    async def _mirror_progress(self) -> None:
        """Best-effort WAL progress-mirror tick; never breaks the monitor loop.

        `mirror_once` already reports transient failures via its return
        value rather than raising, but this is still wrapped so a defect
        there can never take down log-tailing / status-polling.
        """
        if self._mirror is None:
            return
        with contextlib.suppress(Exception):
            await mirror_once(
                self._ssh,
                self._mirror.remote_db,
                self._mirror.remote_snapshot,
                self._mirror.local_target,
            )

    async def _read_status(self) -> dict | None:
        """Read the atomic status marker; None while the run is still live."""
        res = await self._ssh.run(["cat", self._layout.status])
        if res.returncode != 0 or not res.stdout.strip():
            return None
        return json.loads(res.stdout)

    async def _pgid(self) -> int | None:
        """Resolve the workload's process group from status, else pid file."""
        for src in (self._layout.status, self._layout.pid):
            res = await self._ssh.run(["cat", src])
            if res.returncode == 0 and res.stdout.strip():
                with contextlib.suppress(Exception):
                    return int(json.loads(res.stdout)["pgid"])
        return None

    async def _signal_group(self, sig: str) -> None:
        """Signal the workload's whole process group (negative pgid)."""
        pgid = await self._pgid()
        if pgid is not None:
            await self._ssh.run(["kill", f"-{sig}", f"-{pgid}"])

    async def wait(self) -> ExecutionResult:
        """Await terminal completion and return the cached result."""
        await self._terminal.wait()
        return ExecutionResult(
            exit_code=self._exit_code,
            output_log_path=self._log_path,
            timed_out=self._timed_out,
        )

    async def terminate(self, grace_seconds: float) -> None:
        """Send TERM to the process group; escalate to KILL after grace.

        For a docker run, also targeted-stop the container — killing the
        remote `docker run` client does not stop the container itself.
        """
        await self._signal_group("TERM")
        if self._container_ops is not None:
            await self._container_ops.stop(grace_seconds)
        await asyncio.sleep(grace_seconds)
        if not self._terminal.is_set():
            await self._signal_group("KILL")

    async def kill(self) -> None:
        """Force-kill the process group and, for docker, the container."""
        await self._signal_group("KILL")
        if self._container_ops is not None:
            await self._container_ops.stop(0.0)

    async def collect(self) -> CollectResult:
        """Rsync the remote repo to staging, then plan + apply the collect.

        Raises `CollectConflict` (from `ssh_collect.plan_collect`) if the
        remote's changes conflict with local worktree divergence, a
        forbidden path, or a symlink/traversal escape — no worktree
        mutation occurs in that case.
        """
        staging = self._collect.staging_dir
        staging.mkdir(parents=True, exist_ok=True)
        pulled = await self._ssh.rsync(
            f"{self._ssh.host}:{self._layout.repo}/",
            f"{staging}/",
            delete=True,
            excludes=RSYNC_EXCLUDES_COLLECT,
        )
        if pulled.returncode != 0:
            raise RuntimeError(f"collect rsync failed: {pulled.stderr[:400]}")
        plan = plan_collect(
            self._collect.worktree,
            staging,
            self._collect.baseline,
            forbidden=[".git", ".maestro"],
            scope=self._collect.scope,
        )
        apply_collect(
            self._collect.worktree,
            staging,
            plan,
            journal_dir=self._collect.journal_dir,
        )
        return CollectResult(
            applied=True, files_changed=len(plan.modified) + len(plan.deleted)
        )

    async def cleanup(self) -> None:
        """Ownership-checked container removal (docker), then remote `rm -rf`
        + local staging/journal removal."""
        await self._verify_ownership()
        if self._container_ops is not None:
            await self._container_ops.remove()
        await self._ssh.run(["rm", "-rf", self._layout.root])
        for p in (self._collect.staging_dir, self._collect.journal_dir):
            shutil.rmtree(p, ignore_errors=True)
        if self._monitor is not None:
            self._monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor

    async def _verify_ownership(self) -> None:
        """Refuse to proceed unless the remote root is under workdir_root
        AND the remote owner marker matches `expected_owner`.

        No-op when `expected_owner` is unset (caller opted out of the
        check).
        """
        if self._expected_owner is None:
            return
        if not self._layout.root.startswith(self._ssh.workdir_root.rstrip("/") + "/"):
            raise RuntimeError(f"remote_dir {self._layout.root} escapes workdir_root")
        res = await self._ssh.run(["cat", self._layout.owner_marker])
        if res.returncode != 0 or res.stdout.strip() != self._expected_owner:
            raise RuntimeError(
                f"owner marker mismatch: refusing rm -rf {self._layout.root}"
            )
