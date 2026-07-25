import json

import pytest

from maestro.execution.docker_cli import DockerCli


class _FakeDocker:
    """Records argv and returns scripted (rc, out, err)."""

    def __init__(self, script: list[tuple[int, str, str]]) -> None:
        self.script = script  # list[tuple[rc, out, err]]
        self.calls: list[list[str]] = []
        self._i = 0

    async def __call__(
        self,
        argv: list[str],
        timeout: float | None,  # noqa: ASYNC109
    ) -> tuple[int, str, str]:
        self.calls.append(argv)
        rc, out, err = self.script[self._i]
        self._i += 1
        return rc, out, err


@pytest.mark.anyio
async def test_inspect_returns_none_when_absent():
    fake = _FakeDocker([(1, "", "No such object: maestro-x")])
    cli = DockerCli(run_cmd=fake)
    assert await cli.inspect("maestro-x") is None
    assert fake.calls[0][:2] == ["docker", "inspect"]


@pytest.mark.anyio
async def test_inspect_parses_json():
    payload = json.dumps(
        {"Id": "abc", "Config": {"Labels": {"maestro.execution_id": "e1"}}}
    )
    cli = DockerCli(run_cmd=_FakeDocker([(0, payload, "")]))
    got = await cli.inspect("maestro-e1")
    assert got is not None
    assert got["Config"]["Labels"]["maestro.execution_id"] == "e1"


@pytest.mark.anyio
async def test_ps_ids_by_label_splits_lines():
    fake = _FakeDocker([(0, "id1\nid2\n", "")])
    cli = DockerCli(run_cmd=fake)
    ids = await cli.ps_ids_by_label("maestro.execution_id", "e1")
    assert ids == ["id1", "id2"]
    # Assert -a flag is present (recovery-critical)
    assert fake.calls[0] == [
        "docker",
        "ps",
        "-a",
        "-q",
        "--filter",
        "label=maestro.execution_id=e1",
    ]


@pytest.mark.anyio
async def test_inspect_raises_on_daemon_error():
    fake = _FakeDocker(
        [(1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock")]
    )
    cli = DockerCli(run_cmd=fake)
    with pytest.raises(RuntimeError, match="docker inspect"):
        await cli.inspect("maestro-x")


@pytest.mark.anyio
async def test_inspect_json_decode_error():
    fake = _FakeDocker([(0, "not json{", "")])
    cli = DockerCli(run_cmd=fake)
    assert await cli.inspect("maestro-x") is None


@pytest.mark.anyio
async def test_rm_is_forced():
    fake = _FakeDocker([(0, "", "")])
    await DockerCli(run_cmd=fake).rm("maestro-e1")
    assert fake.calls[0] == ["docker", "rm", "-f", "maestro-e1"]


@pytest.mark.anyio
async def test_version_ok_true_on_rc_0():
    fake = _FakeDocker([(0, "Docker version 20.10.0", "")])
    cli = DockerCli(run_cmd=fake)
    assert await cli.version_ok() is True
    assert fake.calls[0] == ["docker", "version"]


@pytest.mark.anyio
async def test_version_ok_false_on_error():
    fake = _FakeDocker([(1, "", "docker: not found")])
    cli = DockerCli(run_cmd=fake)
    assert await cli.version_ok() is False


@pytest.mark.anyio
async def test_image_exists_true_on_rc_0():
    fake = _FakeDocker([(0, '{"Id":"sha256:abc"}', "")])
    cli = DockerCli(run_cmd=fake)
    assert await cli.image_exists("my-image") is True
    assert fake.calls[0] == ["docker", "image", "inspect", "my-image"]


@pytest.mark.anyio
async def test_image_exists_false_on_error():
    fake = _FakeDocker([(1, "", "Error: No such image")])
    cli = DockerCli(run_cmd=fake)
    assert await cli.image_exists("missing-image") is False


@pytest.mark.anyio
async def test_stop_with_timeout():
    fake = _FakeDocker([(0, "", "")])
    cli = DockerCli(run_cmd=fake)
    await cli.stop("maestro-e1", 5.0)
    assert fake.calls[0] == ["docker", "stop", "-t", "5", "maestro-e1"]


@pytest.mark.anyio
async def test_kill_container():
    fake = _FakeDocker([(0, "", "")])
    cli = DockerCli(run_cmd=fake)
    await cli.kill("maestro-e1")
    assert fake.calls[0] == ["docker", "kill", "maestro-e1"]


def _cli(rc: int, err: str = "boom"):
    async def run_cmd(argv, timeout):  # noqa: ASYNC109
        return rc, "", err

    return DockerCli(run_cmd=run_cmd)


@pytest.mark.anyio
async def test_rm_raises_on_nonzero_rc():
    with pytest.raises(RuntimeError, match="docker rm"):
        await _cli(1).rm("maestro-abc")


@pytest.mark.anyio
async def test_stop_raises_on_nonzero_rc():
    with pytest.raises(RuntimeError, match="docker stop"):
        await _cli(1).stop("maestro-abc", 10.0)


@pytest.mark.anyio
async def test_kill_raises_on_nonzero_rc():
    with pytest.raises(RuntimeError, match="docker kill"):
        await _cli(1).kill("maestro-abc")


@pytest.mark.anyio
async def test_rm_ok_on_zero_rc():
    await _cli(0).rm("maestro-abc")  # no raise
