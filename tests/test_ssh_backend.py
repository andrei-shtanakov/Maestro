import json
import subprocess

import pytest

from maestro.execution.exec_config import DockerIsolation, SshTransport
from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.execution.ssh_backend import SshBackend
from maestro.execution.ssh_cli import RunResult
from maestro.execution.ssh_launch import decode_transport_ref, remote_layout


class Recorder:
    def __init__(self):
        self.argvs: list[list[str]] = []

    async def __call__(self, argv, stdin):
        self.argvs.append(argv)
        j = " ".join(argv)
        if "maestro_supervisor.py" in j:
            return RunResult(0, "MAESTRO-SUPERVISOR-READY e\n", "")
        return RunResult(0, "", "")


def _req(tmp_path) -> ExecutionRequest:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "f.txt").write_text("x")
    return ExecutionRequest(
        run_id="api",
        argv=["spec-runner", "run", "--all"],
        workdir=wt,
        log_path=tmp_path / "api.log",
        collect=CollectPolicy(mode="whole_worktree"),
        required_tools=["spec-runner"],
        execution_id="e",
        entity_kind="workstream",
        backend_id="gpu",
    )


@pytest.mark.anyio
async def test_run_awaits_handshake_before_returning(tmp_path, monkeypatch):
    rec = Recorder()
    t = SshTransport(type="ssh", host="gpu", workdir_root="/var/tmp/m")
    backend = SshBackend("gpu", t, secret_env=[], runner=rec)
    # Avoid real git/rsync: monkeypatch the transfer helpers to no-ops that
    # record. (The plan's implementation must route git-bundle/rsync through
    # small injectable seams — see Step 3.)
    monkeypatch.setattr(backend, "_materialize_remote", _fake_async)
    handle = await backend.run(_req(tmp_path))
    joined = [" ".join(a) for a in rec.argvs]
    assert any("maestro_supervisor.py" in j for j in joined)
    assert handle.os_pid is None  # remote


async def _fake_async(*a, **k):
    return None


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


@pytest.mark.anyio
async def test_materialize_remote_removes_local_bundle_after_transfer(tmp_path):
    wt = tmp_path / "gitwt"
    wt.mkdir()
    _git("init", "-q", str(wt))
    _git("-C", str(wt), "config", "user.email", "t@example.com")
    _git("-C", str(wt), "config", "user.name", "t")
    (wt / "f.txt").write_text("x")
    _git("-C", str(wt), "add", ".")
    _git("-C", str(wt), "commit", "-q", "-m", "init")

    rec = Recorder()
    t = SshTransport(type="ssh", host="gpu", workdir_root="/var/tmp/m")
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    backend = SshBackend(
        "gpu", t, secret_env=[], runner=rec, local_staging_root=staging_root
    )
    layout = remote_layout(t.workdir_root, "e")
    req = ExecutionRequest(
        run_id="api",
        argv=["spec-runner", "run", "--all"],
        workdir=wt,
        log_path=tmp_path / "api.log",
        collect=CollectPolicy(mode="whole_worktree"),
        required_tools=["spec-runner"],
        execution_id="e",
        entity_kind="workstream",
        backend_id="gpu",
    )

    await backend._materialize_remote(req, layout)

    bundle = staging_root / "maestro-bundle-e.bundle"
    assert not bundle.exists()


class _FakeRunner:
    """Records tee'd descriptor; answers id/uid and the supervisor handshake."""

    def __init__(self):
        self.descriptor: dict | None = None

    async def __call__(self, argv, stdin):
        tail = argv[-1]
        if "tee" in tail and stdin and stdin.lstrip().startswith("{"):
            self.descriptor = json.loads(stdin)
        if tail.endswith("id -u"):
            return RunResult(0, "1000\n", "")
        if tail.endswith("id -g"):
            return RunResult(0, "1000\n", "")
        if "maestro_supervisor.py" in tail:
            return RunResult(0, "MAESTRO-SUPERVISOR-READY exec1\n", "")
        if ".maestro-owner" in tail:
            return RunResult(0, "exec1\n", "")
        return RunResult(0, "", "")


def _docker_req(tmp_path):
    wd = tmp_path / "wt"
    wd.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=wd, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=wd, check=True
    )
    return ExecutionRequest(
        run_id="api",
        execution_id="exec1",
        entity_kind="workstream",
        argv=["spec-runner", "run", "--all"],
        workdir=wd,
        log_path=tmp_path / "log",
        collect=CollectPolicy(mode="scope_paths", include=["src/**"]),
    )


@pytest.mark.anyio
async def test_docker_run_rewrites_argv_and_ref(tmp_path):
    runner = _FakeRunner()
    backend = SshBackend(
        "remote-sandbox",
        SshTransport(type="ssh", host="h", workdir_root="/var/tmp/maestro"),
        secret_env=["ANTHROPIC_API_KEY"],
        isolation=DockerIsolation(type="docker", image="img:tag", network="none"),
        runner=runner,
    )
    handle = await backend.run(_docker_req(tmp_path))
    desc = runner.descriptor
    assert desc is not None
    assert desc["argv"][0:2] == ["docker", "run"]
    assert desc["argv"][-3:] == ["spec-runner", "run", "--all"]
    assert desc["env_file"].endswith("/noenv")  # secrets NOT sourced by supervisor
    assert "--env-file" in desc["argv"]  # secrets via docker instead
    ref = decode_transport_ref(handle.ref.transport_ref)
    assert ref["isolation"] == "docker"
    assert ref["expected_labels"]["maestro.execution_id"] == "exec1"
    await handle.cleanup()
