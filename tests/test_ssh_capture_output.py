"""Tests for SSH capture-output: `wait()` returns a bounded combined
stdout_tail from the local mirror log when `capture_output=True`, and an
empty tail otherwise. The remote supervisor merges stdout+stderr into one
log, so `stderr_tail` always stays "" — driven by a fake runner, no real
sshd required.
"""

import json

import pytest

from maestro.execution.exec_config import SshTransport
from maestro.execution.models import ExecutionHandleRef
from maestro.execution.ssh_cli import RunResult, SshCli
from maestro.execution.ssh_handle import CollectSpec, SshTaskHandle
from maestro.execution.ssh_launch import remote_layout


def _ref() -> ExecutionHandleRef:
    from datetime import UTC, datetime

    return ExecutionHandleRef(
        backend_id="gpu", run_id="api", transport_ref="{}", started_at=datetime.now(UTC)
    )


class FakeSsh:
    """Scripts responses by matching a substring in the joined remote argv."""

    def __init__(self, responses):
        self._responses = responses
        self.calls: list[list[str]] = []

    async def __call__(self, argv, stdin):
        self.calls.append(argv)
        for needle, result in self._responses:
            if needle in " ".join(argv):
                return result
        return RunResult(0, "", "")


def _make_handle(tmp_path, *, capture_output: bool) -> SshTaskHandle:
    status = json.dumps({"pid": 5, "pgid": 5, "exit_code": 0, "completed_at": 1.0})
    # Needle is ".status" (not the bare "cat" used elsewhere) so the guarded
    # ssh opts' "...Authentication..." (which contains the substring "cat")
    # never accidentally matches the log-tail command too.
    fake = FakeSsh([(".status", RunResult(0, status, ""))])
    ssh = SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=fake)
    layout = remote_layout("/w", "e1")
    return SshTaskHandle(
        ssh,
        layout,
        _ref(),
        log_path=tmp_path / "log",
        timeout_seconds=None,
        collect_spec=CollectSpec(tmp_path / "wt", tmp_path / "st", tmp_path / "j", {}),
        poll_interval=0.01,
        capture_output=capture_output,
    )


@pytest.mark.anyio
async def test_wait_returns_combined_tail_when_capture_output(tmp_path):
    h = _make_handle(tmp_path, capture_output=True)
    h._log_path.write_text("validation output\n")
    h.start()
    result = await h.wait()
    assert result.stdout_tail == "validation output\n"
    assert result.stderr_tail == ""


@pytest.mark.anyio
async def test_wait_returns_empty_tail_without_capture_output(tmp_path):
    h = _make_handle(tmp_path, capture_output=False)
    h._log_path.write_text("validation output\n")
    h.start()
    result = await h.wait()
    assert result.stdout_tail == ""
    assert result.stderr_tail == ""
