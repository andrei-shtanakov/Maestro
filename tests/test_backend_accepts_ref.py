"""Task 13a: `ExecutionBackend.accepts_ref` — sync, no-I/O identity match
between a resolved backend and the ref a prior run minted.

Mirrors `tests/test_backend_probe_isolation.py`'s backend construction
(`LocalBackend(docker=...)`, `SshBackend(..., isolation=..., runner=...)`).
An `("unknown","unknown")` ref (or any mismatched identity) must never
equal a real backend identity -> both backends reject it (fail-closed).
"""

from datetime import UTC, datetime

from maestro.execution.exec_config import DockerIsolation, SshTransport
from maestro.execution.local import LocalBackend
from maestro.execution.models import ExecutionHandleRef
from maestro.execution.ssh_backend import SshBackend
from maestro.execution.ssh_launch import encode_transport_ref, remote_layout


def _ref(transport_ref: str) -> ExecutionHandleRef:
    return ExecutionHandleRef(
        backend_id="x",
        run_id="r",
        transport_ref=transport_ref,
        started_at=datetime.now(UTC),
    )


def _ssh_backend(*, isolation: DockerIsolation | None) -> SshBackend:
    async def runner(argv: list[str], stdin: str | None):
        del argv, stdin
        raise AssertionError("accepts_ref must do no I/O")

    transport = SshTransport(type="ssh", host="gpu", workdir_root="/var/tmp/m")
    return SshBackend(
        "gpu", transport, secret_env=[], isolation=isolation, runner=runner
    )


def _ssh_ref(*, isolation: str) -> ExecutionHandleRef:
    layout = remote_layout("/var/tmp/m", "e1")
    transport_ref = encode_transport_ref(
        "gpu",
        None,
        layout.root,
        layout.status,
        isolation=isolation,
        expected_labels=(
            {"maestro.execution_id": "e1"} if isolation == "docker" else None
        ),
    )
    return _ref(transport_ref)


# ---------------------------------------------------------------------------
# local-bare
# ---------------------------------------------------------------------------


def test_local_bare_accepts_local_pid_ref() -> None:
    backend = LocalBackend()
    assert backend.accepts_ref(_ref("local_pid:4242")) is True


def test_local_bare_rejects_docker_ref() -> None:
    backend = LocalBackend()
    assert backend.accepts_ref(_ref("docker:maestro-e1")) is False


def test_local_bare_rejects_ssh_ref() -> None:
    backend = LocalBackend()
    assert backend.accepts_ref(_ssh_ref(isolation="bare")) is False


def test_local_bare_rejects_unknown_ref() -> None:
    backend = LocalBackend()
    assert backend.accepts_ref(_ref("sandbox:maestro-x")) is False


# ---------------------------------------------------------------------------
# local-docker
# ---------------------------------------------------------------------------


def test_local_docker_accepts_docker_ref() -> None:
    backend = LocalBackend(docker=object())  # type: ignore[arg-type]
    assert backend.accepts_ref(_ref("docker:maestro-e1")) is True


def test_local_docker_rejects_local_pid_ref() -> None:
    backend = LocalBackend(docker=object())  # type: ignore[arg-type]
    assert backend.accepts_ref(_ref("local_pid:4242")) is False


def test_local_docker_rejects_ssh_ref() -> None:
    backend = LocalBackend(docker=object())  # type: ignore[arg-type]
    assert backend.accepts_ref(_ssh_ref(isolation="docker")) is False


def test_local_docker_rejects_unknown_ref() -> None:
    backend = LocalBackend(docker=object())  # type: ignore[arg-type]
    assert backend.accepts_ref(_ref("sandbox:maestro-x")) is False


# ---------------------------------------------------------------------------
# ssh-bare
# ---------------------------------------------------------------------------


def test_ssh_bare_accepts_ssh_bare_ref() -> None:
    backend = _ssh_backend(isolation=None)
    assert backend.accepts_ref(_ssh_ref(isolation="bare")) is True


def test_ssh_bare_rejects_local_pid_ref() -> None:
    backend = _ssh_backend(isolation=None)
    assert backend.accepts_ref(_ref("local_pid:4242")) is False


def test_ssh_bare_rejects_docker_ref() -> None:
    backend = _ssh_backend(isolation=None)
    assert backend.accepts_ref(_ref("docker:maestro-e1")) is False


def test_ssh_bare_rejects_ssh_docker_ref() -> None:
    backend = _ssh_backend(isolation=None)
    assert backend.accepts_ref(_ssh_ref(isolation="docker")) is False


def test_ssh_bare_rejects_unknown_ref() -> None:
    backend = _ssh_backend(isolation=None)
    assert backend.accepts_ref(_ref("sandbox:maestro-x")) is False


# ---------------------------------------------------------------------------
# ssh-docker
# ---------------------------------------------------------------------------


def test_ssh_docker_accepts_ssh_docker_ref() -> None:
    backend = _ssh_backend(isolation=DockerIsolation(type="docker", image="busybox"))
    assert backend.accepts_ref(_ssh_ref(isolation="docker")) is True


def test_ssh_docker_rejects_ssh_bare_ref() -> None:
    backend = _ssh_backend(isolation=DockerIsolation(type="docker", image="busybox"))
    assert backend.accepts_ref(_ssh_ref(isolation="bare")) is False


def test_ssh_docker_rejects_local_pid_ref() -> None:
    backend = _ssh_backend(isolation=DockerIsolation(type="docker", image="busybox"))
    assert backend.accepts_ref(_ref("local_pid:4242")) is False


def test_ssh_docker_rejects_docker_ref() -> None:
    backend = _ssh_backend(isolation=DockerIsolation(type="docker", image="busybox"))
    assert backend.accepts_ref(_ref("docker:maestro-e1")) is False


def test_ssh_docker_rejects_unknown_ref() -> None:
    backend = _ssh_backend(isolation=DockerIsolation(type="docker", image="busybox"))
    assert backend.accepts_ref(_ref("sandbox:maestro-x")) is False
