"""Preflight validation for Mode-2 orchestrator configs (project.yaml).

Aggregates errors and warnings into a ValidationReport instead of raising,
so callers can render everything at once. Schema-level validation (duplicate
ids, unknown deps, self-deps) catches these on config load in the pydantic
models; preflight repeats selected graph-integrity checks (dangling deps,
cycles) as defense-in-depth for configs mutated programmatically after load.

Used by the `maestro validate` CLI command and by `maestro orchestrate`
as a fail-fast preflight.
"""

import subprocess
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from maestro.dag import find_cycle
from maestro.decomposer import ProjectDecomposer, _patterns_overlap
from maestro.domain.profile import DomainProfile
from maestro.execution.exec_config import DockerIsolation, ExecutionConfig, SshTransport
from maestro.models import (
    GatesConfig,
    OrchestratorConfig,
    SpecRunnerConfig,
    Task,
    WorkstreamConfig,
)
from maestro.scope_gate import find_escapes, normalize


Severity = Literal["error", "warning"]


class ValidationBackendError(Exception):
    """A task's validation_backend resolves to an unsupported (SSH) backend."""


def check_validation_backends(
    tasks: list[Task], execution: ExecutionConfig | None
) -> None:
    """Fail loud if any task's validation runs on a non-local-transport backend.

    PR1 supports validation only on ``transport.type == local`` (bare or
    Docker). ``same`` resolves to the task's own backend; a named value
    resolves directly; ``local`` is always the local backend. An SSH target
    raises — never a silent fallback to local. Tasks without a validation_cmd
    are skipped.
    """
    exec_cfg = execution or ExecutionConfig()
    registry = exec_cfg.normalized()
    default_backend = exec_cfg.default_backend
    for task in tasks:
        if not task.validation_cmd:
            continue
        name = task.validation_backend
        if name == "local":
            continue
        # `same` follows the runtime resolver exactly: task.backend, or the
        # config default when the task pins no backend (BackendResolver.resolve
        # maps None -> default_backend). Resolving `same` to None here would let
        # an SSH *default* backend slip through this gate.
        resolved = (task.backend or default_backend) if name == "same" else name
        spec = registry.get(resolved) if resolved is not None else None
        transport = spec.transport if spec is not None else None
        if isinstance(transport, SshTransport):
            raise ValidationBackendError(
                f"task '{task.id}': validation_backend '{name}' resolves to "
                f"backend '{resolved}' (transport ssh:{transport.host}); SSH "
                f"validation is a PR2 follow-up. Use validation_backend: local."
            )


class ValidationIssue(BaseModel):
    """A single preflight finding with a stable machine-readable code."""

    severity: Severity
    code: str
    workstream_ids: list[str] = Field(default_factory=list)
    message: str


class ValidationReport(BaseModel):
    """Aggregated preflight findings."""

    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        """Issues that must block a run."""
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        """Issues that should be reviewed but do not block."""
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        """True when no errors are present (warnings allowed)."""
        return not self.errors


def validate_project(
    config: OrchestratorConfig, *, check_fs: bool = True
) -> ValidationReport:
    """Run preflight checks over an orchestrator config.

    Args:
        config: Already schema-validated orchestrator config.
        check_fs: Also run filesystem checks (repo existence, glob
            matching, exact scope-overlap tier). Disable for
            deterministic runs without the real repo.

    Returns:
        Report with all findings; never raises for config-content problems.
    """
    issues: list[ValidationIssue] = []
    overlap_pairs: set[frozenset[str]] = set()

    if config.workstreams:
        issues.extend(_check_dangling_deps(config.workstreams))
        issues.extend(_check_cycles(config.workstreams))
        issues.extend(_check_scope_empty(config.workstreams))
        issues.extend(_check_overlap_static(config.workstreams, overlap_pairs))

    if config.domain is not None:
        issues.extend(_check_domain(config, config.domain))

    if check_fs:
        repo = Path(config.repo_path).expanduser()
        repo_issues = _check_repo(repo)
        issues.extend(repo_issues)
        if not repo_issues and config.workstreams:
            expected_outputs = (
                config.domain.workspace.expected_outputs
                if config.domain is not None
                else None
            )
            issues.extend(
                _check_scope_fs(
                    config.workstreams, repo, overlap_pairs, expected_outputs
                )
            )
        if not repo_issues:
            issues.extend(_check_tracked_spec_runner_config(repo))
        issues.extend(_check_spec_runner_contract())
        if config.gates is not None:
            issues.extend(_check_gates(config.gates))

    return ValidationReport(issues=issues)


