"""Opt-in end-to-end test for `SshBackend` over real `ssh localhost` with
REAL Docker isolation on the remote (here, local) side.

Sibling to `test_ssh_localhost_e2e.py` (bare isolation, `whole_worktree`
collect) and `test_mode1_ssh_scope_collect_e2e.py` (bare isolation,
`scope_paths` collect): this file drives the same real `SshBackend` stack
but with `DockerIsolation` configured, proving the full SSH+Docker path
(Phase 2c, Tasks 1-11) end-to-end — a real container is launched over SSH,
its write lands back in the worktree via scope-bounded collect, and both
the container and the remote root are gone after cleanup.

Gate: reuses `test_ssh_localhost_e2e.py`'s `MAESTRO_SSH_E2E` opt-in (one
env var enables every ssh e2e in this directory) **and** requires a real
docker daemon (mirrors `tests/test_docker_integration.py`'s docker gate).
Both the `ssh -o BatchMode=yes localhost true` probe and the `docker
version` probe run lazily inside the test body — never at import/collection
time — so a machine missing either precondition skips cleanly.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.anyio

_SSH_E2E_ENABLED = os.environ.get("MAESTRO_SSH_E2E") == "1"
skip_reason = (
    "set MAESTRO_SSH_E2E=1, enable passwordless `ssh localhost`, and have a "
    "docker daemon running"
)


def _init_committed_worktree(wt: Path) -> None:
    """Git-init `wt` with one committed file (sync helper; no async-blocking)."""
    wt.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=wt, check=True)
    (wt / "a.txt").write_text("orig")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
        ],
        cwd=wt,
        check=True,
    )


def _preconditions_met() -> bool:
    """Runtime-only check: opt-in var, real localhost ssh, real docker daemon.

    Never called at import/collection time — only from inside a test body —
    so a machine lacking ssh/docker never shells out during collection.
    """
    if not _SSH_E2E_ENABLED:
        return False
    if not shutil.which("ssh") or not shutil.which("docker"):
        return False
    ssh_ok = (
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "localhost", "true"],
            capture_output=True,
        ).returncode
        == 0
    )
    docker_ok = (
        subprocess.run(["docker", "version"], capture_output=True).returncode == 0
    )
    return ssh_ok and docker_ok


def _ps_by_execution_id(execution_id: str) -> str:
    """Return `docker ps -a` stdout filtered to a maestro.execution_id label."""
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=maestro.execution_id={execution_id}",
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


async def test_ssh_docker_end_to_end(tmp_path: Path) -> None:
    """Real SSH + real Docker: run, scope-collect, and verify full cleanup."""
    if not _preconditions_met():
        pytest.skip(skip_reason)

    from maestro.execution.exec_config import DockerIsolation, SshTransport
    from maestro.execution.models import CollectPolicy, ExecutionRequest
    from maestro.execution.ssh_backend import SshBackend

    wt = tmp_path / "wt"
    _init_committed_worktree(wt)

    workdir_root = tmp_path / "remote"
    workdir_root.mkdir()
    t = SshTransport(type="ssh", host="localhost", workdir_root=str(workdir_root))
    execution_id = "e2e-ssh-docker-1"
    backend = SshBackend(
        "localhost",
        t,
        secret_env=[],
        isolation=DockerIsolation(type="docker", image="alpine", network="none"),
    )

    req = ExecutionRequest(
        run_id="ws-ssh-docker",
        argv=["sh", "-c", "mkdir -p src && echo hi > src/out.txt"],
        workdir=wt,
        log_path=tmp_path / "ws.log",
        collect=CollectPolicy(mode="scope_paths", include=["src/**"]),
        required_tools=[],
        execution_id=execution_id,
        entity_kind="workstream",
        backend_id="localhost",
    )
    handle = await backend.run(req)
    result = await handle.wait()
    assert result.exit_code == 0

    await handle.collect()
    assert (wt / "src" / "out.txt").read_text().strip() == "hi"
    assert (wt / "a.txt").read_text().strip() == "orig"

    remote_root = workdir_root / f"maestro-exec-{execution_id}"
    assert remote_root.exists()

    await handle.cleanup()

    assert not remote_root.exists()
    assert _ps_by_execution_id(execution_id) == ""
