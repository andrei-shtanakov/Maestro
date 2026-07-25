"""Pure builders for the SSH launch: remote path layout, JSON descriptor,
rsync exclude sets, opaque transport_ref. No I/O — trivially unit-testable.
"""

import json
from dataclasses import dataclass


RSYNC_EXCLUDES_OUT = [".git", ".maestro", "*.log"]
RSYNC_EXCLUDES_COLLECT = [
    ".git",
    ".maestro",
    "*.log",
    "env",
    ".maestro-owner",
    "*.status",
    "*.pid",
    "repo/.git",
]


@dataclass(frozen=True)
class RemoteLayout:
    """Absolute remote paths for a single SSH-backed execution."""

    root: str
    repo: str
    env_file: str
    descriptor: str
    supervisor: str
    owner_marker: str
    pid: str
    status: str
    log: str


def remote_layout(workdir_root: str, execution_id: str) -> RemoteLayout:
    """Build the fixed remote directory layout for one execution.

    Rooted at `<workdir_root>/maestro-exec-<execution_id>`.
    """
    root = f"{workdir_root.rstrip('/')}/maestro-exec-{execution_id}"
    return RemoteLayout(
        root=root,
        repo=f"{root}/repo",
        env_file=f"{root}/env",
        descriptor=f"{root}/descriptor.json",
        supervisor=f"{root}/maestro_supervisor.py",
        owner_marker=f"{root}/.maestro-owner",
        pid=f"{root}/{execution_id}.pid",
        status=f"{root}/{execution_id}.status",
        log=f"{root}/{execution_id}.log",
    )


def build_descriptor(
    execution_id: str,
    layout: RemoteLayout,
    argv: list[str],
    workdir_root: str,
) -> dict:
    """Build the JSON-serializable launch descriptor for the remote supervisor."""
    return {
        "v": 1,
        "execution_id": execution_id,
        "cwd": layout.repo,
        "argv": list(argv),
        "env_file": layout.env_file,
        "workdir_root": workdir_root,
        "owner_marker": layout.owner_marker,
        "pid_file": layout.pid,
        "status_file": layout.status,
        "log_file": layout.log,
    }


def encode_transport_ref(
    host: str,
    port: int | None,
    remote_dir: str,
    status_marker: str,
    *,
    isolation: str = "bare",
    expected_labels: dict[str, str] | None = None,
) -> str:
    """Encode an opaque, versioned (v2) transport_ref for an SSH execution.

    `isolation` (`"bare"|"docker"`) and `expected_labels` are the recovery
    SSOT for a run's isolation identity — persisted so a config edit after
    launch cannot change how the run is probed/GC'd.

    Fails closed on an ambiguous ref: `isolation` must be exactly `"bare"` or
    `"docker"` (a typo must never silently decode as bare), and a `"docker"`
    ref must carry a non-empty `expected_labels` (else recovery/GC would
    silently downgrade to a single-id ownership check).
    """
    if isolation not in ("bare", "docker"):
        raise ValueError(f"isolation must be 'bare' or 'docker', got {isolation!r}")
    if isolation == "docker" and not expected_labels:
        raise ValueError("docker isolation requires a non-empty expected_labels")
    return json.dumps(
        {
            "v": 2,
            "transport": "ssh",
            "host": host,
            "port": port,
            "remote_dir": remote_dir,
            "status_marker": status_marker,
            "isolation": isolation,
            "expected_labels": expected_labels or {},
        }
    )


def decode_transport_ref(s: str) -> dict:
    """Decode a `transport_ref`. A legacy `v:1` ref (no `isolation`) reads as
    a `bare` run with an empty expected-label set."""
    data = json.loads(s)
    data.setdefault("isolation", "bare")
    data.setdefault("expected_labels", {})
    return data
