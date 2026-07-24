"""Opt-in end-to-end test for Phase 2b's scope-bounded collect over real SSH.

Sibling to `test_ssh_localhost_e2e.py` (which exercises Mode-2's
`CollectPolicy(mode="whole_worktree")`): this file drives the same real
`SshBackend` stack but with `CollectPolicy(mode="scope_paths", ...)`
(Mode-1 remote), proving the safety property that makes scope-bounded
collect trustworthy — a remote write outside the declared scope is
rejected wholesale (`CollectConflict`), with zero worktree mutation,
rather than silently partially applied.

Gate: identical to `test_ssh_localhost_e2e.py` — skip unless
`MAESTRO_SSH_E2E=1` **and** `ssh -o BatchMode=yes localhost true`
succeeds. The `_GATED` check short-circuits on the env var first, so no
`ssh` subprocess ever runs at import/collection time unless the opt-in
var is already set.
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
async def test_out_of_scope_write_rejected_worktree_unmutated(tmp_path: Path) -> None:
    """A remote write outside `include` raises CollectConflict, no mutation.

    The workload writes BOTH an in-scope file (`out/result.txt`, covered by
    `include=["out/**"]`) and an out-of-scope file (`secret.txt` at the
    worktree root). Even though the in-scope half is "clean", `plan_collect`
    rejects the whole collect because of the out-of-scope change — and
    because it's a preflight-only rejection (no side effects until
    `apply_collect` runs), the local worktree must come out completely
    untouched: no `out/` directory, no `secret.txt`, `a.txt` unchanged.
    """
    from maestro.execution.exec_config import SshTransport
    from maestro.execution.models import CollectPolicy, ExecutionRequest
    from maestro.execution.ssh_backend import SshBackend
    from maestro.execution.ssh_collect import CollectConflict

    wt = tmp_path / "wt"
    _init_committed_worktree(wt)

    workdir_root = tmp_path / "remote"
    workdir_root.mkdir()
    t = SshTransport(type="ssh", host="localhost", workdir_root=str(workdir_root))
    backend = SshBackend("localhost", t, secret_env=[])

    req = ExecutionRequest(
        run_id="task-scope-reject",
        argv=[
            "sh",
            "-c",
            "mkdir -p out && echo hi > out/result.txt && echo leak > secret.txt",
        ],
        workdir=wt,
        log_path=tmp_path / "scope-reject.log",
        collect=CollectPolicy(mode="scope_paths", include=["out/**"]),
        required_tools=[],
        execution_id="e2e-scope-reject",
        entity_kind="task",
        backend_id="localhost",
    )
    handle = await backend.run(req)
    result = await handle.wait()
    assert result.exit_code == 0

    with pytest.raises(CollectConflict):
        await handle.collect()

    # Fail-preserve contract: a rejected collect must leave the worktree
    # exactly as it was before the run — no partial application of the
    # in-scope half.
    assert not (wt / "out").exists()
    assert not (wt / "secret.txt").exists()
    assert (wt / "a.txt").read_text().strip() == "orig"

    await handle.cleanup()


@pytest.mark.skipif(_GATED, reason=skip_reason)
async def test_in_scope_only_write_is_collected(tmp_path: Path) -> None:
    """A remote write fully inside `include` is applied to the worktree."""
    from maestro.execution.exec_config import SshTransport
    from maestro.execution.models import CollectPolicy, ExecutionRequest
    from maestro.execution.ssh_backend import SshBackend

    wt = tmp_path / "wt"
    _init_committed_worktree(wt)

    workdir_root = tmp_path / "remote"
    workdir_root.mkdir()
    t = SshTransport(type="ssh", host="localhost", workdir_root=str(workdir_root))
    backend = SshBackend("localhost", t, secret_env=[])

    req = ExecutionRequest(
        run_id="task-scope-ok",
        argv=["sh", "-c", "mkdir -p out && echo hi > out/result.txt"],
        workdir=wt,
        log_path=tmp_path / "scope-ok.log",
        collect=CollectPolicy(mode="scope_paths", include=["out/**"]),
        required_tools=[],
        execution_id="e2e-scope-ok",
        entity_kind="task",
        backend_id="localhost",
    )
    handle = await backend.run(req)
    result = await handle.wait()
    assert result.exit_code == 0

    await handle.collect()
    assert (wt / "out" / "result.txt").read_text().strip() == "hi"
    assert (wt / "a.txt").read_text().strip() == "orig"

    await handle.cleanup()
