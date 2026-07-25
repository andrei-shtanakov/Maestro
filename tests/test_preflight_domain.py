"""Preflight domain-profile checks (Task 10, §6/§7/§9).

Each check gets a positive fixture (no domain issue) and a violating
fixture (expected error/warning). Domain absent -> zero domain checks,
zero output (covered by test_preflight.py staying green untouched).
"""

from pathlib import Path

from maestro.execution.exec_config import DockerConfig, ExecutionConfig
from maestro.models import OrchestratorConfig, SpecRunnerConfig, WorkstreamConfig
from maestro.preflight import validate_project


def profile_dict(**overrides: object) -> dict:
    """Base valid Task-2 domain profile: no domain issues (visibility=shared)."""
    base: dict = {
        "verification": {
            "verifier": {
                "argv": ["uv", "run", "bench-verify", "--out", "{out}"],
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
    for key, value in overrides.items():
        base[key] = value
    return base


def ws(
    id_: str = "ws-a", scope: list[str] | None = None, backend: str | None = None
) -> WorkstreamConfig:
    return WorkstreamConfig(
        id=id_,
        title=id_,
        description=f"workstream {id_}",
        scope=scope if scope is not None else ["reports/topic-x/**"],
        backend=backend,
    )


def make_config(
    *,
    domain: dict | None,
    workstreams: list[WorkstreamConfig] | None = None,
    repo_path: str = "/nonexistent",
    execution: ExecutionConfig | None = None,
    spec_runner: SpecRunnerConfig | None = None,
) -> OrchestratorConfig:
    kwargs: dict = {
        "project": "test",
        "repo_url": "https://github.com/user/test",
        "repo_path": repo_path,
        "workspace_base": "/tmp/maestro-ws/test",
        "workstreams": workstreams if workstreams is not None else [ws()],
        "domain": domain,
    }
    if execution is not None:
        kwargs["execution"] = execution
    if spec_runner is not None:
        kwargs["spec_runner"] = spec_runner
    return OrchestratorConfig(**kwargs)


def make_git_repo(tmp_path: Path, files: list[str]) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    for rel in files:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    return repo


class TestNoDomainZeroChange:
    def test_no_domain_produces_no_domain_issues(self) -> None:
        config = make_config(domain=None)
        report = validate_project(config, check_fs=False)
        codes = [i.code for i in report.issues if i.code.startswith("domain-")]
        assert codes == []


class TestCapabilityGate:
    def test_verifier_only_with_docker_backend_ok(self) -> None:
        domain = profile_dict()
        domain["verification"]["criteria"]["visibility"] = "verifier_only"
        domain["verification"]["criteria"]["source"] = "/secure/vault/criteria.yaml"
        execution = ExecutionConfig(
            default_backend="docker", docker=DockerConfig(image="python:3.12")
        )
        config = make_config(
            domain=domain,
            workstreams=[ws(backend="docker")],
            execution=execution,
        )
        report = validate_project(config, check_fs=False)
        assert not any(
            i.code == "domain-verifier-only-capability" for i in report.issues
        )

    def test_verifier_only_with_local_backend_is_error(self) -> None:
        domain = profile_dict()
        domain["verification"]["criteria"]["visibility"] = "verifier_only"
        domain["verification"]["criteria"]["source"] = "/secure/vault/criteria.yaml"
        config = make_config(domain=domain, workstreams=[ws(backend=None)])
        report = validate_project(config, check_fs=False)
        errors = [
            i for i in report.errors if i.code == "domain-verifier-only-capability"
        ]
        assert len(errors) == 1
        assert "verifier_only requires an isolated author backend" in errors[0].message
        assert errors[0].workstream_ids == ["ws-a"]


class TestEvidenceRootContainment:
    def test_evidence_root_covered_by_verifier_write_ok(self) -> None:
        config = make_config(domain=profile_dict())
        report = validate_project(config, check_fs=False)
        assert not any(
            i.code == "domain-evidence-root-not-contained" for i in report.issues
        )

    def test_evidence_root_not_covered_is_error(self) -> None:
        domain = profile_dict()
        domain["workspace"]["evidence_root"] = "unrelated/path"
        config = make_config(domain=domain)
        report = validate_project(config, check_fs=False)
        errors = [
            i for i in report.errors if i.code == "domain-evidence-root-not-contained"
        ]
        assert len(errors) == 1


class TestRoleScopeCoherence:
    def test_workstream_scope_subset_of_author_write_ok(self) -> None:
        config = make_config(
            domain=profile_dict(), workstreams=[ws(scope=["reports/topic-x/**"])]
        )
        report = validate_project(config, check_fs=False)
        assert not any(
            i.code == "domain-scope-authority-mismatch" for i in report.issues
        )

    def test_workstream_scope_exceeds_author_write_is_error(self) -> None:
        config = make_config(
            domain=profile_dict(), workstreams=[ws(scope=["reports/**"])]
        )
        report = validate_project(config, check_fs=False)
        errors = [
            i for i in report.errors if i.code == "domain-scope-authority-mismatch"
        ]
        assert len(errors) == 1
        assert errors[0].workstream_ids == ["ws-a"]

    def test_disjoint_author_verifier_write_ok(self) -> None:
        config = make_config(domain=profile_dict())
        report = validate_project(config, check_fs=False)
        assert not any(i.code == "domain-role-write-overlap" for i in report.issues)

    def test_overlapping_author_verifier_write_is_error(self) -> None:
        domain = profile_dict()
        domain["workspace"]["roles"]["verifier"]["write"] = ["reports/topic-x/**"]
        config = make_config(domain=domain)
        report = validate_project(config, check_fs=False)
        errors = [i for i in report.errors if i.code == "domain-role-write-overlap"]
        assert len(errors) == 1


class TestExpectedOutputsExemption:
    def test_expected_output_pattern_silences_scope_no_match(
        self, tmp_path: Path
    ) -> None:
        repo = make_git_repo(tmp_path, ["briefs/topic-x/criteria.yaml"])
        config = make_config(
            domain=profile_dict(),
            workstreams=[ws(scope=["reports/topic-x/**"])],
            repo_path=str(repo),
        )
        report = validate_project(config, check_fs=True)
        no_match = [i for i in report.issues if i.code == "scope-no-match"]
        assert no_match == []

    def test_unlisted_pattern_still_warns_scope_no_match(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, ["briefs/topic-x/criteria.yaml"])
        config = make_config(
            domain=profile_dict(),
            workstreams=[ws(scope=["reports/topic-x/**", "unexpected/dir/**"])],
            repo_path=str(repo),
        )
        report = validate_project(config, check_fs=True)
        no_match = [i for i in report.issues if i.code == "scope-no-match"]
        assert len(no_match) == 1
        assert "unexpected/dir/**" in no_match[0].message


class TestSpecGenSsot:
    def test_domain_spec_gen_with_legacy_disabled_ok(self) -> None:
        domain = profile_dict(spec_gen={"budget_usd": 2.0, "timeout_minutes": 5.0})
        config = make_config(
            domain=domain,
            spec_runner=SpecRunnerConfig(spec_gen_budget_usd=None),
        )
        report = validate_project(config, check_fs=False)
        assert not any(i.code == "domain-spec-gen-ssot-conflict" for i in report.issues)

    def test_domain_spec_gen_with_legacy_still_set_is_error(self) -> None:
        domain = profile_dict(spec_gen={"budget_usd": 2.0, "timeout_minutes": 5.0})
        config = make_config(domain=domain)
        report = validate_project(config, check_fs=False)
        errors = [i for i in report.errors if i.code == "domain-spec-gen-ssot-conflict"]
        assert len(errors) == 1


class TestVerifierOnlySourceSanity:
    def test_verifier_only_source_outside_repo_ok(self) -> None:
        domain = profile_dict()
        domain["verification"]["criteria"]["visibility"] = "verifier_only"
        domain["verification"]["criteria"]["source"] = "/secure/vault/criteria.yaml"
        execution = ExecutionConfig(
            default_backend="docker", docker=DockerConfig(image="python:3.12")
        )
        config = make_config(
            domain=domain,
            workstreams=[ws(backend="docker")],
            execution=execution,
            repo_path="/some/repo",
        )
        report = validate_project(config, check_fs=False)
        assert not any(
            i.code == "domain-verifier-only-source-in-repo" for i in report.issues
        )

    def test_verifier_only_source_inside_repo_is_error(self) -> None:
        domain = profile_dict()
        domain["verification"]["criteria"]["visibility"] = "verifier_only"
        domain["verification"]["criteria"]["source"] = "briefs/topic-x/criteria.yaml"
        execution = ExecutionConfig(
            default_backend="docker", docker=DockerConfig(image="python:3.12")
        )
        config = make_config(
            domain=domain,
            workstreams=[ws(backend="docker")],
            execution=execution,
            repo_path="/some/repo",
        )
        report = validate_project(config, check_fs=False)
        errors = [
            i for i in report.errors if i.code == "domain-verifier-only-source-in-repo"
        ]
        assert len(errors) == 1
        assert "rubric committed to the target repo cannot be verifier-only" in (
            errors[0].message
        )

    def test_verifier_only_source_absolute_under_repo_is_error(self) -> None:
        domain = profile_dict()
        domain["verification"]["criteria"]["visibility"] = "verifier_only"
        domain["verification"]["criteria"]["source"] = (
            "/some/repo/briefs/topic-x/criteria.yaml"
        )
        execution = ExecutionConfig(
            default_backend="docker", docker=DockerConfig(image="python:3.12")
        )
        config = make_config(
            domain=domain,
            workstreams=[ws(backend="docker")],
            execution=execution,
            repo_path="/some/repo",
        )
        report = validate_project(config, check_fs=False)
        errors = [
            i for i in report.errors if i.code == "domain-verifier-only-source-in-repo"
        ]
        assert len(errors) == 1


class TestArtifactDeclared:
    def test_artifact_matched_by_author_write_ok(self) -> None:
        config = make_config(domain=profile_dict())
        report = validate_project(config, check_fs=False)
        assert not any(i.code == "domain-artifact-not-writable" for i in report.issues)

    def test_artifact_not_matched_by_author_write_is_error(self) -> None:
        domain = profile_dict()
        domain["verification"]["artifact"] = "elsewhere/result.md"
        config = make_config(domain=domain)
        report = validate_project(config, check_fs=False)
        errors = [i for i in report.errors if i.code == "domain-artifact-not-writable"]
        assert len(errors) == 1


class TestMissingRoleFailsClosed:
    """Roles dict has no enforced keys at the pydantic level; a domain
    profile missing "author" or "verifier" entirely must fail closed on
    the containment checks rather than vacuously pass (empty write-list
    would otherwise read as "nothing to enforce")."""

    def test_missing_author_role_fails_artifact_declared(self) -> None:
        domain = profile_dict()
        del domain["workspace"]["roles"]["author"]
        config = make_config(domain=domain, workstreams=[])
        report = validate_project(config, check_fs=False)
        errors = [i for i in report.errors if i.code == "domain-artifact-not-writable"]
        assert len(errors) == 1

    def test_missing_verifier_role_fails_evidence_containment(self) -> None:
        domain = profile_dict()
        del domain["workspace"]["roles"]["verifier"]
        config = make_config(domain=domain, workstreams=[])
        report = validate_project(config, check_fs=False)
        errors = [
            i for i in report.errors if i.code == "domain-evidence-root-not-contained"
        ]
        assert len(errors) == 1

    def test_missing_author_role_fails_scope_authority(self) -> None:
        domain = profile_dict()
        del domain["workspace"]["roles"]["author"]
        config = make_config(
            domain=domain, workstreams=[ws(scope=["reports/topic-x/**"])]
        )
        report = validate_project(config, check_fs=False)
        errors = [
            i for i in report.errors if i.code == "domain-scope-authority-mismatch"
        ]
        assert len(errors) == 1
        assert errors[0].workstream_ids == ["ws-a"]
