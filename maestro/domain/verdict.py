"""Verdict contract v2: canonical document models and the strict handshake.

The JSON file is the only authoritative orchestration input; the process
exit code is a fail-closed backstop (design §5). Any inconsistency between
the two, any missing/invalid file, and any echo-field mismatch degrade to
ERROR (never to PASS/FAIL).
"""

import json
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class VerdictValue(StrEnum):
    """Fail-closed verdict values; exit contract 0=PASS, 1=FAIL, 2=ERROR."""

    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"


EXIT_FOR_VERDICT: dict[VerdictValue, int] = {
    VerdictValue.PASS: 0,
    VerdictValue.FAIL: 1,
    VerdictValue.ERROR: 2,
}


class Finding(BaseModel):
    """One criterion violation reported by the verifier."""

    model_config = ConfigDict(frozen=True)

    criterion_id: str
    severity: str
    evidence: str
    author_feedback: str = Field(
        ...,
        description=(
            "Actionable text authored by the verifier FOR the author — the "
            "explicit declassification channel (§7). The only finding field "
            "the rework addendum may carry across the verifier->author "
            "boundary (besides severity)."
        ),
    )


class VerdictIdentity(BaseModel):
    """Identity block binding an attempt to run, artifact, rubric, profile."""

    model_config = ConfigDict(frozen=True)

    verification_run_id: str
    verification_attempt: int = Field(ge=1)
    rework_attempt: int = Field(ge=0)
    workstream_id: str
    artifact: str
    artifact_sha256: str
    criteria_sha256: str
    profile_sha256: str
    verified_source_commit: str
    verified_source_tree: str


class VerdictDocument(BaseModel):
    """Canonical machine verdict; .md/.raw.txt are non-authoritative sidecars."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[2]
    identity: VerdictIdentity
    verdict: VerdictValue
    findings: list[Finding] = Field(default_factory=list)


class EchoExpectations(BaseModel):
    """Maestro-computed values the verifier must echo back unchanged (§5)."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    attempt: int
    workstream_id: str
    artifact: str
    profile_sha256: str
    verified_source_commit: str
    verified_source_tree: str


class HandshakeResult(BaseModel):
    """Outcome of the file/process handshake for one verifier attempt."""

    model_config = ConfigDict(frozen=True)

    outcome: VerdictValue
    protocol_error: str | None = None
    document: VerdictDocument | None = None


def _protocol_error(message: str) -> HandshakeResult:
    return HandshakeResult(
        outcome=VerdictValue.ERROR, protocol_error=message, document=None
    )


def evaluate_handshake(
    json_path: Path,
    exit_code: int | None,
    timed_out: bool,
    expected: EchoExpectations,
) -> HandshakeResult:
    """Apply the §5 handshake table. The document is authoritative only when
    the process completed and every consistency check agrees; otherwise ERROR.
    """
    if timed_out or exit_code is None:
        # File (if any) is retained by the caller as forensic evidence.
        return _protocol_error("verifier process timed out or crashed")
    if not json_path.is_file():
        return _protocol_error(f"verdict file missing: {json_path}")
    try:
        document = VerdictDocument.model_validate(json.loads(json_path.read_text()))
    except (ValueError, ValidationError, OSError) as exc:
        return _protocol_error(f"verdict file invalid: {exc}")

    identity = document.identity
    mismatches = [
        name
        for name, got, want in (
            ("verification_run_id", identity.verification_run_id, expected.run_id),
            ("verification_attempt", identity.verification_attempt, expected.attempt),
            ("workstream_id", identity.workstream_id, expected.workstream_id),
            ("artifact", identity.artifact, expected.artifact),
            ("profile_sha256", identity.profile_sha256, expected.profile_sha256),
            (
                "verified_source_commit",
                identity.verified_source_commit,
                expected.verified_source_commit,
            ),
            (
                "verified_source_tree",
                identity.verified_source_tree,
                expected.verified_source_tree,
            ),
        )
        if got != want
    ]
    if mismatches:
        return _protocol_error(f"echo-field mismatch: {', '.join(mismatches)}")
    if exit_code != EXIT_FOR_VERDICT[document.verdict]:
        return _protocol_error(
            f"exit code {exit_code} does not match verdict {document.verdict}"
        )
    return HandshakeResult(
        outcome=document.verdict, protocol_error=None, document=document
    )