def _check_dangling_deps(
    workstreams: list[WorkstreamConfig],
) -> list[ValidationIssue]:
    known = {w.id for w in workstreams}
    issues: list[ValidationIssue] = []
    for w in workstreams:
        # De-duplicated (depends_on has no dedupe validator, so a
        # mutate-after-load caller can leave repeats) and sorted for a stable,
        # non-repeating message — mirrors the Pydantic validator's set logic.
        unknown = sorted({d for d in w.depends_on if d not in known})
        if unknown:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="dangling-dep",
                    workstream_ids=[w.id],
                    message=(
                        f"Workstream '{w.id}' depends on unknown "
                        f"workstream(s): {', '.join(unknown)}. "
                        "Check the depends_on ids."
                    ),
                )
            )
    return issues


def _check_cycles(workstreams: list[WorkstreamConfig]) -> list[ValidationIssue]:
    cycle = find_cycle({w.id: set(w.depends_on) for w in workstreams})
    if cycle is None:
        return []
    return [
        ValidationIssue(
            severity="error",
            code="dag-cycle",
            workstream_ids=cycle[:-1],
            message=(
                "Cyclic dependency between workstreams: "
                + " -> ".join(cycle)
                + ". Remove one of the depends_on edges to break the cycle."
            ),
        )
    ]


def _check_scope_empty(
    workstreams: list[WorkstreamConfig],
) -> list[ValidationIssue]:
    return [
        ValidationIssue(
            severity="warning",
            code="scope-empty",
            workstream_ids=[w.id],
            message=(
                f"Workstream '{w.id}' has an empty scope: overlap checks "
                "cannot protect it from conflicts with parallel workstreams."
            ),
        )
        for w in workstreams
        if not w.scope
    ]


