"""Daemon-free unit tests for fail-closed docker recovery classification."""

from typing import Any

import pytest

from maestro.execution.docker_recovery import (
    GC_CLEAN_OUTCOMES,
    gc_terminal_handle,
    probe_execution,
)


class _FakeDocker:
    """Fake DockerCli: no subprocess, no daemon."""

    def __init__(
        self,
        ids: list[str],
        labels: dict[str, str] | None = None,
        raise_ps: bool = False,
        raise_inspect: bool = False,
        raise_rm: bool = False,
    ) -> None:
        self._ids = ids
        self._labels = labels
        self._raise_ps = raise_ps
        self._raise_inspect = raise_inspect
        self._raise_rm = raise_rm
        self.rm_calls: list[str] = []

    async def ps_ids_by_label(self, key: str, value: str) -> list[str]:
        if self._raise_ps:
            raise RuntimeError("daemon down")
        return self._ids

    async def inspect(self, name: str) -> dict[str, Any] | None:
        if self._raise_inspect:
            raise RuntimeError("inspect failed")
        return {"Config": {"Labels": self._labels or {}}}

    async def rm(self, name: str) -> None:
        if self._raise_rm:
            raise RuntimeError("rm failed")
        self.rm_calls.append(name)


# =============================================================================
# probe_execution
# =============================================================================


@pytest.mark.anyio
async def test_no_container_proceeds() -> None:
    v = await probe_execution("e1", _FakeDocker(ids=[]))
    assert v.needs_review is False
    assert "no container" in v.reason


@pytest.mark.anyio
async def test_found_container_needs_review() -> None:
    v = await probe_execution(
        "e1", _FakeDocker(ids=["c1"], labels={"maestro.execution_id": "e1"})
    )
    assert v.needs_review is True


@pytest.mark.anyio
async def test_daemon_error_fails_closed() -> None:
    v = await probe_execution("e1", _FakeDocker(ids=[], raise_ps=True))
    assert v.needs_review is True
    assert "probe failed" in v.reason


@pytest.mark.anyio
async def test_ambiguous_multiple_needs_review() -> None:
    v = await probe_execution("e1", _FakeDocker(ids=["c1", "c2"]))
    assert v.needs_review is True
    assert "ambiguous" in v.reason


@pytest.mark.anyio
async def test_label_mismatch_needs_review() -> None:
    v = await probe_execution(
        "e1", _FakeDocker(ids=["c1"], labels={"maestro.execution_id": "other"})
    )
    assert v.needs_review is True
    assert "mismatch" in v.reason


@pytest.mark.anyio
async def test_inspect_error_fails_closed() -> None:
    """A single-match inspect() failure (e.g. a subprocess TimeoutError
    under daemon load right after a crash) must fail closed, not raise —
    an unhandled exception here would abort recovery for every other
    task/workstream in the same recovery pass."""
    v = await probe_execution("e1", _FakeDocker(ids=["c1"], raise_inspect=True))
    assert v.needs_review is True
    assert "inspect failed" in v.reason


@pytest.mark.anyio
async def test_full_label_mismatch_with_expected_labels() -> None:
    """Phase 2c: found container matches `maestro.execution_id` but has the
    wrong `maestro.backend_id` — with a full `expected_labels` set passed,
    this must fail closed on the full-set mismatch, not just the id."""
    v = await probe_execution(
        "e1",
        _FakeDocker(
            ids=["c1"],
            labels={"maestro.execution_id": "e1", "maestro.backend_id": "wrong"},
        ),
        expected_labels={"maestro.execution_id": "e1", "maestro.backend_id": "ssh1"},
    )
    assert v.needs_review is True
    assert "mismatch" in v.reason


@pytest.mark.anyio
async def test_local_path_unaffected_without_expected_labels() -> None:
    """Without `expected_labels` (the local-docker path), a container
    matching only `maestro.execution_id` is still treated as a match — the
    single-id check is preserved exactly."""
    v = await probe_execution(
        "e1",
        _FakeDocker(
            ids=["c1"],
            labels={"maestro.execution_id": "e1", "maestro.backend_id": "whatever"},
        ),
    )
    assert v.needs_review is True
    assert v.reason == "live/leftover container found"


