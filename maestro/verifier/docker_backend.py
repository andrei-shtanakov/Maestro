"""VerifierDockerIsolator: hardened, mount-less launch policy for the judge.

Reuses DockerTaskHandle/DockerCli/docker_recovery lifecycle verbatim (via
LocalBackend.wrap); only the argv/mount/stdin/env construction differs from
the general DockerIsolator (spec §3.2, §5). Owns the eager env-file unlink
(spec §3.5) and the deterministic credential temp-dir (spec §7.3).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from typing import TYPE_CHECKING

from maestro.execution.docker_cli import DockerCli
from maestro.execution.local import LocalTaskHandle, VerifierLaunchError
from maestro.execution.models import (
    ExecutionHandleRef,
    ExecutionRequest,
    PreparedRun,
    PreparedRunPlan,
)
from maestro.execution.secret_file import write_env_file


if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from maestro.execution.backend import TaskHandle
    from maestro.verifier.docker_config import VerifierDockerConfig


ANTHROPIC_ENV_KEY = "ANTHROPIC_API_KEY"
_CIDFILE_WAIT_SECONDS = 30.0
_CIDFILE_POLL_SECONDS = 0.1


def verifier_exec_dir(exec_root: Path, execution_id: str) -> Path:
    """Deterministic per-execution temp-dir under the dedicated verifier root."""
    return exec_root / f"maestro-verify-{execution_id}"


def _security_flags(cfg: VerifierDockerConfig, uid: str, gid: str) -> list[str]:
    """The hardening flag list shared by production launch and preflight probe.

    Baked, non-configurable (spec §3.2): read-only root, cap-drop, no-new-
    privileges, nosuid/nodev/noexec user-owned tmpfs, resource limits,
    bridge network, non-root user, /scratch workdir.
    """
    tmpfs = (
        f"/scratch:rw,nosuid,nodev,noexec,size={cfg.tmpfs_size},"
        f"mode=0700,uid={uid},gid={gid}"
    )
    return [
        "--read-only",
        "--tmpfs",
        tmpfs,
        "--workdir",
        "/scratch",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        cfg.user,
        "--memory",
        cfg.memory,
        "--cpus",
        cfg.cpus,
        "--pids-limit",
        str(cfg.pids_limit),
        "--network",
        "bridge",
    ]


def _synthetic_env() -> dict[str, str]:
    """Maestro-authored container env (NOT host passthrough, spec §2.2)."""
    return {
        "HOME": "/scratch",
        "TMPDIR": "/scratch",
        "XDG_CONFIG_HOME": "/scratch/.config",
        "XDG_CACHE_HOME": "/scratch/.cache",
    }


class VerifierDockerIsolator:
    """Hardened isolator for the verifier judge.

    `id = "docker"` so `DockerTaskHandle`/`docker_recovery` treat it exactly
    like the general docker path; the *backend id* (`verifier-docker`)
    provides identity separation (set by the factory, Task 4).
    """

    id = "docker"

    def __init__(
        self,
        cfg: VerifierDockerConfig,
        *,
        exec_root: Path,
        docker: DockerCli | None = None,
    ) -> None:
        """Initialize with the sandbox config and a dedicated temp-dir root."""
        self._cfg = cfg
        self._exec_root = exec_root
        self._docker = docker or DockerCli()

    def prepare(
        self,
        req: ExecutionRequest,
        *,
        trace_env: Mapping[str, str],
        host_env: Mapping[str, str],
    ) -> PreparedRunPlan:
        """Build the hardened `docker run` argv and execution plan (pure).

        Raises ValueError if req.execution_id is None.
        """
        if req.execution_id is None:
            raise ValueError("VerifierDockerIsolator requires req.execution_id")
        uid, gid = self._cfg.user.split(":")
        name = f"maestro-{req.execution_id}"
        tmp_dir = verifier_exec_dir(self._exec_root, req.execution_id)
        cidfile = tmp_dir / "cid"
        env_file = tmp_dir / "env"

        # Exactly one secret key, only if present on the host.
        secret_keys = [ANTHROPIC_ENV_KEY] if host_env.get(ANTHROPIC_ENV_KEY) else []
        labels = {
            "maestro.execution_id": req.execution_id,
            "maestro.entity_kind": req.entity_kind or "task",
            "maestro.entity_id": req.run_id,
            "maestro.attempt": str(req.attempt),
            "maestro.backend_id": "verifier-docker",
        }
        argv: list[str] = [
            "docker",
            "run",
            "-i",
            "--name",
            name,
            "--cidfile",
            str(cidfile),
            *_security_flags(self._cfg, uid, gid),
        ]
        if secret_keys:
            argv += ["--env-file", str(env_file)]
        for key, value in {**_synthetic_env(), **dict(trace_env)}.items():
            argv += ["-e", f"{key}={value}"]
        for key, value in labels.items():
            argv += ["--label", f"{key}={value}"]
        argv.append(self._cfg.image)
        argv += list(req.argv)

        return PreparedRunPlan(
            argv=argv,
            env=dict(host_env),  # docker CLI subprocess env (PATH/DOCKER_HOST/...)
            container_name=name,
            labels=labels,
            env_file_keys=secret_keys,
            cidfile_path=cidfile,
            tmp_dir=tmp_dir,
        )

    def materialize(self, plan: PreparedRunPlan) -> PreparedRun:
        """Create the 0700 tmp-dir and, if a secret is planned, the 0600 env-file."""
        assert plan.tmp_dir is not None
        env_file: Path | None = None
        try:
            plan.tmp_dir.mkdir(parents=True, exist_ok=True)
            plan.tmp_dir.chmod(0o700)
            if plan.env_file_keys:
                env_file = plan.tmp_dir / "env"
                write_env_file(env_file, plan.env_file_keys, os.environ)
        except Exception:
            shutil.rmtree(plan.tmp_dir, ignore_errors=True)
            if env_file is not None:
                env_file.unlink(missing_ok=True)
            raise
        cleanup = [plan.tmp_dir] + ([env_file] if env_file is not None else [])
        return PreparedRun(plan=plan, env_file=env_file, cleanup_paths=cleanup)

    def transport_ref(self, prepared: PreparedRun, pid: int) -> str:  # noqa: ARG002
        """Return the docker transport reference for this run's container."""
        return f"docker:{prepared.plan.container_name}"

    async def after_spawn(
        self, prepared: PreparedRun, proc: asyncio.subprocess.Process
    ) -> None:
        """Bounded-wait for the cidfile, then eagerly unlink the env-file.

        The credential is consumed by `docker run --env-file` at container
        creation (cidfile appears). Deleting it then shrinks the on-disk
        window to the spawn window only (spec §3.5/§7.3). If the cidfile
        never appears within the bound (timeout / early process exit), raise
        VerifierLaunchError so LocalBackend.run fails closed.
        """
        cidfile = prepared.plan.cidfile_path
        if cidfile is None:  # not a verifier run; nothing to do
            return
        deadline = time.monotonic() + _CIDFILE_WAIT_SECONDS
        while time.monotonic() < deadline:
            if cidfile.exists():
                if prepared.env_file is not None:
                    prepared.env_file.unlink(missing_ok=True)
                return
            if proc.returncode is not None:  # exited before creating the container
                break
            await asyncio.sleep(_CIDFILE_POLL_SECONDS)
        # Fail-closed: never hand back a handle while the credential handoff
        # is unconfirmed. The env-file is removed by LocalBackend.run's
        # _cleanup_prepared on the raised path.
        raise VerifierLaunchError(
            "verifier container never reported a cidfile for "
            f"{prepared.plan.container_name}"
        )

    def wrap(
        self,
        local: LocalTaskHandle,
        prepared: PreparedRun,
        ref: ExecutionHandleRef,
    ) -> TaskHandle:
        """Reuse the general DockerTaskHandle verbatim (shared lifecycle)."""
        from maestro.execution.docker_handle import DockerTaskHandle

        return DockerTaskHandle(
            local=local,
            container_name=prepared.plan.container_name or "",
            expected_labels=prepared.plan.labels,
            cleanup_paths=prepared.cleanup_paths,
            docker=self._docker,
            ref=ref,
        )
