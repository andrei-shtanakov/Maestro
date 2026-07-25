# Stage B: Verification FSM + Domain Contracts (Maestro part) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the workstream-level verification capability to Maestro: verdict
contract v2, `DomainProfile` config, `VERIFYING` FSM state with rework/reverify
resume semantics, evidence ledger, `CommandVerifier` running through the
execution layer, preflight gates — fully CI-covered by a scripted stub
verifier, with zero behavior change when no `domain:` section is configured.

**Architecture:** New `maestro/domain/` subpackage (precedent:
`maestro/execution/` from Phase 0) holds pure contracts (verdict v2 models +
handshake, DomainProfile + canonical profile hash, addendum materializer) and
the verification runtime (CommandVerifier, evidence ledger). `orchestrator.py`
gains the VERIFYING phase between the ex-post gates and MERGING; recovery and
resume reuse the H-6 marker pattern via a new `resume_reason` column.

**Design SSOT:** `../_cowork_output/plans/2026-07-25-stage-b-provider-design.md`
(approved v1.1). Section references (§N) below point there.

**Tech Stack:** Python 3.12+, pydantic v2, aiosqlite, existing
`maestro.execution` layer, pytest + anyio.

## Global Constraints

- ONLY `uv` (`uv run pytest`, `uv run pyrefly check`, `uv run ruff format .`,
  `uv run ruff check .`). Never pip.
- Type hints everywhere; `uv run pyrefly check` clean after every task.
- Line length 88; ruff format+check clean before each commit.
- **Zero behavior change without `domain:`** — no `domain:` in project.yaml ⇒
  `RUNNING → MERGING` path byte-identical (§4). Every task must keep the
  existing test suite green.
- Repo is PR-only: work on branch `feat/stage-b-verification`, no direct
  master commits. Commit after every task.
- Async tests use anyio (`@pytest.mark.anyio`), matching existing tests.
- New public APIs get docstrings.

## File Structure

```
maestro/domain/__init__.py        # re-exports (VerdictDocument, DomainProfile, ...)
maestro/domain/verdict.py         # Task 1: verdict v2 models + handshake evaluation
maestro/domain/profile.py         # Task 2: DomainProfile sections + profile_sha256
maestro/domain/resume.py          # Task 4: resume_reason constants + guards
maestro/domain/ledger.py          # Task 5: evidence ledger (DB index + file store)
maestro/domain/verifier.py        # Task 6: VerificationProvider Protocol + CommandVerifier
maestro/domain/addendum.py        # Task 7: deterministic rework addendum
maestro/models.py                 # Tasks 2,4: OrchestratorConfig.domain; VERIFYING; Workstream fields
maestro/database.py               # Task 3: migrations 12-14
maestro/orchestrator.py           # Task 8: VERIFYING phase, finalization, resume paths
                                  # Task 9: recovery for VERIFYING
maestro/preflight.py              # Task 10: domain checks + capability gate
maestro/schemas/generate.py       # Task 2: emit verdict_v2.json + domain profile schema
tests/fakes/stub_verifier.py      # Task 6: scripted stub verifier (CI tier, §10)
tests/test_domain_verdict.py      # Task 1
tests/test_domain_profile.py      # Task 2
tests/test_db_migration_verification.py  # Task 3
tests/test_workstream_verifying_fsm.py   # Task 4
tests/test_domain_ledger.py       # Task 5
tests/test_command_verifier.py    # Task 6
tests/test_domain_addendum.py     # Task 7
tests/test_orchestrator_verifying.py     # Task 8
tests/test_verifying_recovery.py  # Task 9
tests/test_preflight_domain.py    # Task 10
```

Plan-level concretizations of design open questions (§12), locked in here:
- **Ledger storage:** attempt bundles as files under
  `<db_dir>/evidence/<workstream_id>/<run_id>/attempt-NNN.*` + index table
  `verification_attempts` in maestro.db. Counters live on the workstream row.
- **Echo fields transport:** env vars `MAESTRO_PROFILE_SHA256`,
  `MAESTRO_VERIFIED_SOURCE_COMMIT`, `MAESTRO_VERIFIED_SOURCE_TREE` in the
  verifier's ExecutionRequest env.
- **argv placeholders (closed set):** `{artifact}`, `{criteria}`, `{out}`,
  `{run_id}`, `{attempt}`.
- **Evidence commit trailer:** `Maestro-Verification-Run: <run_id>`.
- **Evidence destination in worktree:** explicit
  `domain.workspace.evidence_root` (e.g. `verdicts/topic-x`); materialization
  writes `<evidence_root>/<run_id>/attempt-NNN.*`; preflight requires
  evidence_root ⊆ `roles.verifier.write`.
- **Criteria staging:** always staged — Maestro reads bytes (worktree path for
  `shared`, operator path for `verifier_only`), verifies pinned sha256, writes
  an ephemeral 0600 copy, passes it as `{criteria}`, deletes after the attempt.

---

### Task 1: Verdict v2 models + handshake evaluation

