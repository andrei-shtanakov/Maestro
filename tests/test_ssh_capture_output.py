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


@pytest.mark.anyio
async def test_final_tail_catch_up_populates_stdout_tail(tmp_path):
    """Prove the final _tail_log() in wait() fetches fresh remote bytes.

    Configures fake ssh so the final tail in wait() returns content NOT
    pre-written to the log. The ONLY way those bytes reach stdout_tail is
    via the final _tail_log() call in wait(). If that call is removed,
    this test fails.
    """
    seq = {"tail_calls": 0}

    class CountingFakeSsh:
        def __init__(self):
            self.calls: list[list[str]] = []

        async def __call__(self, argv, stdin):
            self.calls.append(argv)
            j = " ".join(argv)

            # Count tail -c calls; return fresh bytes only on second+ calls
            if "tail" in j and "-c" in j:
                seq["tail_calls"] += 1
                # First tail (monitor loop): returns empty
                # Second+ tail (final in wait()): returns fresh bytes
                if seq["tail_calls"] >= 2:
                    return RunResult(0, "late remote bytes\n", "")
                return RunResult(0, "", "")

            # Status marker: becomes available after first tail
            if ".status" in j:
                return RunResult(
                    0,
                    json.dumps(
                        {"pid": 5, "pgid": 5, "exit_code": 0, "completed_at": 1.0}
                    ),
                    "",
                )

            return RunResult(0, "", "")

    fake = CountingFakeSsh()
    ssh = SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=fake)
    layout = remote_layout("/w", "e1")
    h = SshTaskHandle(
        ssh,
        layout,
        _ref(),
        log_path=tmp_path / "log",
        timeout_seconds=None,
        collect_spec=CollectSpec(tmp_path / "wt", tmp_path / "st", tmp_path / "j", {}),
        poll_interval=0.01,
        capture_output=True,
    )

    h.start()
    result = await h.wait()

    # The final tail catch-up MUST have been called and populated stdout_tail
    assert "late remote bytes" in result.stdout_tail
    assert result.stderr_tail == ""
    # Verify that at least 2 tail calls were made (monitor + final wait)
    assert seq["tail_calls"] >= 2


def test_tail_text_reads_only_last_limit_bytes_of_large_log(tmp_path):
    """A log larger than `_TAIL_LIMIT` is tailed by seeking from the end —
    only the final `_TAIL_LIMIT` bytes are returned, never the whole file
    (guards the seek offset math, not just the small-file path)."""
    from maestro.execution.ssh_handle import _TAIL_LIMIT, _tail_text

    p = tmp_path / "big.log"
    body = ("A" * (_TAIL_LIMIT * 3)) + "TAILMARKER\n"
    p.write_text(body)

    tail = _tail_text(p)

    assert len(tail.encode("utf-8")) == _TAIL_LIMIT
    assert tail.endswith("TAILMARKER\n")
    # body is pure ASCII, so the last _TAIL_LIMIT bytes == the last chars.
    assert tail == body[-_TAIL_LIMIT:]


def test_tail_text_missing_file_returns_empty(tmp_path):
    """Missing/unreadable file decodes to "" (never raises)."""
    from maestro.execution.ssh_handle import _tail_text

    assert _tail_text(tmp_path / "does-not-exist.log") == ""
