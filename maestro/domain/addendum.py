"""Task 7: Deterministic rework addendum (declassification channel materializer).

The rework addendum is a deterministic text summary of findings for the author,
carrying only severity and author_feedback (§7 declassification channel). No
criterion_id, evidence text, hashes, timestamps, or randomness.
"""

from maestro.domain.verdict import VerdictDocument, VerdictValue


def build_rework_addendum(document: VerdictDocument) -> str:
    """Build deterministic rework addendum from a FAIL VerdictDocument.

    Only ever called on FAIL documents by the rework respawn path (Task 8).

    Args:
        document: A VerdictDocument with verdict=FAIL and identity + findings.

    Returns:
        A deterministic string containing the verification feedback header
        and the list of findings (severity + author_feedback only).

    Raises:
        ValueError: If document.verdict is not FAIL.
    """
    if document.verdict is not VerdictValue.FAIL:
        raise ValueError(
            f"rework addendum requires a FAIL verdict, got {document.verdict}"
        )

    attempt = document.identity.verification_attempt

    lines = [
        f"## Verification feedback (attempt {attempt})",
        "",
        "The previous submission FAILED verification. Address every item below,",
        "then the report will be re-verified.",
        "",
    ]

    for finding in document.findings:
        lines.append(f"- [{finding.severity}] {finding.author_feedback}")

    return "\n".join(lines)
