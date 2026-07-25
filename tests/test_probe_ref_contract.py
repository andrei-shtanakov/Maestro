from datetime import UTC, datetime

from maestro.execution.models import ExecutionHandleRef, ProbeResult


def test_probe_result_needs_review_is_primary():
    r = ProbeResult(needs_review=True, alive=False, detail="dead but uncollected")
    assert r.needs_review is True
    assert r.alive is False  # diagnostic only


def test_probe_result_alive_optional():
    r = ProbeResult(needs_review=False)
    assert r.alive is None


def test_handle_ref_carries_execution_id():
    ref = ExecutionHandleRef(
        backend_id="sandbox",
        run_id="t1",
        transport_ref="sandbox:maestro-e1",
        execution_id="e1",
        started_at=datetime.now(UTC),
    )
    assert ref.execution_id == "e1"
    # Back-compat: field is optional.
    ref2 = ExecutionHandleRef(
        backend_id="local",
        run_id="t1",
        transport_ref="local_pid:5",
        started_at=datetime.now(UTC),
    )
    assert ref2.execution_id is None
