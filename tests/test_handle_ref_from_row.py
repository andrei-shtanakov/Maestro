from maestro.execution.handle_ref import handle_ref_from_row


def test_builds_ref_with_execution_id():
    row = {
        "backend_id": "gpu",
        "entity_id": "t1",
        "transport_ref": "gpu:e1",
        "status_marker": "/t/e1.status",
        "execution_id": "e1",
        "created_at": "2026-07-25T00:00:00+00:00",
    }
    ref = handle_ref_from_row(row)
    assert ref.backend_id == "gpu"
    assert ref.run_id == "t1"
    assert ref.transport_ref == "gpu:e1"
    assert ref.status_marker == "/t/e1.status"
    assert ref.execution_id == "e1"