def _check_overlap_static(
    workstreams: list[WorkstreamConfig],
    seen_pairs: set[frozenset[str]],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for warning in ProjectDecomposer.validate_non_overlap(workstreams):
        pair = frozenset({warning.workstream_a, warning.workstream_b})
        seen_pairs.add(pair)
        issues.append(
            ValidationIssue(
                severity="warning",
                code="scope-overlap",
                workstream_ids=sorted(pair),
                message=(
                    f"{warning}. Overlapping scopes risk merge conflicts; "
                    "split the scopes or add a depends_on edge."
                ),
            )
        )
    return issues


def _check_repo(repo: Path) -> list[ValidationIssue]:
    """Check that repo exists and is a git repository.

    Args:
        repo: Path to the repository.

    Returns:
        List of errors if repo is missing or not a git repository.
    """
    if not repo.exists():
        return [
            ValidationIssue(
                severity="error",
                code="repo-missing",
                message=(
                    f"repo_path does not exist: {repo}. Fix repo_path in the config."
                ),
            )
        ]
    if not (repo / ".git").exists():
        return [
            ValidationIssue(
                severity="error",
                code="repo-not-git",
                message=(
                    f"repo_path is not a git repository (no .git): {repo}. "
                    "Point repo_path at a cloned repository."
                ),
            )
        ]
    return []


def _invalid_pattern_reason(pattern: str) -> str | None:
    """Classify why a scope glob is unsafe to pass to ``Path.glob``.

    Args:
        pattern: Raw scope glob pattern (before './' stripping).

    Returns:
        A human-readable reason if the pattern is invalid, else None.
    """
    if not pattern.strip():
        return "it is empty"
    normalized = pattern.removeprefix("./")
    if normalized.startswith("/"):
        return "it is an absolute path (scope globs must be repo-relative)"
    if ".." in Path(normalized).parts:
        return "it contains a '..' segment, which can escape the repo root"
    return None


def _expand_scope(
    repo: Path, patterns: list[str]
) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Expand each glob to the set of matching repo-relative file paths.

    A pattern matching a directory counts every file under it. Files
    inside .git are excluded. Patterns are normalized by stripping a
    leading './' so './src/**' and 'src/**' expand identically.

    Patterns that are empty, absolute, contain a '..' segment, or are
    otherwise rejected by the glob engine are skipped (they contribute
    no files) and reported back via the second return value instead of
    raising.

    Args:
        repo: Path to the repository root.
        patterns: List of glob patterns (may include ./).

    Returns:
        Tuple of:
        - Dict mapping each *valid* pattern to the set of matching file
          paths (relative to repo).
        - Dict mapping each *invalid* pattern to the reason it was
          rejected.
    """
    matches: dict[str, set[str]] = {}
    invalid: dict[str, str] = {}
    for pattern in patterns:
        reason = _invalid_pattern_reason(pattern)
        if reason is not None:
            invalid[pattern] = reason
            continue
        normalized = pattern.removeprefix("./")
        matched: set[str] = set()
        try:
            globbed = list(repo.glob(normalized))
        except (ValueError, NotImplementedError) as e:
            invalid[pattern] = f"it was rejected by the glob engine: {e}"
            continue
        for p in globbed:
            rel = p.relative_to(repo)
            if ".git" in rel.parts:
                continue
            if p.is_file():
                matched.add(str(rel))
            elif p.is_dir():
                matched.update(
                    str(f.relative_to(repo))
                    for f in p.rglob("*")
                    if f.is_file() and ".git" not in f.relative_to(repo).parts
                )
        matches[pattern] = matched
    return matches, invalid


def _is_expected_output(
    pattern: str, expected_outputs: dict[str, list[str]] | None
) -> bool:
    """True if `pattern` is covered by any phase of domain `expected_outputs`.

    A scope glob that matches no existing files is normally a typo warning
    (`scope-no-match`). But when the domain profile already declares that
    exact future path as an expected output (of any phase — author,
    verification, delivery), the "no match yet" is expected, not noise
    (§6, friction #10): the pattern is exempt, silencing the warning.
    """
    if not expected_outputs:
        return False
    normalized_pattern = pattern.removeprefix("./")
    for patterns in expected_outputs.values():
        for expected in patterns:
            expected_norm = expected.removeprefix("./")
            if expected_norm == normalized_pattern or fnmatch(
                expected_norm, normalized_pattern
            ):
                return True
    return False


def _check_scope_fs(
    workstreams: list[WorkstreamConfig],
    repo: Path,
    seen_pairs: set[frozenset[str]],
    expected_outputs: dict[str, list[str]] | None = None,
) -> list[ValidationIssue]:
    """Check for non-matching glob patterns and exact scope overlaps.

    The static heuristic (at parse time) catches obvious overlaps like
    'src/**' vs 'src/auth/**'. This tier catches misses like './src/**'
    vs 'src/**' by expanding every pattern against actual files.

    Args:
        workstreams: List of workstream configs to check.
        repo: Path to the repository root.
        seen_pairs: Pairs already reported by the static tier
            (to avoid duplicates).
        expected_outputs: Domain `workspace.expected_outputs` (all phases),
            or None for legacy (no domain) configs. Patterns it lists are
            exempt from `scope-no-match` (see `_is_expected_output`).

    Returns:
        List of warnings for unmatched patterns and overlapping scopes.
    """
    issues: list[ValidationIssue] = []
    files_by_ws: dict[str, set[str]] = {}

    for w in workstreams:
        expanded, invalid = _expand_scope(repo, w.scope)
        files_by_ws[w.id] = set().union(*expanded.values()) if expanded else set()
        for pattern, reason in invalid.items():
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="scope-invalid-pattern",
                    workstream_ids=[w.id],
                    message=(
                        f"Scope pattern '{pattern}' of workstream '{w.id}' "
                        f"is invalid: {reason}. Scope globs must be "
                        "repo-relative; fix or remove this pattern."
                    ),
                )
            )
        for pattern, matched in expanded.items():
            if not matched and not _is_expected_output(pattern, expected_outputs):
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="scope-no-match",
                        workstream_ids=[w.id],
                        message=(
                            f"Scope pattern '{pattern}' of workstream "
                            f"'{w.id}' matches no existing files — either a "
                            "typo or a glob for files not yet created."
                        ),
                    )
                )

    for i, a in enumerate(workstreams):
        for b in workstreams[i + 1 :]:
            pair = frozenset({a.id, b.id})
            if pair in seen_pairs:
                continue
            common = files_by_ws[a.id] & files_by_ws[b.id]
            if common:
                sample = ", ".join(sorted(common)[:5])
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="scope-overlap",
                        workstream_ids=sorted(pair),
                        message=(
                            f"Scopes of '{a.id}' and '{b.id}' match the same "
                            f"existing files (e.g. {sample}). Overlapping "
                            "scopes risk merge conflicts; split the scopes "
                            "or add a depends_on edge."
                        ),
                    )
                )
    return issues


def _check_spec_runner_contract() -> list[ValidationIssue]:
    """H-7 guard: the installed spec-runner must advertise --spec-prefix.

    spec-runner is an external subprocess, not a pinned dependency; an old
    binary without the flag would break prefix isolation SILENTLY (files at
    unprefixed paths, missed by the ignore block and the narrowed gates
    exclusion). Fail-closed: missing binary / broken --help / absent flag
    are all errors.
    """
    issue = ValidationIssue(
        severity="error",
        code="spec-runner-prefix-unsupported",
        workstream_ids=[],
        message=(
            "the installed spec-runner does not support --spec-prefix "
            "(or is missing/broken); harness-artifact isolation (H-7) "
            "requires it — upgrade spec-runner"
        ),
    )
    try:
        result = subprocess.run(
            ["spec-runner", "run", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return [issue]
    if result.returncode != 0 or "--spec-prefix" not in result.stdout:
        return [issue]
    return []


def _check_tracked_spec_runner_config(repo: Path) -> list[ValidationIssue]:
    """Warn when the target repo TRACKS its own spec-runner.config.yaml.

    Maestro overwrites that file inside the worktree — a real ownership
    conflict (fail-closed candidate per the gates v1.2 spec). Backstop:
    the overwrite of a tracked config is ex-post-visible as a scope
    violation since gates v1.2 no longer excludes it.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--", "spec-runner.config.yaml"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return [
            ValidationIssue(
                severity="warning",
                code="spec-runner-config-tracked",
                workstream_ids=[],
                message=(
                    "target repo tracks its own spec-runner.config.yaml; "
                    "Maestro will overwrite it inside each worktree (the "
                    "overwrite is ex-post-visible as a scope violation). "
                    "Consider moving the repo's own config or expect "
                    "NEEDS_REVIEW blocks."
                ),
            )
        ]
    return []


def _check_gates(gates: GatesConfig) -> list[ValidationIssue]:
    """Gates are opt-in but fail-closed: a broken setup must not pass preflight.

    A missing steward binary or risk-model file would turn every guard
    evaluation into an error verdict (= block); surface it before the run.
    """
    import os

    issues: list[ValidationIssue] = []
    candidate = gates.steward_bin or os.environ.get("MAESTRO_STEWARD_BIN")
    binary = Path(candidate).expanduser() if candidate else None
    if binary is None or not binary.is_file() or not os.access(binary, os.X_OK):
        issues.append(
            ValidationIssue(
                severity="error",
                code="gates-steward-missing",
                workstream_ids=[],
                message=(
                    "gates enabled but the steward binary is not executable: "
                    f"{candidate or 'set gates.steward_bin or $MAESTRO_STEWARD_BIN'}"
                ),
            )
        )
    if (
        gates.risk_model is not None
        and not Path(gates.risk_model).expanduser().is_file()
    ):
        issues.append(
            ValidationIssue(
                severity="error",
                code="gates-risk-model-missing",
                workstream_ids=[],
                message=f"gates.risk_model file not found: {gates.risk_model}",
            )
        )
    return issues


def _check_domain(
    config: OrchestratorConfig, domain: DomainProfile
) -> list[ValidationIssue]:
    """Run every Stage-B domain-profile check (§6, §7, §9).

    Domain-content checks only — no filesystem access — so they run
    unconditionally whenever `config.domain` is set, independent of the
    `check_fs` flag (mirrors the static workstream-graph checks above).
    """
    issues: list[ValidationIssue] = []
    issues.extend(
        _check_domain_capability_gate(config.workstreams, domain, config.execution)
    )
    issues.extend(_check_domain_evidence_containment(domain))
    issues.extend(_check_domain_role_coherence(config.workstreams, domain))
    issues.extend(_check_domain_artifact_declared(domain))
    issues.extend(_check_domain_spec_gen_ssot(domain, config.spec_runner))
    issues.extend(_check_domain_verifier_only_source(domain, config.repo_path))
    return issues


def _check_domain_capability_gate(
    workstreams: list[WorkstreamConfig],
    domain: DomainProfile,
    execution: ExecutionConfig | None,
) -> list[ValidationIssue]:
    """Capability gate (§7): verifier_only rubrics need an isolated author.

    If the author process can read the plaintext host filesystem outside a
    container, a verifier_only rubric is not actually hidden from it. Every
    participating workstream's resolved backend must carry Docker isolation;
    a single escape (`local`/bare) breaks the guarantee for the whole run.
    """
    if domain.verification.criteria.visibility != "verifier_only":
        return []
    exec_cfg = execution or ExecutionConfig()
    registry = exec_cfg.normalized()
    default_backend = exec_cfg.default_backend
    offending: list[str] = []
    for w in workstreams:
        resolved = w.backend or default_backend
        spec = registry.get(resolved)
        if spec is None or not isinstance(spec.isolation, DockerIsolation):
            offending.append(w.id)
    if not offending:
        return []
    return [
        ValidationIssue(
            severity="error",
            code="domain-verifier-only-capability",
            workstream_ids=sorted(offending),
            message=(
                "verifier_only requires an isolated author backend — "
                "workstream(s) without Docker isolation: "
                f"{', '.join(sorted(offending))}"
            ),
        )
    ]


def _check_domain_evidence_containment(domain: DomainProfile) -> list[ValidationIssue]:
    """evidence_root must be inside the verifier's own write scope.

    A synthetic probe file under `evidence_root` is checked against
    `roles.verifier.write` (a directory prefix like `verdicts/topic-x` isn't
    itself matched by a `verdicts/topic-x/**` glob; a file under it is).
    """
    verifier_scope = domain.workspace.roles.get("verifier")
    probe = f"{domain.workspace.evidence_root.rstrip('/')}/__preflight_probe__"
    if verifier_scope is not None and not find_escapes(
        normalize([probe]), normalize(verifier_scope.write)
    ):
        return []
    return [
        ValidationIssue(
            severity="error",
            code="domain-evidence-root-not-contained",
            workstream_ids=[],
            message=(
                f"evidence_root '{domain.workspace.evidence_root}' is not "
                "matched by roles.verifier.write; the verifier could never "
                "write evidence into its declared root."
            ),
        )
    ]


def _check_domain_role_coherence(
    workstreams: list[WorkstreamConfig], domain: DomainProfile
) -> list[ValidationIssue]:
    """Role/scope coherence: author authority bounded, roles disjoint.

    A workstream's `scope` (its author authority) must be a subset of
    `roles.author.write` — otherwise the workstream could touch files the
    domain profile never granted the author. Separately, author and
    verifier write sets must never overlap: a shared path would let the
    author overwrite verifier-owned evidence (or vice versa).
    """
    issues: list[ValidationIssue] = []
    author_scope = domain.workspace.roles.get("author")
    verifier_scope = domain.workspace.roles.get("verifier")
    author_write = author_scope.write if author_scope else []
    verifier_write = verifier_scope.write if verifier_scope else []

    for w in workstreams:
        if not w.scope:
            continue
        # No declared author role at all -> nothing covers the workstream's
        # scope; fail closed (an empty `author_write` would otherwise read
        # as "no scope to enforce" and pass find_escapes vacuously).
        escapes = (
            normalize(w.scope)
            if author_scope is None
            else find_escapes(normalize(w.scope), normalize(author_write))
        )
        if escapes:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="domain-scope-authority-mismatch",
                    workstream_ids=[w.id],
                    message=(
                        f"Workstream '{w.id}' scope pattern(s) "
                        f"{', '.join(escapes)} are not covered by "
                        "roles.author.write; a workstream's author "
                        "authority must be a subset of its declared "
                        "write scope."
                    ),
                )
            )

    overlaps = [
        f"{a} <-> {b}"
        for a in author_write
        for b in verifier_write
        if _patterns_overlap(a, b)
    ]
    if overlaps:
        issues.append(
            ValidationIssue(
                severity="error",
                code="domain-role-write-overlap",
                workstream_ids=[],
                message=(
                    "roles.author.write and roles.verifier.write overlap "
                    f"({', '.join(overlaps)}); author and verifier write "
                    "scopes must be disjoint."
                ),
            )
        )
    return issues


