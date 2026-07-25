"""Domain contracts and verification runtime (Stage B, design v1.1)."""

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
from maestro.domain.resume import RESUME_REVERIFY, RESUME_REWORK
from maestro.domain.verdict import (
    EchoExpectations,
    Finding,
    HandshakeResult,
    VerdictDocument,
    VerdictIdentity,
    VerdictValue,
    evaluate_handshake,
)


__all__ = [
    "ALLOWED_PLACEHOLDERS",
    "RESUME_REVERIFY",
    "RESUME_REWORK",
    "CriteriaConfig",
    "DeliveryPolicy",
    "DomainProfile",
    "EchoExpectations",
    "Finding",
    "HandshakeResult",
    "RoleScopes",
    "SpecGenSection",
    "VerdictDocument",
    "VerdictIdentity",
    "VerdictValue",
    "VerificationSection",
    "VerifierSpec",
    "WorkspacePolicy",
    "evaluate_handshake",
    "profile_sha256",
    "render_argv",
]
