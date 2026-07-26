import pytest
from pydantic import ValidationError

from maestro.models import VerifierConfig
from maestro.verifier.docker_config import (
    VerifierDockerConfig,
    _parse_docker_size_bytes,
)


_DIGEST = "example.com/img@sha256:" + "a" * 64


def _cfg(**over):
    base = {"image": _DIGEST, "user": "1000:1000"}
    base.update(over)
    return VerifierDockerConfig(**base)


def test_defaults_are_bounded_and_valid():
    c = _cfg()
    assert c.memory == "512m" and c.cpus == "1"
    assert c.pids_limit == 128 and c.tmpfs_size == "64m"


def test_parse_docker_size_bytes():
    assert _parse_docker_size_bytes("128m") == 128 * 1024 * 1024
    assert _parse_docker_size_bytes("8g") == 8 * 1024**3
    assert _parse_docker_size_bytes("512") == 512
    with pytest.raises(ValueError):
        _parse_docker_size_bytes("1e6")
    with pytest.raises(ValueError):
        _parse_docker_size_bytes("")


@pytest.mark.parametrize(
    "over",
    [
        {"image": "example.com/img:latest"},  # bare tag, no digest
        {"image": "img@sha256:short"},  # bad digest
        {"user": "root"},  # symbolic
        {"user": "0:0"},  # uid 0
        {"user": "1000:0"},  # gid 0
        {"user": "1000"},  # not uid:gid
        {"memory": "0"},  # zero
        {"memory": "16g"},  # over 8g
        {"memory": "64k"},  # under 128m
        {"cpus": "0"},  # zero
        {"cpus": "9"},  # over 8
        {"cpus": "1e1"},  # exponent
        {"cpus": "inf"},  # non-finite
        {"pids_limit": 0},  # zero
        {"pids_limit": -1},  # docker "unlimited"
        {"pids_limit": 5000},  # over 4096
        {"tmpfs_size": "8m"},  # under 16m
        {"tmpfs_size": "2g"},  # over 1g
    ],
)
def test_rejects_out_of_contract(over):
    with pytest.raises(ValidationError):
        _cfg(**over)


def test_verifier_config_backend_docker_requires_block():
    with pytest.raises(ValidationError):
        VerifierConfig(backend="docker", model="m")  # no docker block


def test_verifier_config_local_with_docker_block_rejected():
    with pytest.raises(ValidationError):
        VerifierConfig(backend="local", model="m", docker=_cfg())


def test_verifier_config_docker_ok():
    c = VerifierConfig(backend="docker", model="m", docker=_cfg())
    assert c.backend == "docker" and c.docker is not None


def test_size_and_cpus_values_are_stripped():
    # A whitespace-bearing size/cpus must be stored trimmed so it can never
    # reach the `docker run --memory/--cpus/--tmpfs` argv untrimmed.
    c = _cfg(memory=" 512m ", cpus=" 1 ", tmpfs_size=" 64m ")
    assert c.memory == "512m"
    assert c.cpus == "1"
    assert c.tmpfs_size == "64m"
