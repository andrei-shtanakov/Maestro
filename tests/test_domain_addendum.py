"""Tests for Task 7: Deterministic rework addendum."""

import pytest

from maestro.domain.addendum import build_rework_addendum
from maestro.domain.verdict import (
    Finding,
    VerdictDocument,
    VerdictIdentity,
    VerdictValue,
)


@pytest.fixture
def sample_identity() -> VerdictIdentity:
    """Create a sample VerdictIdentity for testing with distinctive hash sentinels."""
    return VerdictIdentity(
        verification_run_id="run-001",
        verification_attempt=1,
        rework_attempt=0,
        workstream_id="ws-001",
        artifact="artifact.md",
        artifact_sha256="cafeaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01",
        criteria_sha256="cafebbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb02",
        profile_sha256="cafecccccccccccccccccccccccccccccccccccccccccccccccccccccccccc03",
        verified_source_commit="commit1",
        verified_source_tree="tree1",
    )


@pytest.fixture
def sample_findings() -> list[Finding]:
    """Create sample findings for testing."""
    return [
        Finding(
            criterion_id="CRIT-001-UNIQUE-SENTINEL",
            severity="major",
            evidence="This is unique evidence text EVIDENCE_SENTINEL_001",
            author_feedback="Separate cited evidence from inference.",
        ),
        Finding(
            criterion_id="CRIT-002-ANOTHER-SENTINEL",
            severity="minor",
            evidence="Another unique evidence EVIDENCE_SENTINEL_002",
            author_feedback="Improve clarity in the explanation.",
        ),
    ]


def test_build_rework_addendum_includes_author_feedback(
    sample_identity: VerdictIdentity,
    sample_findings: list[Finding],
) -> None:
    """Test that author_feedback is included in output."""
    doc = VerdictDocument(
        schema_version=2,
        identity=sample_identity,
        verdict=VerdictValue.FAIL,
        findings=sample_findings,
    )
    result = build_rework_addendum(doc)

    assert "Separate cited evidence from inference." in result
    assert "Improve clarity in the explanation." in result


def test_build_rework_addendum_excludes_criterion_id(
    sample_identity: VerdictIdentity,
    sample_findings: list[Finding],
) -> None:
    """Test that criterion_id values are never in output."""
    doc = VerdictDocument(
        schema_version=2,
        identity=sample_identity,
        verdict=VerdictValue.FAIL,
        findings=sample_findings,
    )
    result = build_rework_addendum(doc)

    assert "CRIT-001-UNIQUE-SENTINEL" not in result
    assert "CRIT-002-ANOTHER-SENTINEL" not in result


def test_build_rework_addendum_excludes_evidence(
    sample_identity: VerdictIdentity,
    sample_findings: list[Finding],
) -> None:
    """Test that evidence field text is never in output."""
    doc = VerdictDocument(
        schema_version=2,
        identity=sample_identity,
        verdict=VerdictValue.FAIL,
        findings=sample_findings,
    )
    result = build_rework_addendum(doc)

    assert "EVIDENCE_SENTINEL_001" not in result
    assert "EVIDENCE_SENTINEL_002" not in result
    assert "This is unique evidence text" not in result
    assert "Another unique evidence" not in result


def test_build_rework_addendum_excludes_identity_hashes(
    sample_identity: VerdictIdentity,
    sample_findings: list[Finding],
) -> None:
    """Test that identity hashes (artifact_sha256, criteria_sha256, profile_sha256) never appear."""
    doc = VerdictDocument(
        schema_version=2,
        identity=sample_identity,
        verdict=VerdictValue.FAIL,
        findings=sample_findings,
    )
    result = build_rework_addendum(doc)

    # Assert none of the distinctive hash sentinels appear
    assert (
        "cafeaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01" not in result
    )
    assert (
        "cafebbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb02" not in result
    )
    assert (
        "cafecccccccccccccccccccccccccccccccccccccccccccccccccccccccccc03" not in result
    )


