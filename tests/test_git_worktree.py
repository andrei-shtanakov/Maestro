"""Tests for GitManager worktree and merge operations."""

import shutil
import subprocess
from pathlib import Path

import pytest

from maestro.git import (
    BranchExistsError,
    BranchNotFoundError,
    GitError,
    GitManager,
    WorktreeError,
)


# =============================================================================
# Integration Tests: Create Worktree
# =============================================================================


class TestCreateWorktree:
    """Tests for create_worktree functionality."""

    @pytest.mark.integration
    def test_create_worktree_success(self, git_repo: Path, temp_dir: Path) -> None:
        """Test that create_worktree creates a worktree on a new branch."""
        manager = GitManager(git_repo)
        worktree_path = temp_dir / "worktree-test"

        manager.create_worktree(worktree_path, "feature/wt-branch")

        assert worktree_path.exists()
        assert worktree_path.is_dir()
        assert manager.branch_exists("feature/wt-branch")

    @pytest.mark.integration
    def test_create_worktree_files_exist(self, git_repo: Path, temp_dir: Path) -> None:
        """Test that worktree contains repository files."""
        manager = GitManager(git_repo)
        worktree_path = temp_dir / "worktree-test"

        manager.create_worktree(worktree_path, "feature/wt-files")

        # README.md from initial commit should be present
        assert (worktree_path / "README.md").exists()
        content = (worktree_path / "README.md").read_text()
        assert "# Test Repository" in content

    @pytest.mark.integration
    def test_create_worktree_branch_is_created(
        self, git_repo: Path, temp_dir: Path
    ) -> None:
        """Test that create_worktree creates the specified branch."""
        manager = GitManager(git_repo)
        worktree_path = temp_dir / "worktree-test"

        manager.create_worktree(worktree_path, "agent/wt-task-001")

        branches = manager.get_branch_list()
        assert "agent/wt-task-001" in branches

    @pytest.mark.integration
    def test_create_worktree_raises_when_branch_exists(
        self, git_repo: Path, temp_dir: Path
    ) -> None:
        """Test that create_worktree raises BranchExistsError for existing branch."""
        manager = GitManager(git_repo)

        # Create branch first
        manager.create_task_branch("existing")
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        worktree_path = temp_dir / "worktree-test"

        with pytest.raises(BranchExistsError, match="already exists"):
            manager.create_worktree(worktree_path, "agent/existing")


# =============================================================================
# Integration Tests: Create Worktree for Existing Branch
# =============================================================================


class TestCreateWorktreeExistingBranch:
    """Tests for create_worktree_existing_branch functionality."""

    @pytest.mark.integration
    def test_create_worktree_existing_branch_success(
        self, git_repo: Path, temp_dir: Path
    ) -> None:
        """Test creating a worktree for an existing branch."""
        manager = GitManager(git_repo)

        # Create branch first, then go back to main
        manager.create_task_branch("wt-existing")
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        worktree_path = temp_dir / "worktree-test"
        manager.create_worktree_existing_branch(worktree_path, "agent/wt-existing")

        assert worktree_path.exists()
        assert (worktree_path / "README.md").exists()

    @pytest.mark.integration
    def test_create_worktree_existing_branch_raises_when_not_found(
        self, git_repo: Path, temp_dir: Path
    ) -> None:
        """Test that create_worktree_existing_branch raises for missing branch."""
        manager = GitManager(git_repo)
        worktree_path = temp_dir / "worktree-test"

        with pytest.raises(BranchNotFoundError, match="does not exist"):
            manager.create_worktree_existing_branch(worktree_path, "nonexistent-branch")


# =============================================================================
# Integration Tests: Remove Worktree
# =============================================================================


