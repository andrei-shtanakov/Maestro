"""Unit-probe `Scheduler._verifier_backend` dispatch resolution (Task 5)."""

from maestro.models import VerifierConfig
from maestro.verifier.docker_config import VerifierDockerConfig


_DIGEST = "example.com/img@sha256:" + "a" * 64


def test_verifier_backend_local(monkeypatch, tmp_path):
    from maestro.scheduler import Scheduler

    sch = Scheduler.__new__(Scheduler)  # bypass full init; unit-probe the helper
    sch._verifier = VerifierConfig(backend="local", model="m")
    sentinel = object()
    sch._backends = type("R", (), {"resolve": lambda _self, _n: sentinel})()
    sch._verifier_docker_cli = None
    sch._verifier_exec_root = tmp_path
    assert sch._verifier_backend() is sentinel


def test_verifier_backend_docker(tmp_path):
    from maestro.execution.local import LocalBackend
    from maestro.scheduler import Scheduler

    sch = Scheduler.__new__(Scheduler)
    sch._verifier = VerifierConfig(
        backend="docker",
        model="m",
        docker=VerifierDockerConfig(image=_DIGEST, user="1000:1000"),
    )
    sch._backends = type("R", (), {"resolve": lambda _self, _n: object()})()
    sch._verifier_docker_cli = object()  # type: ignore[assignment]
    sch._verifier_exec_root = tmp_path
    out = sch._verifier_backend()
    assert isinstance(out, LocalBackend) and out.id == "verifier-docker"
