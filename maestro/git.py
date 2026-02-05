"""Git management operations for task isolation.

This module provides GitManager for handling git branch creation,
rebasing, and pushing operations to ensure task execution isolation.
"""

import contextlib
import shutil
import subprocess
from pathlib import Path


class GitError(Exception):
    """Base exception for git operations."""


class GitNotFoundError(GitError):
    """Raised when git executable is not found."""


class BranchExistsError(GitError):
    """Raised when attempting to create a branch that already exists."""


class BranchNotFoundError(GitError):
    """Raised when attempting to checkout a non-existent branch."""


class RemoteError(GitError):
    """Raised when remote operations fail."""


class RebaseConflictError(GitError):
    """Raised when rebase encounters conflicts."""


class NotARepositoryError(GitError):
    """Raised when the path is not a git repository."""


class GitManager:
    """Manages git operations for task branch isolation.

    Handles branch creation, checkout, rebase, and push operations
    to ensure tasks work in isolated branches.
    """

    def __init__(
        self,
        repo_path: Path,
        base_branch: str = "main",
        branch_prefix: str = "agent/",
    ) -> None:
        """Initialize GitManager with repository path and configuration.

        Args:
            repo_path: Path to the git repository.
            base_branch: Base branch name for rebasing (default: "main").
            branch_prefix: Prefix for task branches (default: "agent/").

        Raises:
            GitNotFoundError: If git executable is not available.
            NotARepositoryError: If repo_path is not a git repository.
        """
        self._repo_path = repo_path
        self._base_branch = base_branch
        self._branch_prefix = branch_prefix

        # Verify git is available
        if shutil.which("git") is None:
            msg = "git executable not found in PATH"
            raise GitNotFoundError(msg)

        # Verify repo_path is a git repository
        if not self._is_git_repository():
            msg = f"'{repo_path}' is not a git repository"
            raise NotARepositoryError(msg)

    @property
    def repo_path(self) -> Path:
        """Return the repository path."""
        return self._repo_path

    @property
    def base_branch(self) -> str:
        """Return the base branch name."""
        return self._base_branch

    @property
    def branch_prefix(self) -> str:
        """Return the branch prefix."""
        return self._branch_prefix

    def _is_git_repository(self) -> bool:
        """Check if repo_path is a valid git repository.

        Returns:
            True if path is a git repository, False otherwise.
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=self._repo_path,
                check=True,
                capture_output=True,
            )
            return result.returncode == 0
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def _run_git(
        self, args: list[str], check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        """Execute a git command.

        Args:
            args: Git command arguments (without 'git' prefix).
            check: Whether to raise on non-zero exit code.

        Returns:
            CompletedProcess instance with command result.

        Raises:
            GitError: If command fails and check=True.
        """
        cmd = ["git", *args]
        try:
            return subprocess.run(
                cmd,
                cwd=self._repo_path,
                check=check,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode("utf-8", errors="replace") if e.stderr else ""
            msg = f"Git command failed: {' '.join(cmd)}\n{stderr}"
            raise GitError(msg) from e

    def _build_branch_name(self, task_id: str) -> str:
        """Build the full branch name for a task.

        Args:
            task_id: Task identifier.

        Returns:
            Full branch name with prefix (e.g., "agent/task-001").
        """
        return f"{self._branch_prefix}{task_id}"

    def get_current_branch(self) -> str:
        """Get the name of the current branch.

        Returns:
            Current branch name.

        Raises:
            GitError: If git command fails.
        """
        result = self._run_git(["branch", "--show-current"])
        return result.stdout.decode("utf-8").strip()

    def branch_exists(self, branch: str) -> bool:
        """Check if a branch exists locally.

        Args:
            branch: Branch name to check.

        Returns:
            True if branch exists, False otherwise.
        """
        result = self._run_git(
            ["rev-parse", "--verify", f"refs/heads/{branch}"],
            check=False,
        )
        return result.returncode == 0

    def create_task_branch(self, task_id: str) -> str:
        """Create and checkout a task-specific branch.

        Creates a new branch from the current HEAD with the naming
        pattern: {branch_prefix}{task_id} (e.g., "agent/task-001").

        Args:
            task_id: Unique task identifier.

        Returns:
            Branch name created (e.g., "agent/task-001").

        Raises:
            BranchExistsError: If branch already exists.
            GitError: If git command fails.
        """
        branch = self._build_branch_name(task_id)

        if self.branch_exists(branch):
            msg = f"Branch '{branch}' already exists"
            raise BranchExistsError(msg)

        self._run_git(["checkout", "-b", branch])
        return branch

    def checkout(self, branch: str) -> None:
        """Checkout an existing branch.

        Args:
            branch: Branch name to checkout.

        Raises:
            BranchNotFoundError: If branch does not exist.
            GitError: If git command fails.
        """
        if not self.branch_exists(branch):
            msg = f"Branch '{branch}' does not exist"
            raise BranchNotFoundError(msg)

        self._run_git(["checkout", branch])

    def rebase_on_base(self) -> None:
        """Rebase current branch on the base branch.

        Fetches the latest changes from origin and rebases the current
        branch on top of the base branch.

        Raises:
            RebaseConflictError: If rebase encounters conflicts.
            GitError: If git command fails.
        """
        # Fetch latest from origin (if available)
        # Remote might not be configured, continue with local rebase
        with contextlib.suppress(GitError):
            self._run_git(["fetch", "origin", self._base_branch])

        # Check if origin/base_branch exists
        origin_ref = f"origin/{self._base_branch}"
        origin_exists = self._run_git(
            ["rev-parse", "--verify", origin_ref], check=False
        )

        # Rebase on origin if available, otherwise local base branch
        if origin_exists.returncode == 0:
            rebase_target = origin_ref
        else:
            rebase_target = self._base_branch

        result = self._run_git(["rebase", rebase_target], check=False)

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            # Abort the failed rebase to restore clean state
            self._run_git(["rebase", "--abort"], check=False)
            if "CONFLICT" in stderr or "conflict" in stderr.lower():
                msg = f"Rebase conflicts encountered:\n{stderr}"
                raise RebaseConflictError(msg)
            msg = f"Rebase failed:\n{stderr}"
            raise GitError(msg)

    def push(self, branch: str | None = None, set_upstream: bool = True) -> None:
        """Push branch to origin.

        Args:
            branch: Branch name to push (default: current branch).
            set_upstream: Whether to set upstream tracking (default: True).

        Raises:
            RemoteError: If remote is not configured or push fails.
            GitError: If git command fails.
        """
        target_branch = branch or self.get_current_branch()

        # Check if remote exists
        result = self._run_git(["remote"], check=False)
        remotes = result.stdout.decode("utf-8").strip().split("\n")
        if not remotes or remotes == [""]:
            msg = "No remote configured"
            raise RemoteError(msg)

        # Build push command
        push_args = ["push"]
        if set_upstream:
            push_args.extend(["-u", "origin", target_branch])
        else:
            push_args.extend(["origin", target_branch])

        try:
            self._run_git(push_args)
        except GitError as e:
            msg = f"Failed to push branch '{target_branch}': {e}"
            raise RemoteError(msg) from e

    def has_uncommitted_changes(self) -> bool:
        """Check if there are uncommitted changes in the working directory.

        Returns:
            True if there are uncommitted changes, False otherwise.
        """
        result = self._run_git(["status", "--porcelain"])
        return bool(result.stdout.decode("utf-8").strip())

    def get_branch_list(self) -> list[str]:
        """Get list of all local branches.

        Returns:
            List of branch names.
        """
        result = self._run_git(["branch", "--format=%(refname:short)"])
        branches = result.stdout.decode("utf-8").strip().split("\n")
        return [b for b in branches if b]
