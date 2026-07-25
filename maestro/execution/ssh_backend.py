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

from maestro._vendor.obs import child_env
from maestro.execution.docker_cli import DockerCli
from maestro.execution.exec_config import DockerIsolation, SshTransport
from maestro.execution.models import (
    BackendHealth,
    CapabilityResult,
    CollectPolicy,
    ExecutionHandleRef,
    ExecutionRequest,
    ProbeResult,
)
from maestro.execution.secret_file import write_env_file
from maestro.execution.ssh_cli import Runner, RunResult, SshCli
from maestro.execution.ssh_collect import capture_baseline
from maestro.execution.ssh_docker import build_docker_run_argv, resolve_effective_user
from maestro.execution.ssh_docker_probe import ContainerOps, ssh_docker_run_cmd
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


def _collect_scope(policy: CollectPolicy) -> list[str] | None:
    """Scope paths for a `scope_paths` collect; None for any other mode.

    Keyed off ``mode``, not ``include`` — an empty ``include`` under
    ``mode="scope_paths"`` must stay a fully-closed (reject-all) scope, not
    collapse to a whole-worktree collect. Keying off ``include or None``
    would silently disable ``plan_collect``'s fail-closed empty-scope
    handling, and would also scope-bound a ``mode="none"``/``whole_worktree``
    request that carried a stray ``include``.
    """
    return list(policy.include) if policy.mode == "scope_paths" else None


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
        isolation: DockerIsolation | None = None,
        runner: Runner | None = None,
        local_staging_root: Path | None = None,
    ) -> None:
        """Build the backend for a named ssh transport (bare or docker)."""
        self._name = name
        self._t = transport
        self._secret_env = secret_env
        self._isolation = isolation
        self._ssh = SshCli(transport, runner=runner)
        self._docker = (
            DockerCli(run_cmd=ssh_docker_run_cmd(self._ssh), op_timeout=60.0)
            if isolation is not None
            else None
        )
        self._staging_root = local_staging_root or Path(
            os.environ.get("TMPDIR", "/tmp")
        )

    @property
    def id(self) -> str:
        """Backend identifier (the configured backend name)."""
        return self._name

    @property
    def isolation_kind(self) -> str:
        """`"docker"` when a container isolation is configured, else `"bare"`."""
        return "docker" if self._isolation is not None else "bare"

    @property
    def docker(self) -> DockerCli | None:
        """The over-SSH DockerCli for the docker path (None for bare)."""
        return self._docker

    def _make_container_ops(self, expected_labels: dict[str, str]) -> ContainerOps:
        """Build the `ContainerOps` for this run's `maestro-<execution_id>`
        container, bound to `self._docker` and its full expected label set.

        The container name matches `ssh_docker.build_docker_run_argv`'s
        `--name maestro-<execution_id>`.
        """
        assert self._docker is not None  # only called on the docker path
        execution_id = expected_labels["maestro.execution_id"]
        return ContainerOps(
            docker=self._docker,
            container_name=f"maestro-{execution_id}",
            expected_labels=expected_labels,
        )

    async def healthcheck(self) -> BackendHealth:
        """Reachability check: `ssh host true`."""
        if await self._ssh.check(["true"]):
            return BackendHealth(reachable=True)
        return BackendHealth(reachable=False, detail=f"ssh {self._t.host} unreachable")

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        """Probe capability on the executor.

        Bare: each `required_tool` on the remote PATH. Docker: the remote
        daemon is reachable, the image exists, each required tool resolves
        INSIDE the image under the effective user, and a probe file can be
        written/read/deleted under the effective user in a remote scratch
        dir that is bind-mounted at `/work` (catches a UID/bind-mount-owner
        incompatibility — e.g. a container UID that cannot write to a bind
        mount owned by the remote SSH user — before the real task).
        """
        if self._isolation is None:
            missing = [
                t for t in req.required_tools if not await self._ssh.probe_tool(t)
            ]
            return CapabilityResult(ok=not missing, missing_tools=missing)

        assert self._docker is not None
        problems: list[str] = []
        if not await self._docker.version_ok():
            return CapabilityResult(
                ok=False, missing_tools=["docker daemon unreachable"]
            )
        image = self._isolation.image
        if not await self._docker.image_exists(image):
            return CapabilityResult(ok=False, missing_tools=[f"image absent: {image}"])
        user = await resolve_effective_user(self._ssh, self._isolation.user)
        for tool in req.required_tools:
            try:
                async with asyncio.timeout(60.0):
                    probe = await self._ssh.run(
                        [
                            "docker",
                            "run",
                            "--rm",
                            "--user",
                            user,
                            image,
                            "sh",
                            "-c",
                            # tool as a positional ($1), never interpolated into
                            # the shell string, so a config-supplied tool name
                            # cannot inject shell syntax.
                            'command -v -- "$1"',
                            "sh",
                            tool,
                        ]
                    )
            except TimeoutError:
                problems.append(f"tool missing in image: {tool}")
                continue
            if probe.returncode != 0:
                problems.append(f"tool missing in image: {tool}")
        mkdir_res = await self._ssh.run(["mktemp", "-d"])
        scratch = mkdir_res.stdout.strip()
        if mkdir_res.returncode != 0 or not scratch:
            problems.append(
                f"could not create remote scratch dir (rc={mkdir_res.returncode})"
            )
        else:
            try:
                try:
                    async with asyncio.timeout(60.0):
                        probe_res = await self._ssh.run(
                            [
                                "docker",
                                "run",
                                "--rm",
                                "--user",
                                user,
                                "-v",
                                f"{scratch}:/work",
                                image,
                                "sh",
                                "-c",
                                't=/work/.maestro-probe && echo ok > "$t" '
                                '&& cat "$t" && rm "$t"',
                            ]
                        )
                except TimeoutError:
                    problems.append(
                        f"scratch write/read/delete failed under user {user}"
                    )
                else:
                    if probe_res.returncode != 0:
                        problems.append(
                            f"scratch write/read/delete failed under user {user}"
                        )
            finally:
                await self._ssh.run(["rm", "-rf", scratch])
        return CapabilityResult(ok=not problems, missing_tools=problems)

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
        isolation = "bare"
        expected_labels: dict[str, str] = {}
        if self._isolation is not None:
            effective_user = await resolve_effective_user(
                self._ssh, self._isolation.user
            )
            trace_env = child_env()  # non-secret trace propagation
            inline_env = {**req.env, **trace_env}
            docker_argv, expected_labels = build_docker_run_argv(
                execution_id=req.execution_id,
                entity_kind=req.entity_kind or "workstream",
                entity_id=req.run_id,
                attempt=req.attempt,
                backend_id=self._name,
                image=self._isolation.image,
                remote_repo=layout.repo,
                remote_root=layout.root,
                remote_env_file=layout.env_file,
                effective_user=effective_user,
                network=self._isolation.network,
                memory=self._isolation.memory,
                cpus=self._isolation.cpus,
                inline_env=inline_env,
                has_secret_env_file=bool(self._secret_env),
                inner_argv=list(req.argv),
            )
            descriptor["argv"] = docker_argv
            # secrets reach the container via docker --env-file; keep them OUT of
            # the docker-run process env by pointing the supervisor's env_file at
            # a non-existent path under root (passes _validate, _load_env skips it).
            descriptor["env_file"] = f"{layout.root}/noenv"
            isolation = "docker"

        # Launch the supervisor and block on its startup handshake, so a channel
        # drop after this point can never leave the run unobservable.
        result = await self._launch_supervisor(layout, descriptor)
        if _HANDSHAKE not in result.stdout:
            raise RuntimeError(f"supervisor handshake missing: {result.stderr[:400]}")

        ref = ExecutionHandleRef(
            backend_id=self._name,
            run_id=req.run_id,
            execution_id=req.execution_id,
            transport_ref=encode_transport_ref(
                self._t.host,
                self._t.port,
                layout.root,
                layout.status,
                isolation=isolation,
                expected_labels=expected_labels,
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
                mode=req.collect.mode,
                scope=_collect_scope(req.collect),
            ),
            expected_owner=req.execution_id,
            mirror_spec=self._build_mirror_spec(req, layout),
            container_ops=(
                self._make_container_ops(expected_labels)
                if self._isolation is not None
                else None
            ),
            capture_output=req.capture_output,
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
        return ProbeResult(
            needs_review=verdict.needs_review, alive=None, detail=verdict.reason
        )


async def _run_local(argv: list[str]) -> None:
    """Run a local subprocess (used only for the local `git bundle` step)."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed: {err.decode('utf-8', 'replace')[:400]}")