def _check_domain_artifact_declared(domain: DomainProfile) -> list[ValidationIssue]:
    """verification.artifact must fall inside roles.author.write (§9).

    If the author has no write authority over the artifact path, the
    author could never actually produce it — a config-time contradiction.
    """
    author_scope = domain.workspace.roles.get("author")
    artifact = normalize([domain.verification.artifact])
    if author_scope is not None and not find_escapes(
        artifact, normalize(author_scope.write)
    ):
        return []
    return [
        ValidationIssue(
            severity="error",
            code="domain-artifact-not-writable",
            workstream_ids=[],
            message=(
                f"verification.artifact '{domain.verification.artifact}' is "
                "not matched by roles.author.write; the author could never "
                "produce it."
            ),
        )
    ]


def _check_domain_spec_gen_ssot(
    domain: DomainProfile, spec_runner: SpecRunnerConfig
) -> list[ValidationIssue]:
    """spec_gen SSOT (§9): budget has exactly one source of truth.

    `domain.spec_gen` and the legacy `spec_runner.spec_gen_budget_usd`
    (default 1.0 — "unset" means explicitly nulled) must not both be
    active; otherwise it's ambiguous which budget actually governs
    `spec-runner plan --full`. When `domain.spec_gen` is the source of truth
    it MUST carry a `budget_usd`, or spec generation would run uncapped (the
    legacy budget having been nulled by this same rule).
    """
    if domain.spec_gen is None:
        return []
    issues: list[ValidationIssue] = []
    if domain.spec_gen.budget_usd is None:
        issues.append(
            ValidationIssue(
                severity="error",
                code="domain-spec-gen-no-budget",
                workstream_ids=[],
                message=(
                    "domain.spec_gen is set but budget_usd is null; spec "
                    "generation would run uncapped — set "
                    "domain.spec_gen.budget_usd."
                ),
            )
        )
    if spec_runner.spec_gen_budget_usd is not None:
        issues.append(
            ValidationIssue(
                severity="error",
                code="domain-spec-gen-ssot-conflict",
                workstream_ids=[],
                message=(
                    "domain.spec_gen and spec_runner.spec_gen_budget_usd are "
                    "both set; spec-gen budget has a single source of truth — "
                    "set spec_runner.spec_gen_budget_usd: null when "
                    "domain.spec_gen is configured."
                ),
            )
        )
    return issues


def _check_domain_verifier_only_source(
    domain: DomainProfile, repo_path: str
) -> list[ValidationIssue]:
    """verifier_only rubrics must not resolve inside the target repo (§7).

    A rubric committed to the repo is readable by the author (git history,
    checkout, grep); verifier_only promises the rubric bytes stay hidden
    from the author, which a repo-resident file cannot deliver.
    """
    if domain.verification.criteria.visibility != "verifier_only":
        return []
    source = Path(domain.verification.criteria.source).expanduser()
    repo = Path(repo_path).expanduser()
    inside_repo = not source.is_absolute() or source.is_relative_to(repo)
    if not inside_repo:
        return []
    return [
        ValidationIssue(
            severity="error",
            code="domain-verifier-only-source-in-repo",
            workstream_ids=[],
            message=(
                "rubric committed to the target repo cannot be "
                f"verifier-only: criteria.source '{domain.verification.criteria.source}' "
                "resolves inside repo_path"
            ),
        )
    ]
