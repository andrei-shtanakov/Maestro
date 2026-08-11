"""Unit tests for preflight validation (maestro validate)."""

import subprocess
from pathlib import Path

from maestro.models import OrchestratorConfig, WorkstreamConfig
from maestro.preflight import ValidationIssue, ValidationReport, validate_project


def make_config(
    workstreams: list[WorkstreamConfig], repo_path: str = "/nonexistent"
) -> OrchestratorConfig:
    return OrchestratorConfig(
        project="test",
        repo_url="https://github.com/user/test",
        repo_path=repo_path,
        workspace_base="/tmp/maestro-ws/test",
        workstreams=workstreams,
    )


def ws(id_: str, scope: list[str], depends_on: list[str]) -> WorkstreamConfig:
    return WorkstreamConfig(
        id=id_,
        title=id_,
        description=f"workstream {id_}",
        scope=scope,
        depends_on=depends_on,
    )


def make_git_repo(tmp_path: Path, files: list[str]) -> Path:
    """Create a fake git repo with the given relative files."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    for rel in files:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    return repo


class TestValidationReport:
    def test_ok_when_only_warnings(self) -> None:
        report = ValidationReport(
            issues=[
                ValidationIssue(severity="warning", code="scope-empty", message="w")
            ]
        )
        assert report.ok
        assert len(report.warnings) == 1
        assert report.errors == []

    def test_not_ok_with_errors(self) -> None:
        report = ValidationReport(
            issues=[ValidationIssue(severity="error", code="dag-cycle", message="e")]
        )
        assert not report.ok
        assert len(report.errors) == 1


class TestStaticChecks:
    def test_clean_config_no_issues(self) -> None:
        config = make_config(
            [
                ws("a", ["src/a/**"], []),
                ws("b", ["src/b/**"], ["a"]),
            ]
        )
        report = validate_project(config, check_fs=False)
        assert report.ok
        assert report.issues == []

    def test_two_node_cycle_is_error(self) -> None:
        config = make_config(
            [
                ws("a", ["src/a/**"], ["b"]),
                ws("b", ["src/b/**"], ["a"]),
            ]
        )
        report = validate_project(config, check_fs=False)
        assert not report.ok
        codes = [i.code for i in report.errors]
        assert codes == ["dag-cycle"]
        assert set(report.errors[0].workstream_ids) == {"a", "b"}

    def test_three_node_cycle_is_error(self) -> None:
        config = make_config(
            [
                ws("a", ["src/a/**"], ["c"]),
                ws("b", ["src/b/**"], ["a"]),
                ws("c", ["src/c/**"], ["b"]),
            ]
        )
        report = validate_project(config, check_fs=False)
        assert [i.code for i in report.errors] == ["dag-cycle"]

    def test_scope_overlap_is_warning(self) -> None:
        config = make_config(
            [
                ws("a", ["src/**"], []),
                ws("b", ["src/auth/**"], []),
            ]
        )
        report = validate_project(config, check_fs=False)
        assert report.ok  # warnings only
        overlap = [i for i in report.issues if i.code == "scope-overlap"]
        assert len(overlap) == 1
        assert set(overlap[0].workstream_ids) == {"a", "b"}

    def test_empty_scope_is_warning(self) -> None:
        config = make_config([ws("a", [], [])])
        report = validate_project(config, check_fs=False)
        assert report.ok
        assert [i.code for i in report.issues] == ["scope-empty"]
        assert report.issues[0].workstream_ids == ["a"]

    def test_empty_workstreams_skips_dag_and_scope_checks(self) -> None:
        config = make_config([])
        report = validate_project(config, check_fs=False)
        assert report.ok
        assert report.issues == []


class TestOrderedOverlapDowngrade:
    """Issue #121: overlap between DAG-ordered workstreams is info, not warning.

    Dependent workstreams never run concurrently, so their scope overlap
    carries no merge-conflict risk; the finding stays visible as info but
    must not advise adding the edge that is already there.
    """

    def test_parallel_pair_stays_warning(self) -> None:
        config = make_config(
            [
                ws("a", ["src/**"], []),
                ws("b", ["src/auth/**"], []),
            ]
        )
        report = validate_project(config, check_fs=False)
        overlap = [i for i in report.issues if i.code == "scope-overlap"]
        assert len(overlap) == 1
        assert overlap[0].severity == "warning"

    def test_direct_edge_downgrades_static_overlap_to_info(self) -> None:
        config = make_config(
            [
                ws("a", ["src/**"], []),
                ws("b", ["src/auth/**"], ["a"]),
            ]
        )
        report = validate_project(config, check_fs=False)
        overlap = [i for i in report.issues if i.code == "scope-overlap"]
        assert len(overlap) == 1
        assert overlap[0].severity == "info"
        assert "add a depends_on edge" not in overlap[0].message
        assert report.warnings == []

    def test_transitive_path_downgrades_static_overlap_to_info(self) -> None:
        config = make_config(
            [
                ws("a", ["src/**"], []),
                ws("mid", ["docs/**"], ["a"]),
                ws("c", ["src/auth/**"], ["mid"]),
            ]
        )
        report = validate_project(config, check_fs=False)
        overlap = [i for i in report.issues if i.code == "scope-overlap"]
        assert len(overlap) == 1
        assert set(overlap[0].workstream_ids) == {"a", "c"}
        assert overlap[0].severity == "info"

    def test_cycle_does_not_break_overlap_check(self) -> None:
        # A cycle is its own error; the overlap pass must still terminate
        # and treat the (never-concurrent) pair as ordered.
        config = make_config(
            [
                ws("a", ["src/**"], ["b"]),
                ws("b", ["src/auth/**"], ["a"]),
            ]
        )
        report = validate_project(config, check_fs=False)
        assert [i.code for i in report.errors] == ["dag-cycle"]
        overlap = [i for i in report.issues if i.code == "scope-overlap"]
        assert len(overlap) == 1
        assert overlap[0].severity == "info"

    def test_fs_tier_overlap_on_ordered_pair_is_info(self, tmp_path: Path) -> None:
        # './src/**' vs 'src/**' slips past the static heuristic; the exact
        # FS tier catches it and must apply the same ordering downgrade.
        repo = make_git_repo(tmp_path, ["src/main.py"])
        config = make_config(
            [
                ws("a", ["./src/**"], []),
                ws("b", ["src/**"], ["a"]),
            ],
            repo_path=str(repo),
        )
        report = validate_project(config)
        overlap = [i for i in report.issues if i.code == "scope-overlap"]
        assert len(overlap) == 1
        assert overlap[0].severity == "info"
        assert "add a depends_on edge" not in overlap[0].message

    def test_info_not_counted_as_warning_or_error(self) -> None:
        report = ValidationReport(
            issues=[ValidationIssue(severity="info", code="scope-overlap", message="i")]
        )
        assert report.ok
        assert report.warnings == []
        assert report.errors == []
        assert len(report.infos) == 1


class TestFilesystemChecks:
    def test_repo_missing_is_error(self, tmp_path: Path) -> None:
        config = make_config(
            [ws("a", ["src/**"], [])], repo_path=str(tmp_path / "nope")
        )
        report = validate_project(config)
        assert [i.code for i in report.errors] == ["repo-missing"]

    def test_repo_not_git_is_error(self, tmp_path: Path) -> None:
        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()
        config = make_config([ws("a", ["src/**"], [])], repo_path=str(plain_dir))
        report = validate_project(config)
        assert [i.code for i in report.errors] == ["repo-not-git"]

    def test_repo_errors_skip_scope_fs_checks(self, tmp_path: Path) -> None:
        config = make_config(
            [ws("a", ["src/**"], [])], repo_path=str(tmp_path / "nope")
        )
        report = validate_project(config)
        assert [i.code for i in report.issues] == ["repo-missing"]

    def test_glob_with_matches_is_silent(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, ["src/a/main.py"])
        config = make_config([ws("a", ["src/a/**"], [])], repo_path=str(repo))
        report = validate_project(config)
        assert report.issues == []

    def test_glob_without_matches_is_warning(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, ["src/a/main.py"])
        config = make_config(
            [ws("a", ["src/a/**", "src/typo/**"], [])], repo_path=str(repo)
        )
        report = validate_project(config)
        assert [i.code for i in report.issues] == ["scope-no-match"]
        assert "src/typo/**" in report.issues[0].message

    def test_directory_scope_without_glob_counts_files(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, ["src/a/main.py"])
        config = make_config([ws("a", ["src/a"], [])], repo_path=str(repo))
        report = validate_project(config)
        assert report.issues == []

    def test_check_fs_false_skips_everything(self, tmp_path: Path) -> None:
        config = make_config(
            [ws("a", ["src/**"], [])], repo_path=str(tmp_path / "nope")
        )
        report = validate_project(config, check_fs=False)
        assert report.issues == []


class TestExactOverlapTier:
    def test_heuristic_false_negative_caught_by_fs_tier(self, tmp_path: Path) -> None:
        # './src/**' vs 'src/**' — the static heuristic misses this
        # (different first segment), the exact tier must catch it.
        repo = make_git_repo(tmp_path, ["src/main.py"])
        config = make_config(
            [
                ws("a", ["./src/**"], []),
                ws("b", ["src/**"], []),
            ],
            repo_path=str(repo),
        )
        report = validate_project(config)
        overlap = [i for i in report.issues if i.code == "scope-overlap"]
        assert len(overlap) == 1
        assert set(overlap[0].workstream_ids) == {"a", "b"}
        assert "src/main.py" in overlap[0].message

    def test_no_duplicate_when_both_tiers_fire(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, ["src/auth/login.py"])
        config = make_config(
            [
                ws("a", ["src/**"], []),
                ws("b", ["src/auth/**"], []),
            ],
            repo_path=str(repo),
        )
        report = validate_project(config)
        overlap = [i for i in report.issues if i.code == "scope-overlap"]
        assert len(overlap) == 1  # static tier fired; exact tier de-duplicated

    def test_disjoint_scopes_no_overlap(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, ["src/a/x.py", "src/b/y.py"])
        config = make_config(
            [
                ws("a", ["src/a/**"], []),
                ws("b", ["src/b/**"], []),
            ],
            repo_path=str(repo),
        )
        report = validate_project(config)
        assert report.issues == []


class TestInvalidScopePatterns:
    def test_absolute_pattern_is_warning_not_crash(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, ["src/a/main.py"])
        config = make_config([ws("a", ["/src/**"], [])], repo_path=str(repo))
        report = validate_project(config)
        assert report.ok
        assert [i.code for i in report.issues] == ["scope-invalid-pattern"]
        assert "a" in report.issues[0].workstream_ids
        assert "/src/**" in report.issues[0].message

    def test_empty_pattern_is_warning_not_crash(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path, ["src/a/main.py"])
        config = make_config([ws("a", [""], [])], repo_path=str(repo))
        report = validate_project(config)
        assert report.ok
        assert [i.code for i in report.issues] == ["scope-invalid-pattern"]

    def test_parent_escape_is_warning_and_contributes_no_files(
        self, tmp_path: Path
    ) -> None:
        repo = make_git_repo(tmp_path, ["src/a/main.py"])
        # Sibling file outside the repo that '../**' would otherwise match.
        (tmp_path / "sibling.py").write_text("x")
        config = make_config(
            [
                ws("a", ["../**"], []),
                ws("b", ["src/a/**"], []),
            ],
            repo_path=str(repo),
        )
        report = validate_project(config)
        assert [i.code for i in report.issues] == ["scope-invalid-pattern"]
        assert not any(i.code == "scope-overlap" for i in report.issues)

    def test_invalid_pattern_does_not_also_emit_scope_no_match(
        self, tmp_path: Path
    ) -> None:
        repo = make_git_repo(tmp_path, ["src/a/main.py"])
        config = make_config([ws("a", ["/src/**"], [])], repo_path=str(repo))
        report = validate_project(config)
        assert not any(i.code == "scope-no-match" for i in report.issues)

    def test_dotdot_in_filename_component_is_not_flagged_invalid(
        self, tmp_path: Path
    ) -> None:
        repo = make_git_repo(tmp_path, ["src/foo..bar/main.py"])
        config = make_config([ws("a", ["src/foo..bar/**"], [])], repo_path=str(repo))
        report = validate_project(config)
        assert not any(i.code == "scope-invalid-pattern" for i in report.issues)


class TestDanglingDeps:
    def test_single_unknown_dep_is_error(self) -> None:
        from maestro.preflight import _check_dangling_deps

        issues = _check_dangling_deps(
            [ws("a", ["src/a/**"], []), ws("b", ["src/b/**"], ["nope"])]
        )
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert issues[0].code == "dangling-dep"
        assert issues[0].workstream_ids == ["b"]
        assert "nope" in issues[0].message

    def test_all_deps_valid_is_empty(self) -> None:
        from maestro.preflight import _check_dangling_deps

        issues = _check_dangling_deps(
            [ws("a", ["src/a/**"], []), ws("b", ["src/b/**"], ["a"])]
        )
        assert issues == []

    def test_each_dangling_workstream_gets_one_issue(self) -> None:
        from maestro.preflight import _check_dangling_deps

        issues = _check_dangling_deps(
            [ws("a", ["src/a/**"], ["x"]), ws("b", ["src/b/**"], ["y"])]
        )
        assert {i.workstream_ids[0] for i in issues} == {"a", "b"}
        assert len(issues) == 2

    def test_multiple_unknown_ids_sorted_in_message(self) -> None:
        from maestro.preflight import _check_dangling_deps

        issues = _check_dangling_deps(
            [ws("a", ["src/a/**"], ["z-missing", "a-missing"])]
        )
        # one issue, unknown ids listed sorted (a-missing before z-missing)
        assert len(issues) == 1
        assert "a-missing, z-missing" in issues[0].message

    def test_repeated_unknown_id_deduplicated_in_message(self) -> None:
        from maestro.preflight import _check_dangling_deps

        # depends_on has no dedupe validator; a mutate-after-load caller can
        # leave repeats — the message must list the id once, not "ghost, ghost".
        w = ws("a", ["src/a/**"], [])
        w.depends_on.extend(["ghost", "ghost"])
        issues = _check_dangling_deps([w])
        assert len(issues) == 1
        assert "ghost" in issues[0].message
        assert "ghost, ghost" not in issues[0].message

    def test_integration_mutate_after_load(self) -> None:
        # bypass the Pydantic load validator by mutating post-construction
        config = make_config([ws("a", ["src/a/**"], []), ws("b", ["src/b/**"], ["a"])])
        config.workstreams[1].depends_on.append("does-not-exist")
        report = validate_project(config, check_fs=False)
        assert report.ok is False
        assert any(i.code == "dangling-dep" for i in report.issues)

    def test_integration_cyclic_and_dangling_independent(self) -> None:
        # a<->b cycle constructs at load (validator accepts pure cycles),
        # then mutate in a dangling edge → both codes present, independently
        config = make_config(
            [ws("a", ["src/a/**"], ["b"]), ws("b", ["src/b/**"], ["a"])]
        )
        config.workstreams[0].depends_on.append("ghost")
        report = validate_project(config, check_fs=False)
        codes = {i.code for i in report.issues}
        assert "dangling-dep" in codes
        assert "dag-cycle" in codes

    def test_valid_project_has_no_dangling_dep(self) -> None:
        config = make_config([ws("a", ["src/a/**"], []), ws("b", ["src/b/**"], ["a"])])
        report = validate_project(config, check_fs=False)
        assert all(i.code != "dangling-dep" for i in report.issues)


class TestSpecRunnerContractGuard:
    """H-7 guard: gates+prefix isolation must not run on a spec-runner
    that does not support --spec-prefix (artifacts would silently land at
    unprefixed paths, missed by both the ignore block and the gates
    exclusion)."""

    def test_help_with_flag_passes(self, monkeypatch) -> None:
        from maestro import preflight

        def fake_run(cmd, **kwargs):
            assert cmd == ["spec-runner", "run", "--help"]
            return subprocess.CompletedProcess(
                cmd, 0, stdout="usage: ... --spec-prefix SPEC_PREFIX ...", stderr=""
            )

        monkeypatch.setattr(preflight.subprocess, "run", fake_run)
        assert preflight._check_spec_runner_contract() == []

    def test_help_without_flag_errors(self, monkeypatch) -> None:
        from maestro import preflight

        monkeypatch.setattr(
            preflight.subprocess,
            "run",
            lambda cmd, **_kw: subprocess.CompletedProcess(
                cmd, 0, stdout="usage: old spec-runner", stderr=""
            ),
        )
        issues = preflight._check_spec_runner_contract()
        assert [i.code for i in issues] == ["spec-runner-prefix-unsupported"]
        assert issues[0].severity == "error"

    def test_missing_binary_errors(self, monkeypatch) -> None:
        from maestro import preflight

        def raise_fnf(cmd, **kw):
            raise FileNotFoundError("spec-runner")

        monkeypatch.setattr(preflight.subprocess, "run", raise_fnf)
        issues = preflight._check_spec_runner_contract()
        assert [i.code for i in issues] == ["spec-runner-prefix-unsupported"]
        assert issues[0].severity == "error"


class TestSpecRunnerVersionGate:
    """The installed spec-runner must meet the pinned floor.

    Two stacked reasons: below 2.16 the harness-owned spec/.gitignore lands in
    task commits (#122), and below 2.24 a run can exit 0 with work undone and
    record no honest stop reason (#169b) — the false-green class the
    completeness gate and the retry classifier are built around. Preflight
    blocks fail-closed before any worktree exists.
    """

    @staticmethod
    def _fake_version(monkeypatch, stdout: str, returncode: int = 0) -> None:
        from maestro import preflight

        def fake_run(cmd, **kwargs):
            assert cmd == ["spec-runner", "--version"]
            return subprocess.CompletedProcess(
                cmd, returncode, stdout=stdout, stderr=""
            )

        monkeypatch.setattr(preflight.subprocess, "run", fake_run)

    def test_2_15_x_is_blocked(self, monkeypatch) -> None:
        from maestro import preflight

        self._fake_version(monkeypatch, "spec-runner 2.15.0\n")
        issues = preflight._check_spec_runner_version()
        assert [i.code for i in issues] == ["spec-runner-version-unsupported"]
        assert issues[0].severity == "error"
        assert "2.15.0" in issues[0].message  # found version
        assert "2.24.0" in issues[0].message  # required version
        assert "spec/.gitignore" in issues[0].message  # reason
        assert "upgrade" in issues[0].message.lower()  # remedy

    def test_previous_floor_is_now_blocked(self, monkeypatch) -> None:
        """2.16 was the floor until #169b; it no longer is.

        A user who upgraded once and stopped is exactly who this gate has to
        stop now: 2.16 still exits 0 with work undone.
        """
        from maestro import preflight

        self._fake_version(monkeypatch, "spec-runner 2.16.0\n")
        issues = preflight._check_spec_runner_version()
        assert [i.code for i in issues] == ["spec-runner-version-unsupported"]
        assert "2.24.0" in issues[0].message

    def test_minimum_version_passes(self, monkeypatch) -> None:
        from maestro import preflight

        self._fake_version(monkeypatch, "spec-runner 2.24.0\n")
        assert preflight._check_spec_runner_version() == []

    def test_newer_version_passes(self, monkeypatch) -> None:
        from maestro import preflight

        self._fake_version(monkeypatch, "spec-runner 3.0.1\n")
        assert preflight._check_spec_runner_version() == []

    def test_malformed_output_is_blocked(self, monkeypatch) -> None:
        from maestro import preflight

        self._fake_version(monkeypatch, "something unexpected\n")
        issues = preflight._check_spec_runner_version()
        assert [i.code for i in issues] == ["spec-runner-version-unsupported"]
        assert issues[0].severity == "error"
        # The failure mode must be distinguishable from a missing binary.
        assert "unrecognized" in issues[0].message
        assert "something unexpected" in issues[0].message

    def test_dev_version_is_not_guessed(self, monkeypatch) -> None:
        from maestro import preflight

        self._fake_version(monkeypatch, "spec-runner 2.24.0.dev1\n")
        issues = preflight._check_spec_runner_version()
        assert [i.code for i in issues] == ["spec-runner-version-unsupported"]

    def test_missing_binary_is_blocked(self, monkeypatch) -> None:
        from maestro import preflight

        def raise_fnf(cmd, **kw):
            raise FileNotFoundError("spec-runner")

        monkeypatch.setattr(preflight.subprocess, "run", raise_fnf)
        issues = preflight._check_spec_runner_version()
        assert [i.code for i in issues] == ["spec-runner-version-unsupported"]
        assert issues[0].severity == "error"
        assert "not found" in issues[0].message

    def test_timeout_is_blocked(self, monkeypatch) -> None:
        from maestro import preflight

        def raise_timeout(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 30)

        monkeypatch.setattr(preflight.subprocess, "run", raise_timeout)
        issues = preflight._check_spec_runner_version()
        assert [i.code for i in issues] == ["spec-runner-version-unsupported"]
        assert issues[0].severity == "error"
        assert "timed out" in issues[0].message

    def test_nonzero_exit_is_blocked(self, monkeypatch) -> None:
        from maestro import preflight

        self._fake_version(monkeypatch, "spec-runner 2.16.0\n", returncode=1)
        issues = preflight._check_spec_runner_version()
        assert [i.code for i in issues] == ["spec-runner-version-unsupported"]
        assert "exited with code 1" in issues[0].message

    def test_override_downgrades_to_warning(self, monkeypatch) -> None:
        from maestro import preflight

        self._fake_version(monkeypatch, "spec-runner 2.15.0\n")
        monkeypatch.setenv("MAESTRO_SPEC_RUNNER_ALLOW_UNVERIFIED", "1")
        issues = preflight._check_spec_runner_version()
        assert [i.code for i in issues] == ["spec-runner-version-unsupported"]
        assert issues[0].severity == "warning"

    def test_gitignore_stays_visible_to_changed_paths(self) -> None:
        # The convention's Maestro half: spec/.gitignore is NOT an
        # orchestrator-managed artifact — the scope gate must see it.
        from maestro.changed_paths import _orchestrator_managed

        assert _orchestrator_managed("spec/.gitignore") is False


class TestSpecRunnerVersionParse:
    def test_plain_version(self) -> None:
        from maestro.spec_runner import parse_spec_runner_version

        assert parse_spec_runner_version("spec-runner 2.16.0\n") == (2, 16, 0)

    def test_whitespace_tolerated(self) -> None:
        from maestro.spec_runner import parse_spec_runner_version

        assert parse_spec_runner_version("  spec-runner 2.16.3  \n") == (2, 16, 3)

    def test_rejects_suffixes_and_garbage(self) -> None:
        from maestro.spec_runner import parse_spec_runner_version

        assert parse_spec_runner_version("spec-runner 2.16.0rc1") is None
        assert parse_spec_runner_version("spec-runner 2.16.0+local") is None
        assert parse_spec_runner_version("2.16.0") is None
        assert parse_spec_runner_version("") is None


class TestTrackedSpecRunnerConfigWarning:
    def test_tracked_config_warns(self, tmp_path) -> None:
        from maestro import preflight

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
        )
        (repo / "spec-runner.config.yaml").write_text("x: 1\n")
        subprocess.run(
            ["git", "-C", str(repo), "add", "spec-runner.config.yaml"],
            check=True,
            capture_output=True,
        )
        issues = preflight._check_tracked_spec_runner_config(repo)
        assert [i.code for i in issues] == ["spec-runner-config-tracked"]
        assert issues[0].severity == "warning"
        # #125: the warning must point at the documented dual-mode pattern.
        assert "Dual-mode repos" in issues[0].message

    def test_untracked_or_absent_is_silent(self, tmp_path) -> None:
        from maestro import preflight

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
        )
        assert preflight._check_tracked_spec_runner_config(repo) == []
