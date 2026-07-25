"""Task 6: isolation-aware `ExecutionBackend.probe` (fail-closed recovery).

`SshBackend.probe` must reproduce the orchestrator's former
`_probe_open_handle` dual-probe (ssh +, when the persisted transport_ref
says so, docker) and `LocalBackend.probe` must gain a docker branch
(`probe_execution` by `execution_id`) alongside its existing local-pid path.
Both are fail-closed: any ambiguity routes to `needs_review=True`.

Fakes mirror existing tests rather than inventing new shapes:
- the ssh runner/`_ssh` helper mirrors `tests/test_ssh_recovery.py`'s
  `_ssh(responses)` (needle -> RunResult) and `tests/test_ssh_backend.py` /
  `tests/test_orchestrator_ssh_wiring.py`'s `SshBackend(..., runner=...)`
  construction.
- `_FakeDockerProbe` mirrors `tests/test_orchestrator_ssh_wiring.py`'s class
  of the same name (satisfies `docker_recovery.DockerProbe`:
  `ps_ids_by_label` / `inspect` / `rm`).
"""

import json
from datetime import UTC, datetime

import pytest

from maestro.execution.exec_config import DockerIsolation, SshTransport
from maestro.execution.local import LocalBackend
from maestro.execution.models import ExecutionHandleRef
from maestro.execution.ssh_backend import SshBackend
from maestro.execution.ssh_cli import RunResult
from maestro.execution.ssh_launch import encode_transport_ref, remote_layout


class _FakeDockerProbe:
    """Fake satisfying `docker_recovery.DockerProbe` without a daemon."""

    def __init__(self, ps_ids: list[str], labels: dict[str, str] | None = None) -> None:
        self._ps_ids = ps_ids
        self._labels = labels or {}
        self.ps_calls: list[tuple[str, str]] = []

    async def ps_ids_by_label(self, key: str, value: str) -> list[str]:
        self.ps_calls.append((key, value))
        return list(self._ps_ids)

    async def inspect(self, name: str) -> dict[str, object] | None:
        del name
        return {"Config": {"Labels": self._labels}}

    async def rm(self, name: str) -> None:
        raise AssertionError("probe must never rm")


def _runner(responses: list[tuple[str, RunResult]]):
    """Needle-matched fake ssh runner (mirrors `test_ssh_recovery._ssh`)."""

    async def runner(argv: list[str], stdin: str | None) -> RunResult:
        del stdin
        joined = " ".join(argv)
        for needle, result in responses:
            if needle in joined:
                return result
        return RunResult(1, "", "")

    return runner


def _ssh_backend(
    *, isolation: DockerIsolation | None, responses: list[tuple[str, RunResult]]
) -> SshBackend:
    transport = SshTransport(type="ssh", host="gpu", workdir_root="/var/tmp/m")
    return SshBackend(
        "gpu",
        transport,
        secret_env=[],
        isolation=isolation,
        runner=_runner(responses),
    )


def _ref(
    *,
    layout,
    isolation: str = "bare",
    expected_labels: dict[str, str] | None = None,
    execution_id: str | None = "e1",
) -> ExecutionHandleRef:
    transport_ref = encode_transport_ref(
        "gpu",
        None,
        layout.root,
        layout.status,
        isolation=isolation,
        expected_labels=expected_labels,
    )
    return ExecutionHandleRef(
        backend_id="gpu",
        run_id="api",
        transport_ref=transport_ref,
        execution_id=execution_id,
        status_marker=layout.status,
        started_at=datetime.now(UTC),
    )


# No-marker, pgid-known responses shared by the ssh+docker cases: probe_ssh
# is fail-closed regardless (always needs_review=True for an open handle),
# so both cases 2 and 3 use the same ssh-side fixture and vary only the
# container-probe outcome.
_NO_MARKER_PGID_DEAD = [
    (".status", RunResult(1, "", "no such file")),
    (".pid", RunResult(0, json.dumps({"pid": 9, "pgid": 9}), "")),
    ("kill -0", RunResult(1, "", "no such process")),
]


