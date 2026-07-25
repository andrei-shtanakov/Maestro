"""Opt-in end-to-end test for SSH *validation* over real `ssh localhost`.

Mirrors `test_ssh_localhost_e2e.py::test_localhost_run_collect_and_real_cleanup`
but exercises the PR2 validation shape instead of the task shape: the
validation command runs on the SSH backend with `capture_output=True` (so the
result is reported, not just pass/fail) and `CollectPolicy(mode="none")` (a
validation run must never write task changes back — `SshTaskHandle.collect`
is a true no-op for `mode="none"`, see `ssh_handle.py`). The real guarded
remote cleanup (ownership-checked `rm -rf`) is exercised end-to-end.

Gate: skip unless `MAESTRO_SSH_E2E=1` **and** `ssh -o BatchMode=yes localhost
true` succeeds (mirrors `tests/test_docker_integration.py`'s docker gate).
Without passwordless localhost sshd, this skips cleanly in CI/dev — the
`_GATED` check below short-circuits on the env var, so no `ssh` subprocess
ever runs at import/collection time unless the opt-in var is already set.
"""

import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.anyio

_GATED = os.environ.get("MAESTRO_SSH_E2E") != "1" or (
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "localhost", "true"],
        capture_output=True,
    ).returncode
    != 0
)
skip_reason = "set MAESTRO_SSH_E2E=1 and enable passwordless `ssh localhost`"


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


@pytest.mark.skipif(_GATED, reason=skip_reason)
async def test_localhost_validation_capture_no_collect_and_real_cleanup(
    tmp_path: Path,
) -> None:
    """Validate over real localhost SSH: capture output, collect=none, cleanup."""
    from maestro.execution.exec_config import SshTransport
    from maestro.execution.models import CollectPolicy, ExecutionRequest
    from maestro.execution.ssh_backend import SshBackend

    wt = tmp_path / "wt"
    _init_committed_worktree(wt)

    workdir_root = tmp_path / "remote"
    workdir_root.mkdir()
    t = SshTransport(type="ssh", host="localhost", workdir_root=str(workdir_root))
    backend = SshBackend("localhost", t, secret_env=[])

    # The validation command: it also mutates the remote worktree, but
    # collect=none means those mutations must never come back locally.
    req = ExecutionRequest(
        run_id="val-ws",
        argv=["sh", "-c", "echo mutated > a.txt; echo VALIDATION-OK"],
        workdir=wt,
        log_path=tmp_path / "val.log",
        capture_output=True,
        collect=CollectPolicy(mode="none"),
        required_tools=[],
        execution_id="e2e-val1",
        entity_kind="task",
        backend_id="localhost",
    )
    handle = await backend.run(req)
    result = await handle.wait()
    assert result.exit_code == 0
    assert "VALIDATION-OK" in result.stdout_tail

    collect_result = await handle.collect()
    assert collect_result.applied is False
    assert collect_result.detail == "collect=none: no-op"
    # No collect applied: the local worktree is untouched by the remote run.
    assert (wt / "a.txt").read_text().strip() == "orig"

    # Real guarded cleanup over localhost SSH: remote tmp actually removed.
    remote_root = workdir_root / "maestro-exec-e2e-val1"
    assert remote_root.exists()
    await handle.cleanup()
    assert not remote_root.exists()
