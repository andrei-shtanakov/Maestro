"""Tests for the Git Manager module."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro.git import (
    BranchExistsError,
    BranchNotFoundError,
    GitError,
    GitManager,
    GitNotFoundError,
    NotARepositoryError,
    RebaseConflictError,
    RemoteError,
)


# =============================================================================
# Unit Tests: Initialization
# =============================================================================


class TestGitManagerInit:
    """Tests for GitManager initialization."""

    def test_init_with_valid_repo(self, git_repo: Path) -> None:
        """Test initialization with valid git repository."""
        manager = GitManager(git_repo)

        assert manager.repo_path == git_repo
        assert manager.base_branch == "main"
        assert manager.branch_prefix == "agent/"

    def test_init_with_custom_base_branch(self, git_repo: Path) -> None:
        """Test initialization with custom base branch."""
        manager = GitManager(git_repo, base_branch="develop")

        assert manager.base_branch == "develop"

    def test_init_with_custom_branch_prefix(self, git_repo: Path) -> None:
        """Test initialization with custom branch prefix."""
        manager = GitManager(git_repo, branch_prefix="task/")

        assert manager.branch_prefix == "task/"

    def test_init_raises_when_git_not_found(self, git_repo: Path) -> None:
        """Test that init raises GitNotFoundError when git is not in PATH."""
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(GitNotFoundError, match="git executable not found"),
        ):
            GitManager(git_repo)

    def test_init_raises_when_not_a_repository(self, temp_dir: Path) -> None:
        """Test that init raises NotARepositoryError for non-repo path."""
        with pytest.raises(NotARepositoryError, match="is not a git repository"):
            GitManager(temp_dir)


# =============================================================================
# Unit Tests: Branch Name Building
# =============================================================================


class TestBranchNameBuilding:
    """Tests for branch name building functionality."""

    def test_build_branch_name_with_default_prefix(self, git_repo: Path) -> None:
        """Test branch name building with default prefix."""
        manager = GitManager(git_repo)
        branch = manager._build_branch_name("task-001")

        assert branch == "agent/task-001"

    def test_build_branch_name_with_custom_prefix(self, git_repo: Path) -> None:
        """Test branch name building with custom prefix."""
        manager = GitManager(git_repo, branch_prefix="feature/")
        branch = manager._build_branch_name("new-feature")

        assert branch == "feature/new-feature"

    def test_build_branch_name_with_empty_prefix(self, git_repo: Path) -> None:
        """Test branch name building with empty prefix."""
        manager = GitManager(git_repo, branch_prefix="")
        branch = manager._build_branch_name("task-001")

        assert branch == "task-001"


# =============================================================================
# Unit Tests: Get Current Branch
# =============================================================================


class TestGetCurrentBranch:
    """Tests for get_current_branch functionality."""

    def test_get_current_branch_returns_branch_name(self, git_repo: Path) -> None:
        """Test that get_current_branch returns the current branch name."""
        manager = GitManager(git_repo)
        branch = manager.get_current_branch()

        # After git init, default branch is usually 'main' or 'master'
        assert branch in ("main", "master")

    def test_get_current_branch_after_checkout(self, git_repo: Path) -> None:
        """Test get_current_branch after checking out a new branch."""
        manager = GitManager(git_repo)

        # Create and checkout new branch
        manager.create_task_branch("test-task")
        branch = manager.get_current_branch()

        assert branch == "agent/test-task"


# =============================================================================
# Unit Tests: Branch Exists
# =============================================================================


class TestBranchExists:
    """Tests for branch_exists functionality."""

    def test_branch_exists_returns_true_for_existing(self, git_repo: Path) -> None:
        """Test branch_exists returns True for existing branch."""
        manager = GitManager(git_repo)

        # Default branch should exist
        current = manager.get_current_branch()
        assert manager.branch_exists(current) is True

    def test_branch_exists_returns_false_for_nonexistent(self, git_repo: Path) -> None:
        """Test branch_exists returns False for non-existent branch."""
        manager = GitManager(git_repo)

        assert manager.branch_exists("nonexistent-branch") is False


# =============================================================================
# Integration Tests: Branch Creation
# =============================================================================


class TestCreateTaskBranch:
    """Tests for create_task_branch functionality."""

    @pytest.mark.integration
    def test_create_task_branch_creates_new_branch(self, git_repo: Path) -> None:
        """Test that create_task_branch creates and checks out new branch."""
        manager = GitManager(git_repo)
        branch = manager.create_task_branch("task-001")

        assert branch == "agent/task-001"
        assert manager.get_current_branch() == "agent/task-001"
        assert manager.branch_exists("agent/task-001")

    @pytest.mark.integration
    def test_create_task_branch_with_custom_prefix(self, git_repo: Path) -> None:
        """Test branch creation with custom prefix."""
        manager = GitManager(git_repo, branch_prefix="feature/")
        branch = manager.create_task_branch("my-feature")

        assert branch == "feature/my-feature"
        assert manager.get_current_branch() == "feature/my-feature"

    @pytest.mark.integration
    def test_create_task_branch_raises_when_exists(self, git_repo: Path) -> None:
        """Test that creating existing branch raises BranchExistsError."""
        manager = GitManager(git_repo)

        # Create branch first time
        manager.create_task_branch("task-001")

        # Go back to main
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Try to create same branch again
        with pytest.raises(BranchExistsError, match="already exists"):
            manager.create_task_branch("task-001")

    @pytest.mark.integration
    def test_create_multiple_task_branches(self, git_repo: Path) -> None:
        """Test creating multiple task branches."""
        manager = GitManager(git_repo)

        # Create first branch
        branch1 = manager.create_task_branch("task-001")
        assert branch1 == "agent/task-001"

        # Go back to main
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Create second branch
        branch2 = manager.create_task_branch("task-002")
        assert branch2 == "agent/task-002"

        # Verify both exist
        assert manager.branch_exists("agent/task-001")
        assert manager.branch_exists("agent/task-002")


# =============================================================================
# Integration Tests: Checkout
# =============================================================================


class TestCheckout:
    """Tests for checkout functionality."""

    @pytest.mark.integration
    def test_checkout_existing_branch(self, git_repo: Path) -> None:
        """Test checking out an existing branch."""
        manager = GitManager(git_repo)

        # Create a new branch
        manager.create_task_branch("task-001")

        # Go back to main using git directly
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Checkout the task branch using manager
        manager.checkout("agent/task-001")
        assert manager.get_current_branch() == "agent/task-001"

    @pytest.mark.integration
    def test_checkout_nonexistent_branch_raises(self, git_repo: Path) -> None:
        """Test that checking out non-existent branch raises error."""
        manager = GitManager(git_repo)

        with pytest.raises(BranchNotFoundError, match="does not exist"):
            manager.checkout("nonexistent-branch")


# =============================================================================
# Integration Tests: Rebase
# =============================================================================


class TestRebaseOnBase:
    """Tests for rebase_on_base functionality."""

    @pytest.mark.integration
    def test_rebase_on_base_with_no_changes(self, git_repo: Path) -> None:
        """Test rebase when there are no new commits on base."""
        manager = GitManager(git_repo)

        # Create task branch
        manager.create_task_branch("task-001")

        # Rebase should succeed with no changes
        manager.rebase_on_base()

        # Should still be on task branch
        assert manager.get_current_branch() == "agent/task-001"

    @pytest.mark.integration
    def test_rebase_on_base_with_new_base_commits(self, git_repo: Path) -> None:
        """Test rebase when base branch has new commits."""
        manager = GitManager(git_repo)

        # Create task branch
        manager.create_task_branch("task-001")

        # Add commit on task branch
        (git_repo / "task_file.txt").write_text("Task content")
        subprocess.run(
            ["git", "add", "."], cwd=git_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Task commit"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Switch to main and add a commit there
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        (git_repo / "main_file.txt").write_text("Main content")
        subprocess.run(
            ["git", "add", "."], cwd=git_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Main commit"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Switch back to task branch
        subprocess.run(
            ["git", "checkout", "agent/task-001"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Rebase should succeed
        manager.rebase_on_base()

        # Verify both files exist (rebased)
        assert (git_repo / "task_file.txt").exists()
        assert (git_repo / "main_file.txt").exists()

    @pytest.mark.integration
    def test_rebase_conflict_raises_error(self, git_repo: Path) -> None:
        """Test that rebase conflict raises RebaseConflictError."""
        manager = GitManager(git_repo)

        # Create task branch
        manager.create_task_branch("task-001")

        # Modify README on task branch
        (git_repo / "README.md").write_text("Task content\n")
        subprocess.run(
            ["git", "add", "."], cwd=git_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Task commit"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Switch to main and modify same file
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        (git_repo / "README.md").write_text("Main content\n")
        subprocess.run(
            ["git", "add", "."], cwd=git_repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Main commit"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Switch back to task branch
        subprocess.run(
            ["git", "checkout", "agent/task-001"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Rebase should fail with conflict
        with pytest.raises(RebaseConflictError):
            manager.rebase_on_base()

        # State should be clean (rebase aborted)
        assert manager.get_current_branch() == "agent/task-001"


# =============================================================================
# Integration Tests: Push
# =============================================================================


class TestPush:
    """Tests for push functionality."""

    @pytest.mark.integration
    def test_push_raises_when_no_remote(self, git_repo: Path) -> None:
        """Test that push raises RemoteError when no remote configured."""
        manager = GitManager(git_repo)

        # Create task branch
        manager.create_task_branch("task-001")

        # Push should fail - no remote
        with pytest.raises(RemoteError, match="No remote configured"):
            manager.push()

    @pytest.mark.integration
    def test_push_with_mock_remote(self, temp_dir: Path) -> None:
        """Test push to a mock remote repository."""
        # Create a bare repository to act as remote
        remote_path = temp_dir / "remote.git"
        remote_path.mkdir()
        subprocess.run(
            ["git", "init", "--bare"],
            cwd=remote_path,
            check=True,
            capture_output=True,
        )

        # Create local repository
        local_path = temp_dir / "local"
        local_path.mkdir()
        subprocess.run(
            ["git", "init"],
            cwd=local_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=local_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=local_path,
            check=True,
            capture_output=True,
        )

        # Create initial commit
        readme = local_path / "README.md"
        readme.write_text("# Test Repository\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=local_path,
            check=True,
            capture_output=True,
        )

        # Add remote
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote_path)],
            cwd=local_path,
            check=True,
            capture_output=True,
        )

        # Push main first to set up tracking
        subprocess.run(
            ["git", "push", "-u", "origin", "main"],
            cwd=local_path,
            check=True,
            capture_output=True,
        )

        # Now use GitManager
        manager = GitManager(local_path)

        # Create task branch with a commit
        manager.create_task_branch("task-001")
        (local_path / "task_file.txt").write_text("Task content")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Task commit"],
            cwd=local_path,
            check=True,
            capture_output=True,
        )

        # Push should succeed
        manager.push()

        # Verify branch exists in remote
        result = subprocess.run(
            ["git", "branch", "-r"],
            cwd=local_path,
            check=True,
            capture_output=True,
        )
        remote_branches = result.stdout.decode("utf-8")
        assert "origin/agent/task-001" in remote_branches


# =============================================================================
# Unit Tests: Uncommitted Changes
# =============================================================================


class TestUncommittedChanges:
    """Tests for has_uncommitted_changes functionality."""

    @pytest.mark.integration
    def test_has_uncommitted_changes_false_when_clean(self, git_repo: Path) -> None:
        """Test returns False when working directory is clean."""
        manager = GitManager(git_repo)

        assert manager.has_uncommitted_changes() is False

    @pytest.mark.integration
    def test_has_uncommitted_changes_true_with_untracked(self, git_repo: Path) -> None:
        """Test returns True when there are untracked files."""
        manager = GitManager(git_repo)

        # Create untracked file
        (git_repo / "untracked.txt").write_text("content")

        assert manager.has_uncommitted_changes() is True

    @pytest.mark.integration
    def test_has_uncommitted_changes_true_with_staged(self, git_repo: Path) -> None:
        """Test returns True when there are staged changes."""
        manager = GitManager(git_repo)

        # Create and stage file
        (git_repo / "staged.txt").write_text("content")
        subprocess.run(
            ["git", "add", "staged.txt"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        assert manager.has_uncommitted_changes() is True


# =============================================================================
# Unit Tests: Branch List
# =============================================================================


class TestGetBranchList:
    """Tests for get_branch_list functionality."""

    @pytest.mark.integration
    def test_get_branch_list_returns_branches(self, git_repo: Path) -> None:
        """Test that get_branch_list returns all local branches."""
        manager = GitManager(git_repo)

        # Create some branches
        manager.create_task_branch("task-001")
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        manager.create_task_branch("task-002")

        branches = manager.get_branch_list()

        # Should contain main and both task branches
        assert "main" in branches
        assert "agent/task-001" in branches
        assert "agent/task-002" in branches


# =============================================================================
# Unit Tests: Command Building (Mocked)
# =============================================================================


class TestCommandBuilding:
    """Tests for internal command building with mocked subprocess."""

    def test_run_git_builds_correct_command(self, git_repo: Path) -> None:
        """Test that _run_git builds command correctly."""
        manager = GitManager(git_repo)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=b"main",
                stderr=b"",
            )

            manager._run_git(["branch", "--show-current"])

            mock_run.assert_called_once()
            call_args = mock_run.call_args
            cmd = call_args[0][0]

            assert cmd == ["git", "branch", "--show-current"]
            assert call_args[1]["cwd"] == git_repo
            assert call_args[1]["check"] is True
            assert call_args[1]["capture_output"] is True

    def test_run_git_raises_on_error(self, git_repo: Path) -> None:
        """Test that _run_git raises GitError on subprocess error."""
        manager = GitManager(git_repo)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1,
                ["git", "invalid-cmd"],
                stderr=b"error: invalid command",
            )

            with pytest.raises(GitError, match="Git command failed"):
                manager._run_git(["invalid-cmd"])

    def test_run_git_with_check_false(self, git_repo: Path) -> None:
        """Test _run_git with check=False returns result even on error."""
        manager = GitManager(git_repo)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout=b"",
                stderr=b"error",
            )

            result = manager._run_git(["some-cmd"], check=False)

            assert result.returncode == 1


# =============================================================================
# Unit Tests: Error Handling
# =============================================================================


class TestErrorHandling:
    """Tests for error handling scenarios."""

    def test_git_error_includes_stderr(self, git_repo: Path) -> None:
        """Test that GitError includes stderr in message."""
        manager = GitManager(git_repo)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                128,
                ["git", "checkout", "nonexistent"],
                stderr=b"error: pathspec 'nonexistent' did not match",
            )

            with pytest.raises(GitError) as exc_info:
                manager._run_git(["checkout", "nonexistent"])

            assert "pathspec" in str(exc_info.value)

    def test_branch_exists_error_message(self, git_repo: Path) -> None:
        """Test BranchExistsError has descriptive message."""
        manager = GitManager(git_repo)

        # Create branch
        manager.create_task_branch("task-001")

        # Go back to main
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Try to create again
        with pytest.raises(BranchExistsError) as exc_info:
            manager.create_task_branch("task-001")

        assert "agent/task-001" in str(exc_info.value)
        assert "already exists" in str(exc_info.value)

    def test_branch_not_found_error_message(self, git_repo: Path) -> None:
        """Test BranchNotFoundError has descriptive message."""
        manager = GitManager(git_repo)

        with pytest.raises(BranchNotFoundError) as exc_info:
            manager.checkout("nonexistent-branch")

        assert "nonexistent-branch" in str(exc_info.value)
        assert "does not exist" in str(exc_info.value)
