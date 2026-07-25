"""Task 7: Deterministic rework addendum (declassification channel materializer).

The rework addendum is a deterministic text summary of findings for the author,
carrying only severity and author_feedback (§7 declassification channel). No
criterion_id, evidence text, hashes, timestamps, or randomness.
"""

from maestro.domain.verdict import VerdictDocument


def build_rework_addendum(document: VerdictDocument) -> str:
    """Build deterministic rework addendum from a VerdictDocument.

    Args:
        document: The VerdictDocument with identity and findings.

    Returns:
        A deterministic string containing the verification feedback header
        and the list of findings (severity + author_feedback only).
    """
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
