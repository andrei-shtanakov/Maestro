import json
from pathlib import Path

from maestro.domain.verdict import (
    Finding,
    TaskIdentityExpectations,
    TaskVerdictDocument,
    TaskVerdictIdentity,
    VerdictValue,
    evaluate_task_document,
)


def _identity(**over):
    base = {
        "task_id": "t1",
        "verification_run_id": "r1",
        "verification_attempt": 1,
        "artifact": "task-diff:t1",
        "artifact_sha256": "a" * 64,
        "criteria_sha256": "b" * 64,
        "profile_sha256": "c" * 64,
        "verified_source_commit": "d" * 40,
        "verified_scope_sha256": "e" * 64,
    }
    base.update(over)
    return TaskVerdictIdentity(**base)


def _expected(**over):
    base = {
        "task_id": "t1",
        "verification_run_id": "r1",
        "verification_attempt": 1,
        "artifact": "task-diff:t1",
        "artifact_sha256": "a" * 64,
        "criteria_sha256": "b" * 64,
        "profile_sha256": "c" * 64,
        "verified_source_commit": "d" * 40,
        "verified_scope_sha256": "e" * 64,
    }
    base.update(over)
    return TaskIdentityExpectations(**base)


def _write(tmp_path, doc: dict) -> Path:
    p = tmp_path / "verdict.json"
    p.write_text(json.dumps(doc))
    return p


def test_valid_pass(tmp_path):
    doc = TaskVerdictDocument(
        schema_version=2, identity=_identity(), verdict=VerdictValue.PASS
    )
    p = _write(tmp_path, json.loads(doc.model_dump_json()))
    res = evaluate_task_document(p, _expected())
    assert res.outcome == VerdictValue.PASS
    assert res.document is not None and res.document.verdict == VerdictValue.PASS


def test_valid_fail_is_fail_not_error(tmp_path):
    """A FAIL verdict is FAIL — there is NO exit-code comparison (unlike Stage B)."""
    doc = TaskVerdictDocument(
        schema_version=2,
        identity=_identity(),
        verdict=VerdictValue.FAIL,
        findings=[
            Finding(
                criterion_id="stub",
                severity="high",
                evidence="x",
                author_feedback="fix y",
            )
        ],
    )
    p = _write(tmp_path, json.loads(doc.model_dump_json()))
    assert evaluate_task_document(p, _expected()).outcome == VerdictValue.FAIL


def test_identity_mismatch_is_error(tmp_path):
    doc = TaskVerdictDocument(
        schema_version=2,
        identity=_identity(artifact_sha256="f" * 64),
        verdict=VerdictValue.PASS,
    )
    p = _write(tmp_path, json.loads(doc.model_dump_json()))
    res = evaluate_task_document(p, _expected())  # expected artifact_sha256 = "a"*64
    assert res.outcome == VerdictValue.ERROR and res.document is None


def test_missing_and_garbage_are_error(tmp_path):
    assert (
        evaluate_task_document(tmp_path / "nope.json", _expected()).outcome
        == VerdictValue.ERROR
    )
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert evaluate_task_document(bad, _expected()).outcome == VerdictValue.ERROR


def test_stage_b_workstream_models_untouched():
    # workstream identity still mandates workstream_id/rework_attempt
    import inspect

    from maestro.domain.verdict import (
        VerdictIdentity,
    )

    assert "workstream_id" in inspect.getsource(VerdictIdentity)
