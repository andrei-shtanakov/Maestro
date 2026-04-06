"""Tests for the ProjectDecomposer module."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro.decomposer import (
    DecomposerError,
    ProjectDecomposer,
    ScopeOverlapWarning,
    _patterns_overlap,
)
from maestro.models import ZadachaConfig


# =============================================================================
# Helper Factories
# =============================================================================


def _make_zadacha_json(
    zadachi: list[dict[str, object]] | None = None,
) -> str:
    """Build a valid JSON response with zadachi.

    Args:
        zadachi: Optional list of zadacha dicts.

    Returns:
        JSON string containing the zadachi array.
    """
    if zadachi is None:
        zadachi = [
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
    return json.dumps(zadachi)


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
    def test_decompose_returns_zadachi_from_valid_json(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test decompose parses valid JSON into ZadachaConfig list."""
        valid_json = _make_zadacha_json()
        # First call: _get_repo_tree (find), second call: _run_claude
        mock_run.side_effect = [
            _make_subprocess_result(stdout=".\n./src\n./tests\n"),
            _make_subprocess_result(stdout=valid_json),
        ]

        decomposer = ProjectDecomposer(temp_dir)
        result = decomposer.decompose("Build a web app")

        assert isinstance(result, list)
        assert len(result) == 2
        assert all(isinstance(z, ZadachaConfig) for z in result)
        assert result[0].id == "auth-module"
        assert result[1].id == "api-endpoints"

    @patch("maestro.decomposer.subprocess.run")
    def test_decompose_parses_json_wrapped_in_markdown(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test decompose extracts JSON from markdown code blocks."""
        wrapped = (
            "Here is the decomposition:\n```json\n" + _make_zadacha_json() + "\n```"
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

        with pytest.raises(DecomposerError, match="Decomposition produced no zadachi"):
            decomposer.decompose("Build a web app")

    @patch("maestro.decomposer.subprocess.run")
    def test_decompose_raises_on_invalid_zadacha_fields(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test decompose raises DecomposerError on invalid zadacha fields."""
        bad_zadachi = json.dumps([{"id": "", "title": "T", "description": "D"}])
        mock_run.side_effect = [
            _make_subprocess_result(stdout=".\n"),
            _make_subprocess_result(stdout=bad_zadachi),
        ]

        decomposer = ProjectDecomposer(temp_dir)

        with pytest.raises(DecomposerError, match="Failed to validate zadachi"):
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
        valid_json = _make_zadacha_json()
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
        valid_json = _make_zadacha_json()
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
    """Tests for the generate_spec method."""

    @patch("maestro.decomposer.subprocess.run")
    def test_generate_spec_creates_tasks_file(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test generate_spec creates tasks.md in spec/ directory."""
        mock_run.return_value = _make_subprocess_result(
            stdout=_make_spec_response(),
        )

        zadacha = ZadachaConfig(
            id="auth-module",
            title="Authentication Module",
            description="Implement user authentication",
            scope=["src/auth/**"],
        )
        workspace = temp_dir / "workspace"
        workspace.mkdir()

        decomposer = ProjectDecomposer(temp_dir)
        decomposer.generate_spec(zadacha, workspace)

        spec_dir = workspace / "spec"
        assert spec_dir.is_dir()
        assert (spec_dir / "tasks.md").exists()

    @patch("maestro.decomposer.subprocess.run")
    def test_generate_spec_tasks_content(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test generate_spec writes correct content to tasks.md."""
        mock_run.return_value = _make_subprocess_result(
            stdout=_make_spec_response(),
        )

        zadacha = ZadachaConfig(
            id="auth-module",
            title="Authentication Module",
            description="Implement user authentication",
            scope=["src/auth/**"],
        )
        workspace = temp_dir / "workspace"
        workspace.mkdir()

        decomposer = ProjectDecomposer(temp_dir)
        decomposer.generate_spec(zadacha, workspace)

        spec_dir = workspace / "spec"
        tasks_text = (spec_dir / "tasks.md").read_text()
        assert "Tasks" in tasks_text
        assert "TASK-001" in tasks_text

    @patch("maestro.decomposer.subprocess.run")
    def test_generate_spec_raises_on_claude_failure(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test generate_spec raises DecomposerError on Claude CLI failure."""
        mock_run.return_value = _make_subprocess_result(
            returncode=1,
            stderr="Error: something went wrong",
        )

        zadacha = ZadachaConfig(
            id="auth-module",
            title="Authentication Module",
            description="Implement user authentication",
            scope=["src/auth/**"],
        )
        workspace = temp_dir / "workspace"
        workspace.mkdir()

        decomposer = ProjectDecomposer(temp_dir)

        with pytest.raises(DecomposerError, match="Claude CLI failed"):
            decomposer.generate_spec(zadacha, workspace)

    @patch("maestro.decomposer.subprocess.run")
    def test_generate_spec_fallback_when_no_markers(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test generate_spec writes tasks.md when no file markers found."""
        mock_run.return_value = _make_subprocess_result(
            stdout="Some plain text response without file markers.\n",
        )

        zadacha = ZadachaConfig(
            id="auth-module",
            title="Authentication Module",
            description="Implement user authentication",
            scope=["src/auth/**"],
        )
        workspace = temp_dir / "workspace"
        workspace.mkdir()

        decomposer = ProjectDecomposer(temp_dir)
        decomposer.generate_spec(zadacha, workspace)

        spec_dir = workspace / "spec"
        assert (spec_dir / "tasks.md").exists()
        content = (spec_dir / "tasks.md").read_text()
        assert "Some plain text response" in content

    @patch("maestro.decomposer.subprocess.run")
    def test_generate_spec_creates_spec_directory(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test generate_spec creates spec/ directory if it does not exist."""
        mock_run.return_value = _make_subprocess_result(
            stdout=_make_spec_response(),
        )

        zadacha = ZadachaConfig(
            id="auth-module",
            title="Authentication Module",
            description="Implement user authentication",
            scope=["src/auth/**"],
        )
        workspace = temp_dir / "workspace"
        workspace.mkdir()

        decomposer = ProjectDecomposer(temp_dir)
        decomposer.generate_spec(zadacha, workspace)

        assert (workspace / "spec").is_dir()

    @patch("maestro.decomposer.subprocess.run")
    def test_generate_spec_raises_on_timeout(
        self, mock_run: MagicMock, temp_dir: Path
    ) -> None:
        """Test generate_spec raises DecomposerError on timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["claude"], timeout=600)

        zadacha = ZadachaConfig(
            id="auth-module",
            title="Authentication Module",
            description="Implement user authentication",
            scope=["src/auth/**"],
        )
        workspace = temp_dir / "workspace"
        workspace.mkdir()

        decomposer = ProjectDecomposer(temp_dir)

        with pytest.raises(DecomposerError, match="timed out"):
            decomposer.generate_spec(zadacha, workspace)


# =============================================================================
# Unit Tests: validate_non_overlap()
# =============================================================================


class TestValidateNonOverlap:
    """Tests for the validate_non_overlap static method."""

    def test_non_overlapping_scopes_returns_no_warnings(self) -> None:
        """Test that non-overlapping scopes produce no warnings."""
        zadachi = [
            ZadachaConfig(
                id="frontend",
                title="Frontend",
                description="Build frontend",
                scope=["src/frontend/**"],
            ),
            ZadachaConfig(
                id="backend",
                title="Backend",
                description="Build backend",
                scope=["src/backend/**"],
            ),
            ZadachaConfig(
                id="tests",
                title="Tests",
                description="Write tests",
                scope=["tests/**"],
            ),
        ]

        warnings = ProjectDecomposer.validate_non_overlap(zadachi)

        assert warnings == []

    def test_overlapping_scopes_returns_warnings(self) -> None:
        """Test that overlapping scopes produce correct warnings."""
        zadachi = [
            ZadachaConfig(
                id="auth",
                title="Auth",
                description="Authentication",
                scope=["src/**"],
            ),
            ZadachaConfig(
                id="api",
                title="API",
                description="API endpoints",
                scope=["src/api/**"],
            ),
        ]

        warnings = ProjectDecomposer.validate_non_overlap(zadachi)

        assert len(warnings) == 1
        assert isinstance(warnings[0], ScopeOverlapWarning)
        assert warnings[0].zadacha_a == "auth"
        assert warnings[0].zadacha_b == "api"

    def test_empty_scopes_returns_no_warnings(self) -> None:
        """Test that empty scopes produce no warnings."""
        zadachi = [
            ZadachaConfig(
                id="task-a",
                title="Task A",
                description="First task",
                scope=[],
            ),
            ZadachaConfig(
                id="task-b",
                title="Task B",
                description="Second task",
                scope=[],
            ),
        ]

        warnings = ProjectDecomposer.validate_non_overlap(zadachi)

        assert warnings == []

    def test_single_zadacha_returns_no_warnings(self) -> None:
        """Test that a single zadacha produces no warnings."""
        zadachi = [
            ZadachaConfig(
                id="solo",
                title="Solo",
                description="Only task",
                scope=["src/**"],
            ),
        ]

        warnings = ProjectDecomposer.validate_non_overlap(zadachi)

        assert warnings == []

    def test_empty_list_returns_no_warnings(self) -> None:
        """Test that empty zadachi list produces no warnings."""
        warnings = ProjectDecomposer.validate_non_overlap([])

        assert warnings == []

    def test_multiple_overlapping_pairs(self) -> None:
        """Test detection of multiple overlapping pairs."""
        zadachi = [
            ZadachaConfig(
                id="task-a",
                title="Task A",
                description="First",
                scope=["src/**"],
            ),
            ZadachaConfig(
                id="task-b",
                title="Task B",
                description="Second",
                scope=["src/module/**"],
            ),
            ZadachaConfig(
                id="task-c",
                title="Task C",
                description="Third",
                scope=["src/module/sub/**"],
            ),
        ]

        warnings = ProjectDecomposer.validate_non_overlap(zadachi)

        # All three pairs should overlap: A<->B, A<->C, B<->C
        assert len(warnings) == 3

    def test_scope_overlap_warning_str(self) -> None:
        """Test ScopeOverlapWarning string representation."""
        warning = ScopeOverlapWarning(
            zadacha_a="auth",
            zadacha_b="api",
            overlapping_patterns=["src/** <-> src/api/**"],
        )

        msg = str(warning)

        assert "auth" in msg
        assert "api" in msg
        assert "src/**" in msg

    def test_mixed_overlapping_and_non_overlapping(self) -> None:
        """Test with a mix of overlapping and non-overlapping scopes."""
        zadachi = [
            ZadachaConfig(
                id="frontend",
                title="Frontend",
                description="Frontend code",
                scope=["frontend/**"],
            ),
            ZadachaConfig(
                id="backend",
                title="Backend",
                description="Backend code",
                scope=["backend/**"],
            ),
            ZadachaConfig(
                id="backend-extra",
                title="Backend Extra",
                description="More backend code",
                scope=["backend/**"],
            ),
        ]

        warnings = ProjectDecomposer.validate_non_overlap(zadachi)

        assert len(warnings) == 1
        assert warnings[0].zadacha_a == "backend"
        assert warnings[0].zadacha_b == "backend-extra"


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
