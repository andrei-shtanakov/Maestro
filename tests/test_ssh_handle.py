"""Tests for SshTaskHandle + monitor: cached poll, byte-offset log tail,
status-marker completion, process-group signals, and ownership-checked
cleanup. All driven by a fake runner — no real sshd required.
"""

import json
from typing import ClassVar

import pytest

from maestro.execution.exec_config import SshTransport
from maestro.execution.models import ExecutionHandleRef
from maestro.execution.ssh_cli import RunResult, SshCli
from maestro.execution.ssh_handle import CollectSpec, MirrorSpec, SshTaskHandle
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


@pytest.mark.anyio
async def test_poll_is_cached_and_wait_reads_status(tmp_path, monkeypatch):
    status = json.dumps({"pid": 5, "pgid": 5, "exit_code": 0, "completed_at": 1.0})
    fake = FakeSsh([("cat", RunResult(0, status, ""))])
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
    )
    h.start()
    res = await h.wait()
    assert res.exit_code == 0
    assert h.poll() == 0  # cached, no I/O


@pytest.mark.anyio
async def test_terminate_signals_process_group(tmp_path):
    status = json.dumps({"pid": 5, "pgid": 42, "exit_code": 143, "completed_at": 1.0})
    pidf = json.dumps({"pid": 5, "pgid": 42})
    fake = FakeSsh(
        [("cat", RunResult(0, status, "")), (".pid", RunResult(0, pidf, ""))]
    )
    ssh = SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=fake)
    h = SshTaskHandle(
        ssh,
        remote_layout("/w", "e1"),
        _ref(),
        log_path=tmp_path / "log",
        timeout_seconds=None,
        collect_spec=CollectSpec(tmp_path / "wt", tmp_path / "st", tmp_path / "j", {}),
        poll_interval=0.01,
    )
    await h.terminate(0.01)
    kill_calls = [c for c in fake.calls if "kill" in " ".join(c)]
    assert any("-42" in " ".join(c) for c in kill_calls)  # negative pgid = group


@pytest.mark.anyio
async def test_cleanup_refuses_when_owner_marker_mismatch(tmp_path):
    # owner marker cat returns a DIFFERENT execution_id → refuse rm -rf
    fake = FakeSsh([(".maestro-owner", RunResult(0, "OTHER\n", ""))])
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
        expected_owner="e1",
    )
    with pytest.raises(RuntimeError, match="owner"):
        await h.cleanup()
    assert not any("rm -rf" in " ".join(c) for c in fake.calls)


@pytest.mark.anyio
async def test_monitor_drives_mirror_once_each_tick(tmp_path):
    """Task 16c: with a `mirror_spec` set, the monitor loop calls
    `mirror_once` (remote snapshot + rsync pull) on ticks while running,
    and still reaches terminal without hanging or duplicating the log."""
    status = json.dumps({"pid": 5, "pgid": 5, "exit_code": 0, "completed_at": 1.0})
    fake = FakeSsh(
        [
            ("python3 - ", RunResult(0, "", "")),  # remote SNAPSHOT_SCRIPT run
            ("rsync", RunResult(0, "", "")),  # snapshot pull
            ("cat", RunResult(0, status, "")),  # status marker
        ]
    )
    ssh = SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=fake)
    layout = remote_layout("/w", "e1")
    mirror = MirrorSpec(
        remote_db=f"{layout.repo}/spec/.executor-maestro-state.db",
        remote_snapshot=f"{layout.root}/state-snapshot.db",
        local_target=tmp_path / "mirror" / ".executor-maestro-state.db",
    )
    (tmp_path / "mirror").mkdir()
    h = SshTaskHandle(
        ssh,
        layout,
        _ref(),
        log_path=tmp_path / "log",
        timeout_seconds=None,
        collect_spec=CollectSpec(tmp_path / "wt", tmp_path / "st", tmp_path / "j", {}),
        poll_interval=0.01,
        mirror_spec=mirror,
    )
    h.start()
    res = await h.wait()
    assert res.exit_code == 0  # reached terminal, no hang

    snapshot_calls = [c for c in fake.calls if "python3 -" in " ".join(c)]
    assert snapshot_calls, "mirror_once never ran its remote snapshot step"
    assert mirror.remote_db in " ".join(snapshot_calls[0])
    assert mirror.remote_snapshot in " ".join(snapshot_calls[0])

    rsync_calls = [c for c in fake.calls if "rsync" in " ".join(c)]
    assert any(mirror.remote_snapshot in " ".join(c) for c in rsync_calls)