**Files:**
- Create: `maestro/domain/__init__.py`
- Create: `maestro/domain/verdict.py`
- Test: `tests/test_domain_verdict.py`

**Interfaces:**
- Produces: `VerdictValue` (StrEnum: `PASS|FAIL|ERROR`), `Finding`,
  `VerdictIdentity`, `VerdictDocument` (pydantic, `schema_version: Literal[2]`),
  `HandshakeResult(outcome: VerdictValue, protocol_error: str | None,
  document: VerdictDocument | None)`,
  `evaluate_handshake(json_path: Path, exit_code: int | None,
  timed_out: bool, expected: EchoExpectations) -> HandshakeResult`,
  `EchoExpectations(run_id, attempt, artifact, profile_sha256,
  verified_source_commit, verified_source_tree, workstream_id)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_domain_verdict.py
"""Verdict v2 contract: models and the strict file/process handshake (§5)."""

import json
from pathlib import Path

import pytest

from maestro.domain.verdict import (
    EchoExpectations,
    HandshakeResult,
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
    identity = {
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_domain_verdict.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'maestro.domain'`

- [ ] **Step 3: Implement `maestro/domain/verdict.py`**

```python
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
        document = VerdictDocument.model_validate(
            json.loads(json_path.read_text())
        )
    except (json.JSONDecodeError, ValidationError, OSError) as exc:
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
```

`maestro/domain/__init__.py`:

```python
"""Domain contracts and verification runtime (Stage B, design v1.1)."""

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
    "EchoExpectations",
    "Finding",
    "HandshakeResult",
    "VerdictDocument",
    "VerdictIdentity",
    "VerdictValue",
    "evaluate_handshake",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_domain_verdict.py -v`
Expected: all PASS

- [ ] **Step 5: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add maestro/domain tests/test_domain_verdict.py
git commit -m "feat(domain): verdict contract v2 models + strict handshake"
```

---

### Task 2: DomainProfile models, canonical profile hash, config + schema wiring

**Files:**
- Create: `maestro/domain/profile.py`
- Modify: `maestro/models.py:1436` (`OrchestratorConfig`: add `domain` field)
- Modify: `maestro/schemas/generate.py` (emit `verdict_v2.json`; the domain
  profile is embedded in the regenerated `orchestrator_config.json`)
- Test: `tests/test_domain_profile.py`

**Interfaces:**
- Consumes: `VerdictDocument` from Task 1 (schema emission only).
- Produces: `VerifierSpec(argv, timeout_seconds, error_retry_budget)`,
  `CriteriaConfig(visibility: Literal["shared","verifier_only"], source: str,
  sha256: str)`, `VerificationSection(verifier, artifact, rework_budget,
  verdict_schema_version, criteria)`, `RoleScopes(write: list[str])`,
  `WorkspacePolicy(roles: dict[str, RoleScopes], read_only, expected_outputs,
  evidence_root)`, `DeliveryPolicy(local_merge: Literal["before_remote_pr"],
  remote: Literal["github_pr"], evidence: Literal["all"])`,
  `SpecGenSection(budget_usd, timeout_minutes)`,
  `DomainProfile(verification, workspace, delivery, spec_gen)`,
  `profile_sha256(profile: DomainProfile) -> str`,
  `render_argv(template: list[str], values: dict[str, str]) -> list[str]`,
  `ALLOWED_PLACEHOLDERS: frozenset[str]`.
  `OrchestratorConfig.domain: DomainProfile | None = None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_domain_profile.py
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
                "argv": ["uv", "run", "bench-verify", "--out", "{out}",
                         "--artifact", "{artifact}", "--criteria", "{criteria}",
                         "--verification-run-id", "{run_id}",
                         "--attempt", "{attempt}"],
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_domain_profile.py -v`
Expected: FAIL with `ImportError` (no `maestro.domain.profile`)

- [ ] **Step 3: Implement `maestro/domain/profile.py`**

```python
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
```

- [ ] **Step 4: Wire `OrchestratorConfig.domain`**

In `maestro/models.py` add near the other imports:

```python
from maestro.domain.profile import DomainProfile
```

(If this creates an import cycle — `profile.py` must NOT import from
`maestro.models`; it doesn't — keep it a plain top-level import.)

In `class OrchestratorConfig` (models.py:1436), add alongside its other
optional sections:

```python
    domain: DomainProfile | None = Field(
        default=None,
        description=(
            "Domain profile (Stage B). Absent -> legacy behavior, "
            "byte-identical: no VERIFYING phase, no evidence machinery."
        ),
    )
```

Also export the verdict schema: in `maestro/schemas/generate.py` extend the
`schemas` dict:

```python
from maestro.domain.verdict import VerdictDocument

    schemas: dict[str, type] = {
        "project_config.json": ProjectConfig,
        "orchestrator_config.json": OrchestratorConfig,
        "verdict_v2.json": VerdictDocument,
    }
