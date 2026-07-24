"""SshBackend: a remote-over-SSH ExecutionBackend (Mode 2, bare isolation).

run() rsyncs a git-bundle-materialized worktree to a remote tmp dir, launches a
daemonizing Python supervisor, and returns only after its startup handshake so a
channel drop can never leave the run unobservable.
"""

import asyncio
import json
import os
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from maestro.execution.exec_config import SshTransport
from maestro.execution.models import (
    BackendHealth,
    CapabilityResult,
    ExecutionHandleRef,
    ExecutionRequest,
    ProbeResult,
)
from maestro.execution.secret_file import write_env_file
from maestro.execution.ssh_cli import Runner, RunResult, SshCli
from maestro.execution.ssh_collect import capture_baseline
from maestro.execution.ssh_handle import CollectSpec, MirrorSpec, SshTaskHandle
from maestro.execution.ssh_launch import (
    RSYNC_EXCLUDES_OUT,
    RemoteLayout,
    build_descriptor,
    encode_transport_ref,
    remote_layout,
)


_HANDSHAKE = "MAESTRO-SUPERVISOR-READY"


class LaunchNotStarted(Exception):
    """The remote run provably never launched — safe to release/rollback."""


def _supervisor_src() -> str:
    """Read the shipped, versioned remote supervisor source."""
    return (
        resources.files("maestro.execution.resources")
        .joinpath("maestro_supervisor.py")
        .read_text(encoding="utf-8")
    )


