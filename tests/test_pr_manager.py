"""Tests for the PR Manager module."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro.git import GitManager
from maestro.pr_manager import GHNotFoundError, PRManager, PRManagerError


# =============================================================================
# Unit Tests: Initialization
# =============================================================================


class TestPRManagerInit:
    """Tests for PRManager initialization."""

    def test_init_stores_git_manager(self, git_repo: Path) -> None:
        """Test that PRManager stores the provided GitManager."""
        git_manager = GitManager(git_repo)
        pr_manager = PRManager(git_manager)

        assert pr_manager._git is git_manager

    def test_init_creates_logger(self, git_repo: Path) -> None:
        """Test that PRManager creates a logger on initialization."""
        git_manager = GitManager(git_repo)
        pr_manager = PRManager(git_manager)

        assert pr_manager._logger is not None
        assert pr_manager._logger.name == "maestro.pr_manager"


# =============================================================================
# Unit Tests: push_branch
# =============================================================================


class TestPushBranch:
    """Tests for push_branch functionality."""

    def test_push_branch_success(self, git_repo: Path) -> None:
        """Test that push_branch calls git push with set_upstream=True."""
        git_manager = GitManager(git_repo)
        pr_manager = PRManager(git_manager)

        with patch.object(git_manager, "push") as mock_push:
            pr_manager.push_branch("feature/my-branch")

            mock_push.assert_called_once_with("feature/my-branch", set_upstream=True)

    def test_push_branch_failure_raises_pr_manager_error(self, git_repo: Path) -> None:
        """Test that push_branch raises PRManagerError when push fails."""
        from maestro.git import RemoteError

        git_manager = GitManager(git_repo)
        pr_manager = PRManager(git_manager)

        with (
            patch.object(
                git_manager,
                "push",
                side_effect=RemoteError("No remote configured"),
            ),
            pytest.raises(PRManagerError, match="Failed to push branch"),
        ):
            pr_manager.push_branch("feature/my-branch")

    def test_push_branch_failure_chains_original_error(self, git_repo: Path) -> None:
        """Test that PRManagerError chains the original RemoteError."""
        from maestro.git import RemoteError

        git_manager = GitManager(git_repo)
        pr_manager = PRManager(git_manager)

        original_error = RemoteError("connection refused")
        with patch.object(git_manager, "push", side_effect=original_error):
            with pytest.raises(PRManagerError) as exc_info:
                pr_manager.push_branch("feature/my-branch")

            assert exc_info.value.__cause__ is original_error


# =============================================================================
# Unit Tests: create_pr
# =============================================================================


class TestCreatePR:
    """Tests for create_pr functionality."""

    def test_create_pr_success(self, git_repo: Path) -> None:
        """Test successful PR creation parses URL from stdout."""
        git_manager = GitManager(git_repo)
        pr_manager = PRManager(git_manager)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/org/repo/pull/42\n"
        mock_result.stderr = ""

        with (
            patch.object(PRManager, "is_available", return_value=True),
            patch(
                "maestro.pr_manager.subprocess.run", return_value=mock_result
            ) as mock_run,
        ):
            url = pr_manager.create_pr(
                branch="agent/task-001",
                title="feat: implement task",
                body="Implements task-001",
                base_branch="main",
            )

            assert url == "https://github.com/org/repo/pull/42"
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            cmd = call_args[0][0]
            assert cmd == [
                "gh",
                "pr",
                "create",
                "--head",
                "agent/task-001",
                "--base",
                "main",
                "--title",
                "feat: implement task",
                "--body",
                "Implements task-001",
            ]
            assert call_args[1]["cwd"] == git_repo
            assert call_args[1]["capture_output"] is True
            assert call_args[1]["text"] is True
            assert call_args[1]["timeout"] == 60

    def test_create_pr_already_exists_fetches_existing_url(
        self, git_repo: Path
    ) -> None:
        """Test that when PR already exists, the existing URL is fetched."""
        git_manager = GitManager(git_repo)
        pr_manager = PRManager(git_manager)

        # First call to gh pr create fails with "already exists"
        create_result = MagicMock()
        create_result.returncode = 1
        create_result.stdout = ""
        create_result.stderr = (
            "a pull request for branch 'agent/task-001' already exists"
        )

        # Second call to gh pr view succeeds
        view_result = MagicMock()
        view_result.returncode = 0
        view_result.stdout = "https://github.com/org/repo/pull/41\n"
        view_result.stderr = ""

        with (
            patch.object(PRManager, "is_available", return_value=True),
            patch(
                "maestro.pr_manager.subprocess.run",
                side_effect=[create_result, view_result],
            ) as mock_run,
        ):
            url = pr_manager.create_pr(
                branch="agent/task-001",
                title="feat: implement task",
                body="Implements task-001",
            )

            assert url == "https://github.com/org/repo/pull/41"
            assert mock_run.call_count == 2

            # Verify the second call is gh pr view
            second_call_cmd = mock_run.call_args_list[1][0][0]
            assert second_call_cmd == [
                "gh",
                "pr",
                "view",
                "agent/task-001",
                "--json",
                "url",
                "--jq",
                ".url",
            ]

    def test_create_pr_already_exists_view_fails_returns_empty(
        self, git_repo: Path
    ) -> None:
        """Test that when PR exists but view fails, empty string is returned."""
        git_manager = GitManager(git_repo)
        pr_manager = PRManager(git_manager)

        create_result = MagicMock()
        create_result.returncode = 1
        create_result.stdout = ""
        create_result.stderr = "already exists for branch"

        view_result = MagicMock()
        view_result.returncode = 1
        view_result.stdout = ""
        view_result.stderr = "not found"

        with (
            patch.object(PRManager, "is_available", return_value=True),
            patch(
                "maestro.pr_manager.subprocess.run",
                side_effect=[create_result, view_result],
            ),
        ):
            url = pr_manager.create_pr(
                branch="agent/task-001",
                title="feat: implement task",
                body="body",
            )

            assert url == ""

    def test_create_pr_gh_not_found_via_is_available(self, git_repo: Path) -> None:
        """Test that create_pr raises GHNotFoundError when gh is not available."""
        git_manager = GitManager(git_repo)
        pr_manager = PRManager(git_manager)

        with (
            patch.object(PRManager, "is_available", return_value=False),
            pytest.raises(GHNotFoundError, match="gh CLI is not installed"),
        ):
            pr_manager.create_pr(
                branch="agent/task-001",
                title="title",
                body="body",
            )

    def test_create_pr_gh_not_found_via_file_not_found(self, git_repo: Path) -> None:
        """Test that create_pr raises GHNotFoundError on FileNotFoundError."""
        git_manager = GitManager(git_repo)
        pr_manager = PRManager(git_manager)

        with (
            patch.object(PRManager, "is_available", return_value=True),
            patch(
                "maestro.pr_manager.subprocess.run",
                side_effect=FileNotFoundError("gh not found"),
            ),
            pytest.raises(GHNotFoundError, match="gh CLI not found"),
        ):
            pr_manager.create_pr(
                branch="agent/task-001",
                title="title",
                body="body",
            )

    def test_create_pr_generic_failure_raises_error(self, git_repo: Path) -> None:
        """Test that create_pr raises PRManagerError on non-zero exit code."""
        git_manager = GitManager(git_repo)
        pr_manager = PRManager(git_manager)

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "authentication required"

        with (
            patch.object(PRManager, "is_available", return_value=True),
            patch(
                "maestro.pr_manager.subprocess.run",
                return_value=mock_result,
            ),
            pytest.raises(
                PRManagerError,
                match=r"gh pr create failed.*authentication",
            ),
        ):
            pr_manager.create_pr(
                branch="agent/task-001",
                title="title",
                body="body",
            )

    def test_create_pr_timeout_raises_error(self, git_repo: Path) -> None:
        """Test that create_pr raises PRManagerError on timeout."""
        git_manager = GitManager(git_repo)
        pr_manager = PRManager(git_manager)

        with (
            patch.object(PRManager, "is_available", return_value=True),
            patch(
                "maestro.pr_manager.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    cmd=["gh", "pr", "create"], timeout=60
                ),
            ),
            pytest.raises(PRManagerError, match="gh pr create timed out"),
        ):
            pr_manager.create_pr(
                branch="agent/task-001",
                title="title",
                body="body",
            )

    def test_create_pr_uses_default_base_branch(self, git_repo: Path) -> None:
        """Test that create_pr uses 'main' as default base branch."""
        git_manager = GitManager(git_repo)
        pr_manager = PRManager(git_manager)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/org/repo/pull/1\n"
        mock_result.stderr = ""

        with (
            patch.object(PRManager, "is_available", return_value=True),
            patch(
                "maestro.pr_manager.subprocess.run",
                return_value=mock_result,
            ) as mock_run,
        ):
            pr_manager.create_pr(
                branch="agent/task-001",
                title="title",
                body="body",
            )

            cmd = mock_run.call_args[0][0]
            base_idx = cmd.index("--base")
            assert cmd[base_idx + 1] == "main"


# =============================================================================
# Unit Tests: push_and_create_pr
# =============================================================================


class TestPushAndCreatePR:
    """Tests for push_and_create_pr functionality."""

    def test_push_and_create_pr_calls_push_then_create(self, git_repo: Path) -> None:
        """Test that push_and_create_pr calls push_branch then create_pr."""
        git_manager = GitManager(git_repo)
        pr_manager = PRManager(git_manager)

        call_order: list[str] = []

        def mock_push(branch: str) -> None:
            call_order.append("push")

        def mock_create(
            branch: str,
            title: str,
            body: str,
            base_branch: str = "main",
        ) -> str:
            call_order.append("create")
            return "https://github.com/org/repo/pull/99"

        with (
            patch.object(pr_manager, "push_branch", side_effect=mock_push),
            patch.object(pr_manager, "create_pr", side_effect=mock_create),
        ):
            url = pr_manager.push_and_create_pr(
                branch="agent/task-001",
                title="feat: task",
                body="body",
                base_branch="develop",
            )

            assert url == "https://github.com/org/repo/pull/99"
            assert call_order == ["push", "create"]

    def test_push_and_create_pr_propagates_push_error(self, git_repo: Path) -> None:
        """Test that push failure in push_and_create_pr propagates."""
        git_manager = GitManager(git_repo)
        pr_manager = PRManager(git_manager)

        with (
            patch.object(
                pr_manager,
                "push_branch",
                side_effect=PRManagerError("push failed"),
            ),
            pytest.raises(PRManagerError, match="push failed"),
        ):
            pr_manager.push_and_create_pr(
                branch="agent/task-001",
                title="feat: task",
                body="body",
            )

    def test_push_and_create_pr_does_not_create_pr_on_push_failure(
        self, git_repo: Path
    ) -> None:
        """Test that create_pr is not called when push_branch fails."""
        git_manager = GitManager(git_repo)
        pr_manager = PRManager(git_manager)

        with (
            patch.object(
                pr_manager,
                "push_branch",
                side_effect=PRManagerError("push failed"),
            ),
            patch.object(pr_manager, "create_pr") as mock_create,
        ):
            with pytest.raises(PRManagerError):
                pr_manager.push_and_create_pr(
                    branch="agent/task-001",
                    title="feat: task",
                    body="body",
                )

            mock_create.assert_not_called()


# =============================================================================
# Unit Tests: is_available
# =============================================================================


class TestIsAvailable:
    """Tests for is_available static method."""

    def test_is_available_returns_true_when_gh_found(self) -> None:
        """Test that is_available returns True when gh CLI is in PATH."""
        with patch(
            "maestro.pr_manager.shutil.which",
            return_value="/usr/local/bin/gh",
        ):
            assert PRManager.is_available() is True

    def test_is_available_returns_false_when_gh_not_found(self) -> None:
        """Test that is_available returns False when gh CLI is not in PATH."""
        with patch("maestro.pr_manager.shutil.which", return_value=None):
            assert PRManager.is_available() is False