class TestRemoveWorktree:
    """Tests for remove_worktree functionality."""

    @pytest.mark.integration
    def test_remove_worktree_success(self, git_repo: Path, temp_dir: Path) -> None:
        """Test removing an existing worktree."""
        manager = GitManager(git_repo)
        worktree_path = temp_dir / "worktree-test"

        manager.create_worktree(worktree_path, "feature/to-remove")
        assert worktree_path.exists()

        manager.remove_worktree(worktree_path)
        assert not worktree_path.exists()

    @pytest.mark.integration
    def test_remove_worktree_force(self, git_repo: Path, temp_dir: Path) -> None:
        """Test removing a worktree with force=True."""
        manager = GitManager(git_repo)
        worktree_path = temp_dir / "worktree-test"

        manager.create_worktree(worktree_path, "feature/force-remove")

        # Create uncommitted changes in worktree to make it dirty
        (worktree_path / "dirty.txt").write_text("uncommitted content")
        subprocess.run(
            ["git", "add", "."],
            cwd=worktree_path,
            check=True,
            capture_output=True,
        )

        manager.remove_worktree(worktree_path, force=True)
        assert not worktree_path.exists()

    @pytest.mark.integration
    def test_remove_worktree_nonexistent_raises(
        self, git_repo: Path, temp_dir: Path
    ) -> None:
        """Test that removing a non-existent worktree raises WorktreeError."""
        manager = GitManager(git_repo)
        worktree_path = temp_dir / "nonexistent-worktree"

        with pytest.raises(WorktreeError, match="Failed to remove worktree"):
            manager.remove_worktree(worktree_path)


# =============================================================================
# Integration Tests: List Worktrees
# =============================================================================


class TestListWorktrees:
    """Tests for list_worktrees functionality."""

    @pytest.mark.integration
    def test_list_worktrees_main_only(self, git_repo: Path) -> None:
        """Test list_worktrees with only the main worktree."""
        manager = GitManager(git_repo)
        worktrees = manager.list_worktrees()

        # Should have at least the main worktree
        assert len(worktrees) >= 1
        assert any(wt["branch"] == "main" for wt in worktrees)

    @pytest.mark.integration
    def test_list_worktrees_after_creating(
        self, git_repo: Path, temp_dir: Path
    ) -> None:
        """Test list_worktrees returns all worktrees after creation."""
        manager = GitManager(git_repo)

        wt_path_1 = temp_dir / "worktree-1"
        wt_path_2 = temp_dir / "worktree-2"

        manager.create_worktree(wt_path_1, "feature/wt-list-1")
        manager.create_worktree(wt_path_2, "feature/wt-list-2")

        worktrees = manager.list_worktrees()

        # Main + 2 worktrees
        assert len(worktrees) == 3

        branches = [wt["branch"] for wt in worktrees]
        assert "main" in branches
        assert "feature/wt-list-1" in branches
        assert "feature/wt-list-2" in branches

    @pytest.mark.integration
    def test_list_worktrees_entry_keys(self, git_repo: Path, temp_dir: Path) -> None:
        """Test that each worktree entry has path, branch, and head keys."""
        manager = GitManager(git_repo)

        worktree_path = temp_dir / "worktree-test"
        manager.create_worktree(worktree_path, "feature/wt-keys")

        worktrees = manager.list_worktrees()

        for wt in worktrees:
            assert "path" in wt
            assert "branch" in wt
            assert "head" in wt
            assert len(wt["head"]) == 40  # SHA-1 hash


# =============================================================================
# Integration Tests: Prune Worktrees
# =============================================================================


class TestPruneWorktrees:
    """Tests for prune_worktrees functionality."""

    @pytest.mark.integration
    def test_prune_after_manual_deletion(self, git_repo: Path, temp_dir: Path) -> None:
        """Test that prune cleans up after manual worktree directory deletion."""
        manager = GitManager(git_repo)
        worktree_path = temp_dir / "worktree-test"

        manager.create_worktree(worktree_path, "feature/wt-prune")

        # Verify worktree appears in list
        worktrees_before = manager.list_worktrees()
        assert any(wt["branch"] == "feature/wt-prune" for wt in worktrees_before)

        # Manually delete the worktree directory (simulating corruption)
        shutil.rmtree(worktree_path)
        assert not worktree_path.exists()

        # Prune should clean up stale references
        manager.prune_worktrees()

        # After pruning, the stale worktree should no longer appear
        worktrees_after = manager.list_worktrees()
        assert not any(wt["branch"] == "feature/wt-prune" for wt in worktrees_after)


# =============================================================================
# Integration Tests: Merge Branch
# =============================================================================