```

Run `uv run python -m maestro.schemas.generate` and commit the regenerated
`orchestrator_config.json` plus the new `verdict_v2.json` (this file is what
research-bench vendors as its pinned contract copy).

- [ ] **Step 5: Run tests, full suite, commit**

Run: `uv run pytest tests/test_domain_profile.py tests/test_config.py tests/test_examples_valid.py -v`
Expected: all PASS (existing configs have no `domain:` — must stay valid)

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add maestro/domain/profile.py maestro/domain/__init__.py maestro/models.py \
        maestro/schemas tests/test_domain_profile.py
git commit -m "feat(domain): DomainProfile schema + canonical profile hash + config wiring"
```

(Also add the new names to `maestro/domain/__init__.py` re-exports.)

---

### Task 3: Database migrations 12–14

**Files:**
- Modify: `maestro/database.py` (schema DDL ~line 179, migrations list
  ~line 442, row↔model mapping for workstreams)
- Test: `tests/test_db_migration_verification.py`

**Interfaces:**
- Produces: workstream columns `verification_run_id TEXT`,
  `verification_attempt INTEGER DEFAULT 0`,
  `verification_error_attempt INTEGER DEFAULT 0`,
  `rework_attempt INTEGER DEFAULT 0`, `resume_reason TEXT`;
  table `verification_attempts`; `execution_phase` accepting `'verification'`.
  DB methods: `insert_verification_attempt(...)`,
  `list_verification_attempts(run_id) -> list[VerificationAttemptRow]`,
  `mark_attempts_materialized(run_id) -> None`.

Three migrations, following the exact pattern of the `ordered` list at
`maestro/database.py:442`:

1. **Migration 12 `workstreams_verification_columns`** — `ALTER TABLE
   workstreams ADD COLUMN` for the five columns above (same additive pattern
   as migration 5 `decomposing_generation_pid`).
2. **Migration 13 `verification_attempts_table`**:

```sql
CREATE TABLE IF NOT EXISTS verification_attempts (
    run_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    workstream_id TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('PASS','FAIL','ERROR')),
    protocol_error TEXT,
    artifact_sha256 TEXT,
    json_path TEXT NOT NULL,
    md_path TEXT,
    raw_path TEXT,
    materialized INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, attempt)
);
```

3. **Migration 14 `execution_phase_verification`** — SQLite cannot alter a
   CHECK constraint (`execution_handles.execution_phase` is
   `CHECK (execution_phase IN ('task','validation'))`, database.py:154), so
   rebuild: `CREATE TABLE execution_handles_new` with
   `CHECK (execution_phase IN ('task','validation','verification'))` and
   otherwise identical DDL (copy from the current CREATE at ~line 140–160),
   `INSERT INTO ... SELECT`, `DROP TABLE execution_handles`,
   `ALTER TABLE execution_handles_new RENAME TO execution_handles`, recreate
   any indexes. Also update the base-schema DDL string so fresh databases get
   the widened CHECK directly.

Update workstream row mapping (both directions — the `Workstream(...)`
construction from rows and the INSERT/UPDATE column lists) and add the model
fields to `class Workstream` (models.py:1100):

```python
    verification_run_id: str | None = Field(default=None)
    verification_attempt: int = Field(default=0, ge=0)
    verification_error_attempt: int = Field(default=0, ge=0)
    rework_attempt: int = Field(default=0, ge=0)
    resume_reason: str | None = Field(
        default=None,
        description="verification_rework | verification_reverify | None",
    )
```

- [ ] **Step 1: Write failing tests** — model on the existing
  `tests/test_db_migration_ssh_handles.py` and
  `tests/test_execution_phase_column.py`:

```python
# tests/test_db_migration_verification.py (representative cases)
async def test_fresh_db_has_verification_columns(tmp_db) -> None:
    cols = await column_names(tmp_db, "workstreams")
    assert {"verification_run_id", "verification_attempt",
            "verification_error_attempt", "rework_attempt",
            "resume_reason"} <= set(cols)

async def test_old_db_migrates_in_place(tmp_path) -> None:
    # Create a DB with the pre-12 schema (copy the fixture approach used by
    # test_db_migration_ssh_handles), open with Database -> migrations run,
    # columns exist, existing rows keep their data.
    ...

async def test_execution_phase_accepts_verification(tmp_db) -> None:
    await tmp_db.save_execution_handle(..., execution_phase="verification")

async def test_verification_attempts_pk_rejects_duplicate(tmp_db) -> None:
    await tmp_db.insert_verification_attempt(run_id="r", attempt=1, ...)
    with pytest.raises(Exception):
        await tmp_db.insert_verification_attempt(run_id="r", attempt=1, ...)
```

Write these as complete tests using the existing fixtures in
`tests/conftest.py` (see how `test_database.py` builds a temp `Database`).

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_db_migration_verification.py -v` → FAIL
- [ ] **Step 3: Implement migrations + row mapping + DB methods** (as specified above)
- [ ] **Step 4: Run new tests + `tests/test_database.py` + `tests/test_execution_phase_column.py` + `tests/test_db_migration_ssh_handles.py`** → all PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(db): verification columns, attempts ledger table, execution_phase='verification'"`

---

