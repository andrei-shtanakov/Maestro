import pytest

from maestro.execution.exec_config import SshTransport
from maestro.execution.ssh_cli import RunResult, SshCli
from maestro.execution.ssh_docker import build_docker_run_argv, resolve_effective_user


def _build(**over):
    kw = {
        "execution_id": "exec1",
        "entity_kind": "workstream",
        "entity_id": "api",
        "attempt": 1,
        "backend_id": "remote-sandbox",
        "image": "img:tag",
        "remote_repo": "/var/tmp/maestro/maestro-exec-exec1/repo",
        "remote_root": "/var/tmp/maestro/maestro-exec-exec1",
        "remote_env_file": "/var/tmp/maestro/maestro-exec-exec1/env",
        "effective_user": "1000:1000",
        "network": "none",
        "memory": "8g",
        "cpus": None,
        "inline_env": {"TRACEPARENT": "00-abc-def-01"},
        "has_secret_env_file": True,
        "inner_argv": ["spec-runner", "run", "--all"],
    }
    kw.update(over)
    return build_docker_run_argv(**kw)


def test_argv_shape_and_no_rm():
    argv, _ = _build()
    assert argv[0:2] == ["docker", "run"]
    assert "--rm" not in argv
    assert "--name" in argv and "maestro-exec1" in argv
    assert "-v" in argv and "/var/tmp/maestro/maestro-exec-exec1/repo:/work" in argv
    assert "-w" in argv and "/work" in argv
    assert "--user" in argv and "1000:1000" in argv
    assert "--network" in argv and "none" in argv
    assert "--memory" in argv and "8g" in argv
    assert "--cpus" not in argv  # None omitted
    # image then inner argv are the tail
    assert argv[-4:] == ["img:tag", "spec-runner", "run", "--all"]


def test_secret_via_env_file_and_trace_inlined():
    argv, _ = _build()
    assert "--env-file" in argv
    assert "/var/tmp/maestro/maestro-exec-exec1/env" in argv
    # trace env inlined as -e KEY=VALUE, not in a file
    joined = " ".join(argv)
    assert "-e TRACEPARENT=00-abc-def-01" in joined


def test_no_env_file_when_no_secrets():
    argv, _ = _build(has_secret_env_file=False)
    assert "--env-file" not in argv


def test_labels_full_set():
    _, labels = _build()
    assert labels == {
        "maestro.execution_id": "exec1",
        "maestro.entity_kind": "workstream",
        "maestro.entity_id": "api",
        "maestro.attempt": "1",
        "maestro.backend_id": "remote-sandbox",
    }
    argv, _ = _build()
    assert "--label" in argv and "maestro.execution_id=exec1" in argv


def test_root_opt_in_user_honored():
    argv, _ = _build(effective_user="0:0")
    assert "0:0" in argv


def _ssh(runner):
    return SshCli(SshTransport(type="ssh", host="h", workdir_root="/r"), runner=runner)


@pytest.mark.anyio
async def test_resolve_effective_user_explicit_wins():
    async def runner(argv, stdin):  # should never be called
        raise AssertionError("no probe when user is explicit")

    assert await resolve_effective_user(_ssh(runner), "1000:1000") == "1000:1000"


@pytest.mark.anyio
async def test_resolve_effective_user_probes_remote():
    calls = []

    async def runner(argv, stdin):
        calls.append(argv[-1])
        val = "1000" if "id -u" in argv[-1] else "2000"
        return RunResult(0, val + "\n", "")

    assert await resolve_effective_user(_ssh(runner), None) == "1000:2000"


@pytest.mark.anyio
async def test_resolve_effective_user_probe_failure_raises():
    async def runner(argv, stdin):
        return RunResult(1, "", "nope")

    with pytest.raises(RuntimeError, match="resolve remote uid"):
        await resolve_effective_user(_ssh(runner), None)
