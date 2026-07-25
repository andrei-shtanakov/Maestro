"""Verdict v2 contract: models and the strict file/process handshake (§5)."""

import json
from pathlib import Path

import pytest

from maestro.domain.verdict import (
    EchoExpectations,
    VerdictDocument,
    VerdictValue,
    evaluate_handshake,
)


EXPECTED = EchoExpectations(
    run_id="01JRUNID0000000000000000",
    attempt=1,
    workstream_id="topic-x-report",
    artifact="reports/topic-x/result.md",
    profile_sha256="p" * 64,
    verified_source_commit="c" * 40,
    verified_source_tree="t" * 40,
)


def make_verdict(verdict: str = "PASS", **overrides: object) -> dict:
    identity: dict[str, object] = {
        "verification_run_id": EXPECTED.run_id,
        "verification_attempt": 1,
        "rework_attempt": 0,
        "workstream_id": EXPECTED.workstream_id,
        "artifact": EXPECTED.artifact,
        "artifact_sha256": "a" * 64,
        "criteria_sha256": "b" * 64,
        "profile_sha256": EXPECTED.profile_sha256,
        "verified_source_commit": EXPECTED.verified_source_commit,
        "verified_source_tree": EXPECTED.verified_source_tree,
    }
    identity.update({k: v for k, v in overrides.items() if k in identity})
    return {
        "schema_version": 2,
        "identity": identity,
        "verdict": verdict,
        "findings": [
            {
                "criterion_id": "synthesis",
                "severity": "major",
                "evidence": "conclusions not separated from inference",
                "author_feedback": "Separate cited evidence from inference.",
            }
        ]
        if verdict == "FAIL"
        else [],
    }


def write_verdict(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "attempt-001.json"
    p.write_text(json.dumps(payload))
    return p


def test_valid_pass_with_matching_exit_code(tmp_path: Path) -> None:
    p = write_verdict(tmp_path, make_verdict("PASS"))
    result = evaluate_handshake(p, exit_code=0, timed_out=False, expected=EXPECTED)
    assert result.outcome is VerdictValue.PASS
    assert result.protocol_error is None
    assert isinstance(result.document, VerdictDocument)


def test_exit_code_mismatch_is_protocol_error(tmp_path: Path) -> None:
    # Valid FAIL document but exit 0 -> ERROR, protocol violation (§5 table).
    p = write_verdict(tmp_path, make_verdict("FAIL"))
    result = evaluate_handshake(p, exit_code=0, timed_out=False, expected=EXPECTED)
    assert result.outcome is VerdictValue.ERROR
    assert result.protocol_error is not None


def test_missing_file_is_error(tmp_path: Path) -> None:
    result = evaluate_handshake(
        tmp_path / "absent.json", exit_code=0, timed_out=False, expected=EXPECTED
    )
    assert result.outcome is VerdictValue.ERROR


def test_invalid_json_is_error(tmp_path: Path) -> None:
    p = tmp_path / "attempt-001.json"
    p.write_text("{not json")
    result = evaluate_handshake(p, exit_code=1, timed_out=False, expected=EXPECTED)
    assert result.outcome is VerdictValue.ERROR


def test_timeout_invalidates_valid_file(tmp_path: Path) -> None:
    # §5: valid verdict + process timeout -> ERROR, file is forensic only.
    p = write_verdict(tmp_path, make_verdict("PASS"))
    result = evaluate_handshake(p, exit_code=None, timed_out=True, expected=EXPECTED)
    assert result.outcome is VerdictValue.ERROR
    assert result.document is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_sha256", "x" * 64),
        ("verified_source_commit", "d" * 40),
        ("verified_source_tree", "e" * 40),
        ("verification_run_id", "OTHER"),
        ("artifact", "reports/other.md"),
    ],
)
def test_echo_field_mismatch_is_protocol_error(
    tmp_path: Path, field: str, value: str
) -> None:
    p = write_verdict(tmp_path, make_verdict("PASS", **{field: value}))
    result = evaluate_handshake(p, exit_code=0, timed_out=False, expected=EXPECTED)
    assert result.outcome is VerdictValue.ERROR
    assert field in (result.protocol_error or "")


def test_error_verdict_with_exit_2(tmp_path: Path) -> None:
    p = write_verdict(tmp_path, make_verdict("ERROR"))
    result = evaluate_handshake(p, exit_code=2, timed_out=False, expected=EXPECTED)
    assert result.outcome is VerdictValue.ERROR
    assert result.protocol_error is None  # infrastructure ERROR, not protocol


def test_non_utf8_file_is_error(tmp_path: Path) -> None:
    p = tmp_path / "attempt-001.json"
    p.write_bytes(b"\xff\xfe garbled \x80")
    result = evaluate_handshake(p, exit_code=2, timed_out=False, expected=EXPECTED)
    assert result.outcome is VerdictValue.ERROR
    assert result.protocol_error is not None
