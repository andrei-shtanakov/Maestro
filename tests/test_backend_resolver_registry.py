import pytest

from maestro.execution.exec_config import BackendSpec, ExecutionConfig, SshTransport
from maestro.execution.resolver import BackendResolver, ExecutionConfigError
from maestro.execution.ssh_backend import SshBackend


def _ssh_cfg() -> ExecutionConfig:
    return ExecutionConfig(
        default_backend="local",
        backends={
            "gpu": BackendSpec(
                transport=SshTransport(type="ssh", host="gpu", workdir_root="/w"),
                isolation={"type": "bare"},
            )
        },
    )


def test_local_resolves_with_no_config():
    r = BackendResolver(None)
    assert r.resolve(None).id == "local"


def test_unknown_backend_raises():
    with pytest.raises(ExecutionConfigError, match="unknown"):
        BackendResolver(_ssh_cfg()).resolve("nope")


def test_ssh_backend_resolves_in_scheduler_mode():
    """SSH backends resolve in scheduler mode after Phase 2b guard removal."""
    r = BackendResolver(_ssh_cfg(), mode="scheduler")
    backend = r.resolve("gpu")
    assert isinstance(backend, SshBackend)
    assert backend.id == "gpu"
