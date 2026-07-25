import pytest

from maestro.execution.docker_cli import DockerCli
from maestro.execution.exec_config import (
    BackendSpec,
    BareIsolation,
    DockerConfig,
    DockerIsolation,
    ExecutionConfig,
    SshTransport,
)
from maestro.execution.isolators import DockerIsolator
from maestro.execution.local import LocalBackend
from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.execution.resolver import BackendResolver, ExecutionConfigError
from maestro.execution.ssh_backend import SshBackend


class FakeDockerCli(DockerCli):
    """Scripted `DockerCli` double: no subprocess, no daemon.

    Subclasses the real `DockerCli` (rather than duck-typing a bare class)
    so it satisfies `LocalBackend`'s/`DockerIsolator`'s `DockerCli`-typed
    parameters under nominal typing. Deliberately skips
    `DockerCli.__init__` (which wires up a real subprocess `run_cmd`) and
    overrides only `version_ok`/`image_exists` — the two methods
    `LocalBackend`'s docker-aware `healthcheck`/`can_run` call.
    """

    def __init__(
        self,
        *,
        version_ok: bool = True,
        image_exists: bool = True,
        version_ok_raises: Exception | None = None,
    ) -> None:
        self._version_ok = version_ok
        self._image_exists = image_exists
        self._version_ok_raises = version_ok_raises

    async def version_ok(self) -> bool:
        if self._version_ok_raises is not None:
            raise self._version_ok_raises
        return self._version_ok

    async def image_exists(self, image: str) -> bool:
        del image
        return self._image_exists


def test_no_execution_config_resolves_local():
    r = BackendResolver(None)
    assert r.default_name == "local"
    assert isinstance(r.resolve(None), LocalBackend)
    assert isinstance(r.resolve("local"), LocalBackend)


def test_unknown_backend_name_fails_fast():
    r = BackendResolver(ExecutionConfig())
    with pytest.raises(ExecutionConfigError):
        r.resolve("gpu-box")


def test_docker_without_docker_config_fails_fast():
    r = BackendResolver(ExecutionConfig(default_backend="local"))
    with pytest.raises(ExecutionConfigError):
        r.resolve("docker")


def test_local_instance_is_cached():
    r = BackendResolver(None)
    assert r.resolve("local") is r.resolve("local")


def test_resolve_uses_entity_backend_then_default():
    """Documents the dispatch contract both loops rely on: an entity's own
    `backend` wins when set, otherwise the configured default is used.
    """
    r = BackendResolver(ExecutionConfig(default_backend="local"))
    # entity backend None -> default_name
    assert r.resolve(None).id == "local"
    # explicit local
    assert r.resolve("local").id == "local"


def test_docker_resolves_when_config_present():
    r = BackendResolver(ExecutionConfig(docker=DockerConfig(image="maestro-runner:x")))
    backend = r.resolve("docker")
    assert backend.id == "docker"


@pytest.mark.anyio
async def test_docker_healthcheck_rejects_ssh_docker_host(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "ssh://gpu-box")
    r = BackendResolver(ExecutionConfig(docker=DockerConfig(image="i")))
    backend = r.resolve("docker")
    health = await backend.healthcheck()
    assert health.reachable is False
    assert "DOCKER_HOST" in health.detail


@pytest.mark.anyio
async def test_docker_healthcheck_rejects_tcp_docker_host(monkeypatch):
    """Mirrors the ssh:// test — tcp:// is remote too, Phase 1 is local only."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://gpu-box:2375")
    docker = FakeDockerCli()
    cfg = DockerConfig(image="maestro-runner:x")
    backend = LocalBackend(
        DockerIsolator(cfg, docker=docker), backend_id="docker", docker=docker
    )
    health = await backend.healthcheck()
    assert health.reachable is False
    assert "DOCKER_HOST" in health.detail


@pytest.mark.anyio
async def test_docker_healthcheck_daemon_unreachable(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    docker = FakeDockerCli(version_ok=False)
    cfg = DockerConfig(image="maestro-runner:x")
    backend = LocalBackend(
        DockerIsolator(cfg, docker=docker), backend_id="docker", docker=docker
    )
    health = await backend.healthcheck()
    assert health.reachable is False
    assert "daemon" in health.detail


@pytest.mark.anyio
async def test_docker_healthcheck_subprocess_error_returns_unreachable(monkeypatch):
    """A `version_ok()` that raises (e.g. `FileNotFoundError` when the
    `docker` binary is missing) must not escape `healthcheck()` — it should
    be reported as unreachable, not propagate into the scheduler/orchestrator
    spawn flow."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    docker = FakeDockerCli(version_ok_raises=FileNotFoundError("docker not found"))
    cfg = DockerConfig(image="maestro-runner:x")
    backend = LocalBackend(
        DockerIsolator(cfg, docker=docker), backend_id="docker", docker=docker
    )
    health = await backend.healthcheck()
    assert health.reachable is False
    assert "docker unreachable" in (health.detail or "")


@pytest.mark.anyio
async def test_docker_healthcheck_reachable_when_daemon_ok(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    docker = FakeDockerCli(version_ok=True)
    cfg = DockerConfig(image="maestro-runner:x")
    backend = LocalBackend(
        DockerIsolator(cfg, docker=docker), backend_id="docker", docker=docker
    )
    health = await backend.healthcheck()
    assert health.reachable is True


def _minimal_request(tmp_path) -> ExecutionRequest:
    """A request with the image-gated docker `can_run` path never inspects
    workdir/argv/etc, but the model requires them.
    """
    return ExecutionRequest(
        run_id="t1",
        argv=["true"],
        workdir=tmp_path,
        log_path=tmp_path / "log.txt",
        collect=CollectPolicy(mode="none"),
    )


@pytest.mark.anyio
async def test_docker_can_run_missing_image(tmp_path):
    docker = FakeDockerCli(image_exists=False)
    cfg = DockerConfig(image="maestro-runner:x")
    backend = LocalBackend(
        DockerIsolator(cfg, docker=docker), backend_id="docker", docker=docker
    )
    cap = await backend.can_run(_minimal_request(tmp_path))
    assert cap.ok is False
    assert cap.missing_tools == ["image:maestro-runner:x"]


@pytest.mark.anyio
async def test_docker_can_run_image_present(tmp_path):
    docker = FakeDockerCli(image_exists=True)
    cfg = DockerConfig(image="maestro-runner:x")
    backend = LocalBackend(
        DockerIsolator(cfg, docker=docker), backend_id="docker", docker=docker
    )
    cap = await backend.can_run(_minimal_request(tmp_path))
    assert cap.ok is True


def test_resolve_ssh_docker_backend():
    cfg = ExecutionConfig(
        default_backend="local",
        backends={
            "rs": BackendSpec(
                transport=SshTransport(type="ssh", host="h", workdir_root="/r"),
                isolation=DockerIsolation(type="docker", image="img:tag"),
                secret_env=["ANTHROPIC_API_KEY"],
            )
        },
    )
    backend = BackendResolver(cfg).resolve("rs")
    assert isinstance(backend, SshBackend)
    assert backend.isolation_kind == "docker"


def test_resolve_ssh_bare_backend():
    cfg = ExecutionConfig(
        default_backend="local",
        backends={
            "rb": BackendSpec(
                transport=SshTransport(type="ssh", host="h", workdir_root="/r"),
                isolation=BareIsolation(),
            )
        },
    )
    backend = BackendResolver(cfg).resolve("rb")
    assert isinstance(backend, SshBackend)
    assert backend.isolation_kind == "bare"
