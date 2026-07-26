
import pytest

from maestro.execution.local import LocalBackend
from maestro.models import VerifierConfig
from maestro.verifier.docker_backend import build_verifier_backend
from maestro.verifier.docker_config import VerifierDockerConfig


_DIGEST = "example.com/img@sha256:" + "a" * 64


class _Sentinel:
    id = "local"


def test_local_returns_passed_backend(tmp_path):
    passed = _Sentinel()
    cfg = VerifierConfig(backend="local", model="m")
    out = build_verifier_backend(
        cfg, local_backend=passed, exec_root=tmp_path  # type: ignore[arg-type]
    )
    assert out is passed  # never a fresh LocalBackend


def test_docker_builds_verifier_docker(tmp_path):
    cfg = VerifierConfig(
        backend="docker",
        model="m",
        docker=VerifierDockerConfig(image=_DIGEST, user="1000:1000"),
    )
    fake_cli = object()
    out = build_verifier_backend(
        cfg,
        local_backend=_Sentinel(),  # type: ignore[arg-type]
        exec_root=tmp_path,
        docker_cli=fake_cli,  # type: ignore[arg-type]
    )
    assert isinstance(out, LocalBackend)
    assert out.id == "verifier-docker"


def test_docker_requires_docker_cli(tmp_path):
    cfg = VerifierConfig(
        backend="docker",
        model="m",
        docker=VerifierDockerConfig(image=_DIGEST, user="1000:1000"),
    )
    with pytest.raises(ValueError):
        build_verifier_backend(
            cfg,
            local_backend=_Sentinel(),  # type: ignore[arg-type]
            exec_root=tmp_path,
            docker_cli=None,
        )