### Task 4: FSM — VERIFYING status, transitions, resume constants

**Files:**
- Modify: `maestro/models.py:966-1014` (`WorkstreamStatus`)
- Create: `maestro/domain/resume.py`
- Test: `tests/test_workstream_verifying_fsm.py`

**Interfaces:**
- Produces: `WorkstreamStatus.VERIFYING`;
  `RESUME_REWORK = "verification_rework"`,
  `RESUME_REVERIFY = "verification_reverify"` in `maestro.domain.resume`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_workstream_verifying_fsm.py
"""§4 topology: one new durable state, five outgoing edges, one READY edge."""

from maestro.models import WorkstreamStatus as WS


def test_running_can_enter_verifying() -> None:
    assert WS.RUNNING.can_transition_to(WS.VERIFYING)


def test_verifying_edges() -> None:
    assert WS.VERIFYING.can_transition_to(WS.MERGING)        # PASS + evidence commit
    assert WS.VERIFYING.can_transition_to(WS.READY)          # FAIL, rework left
    assert WS.VERIFYING.can_transition_to(WS.NEEDS_REVIEW)   # rework exhausted / orphan
    assert WS.VERIFYING.can_transition_to(WS.FAILED)         # ERROR exhausted


def test_ready_can_enter_verifying_for_reverify_resume() -> None:
    assert WS.READY.can_transition_to(WS.VERIFYING)


def test_verifying_not_terminal_and_not_reachable_from_merging() -> None:
    assert not WS.VERIFYING.is_terminal()
    assert not WS.MERGING.can_transition_to(WS.VERIFYING)


def test_legacy_edges_untouched() -> None:
    # Zero-change: the legacy path must remain exactly as before.
    assert WS.RUNNING.can_transition_to(WS.MERGING)
    assert WS.RUNNING.can_transition_to(WS.FAILED)
    assert WS.MERGING.can_transition_to(WS.PR_CREATED)
    assert WS.FAILED.can_transition_to(WS.READY)
    assert WS.FAILED.can_transition_to(WS.NEEDS_REVIEW)
```

- [ ] **Step 2: Run to verify failure** — AttributeError: VERIFYING
- [ ] **Step 3: Implement**

In `WorkstreamStatus` add `VERIFYING = "verifying"` and update
`valid_transitions()` (models.py:995):

```python
            cls.READY: {cls.RUNNING, cls.VERIFYING, cls.NEEDS_REVIEW, cls.ABANDONED},
            cls.RUNNING: {cls.MERGING, cls.VERIFYING, cls.FAILED},
            cls.VERIFYING: {
                cls.MERGING,
                cls.READY,
                cls.FAILED,
                cls.NEEDS_REVIEW,
            },
```

Update the state-machine docstring diagram in the class accordingly (§4
figure, including the note that PASS opens MERGING only after the evidence
commit — finalization happens inside VERIFYING).

`maestro/domain/resume.py`:

```python
"""Durable resume reasons for the verification loop (§4, H-6 precedent).

Invariant: the author is respawned ONLY under RESUME_REWORK, which is set
ONLY by a genuine verdict FAIL. Crash, ERROR, recovery and operator re-queue
never set it.
"""

RESUME_REWORK = "verification_rework"
RESUME_REVERIFY = "verification_reverify"
```

- [ ] **Step 4: Run new tests + `tests/test_models.py` + `tests/test_transitions*.py`** → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(fsm): VERIFYING workstream state + resume reason constants"`

---

### Task 5: Evidence ledger

**Files:**
- Create: `maestro/domain/ledger.py`
- Test: `tests/test_domain_ledger.py`

**Interfaces:**
- Consumes: DB methods from Task 3; `HandshakeResult`/`VerdictValue` (Task 1).
- Produces:

```python
class EvidenceLedger:
    def __init__(self, db: Database, root: Path) -> None: ...
    def staging_dir(self, workstream_id: str, run_id: str, attempt: int) -> Path
    async def ingest_attempt(
        self, *, workstream_id: str, run_id: str, attempt: int,
        result: HandshakeResult, staging: Path,
    ) -> IngestedAttempt        # moves bundle under root, indexes in DB
    async def list_bundle(self, run_id: str) -> list[IngestedAttempt]
    async def materialize(
        self, *, run_id: str, worktree: Path, evidence_root: str,
    ) -> list[Path]             # copies ALL attempts into the worktree (§6/§8)
    async def mark_materialized(self, run_id: str) -> None
    def latest_fail(self, bundle: list[IngestedAttempt]) -> IngestedAttempt | None
```

Behavior (each is a test):
- `ingest_attempt` refuses to overwrite an existing `attempt-NNN.json`
  (protocol ERROR upstream — raise `LedgerCollisionError`); writes are
  atomic (tmp sibling + `os.replace`); missing `.md`/`.raw.txt` sidecars are
  recorded as an anomaly flag on the row, never an exception (§5 sidecar
  rule); the bundle is ingested for EVERY outcome including protocol errors
  (append-only evidence).