class SshBackend:
    """`ExecutionBackend` that runs a harness on a remote host over SSH.

    Bare isolation only (no container on the remote side). The worktree is
    materialized remotely via a git bundle + clone (so history/refs travel)
    followed by an rsync overlay (so dirty/untracked local state travels
    too), then a daemonizing supervisor is launched and its startup
    handshake is awaited before `run()` returns.
    """

    def __init__(
        self,
        name: str,
        transport: SshTransport,
        *,
        secret_env: list[str],
        runner: Runner | None = None,
        local_staging_root: Path | None = None,
    ) -> None:
        """Build the backend for a named ssh transport."""
        self._name = name
        self._t = transport
        self._secret_env = secret_env
        self._ssh = SshCli(transport, runner=runner)
        self._staging_root = local_staging_root or Path(
            os.environ.get("TMPDIR", "/tmp")
        )

    @property
    def id(self) -> str:
        """Backend identifier (the configured backend name)."""
        return self._name

    async def healthcheck(self) -> BackendHealth:
        """Reachability check: `ssh host true`."""
        if await self._ssh.check(["true"]):
            return BackendHealth(reachable=True)
        return BackendHealth(reachable=False, detail=f"ssh {self._t.host} unreachable")

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        """Probe each `req.required_tools` on the remote PATH."""
        missing = [t for t in req.required_tools if not await self._ssh.probe_tool(t)]
        return CapabilityResult(ok=not missing, missing_tools=missing)

    async def run(self, req: ExecutionRequest) -> SshTaskHandle:
        """Materialize the worktree remotely and launch the supervisor.

        Blocks on the supervisor's startup handshake before returning, so a
        channel drop right after launch can never leave the run
        unobservable to the caller.
        """
        if req.execution_id is None:
            raise ValueError("SshBackend requires req.execution_id")
        layout = remote_layout(self._t.workdir_root, req.execution_id)
        baseline = capture_baseline(req.workdir, excludes=RSYNC_EXCLUDES_OUT)

        try:
            await self._materialize_remote(req, layout)
        except Exception as exc:  # nothing launched remotely yet
            raise LaunchNotStarted(str(exc)) from exc

        descriptor = build_descriptor(
            req.execution_id, layout, list(req.argv), self._t.workdir_root
        )
        # Launch the supervisor and block on its startup handshake, so a channel
        # drop after this point can never leave the run unobservable.
        result = await self._launch_supervisor(layout, descriptor)
        if _HANDSHAKE not in result.stdout:
            raise RuntimeError(f"supervisor handshake missing: {result.stderr[:400]}")

        ref = ExecutionHandleRef(
            backend_id=self._name,
            run_id=req.run_id,
            transport_ref=encode_transport_ref(
                self._t.host, self._t.port, layout.root, layout.status
            ),
            status_marker=layout.status,
            started_at=datetime.now(UTC),
        )
        staging = self._staging_root / f"maestro-collect-{req.execution_id}"
        journal = self._staging_root / f"maestro-journal-{req.execution_id}"
        handle = SshTaskHandle(
            self._ssh,
            layout,
            ref,
            log_path=req.log_path,
            timeout_seconds=req.timeout_seconds,
            collect_spec=CollectSpec(
                req.workdir,
                staging,
                journal,
                baseline,
                scope=req.collect.include or None,
            ),
            expected_owner=req.execution_id,
            mirror_spec=self._build_mirror_spec(req, layout),
        )
        handle.start()
        return handle

    def _build_mirror_spec(
        self, req: ExecutionRequest, layout: RemoteLayout
    ) -> MirrorSpec | None:
        """Build the WAL progress-mirror wiring from `req.progress_mirror`.

        None when the request has no mirror policy or the policy names no
        remote state file — local/docker backends never set this, so this
        stays a no-op for them.
        """
        policy = req.progress_mirror
        if policy is None or not policy.remote_globs:
            return None
        state_file = policy.remote_globs[0]
        policy.local_dir.mkdir(parents=True, exist_ok=True)
        return MirrorSpec(
            remote_db=f"{layout.repo}/spec/{state_file}",
            remote_snapshot=f"{layout.root}/state-snapshot.db",
            local_target=policy.local_dir / state_file,
        )

    async def _launch_supervisor(
        self, layout: RemoteLayout, descriptor: dict
    ) -> RunResult:
        """Write descriptor + supervisor remotely, then launch and return.

        Descriptor and supervisor source are non-secret and are delivered
        over stdin to a remote `tee` rather than interpolated into argv.
        """
        await self._ssh.run(["mkdir", "-p", layout.root])
        await self._ssh.run(["tee", layout.descriptor], stdin=json.dumps(descriptor))
        await self._ssh.run(["tee", layout.supervisor], stdin=_supervisor_src())
        return await self._ssh.run(["python3", layout.supervisor, layout.descriptor])

    async def _materialize_remote(
        self, req: ExecutionRequest, layout: RemoteLayout
    ) -> None:
        """git bundle → transfer → clone; rsync worktree overlay; env-file."""
        # 1. Remote root, private.
        await self._ssh.run(["mkdir", "-p", "-m", "700", layout.root])
        # 2. git bundle of the worktree HEAD, transferred and cloned. The
        # local bundle is a scratch file staged only to reach the remote; it
        # is unlinked once the transfer completes so repeated runs don't
        # leak large bundle files under the local staging root.
        bundle = self._staging_root / f"maestro-bundle-{req.execution_id}.bundle"
        try:
            await _run_local(
                ["git", "-C", str(req.workdir), "bundle", "create", str(bundle), "HEAD"]
            )
            await self._ssh.rsync(
                str(bundle),
                f"{self._t.host}:{layout.root}/repo.bundle",
                delete=False,
                excludes=[],
            )
        finally:
            bundle.unlink(missing_ok=True)
        await self._ssh.run(["git", "clone", f"{layout.root}/repo.bundle", layout.repo])
        # 3. Overlay the working tree (incl. dirty/untracked), excluding .git etc.
        await self._ssh.rsync(
            f"{req.workdir}/",
            f"{self._t.host}:{layout.repo}/",
            delete=False,
            excludes=RSYNC_EXCLUDES_OUT,
        )
        # 4. Secret env-file, written locally at 0600 then transferred.
        if self._secret_env:
            local_env = self._staging_root / f"maestro-env-{req.execution_id}"
            write_env_file(local_env, self._secret_env, os.environ)
            await self._ssh.rsync(
                str(local_env),
                f"{self._t.host}:{layout.env_file}",
                delete=False,
                excludes=[],
            )
            local_env.unlink(missing_ok=True)

    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        """Delegate liveness recovery to `ssh_recovery.probe_ssh` (Task E3).

        Imported lazily so this module has no hard import-time dependency on
        a sibling that lands in a later task.
        """
        from maestro.execution.ssh_recovery import probe_ssh

        verdict = await probe_ssh(self._ssh, ref)
        return ProbeResult(alive=verdict.needs_review, detail=verdict.reason)


async def _run_local(argv: list[str]) -> None:
    """Run a local subprocess (used only for the local `git bundle` step)."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed: {err.decode('utf-8', 'replace')[:400]}")