class _FakeContainerOps:
    """Records call order for stop()/remove() without touching any docker."""

    def __init__(self):
        self.calls: list[str] = []

    async def stop(self, grace):
        self.calls.append("stop")

    async def remove(self):
        self.calls.append("remove")


def _make_handle(ssh, log_path, collect_spec, container_ops):
    """Untyped `container_ops` param: a duck-typed fake, not a real
    `ContainerOps` instance — matches the fake-injection pattern already
    used for `DockerTaskHandle` in `tests/test_docker_handle.py`."""
    return SshTaskHandle(
        ssh,
        remote_layout("/w", "e1"),
        _ref(),
        log_path=log_path,
        timeout_seconds=None,
        collect_spec=collect_spec,
        poll_interval=0.01,
        container_ops=container_ops,
    )


@pytest.mark.anyio
async def test_terminate_stops_container_when_container_ops_set(tmp_path):
    status = json.dumps({"pid": 5, "pgid": 42, "exit_code": 143, "completed_at": 1.0})
    pidf = json.dumps({"pid": 5, "pgid": 42})
    fake = FakeSsh(
        [("cat", RunResult(0, status, "")), (".pid", RunResult(0, pidf, ""))]
    )
    ssh = SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=fake)
    ops = _FakeContainerOps()
    h = _make_handle(
        ssh,
        tmp_path / "log",
        CollectSpec(tmp_path / "wt", tmp_path / "st", tmp_path / "j", {}),
        ops,
    )
    await h.terminate(0.01)
    assert ops.calls == ["stop"]


@pytest.mark.anyio
async def test_cleanup_removes_container_before_remote_rm_rf(tmp_path):
    fake = FakeSsh([])
    ssh = SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=fake)
    order: list[str] = []

    class _RecordingContainerOps(_FakeContainerOps):
        async def remove(self):
            order.append("remove")

    real_run = ssh.run

    async def _recording_ssh_run(argv, *, stdin=None):
        result = await real_run(argv, stdin=stdin)
        if "rm" in argv and "-rf" in argv:
            order.append("rm_rf")
        return result

    ssh.run = _recording_ssh_run  # patch just this instance, keep real behavior
    ops = _RecordingContainerOps()
    h = _make_handle(
        ssh,
        tmp_path / "log",
        CollectSpec(tmp_path / "wt", tmp_path / "st", tmp_path / "j", {}),
        ops,
    )
    await h.cleanup()
    assert order == ["remove", "rm_rf"]


@pytest.mark.anyio
async def test_tail_uses_byte_offset_no_dup(tmp_path):
    # First tail returns "abc", status still absent; second tail returns "" then status.
    seq = {"n": 0}

    class Seq:
        calls: ClassVar[list[list[str]]] = []

        async def __call__(self, argv, stdin):
            self.calls.append(argv)
            j = " ".join(argv)
            if "tail" in j:
                seq["n"] += 1
                return RunResult(0, "abc" if seq["n"] == 1 else "", "")
            if "cat" in j and ".status" in j:
                return RunResult(
                    0,
                    ""
                    if seq["n"] < 2
                    else json.dumps(
                        {"pid": 1, "pgid": 1, "exit_code": 0, "completed_at": 1.0}
                    ),
                    "",
                )
            return RunResult(0, "", "")

    fake = Seq()
    ssh = SshCli(SshTransport(type="ssh", host="gpu", workdir_root="/w"), runner=fake)
    h = SshTaskHandle(
        ssh,
        remote_layout("/w", "e1"),
        _ref(),
        log_path=tmp_path / "log",
        timeout_seconds=None,
        collect_spec=CollectSpec(tmp_path / "wt", tmp_path / "st", tmp_path / "j", {}),
        poll_interval=0.01,
    )
    h.start()
    await h.wait()
    assert (tmp_path / "log").read_text() == "abc"  # written once, no duplication
    # second tail requested a higher offset
    tails = [c for c in fake.calls if "tail" in " ".join(c)]
    assert "+4" in " ".join(tails[1])