- Ledger root lives beside the DB: orchestrator passes
  `db_path.parent / "evidence"`; final layout
  `<root>/<workstream_id>/<run_id>/attempt-001.json` etc.
- `materialize` writes `<worktree>/<evidence_root>/<run_id>/attempt-NNN.*`
  for all ingested attempts and returns the created paths; it never
  overwrites divergent existing files (idempotent re-run tolerates identical
  content — compare bytes, raise on mismatch).

- [ ] **Step 1: Write failing tests** for each behavior above (complete
  pytest functions with tmp_path + temp Database, modeled on
  `tests/test_database.py` fixtures).
- [ ] **Step 2: Verify failure** — `uv run pytest tests/test_domain_ledger.py -v`
- [ ] **Step 3: Implement `EvidenceLedger`** exactly to the interface above.
- [ ] **Step 4: Verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(domain): evidence ledger (durable attempt store + delivery materialization)"`

---

### Task 6: VerificationProvider Protocol + CommandVerifier + stub verifier

**Files:**
- Create: `maestro/domain/verifier.py`
- Create: `tests/fakes/stub_verifier.py`
- Test: `tests/test_command_verifier.py`

**Interfaces:**
- Consumes: `render_argv`/`VerifierSpec`/`CriteriaConfig` (Task 2),
  `evaluate_handshake`/`EchoExpectations` (Task 1), `ExecutionBackend` /
  `ExecutionRequest` (`maestro/execution/models.py:36` — note required
  fields `run_id, argv, workdir, log_path, collect`, use
  `CollectPolicy` mode none as SSH tests do), ledger staging dir (Task 5).
- Produces:

```python
class VerificationProvider(Protocol):
    async def verify(self, ctx: VerificationContext) -> HandshakeResult: ...

class VerificationContext(BaseModel):
    workstream_id: str
    run_id: str
    attempt: int
    rework_attempt: int
    worktree: Path
    out_json: Path                  # Maestro-assigned --out (ledger staging)
    profile_sha256: str
    verified_source_commit: str
    verified_source_tree: str

class CommandVerifier:                      # implements the Protocol
    def __init__(self, spec: VerifierSpec, criteria: CriteriaConfig,
                 artifact: str, backend: ExecutionBackend, ...) -> None
    async def verify(self, ctx: VerificationContext) -> HandshakeResult
```

`CommandVerifier.verify` must:
1. Verify the worktree is clean (`git status --porcelain` empty) —
   pre-run baseline (§6 step 1); dirty → protocol ERROR without running.
2. Stage criteria: read bytes (worktree-relative for `shared`, absolute
   operator path for `verifier_only`), check
   `hashlib.sha256(bytes) == criteria.sha256` (mismatch → protocol ERROR),
   write 0600 tmp copy, delete in `finally` (§7).
3. Recompute `artifact_sha256` of `<worktree>/<artifact>` — kept for the
   post-run cross-check of the returned document (§5).
4. Build `ExecutionRequest(run_id=f"verify-{ctx.run_id}-{ctx.attempt}",
   argv=render_argv(...), workdir=ctx.worktree, timeout_seconds=spec.
   timeout_seconds, env={"MAESTRO_PROFILE_SHA256": ..., "MAESTRO_VERIFIED_
   SOURCE_COMMIT": ..., "MAESTRO_VERIFIED_SOURCE_TREE": ...},
   execution_id=..., entity_kind="workstream", attempt=ctx.attempt,
   labels={"execution_phase": "verification"}, collect=<none policy>,
   log_path=<beside out_json>)` and run it via the backend, persisting the
   handle with `execution_phase="verification"` (mirror how orchestrator.py
   persists task handles at ~line 1208-1241).
5. `evaluate_handshake(ctx.out_json, exit_code, timed_out, expected)`; then
   two extra protocol checks: recomputed artifact sha vs
   `document.identity.artifact_sha256`, and worktree still clean
   (`git status --porcelain` empty — verifier must not touch the worktree,
   §6 step 3); on violation degrade to protocol ERROR.

**Stub verifier** (`tests/fakes/stub_verifier.py`) — the CI-tier workhorse
(§10): an executable script taking `--out`, `--script` (JSON file listing
per-attempt directives) plus all real flags; each invocation pops the next
directive: `{"verdict": "PASS"|"FAIL"|"ERROR"}` |
`{"mode": "missing_file"}` | `{"mode": "exit_mismatch"}` |
`{"mode": "hang"}` (sleep past timeout) | `{"mode": "dirty_worktree"}` |
`{"mode": "wrong_echo", "field": ...}`. It writes a fully valid v2 document
(echo fields from env/argv) unless the directive says otherwise, and exits
with the matching contract code. Keep it dependency-free (stdlib only) so it
runs as a subprocess under LocalBackend in tests.

