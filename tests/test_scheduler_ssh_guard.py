"""Phase 2b lifts the Mode-1 SSH guard: an ssh backend now resolves in
scheduler mode (safety moved to the scheduler's reservation lock)."""

from maestro.execution.exec_config import (
    BackendSpec,
    BareIsolation,
    ExecutionConfig,
    SshTransport,
)
from maestro.execution.resolver import BackendResolver
from maestro.execution.ssh_backend import SshBackend


def test_ssh_resolves_in_scheduler_mode() -> None:
    """SSH backend resolves in scheduler mode after Phase 2b guard removal."""
    ex = ExecutionConfig(
        default_backend="local",
        backends={
            "remote": BackendSpec(
                transport=SshTransport(type="ssh", host="h", workdir_root="/remote"),
                isolation=BareIsolation(),
            )
        },
    )
    resolver = BackendResolver(ex, mode="scheduler")
    backend = resolver.resolve("remote")
    assert isinstance(backend, SshBackend)
    assert backend.id == "remote"