@pytest.mark.anyio
async def test_bare_ssh_open_handle_always_needs_review() -> None:
    """Case 1: bare ssh, no docker isolation persisted -> always review."""
    layout = remote_layout("/var/tmp/m", "e1")
    backend = _ssh_backend(
        isolation=None,
        responses=[
            (".status", RunResult(1, "", "no such file")),
            (".pid", RunResult(1, "", "no such file")),
        ],
    )
    ref = _ref(layout=layout, isolation="bare")

    result = await backend.probe(ref)

    assert result.needs_review is True


@pytest.mark.anyio
async def test_ssh_docker_pgid_dead_but_container_present_needs_review() -> None:
    """Case 2: ssh+docker, pgid confirmed dead but a leftover container with
    matching labels is found -> review (container probe is consulted, and
    on its own would already force review)."""
    layout = remote_layout("/var/tmp/m", "e1")
    backend = _ssh_backend(
        isolation=DockerIsolation(type="docker", image="busybox"),
        responses=_NO_MARKER_PGID_DEAD,
    )
    fake_docker = _FakeDockerProbe(
        ps_ids=["cid1"], labels={"maestro.execution_id": "e1"}
    )
    backend._docker = fake_docker  # type: ignore[assignment]
    ref = _ref(
        layout=layout,
        isolation="docker",
        expected_labels={"maestro.execution_id": "e1"},
    )

    result = await backend.probe(ref)

    assert result.needs_review is True
    assert fake_docker.ps_calls == [("maestro.execution_id", "e1")]


@pytest.mark.anyio
async def test_ssh_docker_both_clean_still_needs_review() -> None:
    """Case 3: ssh+docker, pgid dead AND no leftover container -> STILL
    review. `probe_ssh` is unconditionally fail-closed for an open ssh
    handle (a terminal marker cannot prove collect already applied), so the
    verdict must stay `needs_review=True` even though the container probe
    alone would have said "clean"."""
    layout = remote_layout("/var/tmp/m", "e1")
    backend = _ssh_backend(
        isolation=DockerIsolation(type="docker", image="busybox"),
        responses=_NO_MARKER_PGID_DEAD,
    )
    fake_docker = _FakeDockerProbe(ps_ids=[])  # no container found
    backend._docker = fake_docker  # type: ignore[assignment]
    ref = _ref(
        layout=layout,
        isolation="docker",
        expected_labels={"maestro.execution_id": "e1"},
    )

    result = await backend.probe(ref)

    assert result.needs_review is True
    # The container probe still ran (not short-circuited by ssh's verdict).
    assert fake_docker.ps_calls == [("maestro.execution_id", "e1")]


@pytest.mark.anyio
async def test_local_docker_no_container_is_reclaimable() -> None:
    """Case 4: local-docker backend, no leftover container -> not review
    (safe to silently reclaim)."""
    fake_docker = _FakeDockerProbe(ps_ids=[])
    backend = LocalBackend(docker=fake_docker)  # type: ignore[arg-type]
    ref = ExecutionHandleRef(
        backend_id="docker",
        run_id="api",
        transport_ref="docker:e1",
        execution_id="e1",
        started_at=datetime.now(UTC),
    )

    result = await backend.probe(ref)

    assert result.needs_review is False
    assert fake_docker.ps_calls == [("maestro.execution_id", "e1")]


@pytest.mark.anyio
async def test_local_docker_null_execution_id_fails_closed() -> None:
    """Case 5: local-docker backend, ref carries no execution_id -> fail
    closed (no name to probe by) without attempting a container probe."""
    fake_docker = _FakeDockerProbe(ps_ids=["cid1"])
    backend = LocalBackend(docker=fake_docker)  # type: ignore[arg-type]
    ref = ExecutionHandleRef(
        backend_id="docker",
        run_id="api",
        transport_ref="docker:unknown",
        execution_id=None,
        started_at=datetime.now(UTC),
    )

    result = await backend.probe(ref)

    assert result.needs_review is True
    assert fake_docker.ps_calls == []
