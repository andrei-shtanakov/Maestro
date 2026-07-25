"""Test that execution_id is carried on every handle ref."""

from pathlib import Path

import pytest

from maestro.execution.local import LocalBackend
from maestro.execution.models import CollectPolicy, ExecutionRequest


pytestmark = pytest.mark.anyio


async def test_local_handle_ref_carries_execution_id(tmp_path: Path) -> None:
    """Test that LocalBackend.run fills ref.execution_id from ExecutionRequest."""
    req = ExecutionRequest(
        run_id="t1",
        argv=["true"],
        workdir=tmp_path,
        log_path=tmp_path / "l.log",
        collect=CollectPolicy(mode="none"),
        backend_id="local",
        execution_id="exec-123",
    )
    handle = await LocalBackend().run(req)
    try:
        assert handle.ref.execution_id == "exec-123"
    finally:
        await handle.wait()
