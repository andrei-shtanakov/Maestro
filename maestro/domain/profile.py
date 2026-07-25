"""DomainProfile: the umbrella config for domain-adapted workstreams (§9).

One Protocol (verification) + two declarative policy sections (workspace,
delivery). The canonical effective-profile hash binds every verdict to the
exact behavior-affecting configuration it was produced under.
"""

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ALLOWED_PLACEHOLDERS: frozenset[str] = frozenset(
    {"artifact", "criteria", "out", "run_id", "attempt"}
)
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
_CANONICALIZATION_VERSION = 1


class VerifierSpec(BaseModel):
    """How to invoke the verifier: argv template, no shell interpolation."""

    model_config = ConfigDict(frozen=True)

    argv: list[str] = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    error_retry_budget: int = Field(ge=0, le=10)

    @field_validator("argv")
    @classmethod
    def validate_placeholders(cls, v: list[str]) -> list[str]:
        """Only the closed placeholder set is allowed (preflight-hard, §9)."""
        for arg in v:
            for name in _PLACEHOLDER_RE.findall(arg):
                if name not in ALLOWED_PLACEHOLDERS:
                    msg = f"unknown placeholder '{{{name}}}' in verifier argv"
                    raise ValueError(msg)
        return v


class CriteriaConfig(BaseModel):
    """Rubric reference. `source` is a reference, never inline rubric bytes.

    For visibility=shared the source is a repo-relative path; for
    verifier_only it is an operator-side reference whose BYTES are the only
    secret — the reference string itself is visible to the author (§7, §9).
    """

    model_config = ConfigDict(frozen=True)

    visibility: Literal["shared", "verifier_only"]
    source: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class VerificationSection(BaseModel):
    """Workstream-final verification; presence activates VERIFYING (§4, §9)."""

    model_config = ConfigDict(frozen=True)

    verifier: VerifierSpec
    artifact: str
    rework_budget: int = Field(ge=0, le=10)
    verdict_schema_version: Literal[2]
    criteria: CriteriaConfig


class RoleScopes(BaseModel):
    model_config = ConfigDict(frozen=True)

    write: list[str] = Field(min_length=1)


class WorkspacePolicy(BaseModel):
    """Per-role write authority + declared outputs (§6). Declarative only."""

    model_config = ConfigDict(frozen=True)

    roles: dict[str, RoleScopes]
    read_only: list[str] = Field(default_factory=list)
    evidence_root: str
    expected_outputs: dict[str, list[str]] = Field(default_factory=dict)


class DeliveryPolicy(BaseModel):
    """Declare-and-validate: names the one supported semantics (§8)."""

    model_config = ConfigDict(frozen=True)

    local_merge: Literal["before_remote_pr"]
    remote: Literal["github_pr"]
    evidence: Literal["all"]


class SpecGenSection(BaseModel):
    """Domain-owned spec-generation budgets; single SSOT rule in preflight."""

    model_config = ConfigDict(frozen=True)

    budget_usd: float | None = None
    timeout_minutes: float | None = None


class DomainProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    verification: VerificationSection
    workspace: WorkspacePolicy
    delivery: DeliveryPolicy
    spec_gen: SpecGenSection | None = None


def profile_sha256(profile: DomainProfile) -> str:
    """Hash of the canonical effective profile (§9).

    Computed AFTER pydantic validation/default expansion, sorted keys,
    canonical JSON, host-specific `criteria.source` excluded (its pinned
    sha256 stays in), explicit canonicalization version prefix so the
    algorithm itself is versioned.
    """
    payload = profile.model_dump(mode="json")
    payload["verification"]["criteria"].pop("source")
    payload["_canonicalization_version"] = _CANONICALIZATION_VERSION
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render_argv(template: list[str], values: dict[str, str]) -> list[str]:
    """Substitute placeholders; no shell is ever involved."""

    def sub(arg: str) -> str:
        return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], arg)

    return [sub(a) for a in template]
