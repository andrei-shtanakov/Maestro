"""Task 7b: `BackendResolver`'s constructor-injected `local_docker`.

Note: several assertions below reach into private attributes
(`LocalBackend._docker`/`._isolator`, `DockerIsolator._cfg`,
`SshBackend.docker`) — that's the only way to observe which concrete
`DockerCli` instance a resolved backend was wired to, which is exactly
what constructor injection is about.

Every *local* docker-isolated backend (legacy `execution.docker` shorthand
or a named `backends.<name>` entry with `isolation: docker`) built by a
resolver constructed with `local_docker=<cli>` shares that same `DockerCli`
instance — both the `LocalBackend` and its paired `DockerIsolator` — so a
caller that owns a test-injectable/long-lived `DockerCli` (Mode-2 startup
recovery, and Mode-1 `StateRecovery` in the next task) can route `docker`
recovery through `backend.probe()` uniformly with every other backend,
instead of hand-composing `probe_execution` against a separately-tracked
client. SSH+Docker backends are unaffected: their `DockerCli` is always
bound to their own `SshCli` and must never receive `local_docker`.
"""

from typing import TYPE_CHECKING, cast

from maestro.execution.docker_cli import DockerCli
from maestro.execution.exec_config import (
    BackendSpec,
    DockerConfig,
    DockerIsolation,
    ExecutionConfig,
    LocalTransport,
    SshTransport,
)
from maestro.execution.local import LocalBackend
from maestro.execution.resolver import BackendResolver
from maestro.execution.ssh_backend import SshBackend


if TYPE_CHECKING:
    from maestro.execution.isolators import DockerIsolator


class FakeDockerCli(DockerCli):
    """A `DockerCli` double: no subprocess, no daemon.

    Subclasses the real `DockerCli` (rather than duck-typing a bare class)
    so it satisfies `LocalBackend`'s/`DockerIsolator`'s `DockerCli`-typed
    parameters under nominal typing, and skips `DockerCli.__init__` (which
    wires up a real subprocess `run_cmd`) entirely — this fake is never
    actually called in these tests, only compared for identity.
    """

    def __init__(self) -> None:
        pass


def test_legacy_docker_backend_uses_injected_cli() -> None:
    """Case 1: the legacy `execution.docker` shorthand resolves through the
    injected CLI — both the built `LocalBackend` and its `DockerIsolator`
    share the same instance."""
    fake = FakeDockerCli()
    cfg = ExecutionConfig(docker=DockerConfig(image="maestro-runner:x"))
    resolver = BackendResolver(cfg, local_docker=fake)

    backend = resolver.resolve("docker")

    assert isinstance(backend, LocalBackend)
    assert backend._docker is fake
    isolator = cast("DockerIsolator", backend._isolator)
    assert isolator._docker is fake


def test_two_named_local_docker_backends_share_cli_keep_own_config() -> None:
    """Case 2: two named `transport: local` / `isolation: docker` backends
    resolved from one resolver share the injected CLI but keep their own
    per-backend config (image differs)."""
    fake = FakeDockerCli()
    cfg = ExecutionConfig(
        backends={
            "sandbox": BackendSpec(
                transport=LocalTransport(),
                isolation=DockerIsolation(type="docker", image="img-a"),
            ),
            "gpu-sbx": BackendSpec(
                transport=LocalTransport(),
                isolation=DockerIsolation(type="docker", image="img-b"),
            ),
        }
    )
    resolver = BackendResolver(cfg, local_docker=fake)

    sandbox = resolver.resolve("sandbox")
    gpu_sbx = resolver.resolve("gpu-sbx")

    assert isinstance(sandbox, LocalBackend)
    assert isinstance(gpu_sbx, LocalBackend)
    assert sandbox._docker is fake
    assert gpu_sbx._docker is fake
    assert sandbox.id == "sandbox"
    assert gpu_sbx.id == "gpu-sbx"
    sandbox_isolator = cast("DockerIsolator", sandbox._isolator)
    gpu_sbx_isolator = cast("DockerIsolator", gpu_sbx._isolator)
    assert sandbox_isolator._cfg.image == "img-a"
    assert gpu_sbx_isolator._cfg.image == "img-b"
    assert sandbox_isolator._cfg.image != gpu_sbx_isolator._cfg.image


def test_local_bare_and_ssh_do_not_receive_injected_cli() -> None:
    """Case 3: a bare local backend gets no docker client at all, and an
    SSH+Docker backend's `DockerCli` is its own ssh-bound instance — never
    the injected `local_docker`."""
    fake = FakeDockerCli()
    cfg = ExecutionConfig(
        backends={
            "remote-gpu": BackendSpec(
                transport=SshTransport(type="ssh", host="gpu1", workdir_root="/tmp/m"),
                isolation=DockerIsolation(type="docker", image="img-c"),
            ),
        }
    )
    resolver = BackendResolver(cfg, local_docker=fake)

    local_backend = resolver.resolve("local")
    ssh_backend = resolver.resolve("remote-gpu")

    assert isinstance(local_backend, LocalBackend)
    assert local_backend._docker is None

    assert isinstance(ssh_backend, SshBackend)
    assert ssh_backend.docker is not None
    assert ssh_backend.docker is not fake


def test_no_injection_builds_normal_docker_cli() -> None:
    """Case 4: without `local_docker`, `resolve("docker")` still builds a
    real, working `DockerCli` (no daemon required for construction)."""
    cfg = ExecutionConfig(docker=DockerConfig(image="maestro-runner:x"))
    resolver = BackendResolver(cfg)

    backend = resolver.resolve("docker")

    assert isinstance(backend, LocalBackend)
    assert backend._docker is not None
    assert isinstance(backend._docker, DockerCli)


def test_probe_open_handle_no_longer_calls_probe_execution_directly() -> None:
    """Case 5: after Task 7b, `Orchestrator._probe_open_handle` no longer
    hand-composes `probe_execution` for a `docker` `backend_id` special
    case — every backend_id (docker included) goes through
    `self._backends.resolve()` + `backend.probe()`. The only other
    `gc_terminal_handle`-based docker path in the module (a different
    function) lives in `_gc_terminal_handles`, out of scope here."""
    import inspect

    from maestro import orchestrator as orch_mod

    source = inspect.getsource(orch_mod.Orchestrator._probe_open_handle)
    assert "probe_execution(" not in source
    assert 'backend_id == "docker"' not in source
    # The import itself is gone too — nothing left in the module calls it.
    assert not hasattr(orch_mod, "probe_execution")
