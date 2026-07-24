import json

from maestro.execution.ssh_launch import (
    build_descriptor,
    decode_transport_ref,
    encode_transport_ref,
    remote_layout,
)


def test_remote_layout_paths_are_under_tmp():
    lay = remote_layout("/var/tmp/maestro", "e1")
    assert lay.root == "/var/tmp/maestro/maestro-exec-e1"
    assert lay.repo == "/var/tmp/maestro/maestro-exec-e1/repo"
    assert lay.status.endswith("/e1.status")
    assert lay.owner_marker.endswith("/.maestro-owner")


def test_descriptor_carries_argv_verbatim():
    lay = remote_layout("/w", "e1")
    d = build_descriptor("e1", lay, ["spec-runner", "run", "--all"], "/w")
    assert d["v"] == 1
    assert d["argv"] == ["spec-runner", "run", "--all"]
    assert d["cwd"] == lay.repo
    # round-trips as JSON
    assert json.loads(json.dumps(d))["execution_id"] == "e1"


def test_transport_ref_is_opaque_versioned_json():
    ref = encode_transport_ref(
        "gpu", 2222, "/w/maestro-exec-e1", "/w/maestro-exec-e1/e1.status"
    )
    obj = json.loads(ref)
    assert obj["v"] == 2 and obj["transport"] == "ssh" and obj["host"] == "gpu"


def test_transport_ref_v2_docker_roundtrip():
    labels = {"maestro.execution_id": "e1", "maestro.backend_id": "rs"}
    s = encode_transport_ref(
        "h",
        22,
        "/r/maestro-exec-e1",
        "/r/maestro-exec-e1/e1.status",
        isolation="docker",
        expected_labels=labels,
    )
    d = decode_transport_ref(s)
    assert d["v"] == 2
    assert d["isolation"] == "docker"
    assert d["expected_labels"] == labels


def test_transport_ref_default_is_bare():
    s = encode_transport_ref("h", None, "/r/x", "/r/x/x.status")
    d = decode_transport_ref(s)
    assert d["isolation"] == "bare"
    assert d["expected_labels"] == {}


def test_legacy_v1_decodes_as_bare():
    legacy = json.dumps(
        {
            "v": 1,
            "transport": "ssh",
            "host": "h",
            "port": None,
            "remote_dir": "/r/x",
            "status_marker": "/r/x/x.status",
        }
    )
    d = decode_transport_ref(legacy)
    assert d["isolation"] == "bare"
    assert d["expected_labels"] == {}
