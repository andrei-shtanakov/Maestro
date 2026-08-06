"""Domain contracts and verification runtime (Stage B, design v1.1)."""

from maestro.domain.addendum import build_rework_addendum
from maestro.domain.ledger import EvidenceLedger, IngestedAttempt, LedgerCollisionError
from maestro.domain.profile import (
    ALLOWED_PLACEHOLDERS,
    CriteriaConfig,
    DeliveryPolicy,
    DomainProfile,
    RoleScopes,
    SpecGenSection,
    VerificationSection,
    VerifierSpec,
    WorkspacePolicy,
    profile_sha256,
    render_argv,
)
from maestro.domain.resume import (
    KNOWN_RESUME_REASONS,
    RESUME_OPERATOR_REWORK,
    RESUME_REVERIFY,
    RESUME_REWORK,
)
from maestro.domain.verdict import (
    EchoExpectations,
    Finding,
    HandshakeResult,
    VerdictDocument,
    VerdictIdentity,
    VerdictValue,
    evaluate_handshake,
)
from maestro.domain.verifier import (
    CommandVerifier,
    VerificationContext,
    VerificationProvider,
)


__all__ = [
    "ALLOWED_PLACEHOLDERS",
    "KNOWN_RESUME_REASONS",
    "RESUME_OPERATOR_REWORK",
    "RESUME_REVERIFY",
    "RESUME_REWORK",
    "CommandVerifier",
    "CriteriaConfig",
    "DeliveryPolicy",
    "DomainProfile",
    "EchoExpectations",
    "EvidenceLedger",
    "Finding",
    "HandshakeResult",
    "IngestedAttempt",
    "LedgerCollisionError",
    "RoleScopes",
    "SpecGenSection",
    "VerdictDocument",
    "VerdictIdentity",
    "VerdictValue",
    "VerificationContext",
    "VerificationProvider",
    "VerificationSection",
    "VerifierSpec",
    "WorkspacePolicy",
    "build_rework_addendum",
    "evaluate_handshake",
    "profile_sha256",
    "render_argv",
]
