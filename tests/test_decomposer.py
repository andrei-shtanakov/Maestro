"""Tests for the ProjectDecomposer module."""

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.decomposer import (
    DecomposerError,
    ProjectDecomposer,
    ScopeOverlapWarning,
    _patterns_overlap,
    resolve_spec_gen_settings,
)
from maestro.domain import (
    CriteriaConfig,
    DeliveryPolicy,
    DomainProfile,
    RoleScopes,
    SpecGenSection,
    VerificationSection,
    VerifierSpec,
    WorkspacePolicy,
)
from maestro.models import SPEC_PREFIX, WorkstreamConfig


def _domain_with_spec_gen(
    *, budget_usd: float | None, timeout_minutes: float | None = None
) -> DomainProfile:
    """Minimal DomainProfile whose spec_gen carries the given budget/timeout."""
    return DomainProfile(
        verification=VerificationSection(
            verifier=VerifierSpec(
                argv=["v", "--out", "{out}"], timeout_seconds=60.0, error_retry_budget=0
            ),
            artifact="a.md",
            rework_budget=0,
            verdict_schema_version=2,
            criteria=CriteriaConfig(
                visibility="shared", source="c.yaml", sha256="b" * 64
            ),
        ),
        workspace=WorkspacePolicy(
            roles={"verifier": RoleScopes(write=["evidence/**"])},
            evidence_root="evidence",
        ),
        delivery=DeliveryPolicy(
            local_merge="before_remote_pr", remote="github_pr", evidence="all"
        ),
        spec_gen=SpecGenSection(budget_usd=budget_usd, timeout_minutes=timeout_minutes),
    )


# =============================================================================
# Helper Factories
# =============================================================================


def _make_workstream_json(
    workstreams: list[dict[str, object]] | None = None,
) -> str:
    """Build a valid JSON response with workstreams.

    Args:
        workstreams: Optional list of workstream dicts.

    Returns:
        JSON string containing the workstreams array.
    """
    if workstreams is None:
        workstreams = [
            {
                "id": "auth-module",
                "title": "Authentication Module",
                "description": "Implement user authentication",
                "scope": ["src/auth/**"],
                "depends_on": [],
                "priority": 80,
            },
            {
                "id": "api-endpoints",
                "title": "REST API Endpoints",
                "description": "Implement REST API endpoints",
                "scope": ["src/api/**"],
                "depends_on": ["auth-module"],
                "priority": 60,
            },
        ]
    return json.dumps(workstreams)


def _make_spec_response() -> str:
    """Build a valid tasks.md response from Claude.

    Returns:
        String with tasks.md content.
    """
    return (
        "# Tasks\n"
        "\n"
        "### TASK-001: Implement login endpoint\n"
        "🔴 P0 | ⬜ TODO | Est: 2h\n"
        "\n"
        "Implement JWT login.\n"
        "\n"
        "**Checklist:**\n"
        "- [ ] Create endpoint\n"
        "- [ ] Add tests\n"
    )


