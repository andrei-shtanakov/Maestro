"""GitHub PR management for multi-process orchestration.

This module provides PRManager for creating pull requests
via the GitHub CLI (gh) after zadachi complete.
"""

import logging
import shutil
import subprocess

from maestro.git import GitManager, RemoteError


class PRManagerError(Exception):
    """Base exception for PR operations."""


class GHNotFoundError(PRManagerError):
    """Raised when gh CLI is not available."""


class PRManager:
    """Manages GitHub pull request creation via gh CLI.

    Creates PRs from zadacha branches to the base branch
    after all subtasks in a zadacha are complete.
    """

    def __init__(
        self,
        git_manager: GitManager,
    ) -> None:
        """Initialize PR manager.

        Args:
            git_manager: GitManager for the main repository.
        """
        self._git = git_manager
        self._logger = logging.getLogger(__name__)

    @staticmethod
    def is_available() -> bool:
        """Check if gh CLI is available."""
        return shutil.which("gh") is not None

    def push_branch(self, branch: str) -> None:
        """Push a branch to the remote.

        Args:
            branch: Branch name to push.

        Raises:
            PRManagerError: If push fails.
        """
        try:
            self._git.push(branch, set_upstream=True)
        except RemoteError as e:
            msg = f"Failed to push branch '{branch}': {e}"
            raise PRManagerError(msg) from e

    def create_pr(
        self,
        branch: str,
        title: str,
        body: str,
        base_branch: str = "main",
    ) -> str:
        """Create a GitHub PR using gh CLI.

        Args:
            branch: Head branch for the PR.
            title: PR title.
            body: PR body/description.
            base_branch: Base branch to merge into.

        Returns:
            URL of the created PR.

        Raises:
            GHNotFoundError: If gh CLI not found.
            PRManagerError: If PR creation fails.
        """
        if not self.is_available():
            msg = "gh CLI is not installed"
            raise GHNotFoundError(msg)

        cmd = [
            "gh",
            "pr",
            "create",
            "--head",
            branch,
            "--base",
            base_branch,
            "--title",
            title,
            "--body",
            body,
        ]

        try:
            result = subprocess.run(
                cmd,
                cwd=self._git.repo_path,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            msg = "gh pr create timed out"
            raise PRManagerError(msg) from e
        except FileNotFoundError as e:
            msg = "gh CLI not found"
            raise GHNotFoundError(msg) from e

        if result.returncode != 0:
            stderr = result.stderr.strip()
            # Check for "already exists" — not an error
            if "already exists" in stderr.lower():
                self._logger.info(
                    "PR already exists for branch '%s'",
                    branch,
                )
                # Try to get existing PR URL
                return self._get_existing_pr_url(branch)
            msg = f"gh pr create failed (code {result.returncode}): {stderr}"
            raise PRManagerError(msg)

        pr_url = result.stdout.strip()
        self._logger.info("Created PR: %s", pr_url)
        return pr_url

    def _get_existing_pr_url(self, branch: str) -> str:
        """Get URL of an existing PR for a branch.

        Args:
            branch: Branch name to look up.

        Returns:
            PR URL or empty string if not found.
        """
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    branch,
                    "--json",
                    "url",
                    "--jq",
                    ".url",
                ],
                cwd=self._git.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return ""

    def push_and_create_pr(
        self,
        branch: str,
        title: str,
        body: str,
        base_branch: str = "main",
    ) -> str:
        """Push branch and create PR in one step.

        Args:
            branch: Branch to push and create PR from.
            title: PR title.
            body: PR body/description.
            base_branch: Base branch to merge into.

        Returns:
            URL of the created PR.

        Raises:
            PRManagerError: If push or PR creation fails.
        """
        self.push_branch(branch)
        return self.create_pr(branch, title, body, base_branch)