- [ ] **Step 1: Write failing tests** — via LocalBackend + tmp git repo
  fixture (init repo, commit an artifact file):
  - PASS directive → `HandshakeResult.outcome is PASS`, handle persisted
    with `execution_phase="verification"`.
  - FAIL directive → FAIL with findings parsed.
  - `exit_mismatch` → ERROR + protocol_error.
  - `missing_file` → ERROR.
  - `hang` with `timeout_seconds=1` → ERROR (timed out), no exception.
  - `dirty_worktree` → ERROR protocol (worktree not clean after run).
  - criteria sha mismatch → ERROR protocol, verifier process never spawned.
  - staged criteria copy is deleted after the attempt (assert tmp gone).
  - artifact sha cross-check: stub writes wrong artifact_sha256 → ERROR.
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement `verifier.py` + `stub_verifier.py`.**
- [ ] **Step 4: Verify pass** — `uv run pytest tests/test_command_verifier.py -v`
- [ ] **Step 5: Commit** — `git commit -m "feat(domain): CommandVerifier via execution layer + scripted stub verifier"`

---

### Task 7: Deterministic rework addendum

**Files:**
- Create: `maestro/domain/addendum.py`
- Test: `tests/test_domain_addendum.py`

**Interfaces:**
- Consumes: `VerdictDocument`, `Finding` (Task 1).
- Produces: `build_rework_addendum(document: VerdictDocument) -> str`.

Rules (§7 — each is a test):
- Output contains, per finding: severity + `author_feedback` text only.
- Output NEVER contains: `criterion_id`, `evidence` field text, raw
  identity hashes (`criteria_sha256` value), the word-for-word rubric.
- Deterministic: same document → identical string (no timestamps, no
  randomness, no LLM).
- Stable shape (consumed by the respawn description, Task 8):

```
## Verification feedback (attempt N)

The previous submission FAILED verification. Address every item below,
then the report will be re-verified.

- [major] Separate cited evidence from inference.
- [minor] ...
```

- [ ] **Step 1: Write failing tests** (assert inclusion of author_feedback,
  exclusion of criterion_id/evidence strings, determinism via double call).
- [ ] **Step 2: Verify failure.** **Step 3: Implement (~30 lines).**
- [ ] **Step 4: Verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(domain): deterministic rework addendum (declassification channel only)"`

---

### Task 8: Orchestrator integration — the VERIFYING phase

**Files:**
- Modify: `maestro/orchestrator.py` (`_handle_success` at :1688; READY pickup
  where the H-6 marker is parsed at :999; spawn path for description
  augmentation)
- Test: `tests/test_orchestrator_verifying.py`

**Interfaces:**
- Consumes: everything from Tasks 1–7.
- Produces: `Orchestrator._run_verification(workstream_id, workspace_path)`,
  `Orchestrator._finalize_verification(...)` (ledger materialize + evidence
  commit + MERGING), `Orchestrator._respawn_for_rework(...)`.

Control flow — replace the unconditional MERGING transition inside
`_handle_success` (orchestrator.py:1709-1714). After both gates pass:

```python
        profile = self._config.domain
        if profile is not None:
            await self._transition(
                workstream_id,
                WorkstreamStatus.VERIFYING,
                expected_status=WorkstreamStatus.RUNNING,
            )
            await self._run_verification(workstream_id, workspace_path)
            return
        # Legacy path (zero-change): straight to MERGING as before.
```

`_run_verification` (new, ~orchestrator.py after `_handle_success`):

1. Load workstream; mint `verification_run_id` if empty
   (`uuid.uuid7()` if available else `uuid.uuid4()`, stored via
   `update_workstream_status` extra fields) — minted ONCE per run (§5).
2. Compute `verified_source_commit` (existing `_workspace_head` helper,
   orchestrator.py:1576) and `verified_source_tree`
   (`git rev-parse HEAD^{tree}` via the same git-subprocess helper style).
3. ERROR loop: while `verification_error_attempt <= error_retry_budget`:
   increment `verification_attempt` (persist BEFORE spawning — §5
   monotonicity), build `VerificationContext` with
   `out_json=ledger.staging_dir(...)/attempt-NNN.json`, call
   `CommandVerifier.verify`, `ledger.ingest_attempt(...)` (ALWAYS — every
   outcome is evidence), then:
   - **PASS** → `_finalize_verification(...)`; return.
   - **FAIL** → if `rework_attempt < rework_budget`: transition
     VERIFYING→READY with extra fields `resume_reason=RESUME_REWORK`,
     `rework_attempt=rework_attempt + 1`, `process_pid=None`; else
     transition VERIFYING→NEEDS_REVIEW with an explanatory error_message
     (rework budget exhausted). Return.
   - **ERROR** → increment `verification_error_attempt`, continue loop.
4. Loop exhausted → transition VERIFYING→FAILED with
   `resume_reason=RESUME_REVERIFY` and error_message naming the last
   protocol/infrastructure error. (The retry rule / operator re-queue then
   lands it in READY, and the READY pickup below routes it back to
   VERIFYING — never to an author respawn. §4 invariant.)

`_finalize_verification` (PASS only; workstream REMAINS in VERIFYING
throughout — §4 finalization):

1. `ledger.materialize(run_id=..., worktree=workspace_path,
   evidence_root=profile.workspace.evidence_root)`.