def _make_subprocess_result(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> MagicMock:
    """Build a mock subprocess.CompletedProcess.

    Args:
        stdout: Standard output text.
        stderr: Standard error text.
        returncode: Process return code.

    Returns:
        MagicMock mimicking subprocess.CompletedProcess.
    """
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


# =============================================================================
# Unit Tests: Initialization
# =============================================================================


class TestProjectDecomposerInit:
    """Tests for ProjectDecomposer initialization."""

    def test_init_with_defaults(self, temp_dir: Path) -> None:
        """Test initialization with default parameters."""
        decomposer = ProjectDecomposer(temp_dir)

        assert decomposer._repo_path == temp_dir
        assert decomposer._claude_command == "claude"

    def test_init_with_custom_claude_command(self, temp_dir: Path) -> None:
        """Test initialization with a custom Claude command."""
        decomposer = ProjectDecomposer(
            temp_dir,
            claude_command="/usr/local/bin/claude-dev",
        )

        assert decomposer._claude_command == "/usr/local/bin/claude-dev"

    def test_init_stores_repo_path(self, temp_dir: Path) -> None:
        """Test that repo_path is stored as provided."""
        decomposer = ProjectDecomposer(temp_dir)

        assert decomposer._repo_path == temp_dir


# =============================================================================
# Unit Tests: decompose()
# =============================================================================


class TestDecompose:
    """Tests for the decompose method."""

    @patch("maestro.decomposer.subprocess.run")
    def test_decompose_returns_workstreams_from_valid_json(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test decompose parses valid JSON into WorkstreamConfig list."""
        valid_json = _make_workstream_json()
        # First call: _get_repo_tree (find), second call: _run_claude
        mock_run.side_effect = [
            _make_subprocess_result(stdout=".\n./src\n./tests\n"),
            _make_subprocess_result(stdout=valid_json),
        ]

        decomposer = ProjectDecomposer(temp_dir)
        result = decomposer.decompose("Build a web app")

        assert isinstance(result, list)
        assert len(result) == 2
        assert all(isinstance(z, WorkstreamConfig) for z in result)
        assert result[0].id == "auth-module"
        assert result[1].id == "api-endpoints"

    @patch("maestro.decomposer.subprocess.run")
    def test_decompose_parses_json_wrapped_in_markdown(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test decompose extracts JSON from markdown code blocks."""
        wrapped = (
            "Here is the decomposition:\n```json\n" + _make_workstream_json() + "\n```"
        )
        mock_run.side_effect = [
            _make_subprocess_result(stdout=".\n./src\n"),
            _make_subprocess_result(stdout=wrapped),
        ]

        decomposer = ProjectDecomposer(temp_dir)
        result = decomposer.decompose("Build a web app")

        assert len(result) == 2
        assert result[0].id == "auth-module"

    @patch("maestro.decomposer.subprocess.run")
    def test_decompose_raises_on_invalid_json(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test decompose raises DecomposerError on invalid JSON."""
        mock_run.side_effect = [
            _make_subprocess_result(stdout=".\n"),
            _make_subprocess_result(stdout="[{invalid json}]"),
        ]

        decomposer = ProjectDecomposer(temp_dir)

        with pytest.raises(DecomposerError, match="Failed to parse JSON"):
            decomposer.decompose("Build a web app")

    @patch("maestro.decomposer.subprocess.run")
    def test_decompose_raises_on_no_json_array(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test decompose raises DecomposerError when no JSON array found."""
        mock_run.side_effect = [
            _make_subprocess_result(stdout=".\n"),
            _make_subprocess_result(stdout="No valid JSON here."),
        ]

        decomposer = ProjectDecomposer(temp_dir)

        with pytest.raises(DecomposerError, match="No JSON array found"):
            decomposer.decompose("Build a web app")

    @patch("maestro.decomposer.subprocess.run")
    def test_decompose_raises_on_nonzero_exit_code(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test decompose raises DecomposerError when Claude CLI returns non-zero."""
        mock_run.side_effect = [
            _make_subprocess_result(stdout=".\n"),
            _make_subprocess_result(
                returncode=1,
                stderr="Error: API rate limit exceeded",
            ),
        ]

        decomposer = ProjectDecomposer(temp_dir)

        with pytest.raises(DecomposerError, match="Claude CLI failed with code 1"):
            decomposer.decompose("Build a web app")

    @patch("maestro.decomposer.subprocess.run")
    def test_decompose_raises_on_empty_result(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test decompose raises DecomposerError when result is empty list."""
        mock_run.side_effect = [
            _make_subprocess_result(stdout=".\n"),
            _make_subprocess_result(stdout="[]"),
        ]

        decomposer = ProjectDecomposer(temp_dir)

        with pytest.raises(
            DecomposerError, match="Decomposition produced no workstreams"
        ):
            decomposer.decompose("Build a web app")

    @patch("maestro.decomposer.subprocess.run")
    def test_decompose_raises_on_invalid_workstream_fields(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test decompose raises DecomposerError on invalid workstream fields."""
        bad_workstreams = json.dumps([{"id": "", "title": "T", "description": "D"}])
        mock_run.side_effect = [
            _make_subprocess_result(stdout=".\n"),
            _make_subprocess_result(stdout=bad_workstreams),
        ]

        decomposer = ProjectDecomposer(temp_dir)

        with pytest.raises(DecomposerError, match="Failed to validate workstreams"):
            decomposer.decompose("Build a web app")

    @patch("maestro.decomposer.subprocess.run")
    def test_decompose_raises_on_timeout(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test decompose raises DecomposerError on subprocess timeout."""
        mock_run.side_effect = [
            _make_subprocess_result(stdout=".\n"),
            subprocess.TimeoutExpired(cmd=["claude"], timeout=600),
        ]

        decomposer = ProjectDecomposer(temp_dir)

        with pytest.raises(DecomposerError, match="timed out"):
            decomposer.decompose("Build a web app")

    @patch("maestro.decomposer.subprocess.run")
    def test_decompose_raises_on_command_not_found(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test decompose raises DecomposerError when claude binary missing."""
        mock_run.side_effect = [
            _make_subprocess_result(stdout=".\n"),
            FileNotFoundError("No such file: 'claude'"),
        ]

        decomposer = ProjectDecomposer(temp_dir)

        with pytest.raises(DecomposerError, match="not found"):
            decomposer.decompose("Build a web app")

    @patch("maestro.decomposer.subprocess.run")
    def test_decompose_preserves_depends_on(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test decompose preserves dependency information."""
        valid_json = _make_workstream_json()
        mock_run.side_effect = [
            _make_subprocess_result(stdout=".\n"),
            _make_subprocess_result(stdout=valid_json),
        ]

        decomposer = ProjectDecomposer(temp_dir)
        result = decomposer.decompose("Build a web app")

        assert result[0].depends_on == []
        assert result[1].depends_on == ["auth-module"]

    @patch("maestro.decomposer.subprocess.run")
    def test_decompose_preserves_priority(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test decompose preserves priority values."""
        valid_json = _make_workstream_json()
        mock_run.side_effect = [
            _make_subprocess_result(stdout=".\n"),
            _make_subprocess_result(stdout=valid_json),
        ]

        decomposer = ProjectDecomposer(temp_dir)
        result = decomposer.decompose("Build a web app")

        assert result[0].priority == 80
        assert result[1].priority == 60


# =============================================================================
# Unit Tests: generate_spec()
# =============================================================================


class TestGenerateSpec:
    """generate_spec delegates to `spec-runner plan --full` (async)."""

    @pytest.fixture
    def workstream(self) -> WorkstreamConfig:
        return WorkstreamConfig(
            id="ws1",
            title="Feature X",
            description="Do the thing",
            scope=["src/x.py", "tests/test_x.py"],
        )

    def _fake_proc(self, returncode: int = 0, stderr: bytes = b""):
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(b"", stderr))
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=returncode)
        return proc

    @pytest.mark.anyio
    async def test_invokes_spec_runner_plan_full(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        workspace.mkdir()
        (workspace / "spec").mkdir()
        (workspace / "spec" / f"{SPEC_PREFIX}tasks.md").write_text(
            "# tasks\n", encoding="utf-8"
        )
        dec = ProjectDecomposer(repo_path=temp_dir)
        proc = self._fake_proc()
        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
        ) as exec_mock:
            await dec.generate_spec(workstream, workspace)
        cmd = list(exec_mock.call_args[0])
        assert cmd[:4] == ["spec-runner", "plan", "--full", "--from-file"]
        assert "--no-branch" in cmd and "--no-commit" in cmd
        assert "--no-interactive" in cmd
        assert "--budget" in cmd and cmd[cmd.index("--budget") + 1] == "1.0"
        assert exec_mock.call_args.kwargs["cwd"] == workspace

    @pytest.mark.anyio
    async def test_description_file_has_workstream_fields(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        (workspace / "spec").mkdir(parents=True)
        (workspace / "spec" / f"{SPEC_PREFIX}tasks.md").write_text(
            "x", encoding="utf-8"
        )
        dec = ProjectDecomposer(repo_path=temp_dir)
        captured = {}

        async def fake_exec(*args, **kwargs):
            desc_path = args[args.index("--from-file") + 1]
            captured["text"] = Path(desc_path).read_text(  # noqa: ASYNC240
                encoding="utf-8"
            )
            return self._fake_proc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await dec.generate_spec(workstream, workspace)
        assert "Feature X" in captured["text"]
        assert "Do the thing" in captured["text"]
        assert "src/x.py" in captured["text"]

    @pytest.mark.anyio
    async def test_budget_none_omits_flag(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        (workspace / "spec").mkdir(parents=True)
        (workspace / "spec" / f"{SPEC_PREFIX}tasks.md").write_text(
            "x", encoding="utf-8"
        )
        dec = ProjectDecomposer(repo_path=temp_dir, spec_gen_budget_usd=None)
        proc = self._fake_proc()
        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
        ) as exec_mock:
            await dec.generate_spec(workstream, workspace)
        assert "--budget" not in list(exec_mock.call_args[0])

    @pytest.mark.anyio
    async def test_domain_spec_gen_budget_is_wired(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        """F4: domain.spec_gen.budget_usd is the SSOT and reaches --budget."""
        workspace = temp_dir / "ws"
        (workspace / "spec").mkdir(parents=True)
        (workspace / "spec" / f"{SPEC_PREFIX}tasks.md").write_text(
            "x", encoding="utf-8"
        )
        domain = _domain_with_spec_gen(budget_usd=2.5)
        budget, timeout = resolve_spec_gen_settings(domain, None)
        assert budget == 2.5
        assert timeout is None
        dec = ProjectDecomposer(
            repo_path=temp_dir,
            spec_gen_budget_usd=budget,
            spec_gen_timeout_minutes=timeout,
        )
        proc = self._fake_proc()
        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
        ) as exec_mock:
            await dec.generate_spec(workstream, workspace)
        cmd = list(exec_mock.call_args[0])
        assert "--budget" in cmd and cmd[cmd.index("--budget") + 1] == "2.5"

    def test_resolve_spec_gen_falls_back_to_legacy_budget(self) -> None:
        """No domain.spec_gen -> legacy spec_runner budget, no gen timeout."""
        assert resolve_spec_gen_settings(None, 1.0) == (1.0, None)

    @pytest.mark.anyio
    async def test_nonzero_exit_raises(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        (workspace / "spec").mkdir(parents=True)
        dec = ProjectDecomposer(repo_path=temp_dir)
        proc = self._fake_proc(returncode=1, stderr=b"boom")
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            pytest.raises(DecomposerError, match="boom"),
        ):
            await dec.generate_spec(workstream, workspace)

    @pytest.mark.anyio
    async def test_zero_exit_but_no_tasks_file_raises(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        (workspace / "spec").mkdir(parents=True)  # no tasks.md written
        dec = ProjectDecomposer(repo_path=temp_dir)
        proc = self._fake_proc(returncode=0)
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            pytest.raises(DecomposerError, match=r"tasks\.md"),
        ):
            await dec.generate_spec(workstream, workspace)

    @pytest.mark.anyio
    async def test_spec_runner_not_found_raises(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        (workspace / "spec").mkdir(parents=True)
        dec = ProjectDecomposer(repo_path=temp_dir)
        with (
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(side_effect=FileNotFoundError("spec-runner")),
            ),
            pytest.raises(DecomposerError, match="spec-runner"),
        ):
            await dec.generate_spec(workstream, workspace)

    @pytest.mark.anyio
    async def test_cancellation_terminates_subprocess(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        (workspace / "spec").mkdir(parents=True)
        dec = ProjectDecomposer(repo_path=temp_dir)
        proc = self._fake_proc()
        proc.communicate = AsyncMock(side_effect=asyncio.CancelledError())
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            pytest.raises(asyncio.CancelledError),
        ):
            await dec.generate_spec(workstream, workspace)
        proc.terminate.assert_called_once()

    @pytest.mark.anyio
    async def test_temp_desc_file_removed_on_success(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        (workspace / "spec").mkdir(parents=True)
        (workspace / "spec" / f"{SPEC_PREFIX}tasks.md").write_text(
            "x", encoding="utf-8"
        )
        dec = ProjectDecomposer(repo_path=temp_dir)
        captured: dict[str, str] = {}

        async def fake_exec(*args, **kwargs):
            captured["desc"] = args[args.index("--from-file") + 1]
            return self._fake_proc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await dec.generate_spec(workstream, workspace)
        assert not Path(captured["desc"]).exists()  # noqa: ASYNC240

    @pytest.mark.anyio
    async def test_temp_desc_file_removed_on_failure(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        (workspace / "spec").mkdir(parents=True)
        dec = ProjectDecomposer(repo_path=temp_dir)
        captured: dict[str, str] = {}

        async def fake_exec(*args, **kwargs):
            captured["desc"] = args[args.index("--from-file") + 1]
            return self._fake_proc(returncode=1, stderr=b"boom")

        with (
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            pytest.raises(DecomposerError, match="boom"),
        ):
            await dec.generate_spec(workstream, workspace)
        assert not Path(captured["desc"]).exists()  # noqa: ASYNC240


# =============================================================================
# Unit Tests: generate_spec() prefixing (H-7)
# =============================================================================


class TestGenerateSpecPrefix:
    """H-7: generation is namespaced with SPEC_PREFIX end to end."""

    @pytest.fixture
    def workstream(self) -> WorkstreamConfig:
        return WorkstreamConfig(
            id="ws1",
            title="Feature X",
            description="Do the thing",
            scope=["src/x.py", "tests/test_x.py"],
        )

    def _fake_proc(self, returncode: int = 0, stderr: bytes = b""):
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(b"", stderr))
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=returncode)
        return proc

    @pytest.mark.anyio
    async def test_plan_cmd_carries_spec_prefix_and_checks_prefixed_tasks(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        workspace.mkdir()
        (workspace / "spec").mkdir()
        (workspace / "spec" / f"{SPEC_PREFIX}tasks.md").write_text(
            "### TASK-001: t\n", encoding="utf-8"
        )
        dec = ProjectDecomposer(repo_path=temp_dir)
        proc = self._fake_proc()
        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
        ) as exec_mock:
            await dec.generate_spec(workstream, workspace)
        cmd = list(exec_mock.call_args[0])
        idx = cmd.index("--spec-prefix")
        assert cmd[idx + 1] == SPEC_PREFIX

    @pytest.mark.anyio
    async def test_unprefixed_tasks_md_no_longer_satisfies_check(
        self, temp_dir: Path, workstream: WorkstreamConfig
    ) -> None:
        workspace = temp_dir / "ws"
        (workspace / "spec").mkdir(parents=True)
        (workspace / "spec" / "tasks.md").write_text(  # legacy unprefixed path
            "### TASK-001: t\n", encoding="utf-8"
        )
        dec = ProjectDecomposer(repo_path=temp_dir)
        proc = self._fake_proc()
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            pytest.raises(DecomposerError, match=r"tasks\.md was not"),
        ):
            await dec.generate_spec(workstream, workspace)


# =============================================================================
# Unit Tests: on_pid callback (_run_spec_runner)
# =============================================================================


class _FakeProc:
    """Minimal fake asyncio subprocess for on_pid tests."""

    def __init__(self, pid: int = 1234, returncode: int = 0) -> None:
        self.pid = pid
        self.returncode = returncode
        self.terminated = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return (b"", b"")

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    async def wait(self) -> int:
        return self.returncode


class TestOnPidCallback:
    """on_pid is invoked with the spawned pid; failure terminates the proc."""

    @pytest.mark.anyio
    async def test_on_pid_called_with_spawned_pid(self, temp_dir: Path) -> None:
        proc = _FakeProc(pid=5150)

        async def fake_exec(*args, **kwargs):
            return proc

        dec = ProjectDecomposer(repo_path=temp_dir)
        seen = []

        async def on_pid(pid: int) -> None:
            seen.append(pid)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await dec._run_spec_runner(["spec-runner"], temp_dir, 1, on_pid=on_pid)
        assert seen == [5150]

    @pytest.mark.anyio
    async def test_on_pid_failure_terminates_and_raises(self, temp_dir: Path) -> None:
        proc = _FakeProc(pid=5151)

        async def fake_exec(*args, **kwargs):
            return proc

        dec = ProjectDecomposer(repo_path=temp_dir)

        async def bad_on_pid(pid: int) -> None:
            raise RuntimeError("db down")

        with (
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            pytest.raises(RuntimeError, match="db down"),
        ):
            await dec._run_spec_runner(["spec-runner"], temp_dir, 1, on_pid=bad_on_pid)
        assert proc.terminated is True  # process killed, not orphaned

    @pytest.mark.anyio
    async def test_on_pid_cancel_terminates_and_reraises(self, temp_dir: Path) -> None:
        proc = _FakeProc(pid=5152)

        async def fake_exec(*args, **kwargs):
            return proc

        dec = ProjectDecomposer(repo_path=temp_dir)

        async def cancelled_on_pid(pid: int) -> None:
            raise asyncio.CancelledError()

        with (
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            pytest.raises(asyncio.CancelledError),
        ):
            await dec._run_spec_runner(
                ["spec-runner"], temp_dir, 1, on_pid=cancelled_on_pid
            )
        assert proc.terminated is True  # not orphaned on cancel-during-persist


# =============================================================================
# Unit Tests: validate_non_overlap()
# =============================================================================


class TestValidateNonOverlap:
    """Tests for the validate_non_overlap static method."""

    def test_non_overlapping_scopes_returns_no_warnings(self) -> None:
        """Test that non-overlapping scopes produce no warnings."""
        workstreams = [
            WorkstreamConfig(
                id="frontend",
                title="Frontend",
                description="Build frontend",
                scope=["src/frontend/**"],
            ),
            WorkstreamConfig(
                id="backend",
                title="Backend",
                description="Build backend",
                scope=["src/backend/**"],
            ),
            WorkstreamConfig(
                id="tests",
                title="Tests",
                description="Write tests",
                scope=["tests/**"],
            ),
        ]

        warnings = ProjectDecomposer.validate_non_overlap(workstreams)

        assert warnings == []

    def test_overlapping_scopes_returns_warnings(self) -> None:
        """Test that overlapping scopes produce correct warnings."""
        workstreams = [
            WorkstreamConfig(
                id="auth",
                title="Auth",
                description="Authentication",
                scope=["src/**"],
            ),
            WorkstreamConfig(
                id="api",
                title="API",
                description="API endpoints",
                scope=["src/api/**"],
            ),
        ]

        warnings = ProjectDecomposer.validate_non_overlap(workstreams)

        assert len(warnings) == 1
        assert isinstance(warnings[0], ScopeOverlapWarning)
        assert warnings[0].workstream_a == "auth"
        assert warnings[0].workstream_b == "api"

    def test_empty_scopes_returns_no_warnings(self) -> None:
        """Test that empty scopes produce no warnings."""
        workstreams = [
            WorkstreamConfig(
                id="task-a",
                title="Task A",
                description="First task",
                scope=[],
            ),
            WorkstreamConfig(
                id="task-b",
                title="Task B",
                description="Second task",
                scope=[],
            ),
        ]

        warnings = ProjectDecomposer.validate_non_overlap(workstreams)

        assert warnings == []

    def test_single_workstream_returns_no_warnings(self) -> None:
        """Test that a single workstream produces no warnings."""
        workstreams = [
            WorkstreamConfig(
                id="solo",
                title="Solo",
                description="Only task",
                scope=["src/**"],
            ),
        ]

        warnings = ProjectDecomposer.validate_non_overlap(workstreams)

        assert warnings == []

    def test_empty_list_returns_no_warnings(self) -> None:
        """Test that empty workstreams list produces no warnings."""
        warnings = ProjectDecomposer.validate_non_overlap([])

        assert warnings == []

    def test_multiple_overlapping_pairs(self) -> None:
        """Test detection of multiple overlapping pairs."""
        workstreams = [
            WorkstreamConfig(
                id="task-a",
                title="Task A",
                description="First",
                scope=["src/**"],
            ),
            WorkstreamConfig(
                id="task-b",
                title="Task B",
                description="Second",
                scope=["src/module/**"],
            ),
            WorkstreamConfig(
                id="task-c",
                title="Task C",
                description="Third",
                scope=["src/module/sub/**"],
            ),
        ]

        warnings = ProjectDecomposer.validate_non_overlap(workstreams)

        # All three pairs should overlap: A<->B, A<->C, B<->C
        assert len(warnings) == 3

    def test_scope_overlap_warning_str(self) -> None:
        """Test ScopeOverlapWarning string representation."""
        warning = ScopeOverlapWarning(
            workstream_a="auth",
            workstream_b="api",
            overlapping_patterns=["src/** <-> src/api/**"],
        )

        msg = str(warning)

        assert "auth" in msg
        assert "api" in msg
        assert "src/**" in msg

    def test_mixed_overlapping_and_non_overlapping(self) -> None:
        """Test with a mix of overlapping and non-overlapping scopes."""
        workstreams = [
            WorkstreamConfig(
                id="frontend",
                title="Frontend",
                description="Frontend code",
                scope=["frontend/**"],
            ),
            WorkstreamConfig(
                id="backend",
                title="Backend",
                description="Backend code",
                scope=["backend/**"],
            ),
            WorkstreamConfig(
                id="backend-extra",
                title="Backend Extra",
                description="More backend code",
                scope=["backend/**"],
            ),
        ]

        warnings = ProjectDecomposer.validate_non_overlap(workstreams)

        assert len(warnings) == 1
        assert warnings[0].workstream_a == "backend"
        assert warnings[0].workstream_b == "backend-extra"


# =============================================================================
# Unit Tests: _patterns_overlap() helper
# =============================================================================


class TestPatternsOverlap:
    """Tests for the _patterns_overlap helper function."""

    def test_glob_star_overlaps_specific_file(self) -> None:
        """Test that 'src/**' overlaps with 'src/main.py'."""
        assert _patterns_overlap("src/**", "src/main.py") is True

    def test_different_directories_do_not_overlap(self) -> None:
        """Test that 'src/**' does not overlap with 'tests/**'."""
        assert _patterns_overlap("src/**", "tests/**") is False

    def test_identical_patterns_overlap(self) -> None:
        """Test that identical patterns overlap."""
        assert _patterns_overlap("src/**", "src/**") is True

    def test_identical_specific_files_overlap(self) -> None:
        """Test that identical specific file patterns overlap."""
        assert _patterns_overlap("src/main.py", "src/main.py") is True

    def test_parent_glob_overlaps_child_glob(self) -> None:
        """Test that parent directory glob overlaps child directory glob."""
        assert _patterns_overlap("src/**", "src/module/**") is True

    def test_sibling_directories_do_not_overlap(self) -> None:
        """Test that sibling directory globs do not overlap."""
        assert _patterns_overlap("src/auth/**", "src/api/**") is False

    def test_different_top_level_dirs(self) -> None:
        """Test that completely different top-level dirs do not overlap."""
        assert _patterns_overlap("docs/**", "lib/**") is False

    def test_no_directory_separator_patterns(self) -> None:
        """Test patterns without directory separators."""
        assert _patterns_overlap("*.py", "*.py") is True

    def test_root_file_and_nested_glob_no_overlap(self) -> None:
        """Test that a root file does not overlap nested directory glob."""
        assert _patterns_overlap("README.md", "src/**") is False

    def test_same_dir_wildcard_vs_double_star(self) -> None:
        """Test same-dir wildcard pattern vs double star."""
        assert _patterns_overlap("src/**", "src/*.py") is True

    def test_completely_unrelated_patterns(self) -> None:
        """Test two completely unrelated specific file paths."""
        assert _patterns_overlap("src/main.py", "tests/test_main.py") is False

    def test_nested_double_star_overlap(self) -> None:
        """Test nested double star patterns in same tree."""
        assert _patterns_overlap("src/**", "src/deep/nested/**") is True


# =============================================================================
# Unit Tests: _get_repo_tree()
# =============================================================================


class TestGetRepoTree:
    """Tests for the _get_repo_tree private method."""

    @patch("maestro.decomposer.subprocess.run")
    def test_get_repo_tree_returns_stdout(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test _get_repo_tree returns find command output."""
        mock_run.return_value = _make_subprocess_result(
            stdout=".\n./src\n./src/main.py\n./tests\n",
        )

        decomposer = ProjectDecomposer(temp_dir)
        result = decomposer._get_repo_tree()

        assert "./src" in result
        assert "./tests" in result

    @patch("maestro.decomposer.subprocess.run")
    def test_get_repo_tree_truncates_long_output(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test _get_repo_tree truncates output to 5000 chars."""
        long_output = "x" * 10000
        mock_run.return_value = _make_subprocess_result(stdout=long_output)

        decomposer = ProjectDecomposer(temp_dir)
        result = decomposer._get_repo_tree()

        assert len(result) == 5000

    @patch("maestro.decomposer.subprocess.run")
    def test_get_repo_tree_handles_find_not_found(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test _get_repo_tree returns fallback when find command missing."""
        mock_run.side_effect = FileNotFoundError("find not found")

        decomposer = ProjectDecomposer(temp_dir)
        result = decomposer._get_repo_tree()

        assert result == "(unable to list directory)"


# =============================================================================
# Unit Tests: _run_claude()
# =============================================================================


class TestRunClaude:
    """Tests for the _run_claude private method."""

    @patch("maestro.decomposer.subprocess.run")
    def test_run_claude_passes_correct_arguments(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test _run_claude passes the correct command and args."""
        mock_run.return_value = _make_subprocess_result(stdout="response")

        decomposer = ProjectDecomposer(temp_dir)
        decomposer._run_claude("test prompt")

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert cmd == [
            "claude",
            "--print",
            "-p",
            "test prompt",
            "--disallowedTools",
            "Edit",
            "Write",
            "Bash",
            "NotebookEdit",
        ]
        assert call_args[1]["cwd"] == temp_dir

    @patch("maestro.decomposer.subprocess.run")
    def test_run_claude_uses_custom_command(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test _run_claude uses the custom claude command."""
        mock_run.return_value = _make_subprocess_result(stdout="response")

        decomposer = ProjectDecomposer(temp_dir, claude_command="my-claude")
        decomposer._run_claude("test prompt")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "my-claude"

    @patch("maestro.decomposer.subprocess.run")
    def test_run_claude_returns_stdout(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test _run_claude returns the stdout content."""
        mock_run.return_value = _make_subprocess_result(
            stdout="Claude says hello",
        )

        decomposer = ProjectDecomposer(temp_dir)
        result = decomposer._run_claude("say hello")

        assert result == "Claude says hello"

    @patch("maestro.decomposer.subprocess.run")
    def test_run_claude_raises_on_nonzero_exit(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test _run_claude raises DecomposerError on non-zero exit code."""
        mock_run.return_value = _make_subprocess_result(
            returncode=2,
            stderr="fatal error",
        )

        decomposer = ProjectDecomposer(temp_dir)

        with pytest.raises(DecomposerError, match="Claude CLI failed with code 2"):
            decomposer._run_claude("bad prompt")

    @patch("maestro.decomposer.subprocess.run")
    def test_run_claude_raises_on_timeout(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test _run_claude raises DecomposerError on timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["claude"], timeout=600)

        decomposer = ProjectDecomposer(temp_dir)

        with pytest.raises(DecomposerError, match="timed out"):
            decomposer._run_claude("slow prompt")

    @patch("maestro.decomposer.subprocess.run")
    def test_run_claude_raises_on_file_not_found(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test _run_claude raises DecomposerError when binary not found."""
        mock_run.side_effect = FileNotFoundError("claude not found")

        decomposer = ProjectDecomposer(temp_dir)

        with pytest.raises(DecomposerError, match="not found"):
            decomposer._run_claude("prompt")
