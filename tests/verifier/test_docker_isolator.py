import asyncio
from pathlib import Path

import pytest

from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.verifier.docker_backend import (
    ANTHROPIC_ENV_KEY,
    VerifierDockerIsolator,
    verifier_exec_dir,
)
from maestro.verifier.docker_config import VerifierDockerConfig


pytestmark = pytest.mark.anyio

_DIGEST = "example.com/img@sha256:" + "a" * 64


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _req(execution_id="e1", workdir=Path("/tmp/scratch-x")):
    return ExecutionRequest(
        run_id="verify-t1-1",
        argv=["claude", "-p", "PROMPT", "--output-format", "json", "--model", "m"],
        workdir=workdir,
        log_path=workdir / "judge.log",
        stdin="ENVELOPE",
        collect=CollectPolicy(mode="none"),
        execution_id=execution_id,
        entity_kind="task",
        attempt=1,
        backend_id="verifier-docker",
    )


def _iso(tmp_path):
    cfg = VerifierDockerConfig(image=_DIGEST, user="1000:1000")
    return VerifierDockerIsolator(cfg, exec_root=tmp_path)


def test_production_argv_has_full_hardening(tmp_path):
    iso = _iso(tmp_path)
    plan = iso.prepare(_req(), trace_env={}, host_env={ANTHROPIC_ENV_KEY: "sk-x"})
    a = plan.argv
    assert a[0:2] == ["docker", "run"]
    assert "-i" in a
    assert "--read-only" in a
    assert "--cap-drop=ALL" in a
    assert "--security-opt=no-new-privileges" in a
    assert "--user" in a and "1000:1000" in a
    assert "--workdir" in a and "/scratch" in a
    assert "--network" in a and "bridge" in a
    assert "--pids-limit" in a
    # tmpfs owned by the effective user, noexec/nosuid/nodev
    tmpfs = a[a.index("--tmpfs") + 1]
    assert tmpfs.startswith("/scratch:rw,nosuid,nodev,noexec,")
    assert "mode=0700" in tmpfs and "uid=1000" in tmpfs and "gid=1000" in tmpfs
    # NO project bind mount
    assert not any(tok == "-v" for tok in a)
    # digest image present, judge command tail present
    assert _DIGEST in a
    assert a[-7:] == [
        "claude",
        "-p",
        "PROMPT",
        "--output-format",
        "json",
        "--model",
        "m",
    ]


def test_synthetic_env_no_host_passthrough(tmp_path):
    iso = _iso(tmp_path)
    plan = iso.prepare(
        _req(),
        trace_env={},
        host_env={ANTHROPIC_ENV_KEY: "sk-x", "HOME": "/Users/real", "GH_TOKEN": "t"},
    )
    joined = " ".join(plan.argv)
    assert "HOME=/scratch" in joined
    assert "XDG_CONFIG_HOME=/scratch/.config" in joined
    assert "/Users/real" not in joined  # host HOME never leaks
    assert "GH_TOKEN" not in joined  # denylisted, never present
    # secret goes via --env-file, value never in argv
    assert "--env-file" in plan.argv
    assert "sk-x" not in joined


def test_deterministic_exec_dir(tmp_path):
    assert verifier_exec_dir(tmp_path, "e1") == tmp_path / "maestro-verify-e1"


async def test_after_spawn_unlinks_env_file_once_cidfile_appears(tmp_path, monkeypatch):
    iso = _iso(tmp_path)
    plan = iso.prepare(_req(), trace_env={}, host_env={ANTHROPIC_ENV_KEY: "sk-x"})
    monkeypatch.setenv(ANTHROPIC_ENV_KEY, "sk-x")
    prepared = iso.materialize(plan)
    assert prepared.env_file is not None and prepared.env_file.exists()
    assert plan.cidfile_path is not None
    cidfile = plan.cidfile_path
    proc = await asyncio.create_subprocess_exec("sleep", "5")

    async def _touch_cid():
        await asyncio.sleep(0.2)
        cidfile.write_text("deadbeef")

    task = asyncio.create_task(_touch_cid())
    await iso.after_spawn(prepared, proc)
    await task
    assert not prepared.env_file.exists()  # eagerly unlinked
    proc.kill()
    await proc.wait()


async def test_after_spawn_fails_closed_when_cidfile_never_appears(
    tmp_path, monkeypatch
):
    iso = _iso(tmp_path)
    # short bound for the test
    monkeypatch.setattr("maestro.verifier.docker_backend._CIDFILE_WAIT_SECONDS", 0.3)
    plan = iso.prepare(_req(), trace_env={}, host_env={ANTHROPIC_ENV_KEY: "sk-x"})
    monkeypatch.setenv(ANTHROPIC_ENV_KEY, "sk-x")
    prepared = iso.materialize(plan)
    proc = await asyncio.create_subprocess_exec("sleep", "5")
    from maestro.execution.local import VerifierLaunchError

    with pytest.raises(VerifierLaunchError):
        await iso.after_spawn(prepared, proc)
    proc.kill()
    await proc.wait()