def test_build_rework_addendum_deterministic(
    sample_identity: VerdictIdentity,
    sample_findings: list[Finding],
) -> None:
    """Test that same document always produces identical output."""
    doc = VerdictDocument(
        schema_version=2,
        identity=sample_identity,
        verdict=VerdictValue.FAIL,
        findings=sample_findings,
    )
    result1 = build_rework_addendum(doc)
    result2 = build_rework_addendum(doc)

    assert result1 == result2


def test_build_rework_addendum_header_format(
    sample_identity: VerdictIdentity,
    sample_findings: list[Finding],
) -> None:
    """Test the exact header format with attempt number."""
    doc = VerdictDocument(
        schema_version=2,
        identity=sample_identity,
        verdict=VerdictValue.FAIL,
        findings=sample_findings,
    )
    result = build_rework_addendum(doc)

    # Check header with attempt number
    assert "## Verification feedback (attempt 1)" in result

    # Check intro text
    assert "The previous submission FAILED verification" in result
    assert "Address every item below" in result
    assert "then the report will be re-verified" in result


def test_build_rework_addendum_severity_format(
    sample_identity: VerdictIdentity,
    sample_findings: list[Finding],
) -> None:
    """Test that findings are formatted as [severity] author_feedback."""
    doc = VerdictDocument(
        schema_version=2,
        identity=sample_identity,
        verdict=VerdictValue.FAIL,
        findings=sample_findings,
    )
    result = build_rework_addendum(doc)

    # Check format: - [severity] feedback
    assert "- [major] Separate cited evidence from inference." in result
    assert "- [minor] Improve clarity in the explanation." in result


def test_build_rework_addendum_attempt_number_2(
    sample_findings: list[Finding],
) -> None:
    """Test with attempt number = 2 in the header."""
    identity = VerdictIdentity(
        verification_run_id="run-002",
        verification_attempt=2,
        rework_attempt=1,
        workstream_id="ws-002",
        artifact="artifact2.md",
        artifact_sha256="abc124",
        criteria_sha256="def457",
        profile_sha256="ghi790",
        verified_source_commit="commit2",
        verified_source_tree="tree2",
    )
    doc = VerdictDocument(
        schema_version=2,
        identity=identity,
        verdict=VerdictValue.FAIL,
        findings=sample_findings,
    )
    result = build_rework_addendum(doc)

    assert "## Verification feedback (attempt 2)" in result


def test_build_rework_addendum_requires_fail_verdict(
    sample_identity: VerdictIdentity,
    sample_findings: list[Finding],
) -> None:
    """Test that PASS and ERROR verdicts raise ValueError."""
    # Test PASS verdict
    doc_pass = VerdictDocument(
        schema_version=2,
        identity=sample_identity,
        verdict=VerdictValue.PASS,
        findings=sample_findings,
    )
    with pytest.raises(ValueError, match="rework addendum requires a FAIL verdict"):
        build_rework_addendum(doc_pass)

    # Test ERROR verdict
    doc_error = VerdictDocument(
        schema_version=2,
        identity=sample_identity,
        verdict=VerdictValue.ERROR,
        findings=sample_findings,
    )
    with pytest.raises(ValueError, match="rework addendum requires a FAIL verdict"):
        build_rework_addendum(doc_error)


def test_build_rework_addendum_empty_findings(
    sample_identity: VerdictIdentity,
) -> None:
    """Test with empty findings list."""
    doc = VerdictDocument(
        schema_version=2,
        identity=sample_identity,
        verdict=VerdictValue.FAIL,
        findings=[],
    )
    result = build_rework_addendum(doc)

    # Should still have header
    assert "## Verification feedback (attempt 1)" in result
    assert "The previous submission FAILED verification" in result
    # Verify no finding bullets when list is empty
    assert "- [" not in result