# =============================================================================
# gc_terminal_handle
# =============================================================================


@pytest.mark.anyio
async def test_gc_no_container_found() -> None:
    docker = _FakeDocker(ids=[])
    outcome = await gc_terminal_handle({"execution_id": "e1"}, docker)
    assert outcome == "no container found"
    assert outcome in GC_CLEAN_OUTCOMES
    assert docker.rm_calls == []


@pytest.mark.anyio
async def test_gc_removes_matching_container() -> None:
    docker = _FakeDocker(ids=["c1"], labels={"maestro.execution_id": "e1"})
    outcome = await gc_terminal_handle({"execution_id": "e1"}, docker)
    assert outcome == "removed"
    assert outcome in GC_CLEAN_OUTCOMES
    assert docker.rm_calls == ["c1"]


@pytest.mark.anyio
async def test_gc_skips_ambiguous() -> None:
    docker = _FakeDocker(ids=["c1", "c2"])
    outcome = await gc_terminal_handle({"execution_id": "e1"}, docker)
    assert "ambiguous" in outcome
    assert outcome not in GC_CLEAN_OUTCOMES
    assert docker.rm_calls == []


@pytest.mark.anyio
async def test_gc_skips_label_mismatch() -> None:
    docker = _FakeDocker(ids=["c1"], labels={"maestro.execution_id": "other"})
    outcome = await gc_terminal_handle({"execution_id": "e1"}, docker)
    assert "mismatch" in outcome
    assert outcome not in GC_CLEAN_OUTCOMES
    assert docker.rm_calls == []


@pytest.mark.anyio
async def test_gc_ps_error_never_raises() -> None:
    docker = _FakeDocker(ids=[], raise_ps=True)
    outcome = await gc_terminal_handle({"execution_id": "e1"}, docker)
    assert outcome.startswith("gc failed")
    assert outcome not in GC_CLEAN_OUTCOMES


@pytest.mark.anyio
async def test_gc_rm_error_never_raises() -> None:
    docker = _FakeDocker(
        ids=["c1"], labels={"maestro.execution_id": "e1"}, raise_rm=True
    )
    outcome = await gc_terminal_handle({"execution_id": "e1"}, docker)
    assert outcome.startswith("gc failed")
    assert outcome not in GC_CLEAN_OUTCOMES


@pytest.mark.anyio
async def test_gc_full_label_mismatch_skips_with_expected_labels() -> None:
    """Phase 2c: found container matches `maestro.execution_id` but has the
    wrong `maestro.backend_id` — with `expected_labels` passed, GC must
    skip (not rm) on the full-set mismatch."""
    docker = _FakeDocker(
        ids=["c1"],
        labels={"maestro.execution_id": "e1", "maestro.backend_id": "wrong"},
    )
    outcome = await gc_terminal_handle(
        {"execution_id": "e1"},
        docker,
        expected_labels={"maestro.execution_id": "e1", "maestro.backend_id": "ssh1"},
    )
    assert outcome == "label mismatch, skipped"
    assert outcome not in GC_CLEAN_OUTCOMES
    assert docker.rm_calls == []


@pytest.mark.anyio
async def test_gc_local_path_unaffected_without_expected_labels() -> None:
    """Without `expected_labels` (the local-docker path), a container
    matching only `maestro.execution_id` is still removed — the single-id
    behavior is preserved exactly."""
    docker = _FakeDocker(
        ids=["c1"],
        labels={"maestro.execution_id": "e1", "maestro.backend_id": "whatever"},
    )
    outcome = await gc_terminal_handle({"execution_id": "e1"}, docker)
    assert outcome == "removed"
    assert outcome in GC_CLEAN_OUTCOMES
    assert docker.rm_calls == ["c1"]
