import sys
from pathlib import Path

import pytest

from maestro.execution.local import LocalBackend
from maestro.execution.models import CollectPolicy, ExecutionRequest


@pytest.mark.anyio
async def test_local_backend_runs_and_reaps(tmp_path: Path):
    log = tmp_path / "log.txt"
    req = ExecutionRequest(
        run_id="t1",
        # sys.executable (an absolute path) skips PATH lookup entirely, so
        # this is environment-agnostic even though inherit_env=False strips
        # PATH from the child's env (see the isolator seam's allowlist).
        argv=[sys.executable, "-c", "print('hello-local')"],
        workdir=tmp_path,
        log_path=log,
        collect=CollectPolicy(mode="none"),
    )
    handle = await LocalBackend().run(req)
    result = await handle.wait()
    assert result.exit_code == 0
    assert "hello-local" in log.read_text()


@pytest.mark.anyio
async def test_local_backend_passes_allowlisted_secret_not_full_env(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("MY_SECRET", "s3cr3t")
    monkeypatch.setenv("MY_OTHER", "leak")
    log = tmp_path / "log.txt"
    req = ExecutionRequest(
        run_id="t2",
        argv=[
            sys.executable,
            "-c",
            "import os;print(os.environ.get('MY_SECRET'),os.environ.get('MY_OTHER'))",
        ],
        workdir=tmp_path,
        log_path=log,
        secret_env=["MY_SECRET"],
        inherit_env=False,
        collect=CollectPolicy(mode="none"),
    )
    handle = await LocalBackend().run(req)
    await handle.wait()
    # allowlisted secret present, non-allowlisted host var absent
    assert "s3cr3t None" in log.read_text()


@pytest.mark.anyio
async def test_spawn_failure_cleans_materialized_files(tmp_path: Path):
    """Must-have #3: a spawn failure removes files the isolator materialized."""
    from maestro.execution.models import PreparedRun, PreparedRunPlan

    leftover = tmp_path / "env"
    leftover.write_text("SECRET=1")

    class _FakeIso:
        id = "fake"

        def prepare(self, req, *, trace_env, host_env):
            return PreparedRunPlan(argv=["/nonexistent/binary-xyz"], env={})

        def materialize(self, plan):
            return PreparedRun(plan=plan, cleanup_paths=[leftover])

        def transport_ref(self, prepared, pid):
            return f"local_pid:{pid}"

        async def after_spawn(self, prepared, proc):
            return None

        def wrap(self, local, prepared, ref):
            return local

    req = ExecutionRequest(
        run_id="t3",
        argv=["ignored"],
        workdir=tmp_path,
        log_path=tmp_path / "log",
        collect=CollectPolicy(mode="none"),
    )
    with pytest.raises(FileNotFoundError):
        await LocalBackend(_FakeIso()).run(req)
    assert not leftover.exists()  # spawn-failure path unlinked it