2. Evidence commit: stage exactly the materialized paths, verify
   `git diff --cached --name-only` ⊆ `roles.verifier.write` globs (reuse
   `find_escapes`/`normalize` from the scope gate imports), verify
   `HEAD == verified_source_commit` (parent rule §8; mismatch → stale PASS →
   VERIFYING→NEEDS_REVIEW), commit with message
   `"verification evidence <run_id>\n\nMaestro-Verification-Run: <run_id>"`.
   Idempotency: if a commit with this trailer already exists on the branch
   (check `git log --grep`), skip creation (recovery re-entry).
3. `ledger.mark_materialized(run_id)` (wraps the Task-3 DB method
   `mark_attempts_materialized`).
4. Transition VERIFYING→MERGING, then fall into the existing MERGING tail
   (push/PR — reuse the code currently under the MERGING transition by
   extracting it into `_merge_and_pr(workstream_id, workstream)` so both the
   legacy path and finalization call it; pure refactor, no behavior change).
5. Any failure in steps 1–3 → log + workstream STAYS in VERIFYING
   (delivery-preparation error, §8); recovery re-enters finalization.

READY pickup (at the H-6 marker parse, orchestrator.py:999): before the
normal spawn decision add:

```python
        if workstream.resume_reason == RESUME_REVERIFY:
            await self._transition(
                workstream_id,
                WorkstreamStatus.VERIFYING,
                expected_status=WorkstreamStatus.READY,
                resume_reason=None,
            )
            await self._run_verification(workstream_id, workspace_path)
            continue
        if workstream.resume_reason == RESUME_REWORK:
            # Normal respawn path, but the spec-gen description gets the
            # addendum; clear the marker only after a successful spawn.
            addendum = await self._load_rework_addendum(workstream)
```

`_load_rework_addendum`: `ledger.list_bundle(run_id)` → `latest_fail` →
parse its `json_path` into `VerdictDocument` → `build_rework_addendum`.
Append to the workstream description passed into spec generation (the
existing decomposer/generate_spec call path) — the §7 visibility filter is
exactly this materialization point.

- [ ] **Step 1: Write failing tests** — drive a real `Orchestrator` with a
  temp git repo + LocalBackend + stub verifier profile (fixture: project
  config with `domain:` pointing verifier argv at
  `sys.executable tests/fakes/stub_verifier.py ...`), monkeypatching the
  spec-runner spawn to a no-op that commits a fixed artifact (follow the
  established pattern in `tests/e2e`/`test_orchestrator*.py` fakes):
  - PASS script → workstream reaches MERGING; branch has evidence commit
    with trailer; commit parent == pre-verification HEAD; diff ⊆
    verifier.write.
  - FAIL then PASS script → after FAIL: status READY,
    `resume_reason == "verification_rework"`, `rework_attempt == 1`, run_id
    unchanged; respawned description contains addendum text and does NOT
    contain `criterion_id`; second cycle ends in MERGING with BOTH attempts
    materialized.
  - FAIL with `rework_budget: 0` → NEEDS_REVIEW (golden-4 terminal rule).
  - ERROR×(budget+1) script → FAILED with
    `resume_reason == "verification_reverify"`; re-queue to READY →
    orchestrator goes straight to VERIFYING (stub now PASS) → MERGING;
    author spawn count unchanged; `rework_attempt == 0`.
  - No `domain:` in config → stub never invoked, RUNNING→MERGING direct
    (zero-change; assert no VERIFYING row ever written).
  - Finalization failure (make evidence_root unwritable) → workstream still
    VERIFYING; second `_run_verification` entry completes materialization
    without a duplicate evidence commit.
- [ ] **Step 2: Verify failure.** **Step 3: Implement.**
- [ ] **Step 4: Run the new tests + the full orchestrator test files** → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(orchestrator): VERIFYING phase — verification loop, finalization, rework/reverify resume"`

---

### Task 9: Recovery for VERIFYING

**Files:**
- Modify: `maestro/orchestrator.py` (startup reconcile block, ~:374-560)
- Test: `tests/test_verifying_recovery.py`

**Interfaces:**
- Consumes: execution-handle probe machinery already used for RUNNING
  recovery (orchestrator.py:660-780), `EvidenceLedger`, resume constants.

Reconcile rules for a workstream found in VERIFYING at startup (§4/§10,
mirroring the existing stranded-state table at orchestrator.py:374):

- Open verification handle probes **alive** → leave in VERIFYING, resume
  monitoring (no duplicate spawn).
- Probe says **dead/collected** → stay in VERIFYING; the main loop re-enters
  `_run_verification`, which mints a NEW `verification_attempt` under the
  SAME `verification_run_id` (re-verify is safe: append-only attempts).
- Probe **ambiguous** (spawning sentinel / probe error) → NEEDS_REVIEW,
  fail-closed, same accounting as the existing live-orphan rule.
- A PASS attempt already in the ledger but no evidence commit (crash inside
  finalization) → re-enter `_finalize_verification` only (idempotent by
  trailer; no new verifier run).
