"""DomainProfile: schema, canonical profile hash, argv placeholders (§9)."""

import pytest
from pydantic import ValidationError

from maestro.domain.profile import (
    ALLOWED_PLACEHOLDERS,
    DomainProfile,
    profile_sha256,
    render_argv,
)
from maestro.models import OrchestratorConfig


def profile_dict(**overrides: object) -> dict:
    base: dict = {
        "verification": {
            "verifier": {
                "argv": [
                    "uv",
                    "run",
                    "bench-verify",
                    "--out",
                    "{out}",
                    "--artifact",
                    "{artifact}",
                    "--criteria",
                    "{criteria}",
                    "--verification-run-id",
                    "{run_id}",
                    "--attempt",
                    "{attempt}",
                ],
                "timeout_seconds": 180,
                "error_retry_budget": 2,
            },
            "artifact": "reports/topic-x/result.md",
            "rework_budget": 2,
            "verdict_schema_version": 2,
            "criteria": {
                "visibility": "shared",
                "source": "briefs/topic-x/criteria.yaml",
                "sha256": "b" * 64,
            },
        },
        "workspace": {
            "roles": {
                "author": {"write": ["reports/topic-x/**"]},
                "verifier": {"write": ["verdicts/topic-x/**"]},
            },
            "read_only": ["briefs/**"],
            "evidence_root": "verdicts/topic-x",
            "expected_outputs": {
                "author": ["reports/topic-x/result.md"],
                "verification": ["verdicts/topic-x/*/attempt-*.json"],
                "delivery": ["reports/topic-x/result.md", "verdicts/topic-x/**"],
            },
        },
        "delivery": {
            "local_merge": "before_remote_pr",
            "remote": "github_pr",
            "evidence": "all",
        },
    }
    base.update(overrides)
    return base


def test_valid_profile_parses() -> None:
    profile = DomainProfile.model_validate(profile_dict())
    assert profile.verification.verifier.error_retry_budget == 2


def test_unknown_delivery_mode_rejected() -> None:
    # declare-and-validate (§8): only before_remote_pr exists in Stage B.
    bad = profile_dict()
    bad["delivery"]["local_merge"] = "none"
    with pytest.raises(ValidationError):
        DomainProfile.model_validate(bad)


def test_unknown_placeholder_rejected() -> None:
    bad = profile_dict()
    bad["verification"]["verifier"]["argv"] = ["run", "{unknown}"]
    with pytest.raises(ValidationError, match="unknown placeholder"):
        DomainProfile.model_validate(bad)


def test_profile_hash_ignores_host_specific_source() -> None:
    # §9: criteria.source excluded from canonicalization, criteria.sha256 kept.
    a = DomainProfile.model_validate(profile_dict())
    b_dict = profile_dict()
    b_dict["verification"]["criteria"]["source"] = "/other/host/criteria.yaml"
    b = DomainProfile.model_validate(b_dict)
    assert profile_sha256(a) == profile_sha256(b)


def test_profile_hash_changes_on_behavior_change() -> None:
    a = DomainProfile.model_validate(profile_dict())
    b_dict = profile_dict()
    b_dict["verification"]["rework_budget"] = 3
    b = DomainProfile.model_validate(b_dict)
    assert profile_sha256(a) != profile_sha256(b)


def test_render_argv_substitutes_all_placeholders() -> None:
    values = {p: f"V_{p}" for p in ALLOWED_PLACEHOLDERS}
    out = render_argv(["x", "{out}", "pre-{attempt}"], values)
    assert out == ["x", "V_out", "pre-V_attempt"]


def test_orchestrator_config_domain_defaults_to_none() -> None:
    # Zero-change guarantee: domain is absent unless configured.
    field = OrchestratorConfig.model_fields["domain"]
    assert field.default is None
