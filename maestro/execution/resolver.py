"""Per-dispatch backend resolution. Fail-fast; never falls back to local."""

from typing import Literal

from maestro.execution.backend import ExecutionBackend
from maestro.execution.exec_config import (
    BackendSpec,
    DockerIsolation,
    ExecutionConfig,
    SshTransport,
)
from maestro.execution.local import LocalBackend


class ExecutionConfigError(Exception):
    """Raised for an unusable execution config (unknown/mis-specified backend)."""


class BackendResolver:
    """Resolves a backend name to an ExecutionBackend, caching instances."""

    def __init__(
        self,
        execution: ExecutionConfig | None,
        *,
        mode: Literal["scheduler", "orchestrator"] = "orchestrator",
    ) -> None:
        self._execution = execution or ExecutionConfig()
        self._registry = self._execution.normalized()
        self._mode = mode
        self._cache: dict[str, ExecutionBackend] = {}

    @property
    def default_name(self) -> str:
        return self._execution.default_backend

    def resolve(self, name: str | None) -> ExecutionBackend:
        backend_name = name or self._execution.default_backend
        if backend_name in self._cache:
            return self._cache[backend_name]
        backend = self._build(backend_name)
        self._cache[backend_name] = backend
        return backend

    def _build(self, name: str) -> ExecutionBackend:
        spec = self._registry.get(name)
        if spec is None:
            raise ExecutionConfigError(f"unknown backend {name!r}")
        transport = spec.transport
        if transport.type == "local":
            return self._build_local(name, spec)
        if isinstance(transport, SshTransport):
            return self._build_ssh(name, spec, transport)
        raise ExecutionConfigError(f"backend {name!r}: unsupported transport")

    def _build_local(self, name: str, spec: BackendSpec) -> ExecutionBackend:
        if isinstance(spec.isolation, DockerIsolation):
            from maestro.execution.docker_cli import DockerCli
            from maestro.execution.exec_config import DockerConfig
            from maestro.execution.isolators import DockerIsolator

            docker = DockerCli()
            docker_cfg = DockerConfig(
                image=spec.isolation.image,
                network=spec.isolation.network,
                memory=spec.isolation.memory,
                cpus=spec.isolation.cpus,
                user=spec.isolation.user,
                secret_env=spec.secret_env,
            )
            isolator = DockerIsolator(docker_cfg, docker=docker)
            return LocalBackend(isolator, backend_id=name, docker=docker)
        return LocalBackend(backend_id=name)

    def _build_ssh(
        self, name: str, spec: BackendSpec, transport: SshTransport
    ) -> ExecutionBackend:
        from maestro.execution.ssh_backend import SshBackend

        isolation = None
        if isinstance(spec.isolation, DockerIsolation):
            isolation = spec.isolation
        return SshBackend(
            name,
            transport,
            secret_env=self._execution.effective_secret_env(name),
            isolation=isolation,
        )