- In every branch: `rework_attempt` is NOT incremented (assert in tests).

- [ ] **Step 1: Write failing tests** — construct DB states directly
  (workstream row in VERIFYING + handle rows / ledger rows), run the
  recovery entrypoint, assert the resulting status + counters (pattern:
  `tests/test_docker_recovery.py`).
- [ ] **Step 2: Verify failure.** **Step 3: Implement.**
- [ ] **Step 4: Verify pass + full recovery test files.**
- [ ] **Step 5: Commit** — `git commit -m "feat(recovery): VERIFYING reconcile — alive/dead/ambiguous handles, idempotent finalization"`

---

### Task 10: Preflight domain checks

**Files:**
- Modify: `maestro/preflight.py` (add `_check_domain(config, report)` wired
  into `validate_project`, preflight.py:97)
- Test: `tests/test_preflight_domain.py`

Checks (each one test; severities: error unless noted):

1. **Capability gate (§7):** `criteria.visibility == "verifier_only"` and
   any participating workstream resolves to a backend without filesystem
   isolation (backend id `local` / isolation not docker — resolve via the
   same config the orchestrator uses) → ERROR "verifier_only requires an
   isolated author backend".
2. **evidence_root containment:** `evidence_root` not matched by
   `roles.verifier.write` globs → ERROR.
3. **Role/scope coherence:** workstream `scope` (author authority) must
   equal/⊆ `roles.author.write`; overlap between author and verifier write
   sets → ERROR.
4. **expected_outputs vs preflight noise (§6, friction #10):** patterns
   listed under `expected_outputs` are exempted from the existing
   scope-no-match filesystem warnings in `_check_scope_fs`
   (preflight.py:326) — WARNING downgrade to silence.
5. **spec_gen SSOT (§9):** `domain.spec_gen` set AND legacy
   `spec_runner.spec_gen_budget_usd`-style fields set → ERROR (single
   source rule).
6. **verifier_only source sanity:** `criteria.source` resolving inside the
   repo (relative path or under repo_path) while visibility is
   verifier_only → ERROR ("rubric committed to the target repo cannot be
   verifier-only", §7).
7. **artifact declared:** `verification.artifact` matched by
   `roles.author.write` → OK; not matched → ERROR (the author could never
   produce it).

- [ ] **Step 1: Write failing tests** (build `OrchestratorConfig` fixtures
  with the Task-2 profile dict; call `validate_project`; assert
  `report.errors` codes/messages).
- [ ] **Step 2: Verify failure.** **Step 3: Implement.**
- [ ] **Step 4: Verify pass + `tests/test_preflight*.py`.**
- [ ] **Step 5: Commit** — `git commit -m "feat(preflight): domain profile checks incl. verifier_only capability gate"`

---

### Task 11: Docs, schema artifacts, full-suite gate

**Files:**
- Modify: `CLAUDE.md` (Workstream State Machine diagram + a short
  "Domain verification (Stage B)" subsection under Key Design Decisions)
- Modify: `README.md` if it shows the workstream FSM
- Regenerate: `maestro/schemas/*.json`

- [ ] **Step 1:** Update the CLAUDE.md workstream FSM diagram to include
  VERIFYING with its five edges + the READY reverify edge; add 6-8 lines:
  activation by `domain.verification.verifier` presence, evidence ledger
  location (`<db_dir>/evidence/`), evidence-commit trailer, zero-change
  guarantee.
- [ ] **Step 2:** `uv run python -m maestro.schemas.generate` — commit
  regenerated JSON (verdict_v2.json is the vendoring source for
  research-bench).
- [ ] **Step 3:** Full gate:

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
uv run pytest
```

Expected: entire suite PASS (zero-change proof for legacy configs).

- [ ] **Step 4: Commit** — `git commit -m "docs+schemas: VERIFYING state machine, domain verification notes, verdict v2 schema"`
- [ ] **Step 5:** Push branch, open PR
  (`gh pr create` — title "Stage B: verification FSM + domain contracts"),
  then action the GitHub Copilot review before calling it done (repo rule).

---

## Design §10 CI-coverage map (verify before opening the PR)

| Design CI bullet | Covered by |
|---|---|
| Zero-change без `domain:` | Task 8 test (no-domain) + full suite in Task 11 |
| Handshake-таблица целиком + attempt-коллизия | Task 1 tests; Task 5 collision test |
| Recovery: alive/dead/ambiguous, rework_attempt intact | Task 9 tests |
| Stale-PASS: tree changed / parent rule | Task 8 (finalization parent check) |
| Сбой материализации не перезапускает никого | Task 8 finalization-failure test |
| Идемпотентность evidence commit по trailer | Task 8 + Task 9 tests |
| Два resume paths (rework vs reverify) | Task 8 tests |
| Ledger недоступен автору до delivery | Ledger root вне worktree (Task 5) + Task 8 assert (addendum-only in description) |
| Невалидные sha / echo-поля → protocol ERROR | Task 1 + Task 6 tests |
| Capability gate в preflight | Task 10 test 1 |
