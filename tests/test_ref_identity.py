"""Task 13a: `ref_identity` — pure classifier of a persisted `transport_ref`
into its `(transport, isolation)` identity.

Covers every branch: bare local pid, local docker (incl. the legacy backend
named literally `"docker"`, whose placeholder ref already matches its real
ref), versioned SSH JSON (bare/docker/unknown isolation), an unrelated
placeholder (`"sandbox:maestro-x"`), and malformed/empty input.
"""

import json

from maestro.execution.ref_identity import RefIdentity, ref_identity


def test_local_pid_is_local_bare() -> None:
    assert ref_identity("local_pid:4242") == RefIdentity("local", "bare")


def test_docker_prefix_is_local_docker() -> None:
    assert ref_identity("docker:maestro-e1") == RefIdentity("local", "docker")


def test_legacy_docker_backend_placeholder_matches_real_ref_identity() -> None:
    """The legacy backend named literally "docker" seeds a placeholder
    `f"{backend.id}:maestro-{execution_id}"` == `"docker:maestro-x"`, which
    happens to classify identically to its real minted ref."""
    assert ref_identity("docker:maestro-x") == RefIdentity("local", "docker")


def test_ssh_json_bare_isolation() -> None:
    ref = json.dumps({"v": 2, "transport": "ssh", "isolation": "bare"})
    assert ref_identity(ref) == RefIdentity("ssh", "bare")


def test_ssh_json_docker_isolation() -> None:
    ref = json.dumps({"v": 2, "transport": "ssh", "isolation": "docker"})
    assert ref_identity(ref) == RefIdentity("ssh", "docker")


def test_ssh_json_missing_isolation_is_unknown() -> None:
    ref = json.dumps({"v": 2, "transport": "ssh"})
    assert ref_identity(ref) == RefIdentity("ssh", "unknown")


def test_ssh_json_bogus_isolation_is_unknown() -> None:
    ref = json.dumps({"v": 2, "transport": "ssh", "isolation": "bogus"})
    assert ref_identity(ref) == RefIdentity("ssh", "unknown")


def test_non_ssh_json_dict_is_unknown() -> None:
    ref = json.dumps({"transport": "carrier-pigeon"})
    assert ref_identity(ref) == RefIdentity("unknown", "unknown")


def test_placeholder_sandbox_ref_is_unknown() -> None:
    assert ref_identity("sandbox:maestro-x") == RefIdentity("unknown", "unknown")


def test_placeholder_remote_ref_is_unknown() -> None:
    assert ref_identity("remote:maestro-x") == RefIdentity("unknown", "unknown")


def test_malformed_json_is_unknown() -> None:
    assert ref_identity("{not json") == RefIdentity("unknown", "unknown")


def test_empty_string_is_unknown() -> None:
    assert ref_identity("") == RefIdentity("unknown", "unknown")


def test_json_scalar_is_unknown() -> None:
    """A syntactically valid JSON value that is not a dict (e.g. a bare
    number/string) must not raise -- just classify as unknown."""
    assert ref_identity("42") == RefIdentity("unknown", "unknown")