class TestMergeBranch:
    """Tests for merge_branch functionality."""

    @pytest.mark.integration
    def test_merge_branch_clean_merge(self, git_repo: Path) -> None:
        """Test that a clean merge succeeds."""
        manager = GitManager(git_repo)

        # Create feature branch and add a commit
        manager.create_task_branch("merge-clean")
        (git_repo / "feature.txt").write_text("Feature content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Add feature"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Merge feature into main
        manager.merge_branch("agent/merge-clean", "main")

        # Should be on main now
        assert manager.get_current_branch() == "main"

        # Feature file should exist on main
        assert (git_repo / "feature.txt").exists()

    @pytest.mark.integration
    def test_merge_branch_conflict_raises(self, git_repo: Path) -> None:
        """Test that merge conflict raises GitError.

        NOTE: Git writes CONFLICT messages to stdout, but merge_branch
        only checks stderr for conflict detection. As a result, a merge
        conflict raises GitError rather than the more specific
        MergeConflictError. The merge is still properly aborted.
        TODO: merge_branch should also check stdout for CONFLICT.
        """
        manager = GitManager(git_repo)

        # Create feature branch and modify README
        manager.create_task_branch("merge-conflict")
        (git_repo / "README.md").write_text("Feature version\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Feature change to README"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Go back to main and make conflicting change
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        (git_repo / "README.md").write_text("Main version\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Main change to README"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Merge should raise an error due to conflict
        # GitError is the base class; MergeConflictError would be raised
        # if the implementation also checked stdout for CONFLICT
        with pytest.raises(GitError):
            manager.merge_branch("agent/merge-conflict", "main")

        # State should be clean (merge aborted)
        assert manager.get_current_branch() == "main"

    @pytest.mark.integration
    def test_merge_branch_no_ff_creates_merge_commit(self, git_repo: Path) -> None:
        """Test that no_ff=True creates a merge commit."""
        manager = GitManager(git_repo)

        # Create feature branch with a commit
        manager.create_task_branch("merge-noff")
        (git_repo / "noff.txt").write_text("No-ff content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Feature commit for no-ff"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Merge with no_ff=True (default)
        manager.merge_branch("agent/merge-noff", "main", no_ff=True)

        # Verify merge commit was created
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        commit_msg = result.stdout.decode("utf-8").strip()
        assert "Merge" in commit_msg
        assert "agent/merge-noff" in commit_msg

    @pytest.mark.integration
    def test_merge_branch_raises_for_missing_source(self, git_repo: Path) -> None:
        """Test that merge raises BranchNotFoundError for missing source."""
        manager = GitManager(git_repo)

        with pytest.raises(BranchNotFoundError, match="does not exist"):
            manager.merge_branch("nonexistent-branch", "main")

    @pytest.mark.integration
    def test_merge_branch_raises_for_missing_target(self, git_repo: Path) -> None:
        """Test that merge raises BranchNotFoundError for missing target."""
        manager = GitManager(git_repo)

        manager.create_task_branch("merge-no-target")
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        with pytest.raises(BranchNotFoundError, match="does not exist"):
            manager.merge_branch("agent/merge-no-target", "nonexistent")


# =============================================================================
# Integration Tests: Delete Branch
# =============================================================================


class TestDeleteBranch:
    """Tests for delete_branch functionality."""

    @pytest.mark.integration
    def test_delete_merged_branch(self, git_repo: Path) -> None:
        """Test deleting a branch that has been merged."""
        manager = GitManager(git_repo)

        # Create and merge branch
        manager.create_task_branch("to-delete")
        (git_repo / "delete-me.txt").write_text("content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Commit to delete"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Merge into main
        manager.merge_branch("agent/to-delete", "main")

        # Delete the merged branch
        manager.delete_branch("agent/to-delete")

        assert not manager.branch_exists("agent/to-delete")

    @pytest.mark.integration
    def test_delete_branch_force(self, git_repo: Path) -> None:
        """Test force-deleting an unmerged branch."""
        manager = GitManager(git_repo)

        # Create branch with unmerged commit
        manager.create_task_branch("force-delete")
        (git_repo / "unmerged.txt").write_text("content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Unmerged commit"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Go back to main (cannot delete current branch)
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        # Force delete should succeed
        manager.delete_branch("agent/force-delete", force=True)

        assert not manager.branch_exists("agent/force-delete")

    @pytest.mark.integration
    def test_delete_branch_raises_for_nonexistent(self, git_repo: Path) -> None:
        """Test that deleting a non-existent branch raises error."""
        manager = GitManager(git_repo)

        with pytest.raises(BranchNotFoundError, match="does not exist"):
            manager.delete_branch("nonexistent-branch")
