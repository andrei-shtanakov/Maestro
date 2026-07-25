"""Tests for `CollectPolicy(mode="none")` as a true no-op on `SshTaskHandle`.

Mirrors the `collect_spec` fixtures in `tests/test_ssh_handle.py`: a fake
`SshCli` runner and a bare `CollectSpec`/`SshTaskHandle` construction, no real
sshd required. `mode="none"` must short-circuit `collect()` before any rsync
or remote command runs; other modes must still rsync (guard case, proving
the short-circuit is mode-specific, not unconditional).
"""

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


class _RaisingSsh:
    """A fake ssh whose `.run`/`.rsync` raise if called at all.

    `collect(mode="none")` must return without touching this fake in any
    way, since it is not a valid `SshCli.run`/`rsync` signature — any call
    is itself proof the short-circuit didn't fire.
    """

    host = "gpu"
    workdir_root = "/w"

    async def run(self, argv, *, stdin=None):
        raise AssertionError(f"ssh.run() must not be called for mode=none: {argv}")

    async def rsync(self, src, dst, *, delete=False, excludes=()):
        raise AssertionError("ssh.rsync() must not be called for mode=none")


def _make_handle(ssh, tmp_path, mode: str) -> SshTaskHandle:
    return SshTaskHandle(
        ssh,
        remote_layout("/w", "e1"),
        _ref(),
        log_path=tmp_path / "log",
        timeout_seconds=None,
        collect_spec=CollectSpec(
            tmp_path / "wt", tmp_path / "st", tmp_path / "j", {}, mode=mode
        ),
        poll_interval=0.01,
    )


@pytest.mark.anyio
async def test_collect_mode_none_is_true_noop(tmp_path):
    ssh = _RaisingSsh()
    h = _make_handle(ssh, tmp_path, mode="none")
    result = await h.collect()
    assert result.applied is False


@pytest.mark.anyio
async def test_collect_mode_whole_worktree_still_rsyncs(tmp_path):
    """Guard: a non-"none" mode still calls rsync, proving the short-circuit
    in `collect()` is keyed on mode, not an unconditional skip."""
    fake = FakeSsh([])
    ssh = SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=fake)
    h = _make_handle(ssh, tmp_path, mode="whole_worktree")
    await h.collect()
    assert any("rsync" in " ".join(c) for c in fake.calls)


class FakeSsh:
    """Scripts responses by matching a substring in the joined remote argv.

    Mirrors `tests/test_ssh_handle.py`'s `FakeSsh`.
    """

    def __init__(self, responses):
        self._responses = responses
        self.calls: list[list[str]] = []

    async def __call__(self, argv, stdin):
        self.calls.append(argv)
        for needle, result in self._responses:
            if needle in " ".join(argv):
                return result
        return RunResult(0, "", "")
