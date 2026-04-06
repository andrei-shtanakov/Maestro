"""Tests for the Workspace Manager module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from maestro.git import GitManager, WorktreeError
from maestro.workspace import (
    WorkspaceError,
    WorkspaceExistsError,
    WorkspaceManager,
    WorkspaceNotFoundError,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def workspace_mgr(
    git_repo: Path,
    temp_dir: Path,
) -> WorkspaceManager:
    """Create a WorkspaceManager backed by a real git repo."""
    git_mgr = GitManager(git_repo)
    workspace_base = temp_dir / "workspaces"
    return WorkspaceManager(git_manager=git_mgr, workspace_base=workspace_base)


# =============================================================================
# Unit Tests: Create Workspace
# =============================================================================


class TestCreateWorkspace:
    """Tests for create_workspace functionality."""

    @pytest.mark.integration
    def test_create_workspace_success(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that create_workspace creates a worktree directory."""
        result = workspace_mgr.create_workspace(
            "zadacha-001",
            "agent/zadacha-001",
        )

        assert result.exists()
        assert result.is_dir()
        assert result.name == "zadacha-001"
        # The worktree should contain the repo files
        assert (result / "README.md").exists()

    @pytest.mark.integration
    def test_create_workspace_returns_correct_path(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that create_workspace returns path under workspace_base."""
        result = workspace_mgr.create_workspace(
            "zadacha-002",
            "agent/zadacha-002",
        )

        expected = workspace_mgr.workspace_base / "zadacha-002"
        assert result == expected

    @pytest.mark.integration
    def test_create_workspace_already_exists_error(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that creating a workspace in an existing dir raises error."""
        # Create the directory manually so it already exists
        workspace_path = workspace_mgr.workspace_base / "zadacha-dup"
        workspace_path.mkdir(parents=True)

        with pytest.raises(
            WorkspaceExistsError,
            match="already exists",
        ):
            workspace_mgr.create_workspace(
                "zadacha-dup",
                "agent/zadacha-dup",
            )

    @pytest.mark.integration
    def test_create_workspace_worktree_failure(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that WorktreeError from git is wrapped in WorkspaceError."""
        with (
            patch.object(
                workspace_mgr._git,
                "create_worktree",
                side_effect=WorktreeError("git worktree add failed"),
            ),
            pytest.raises(
                WorkspaceError,
                match="Failed to create workspace",
            ),
        ):
            workspace_mgr.create_workspace(
                "zadacha-fail",
                "agent/zadacha-fail",
            )

    @pytest.mark.integration
    def test_create_workspace_creates_base_dir(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that create_workspace creates workspace_base if missing."""
        assert not workspace_mgr.workspace_base.exists()

        workspace_mgr.create_workspace(
            "zadacha-first",
            "agent/zadacha-first",
        )

        assert workspace_mgr.workspace_base.exists()


# =============================================================================
# Unit Tests: Setup Spec Runner
# =============================================================================


class TestSetupSpecRunner:
    """Tests for setup_spec_runner functionality."""

    @pytest.mark.integration
    def test_setup_spec_runner_writes_config_file(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that setup_spec_runner writes executor.config.yaml."""
        workspace_path = workspace_mgr.create_workspace(
            "zadacha-spec",
            "agent/zadacha-spec",
        )
        config = {"mode": "test", "timeout": 60}

        workspace_mgr.setup_spec_runner(workspace_path, config)

        config_file = workspace_path / "spec-runner.config.yaml"
        assert config_file.exists()

        with config_file.open() as f:
            loaded = yaml.safe_load(f)
        assert loaded == {"mode": "test", "timeout": 60}

    @pytest.mark.integration
    def test_setup_spec_runner_creates_spec_dir(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that setup_spec_runner creates spec/ directory."""
        workspace_path = workspace_mgr.create_workspace(
            "zadacha-specdir",
            "agent/zadacha-specdir",
        )

        workspace_mgr.setup_spec_runner(workspace_path, {"key": "value"})

        spec_dir = workspace_path / "spec"
        assert spec_dir.exists()
        assert spec_dir.is_dir()

    def test_setup_spec_runner_workspace_not_found(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that setup_spec_runner raises if workspace missing."""
        nonexistent = workspace_mgr.workspace_base / "does-not-exist"

        with pytest.raises(
            WorkspaceNotFoundError,
            match="Workspace not found",
        ):
            workspace_mgr.setup_spec_runner(nonexistent, {"key": "val"})

    @pytest.mark.integration
    def test_setup_spec_runner_idempotent_spec_dir(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that calling setup_spec_runner twice doesn't fail."""
        workspace_path = workspace_mgr.create_workspace(
            "zadacha-idem",
            "agent/zadacha-idem",
        )
        config = {"run": True}

        workspace_mgr.setup_spec_runner(workspace_path, config)
        # Second call should not raise
        workspace_mgr.setup_spec_runner(workspace_path, config)

        assert (workspace_path / "spec").is_dir()
        assert (workspace_path / "spec-runner.config.yaml").exists()


# =============================================================================
# Unit Tests: Cleanup Workspace
# =============================================================================


class TestCleanupWorkspace:
    """Tests for cleanup_workspace functionality."""

    @pytest.mark.integration
    def test_cleanup_workspace_success(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that cleanup_workspace removes the worktree directory."""
        workspace_mgr.create_workspace(
            "zadacha-clean",
            "agent/zadacha-clean",
        )
        assert workspace_mgr.workspace_exists("zadacha-clean")

        workspace_mgr.cleanup_workspace("zadacha-clean")

        assert not workspace_mgr.workspace_exists("zadacha-clean")

    def test_cleanup_workspace_already_cleaned_noop(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that cleaning a non-existent workspace is a no-op."""
        # Should not raise
        workspace_mgr.cleanup_workspace("never-existed")

    @pytest.mark.integration
    def test_cleanup_workspace_fallback_to_shutil(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that cleanup falls back to shutil when git remove fails."""
        workspace_path = workspace_mgr.create_workspace(
            "zadacha-fallback",
            "agent/zadacha-fallback",
        )
        assert workspace_path.exists()

        with (
            patch.object(
                workspace_mgr._git,
                "remove_worktree",
                side_effect=WorktreeError("remove failed"),
            ),
            patch.object(
                workspace_mgr._git,
                "prune_worktrees",
            ) as mock_prune,
        ):
            workspace_mgr.cleanup_workspace("zadacha-fallback")

        # shutil.rmtree should have removed it
        assert not workspace_path.exists()
        mock_prune.assert_called_once()


# =============================================================================
# Unit Tests: Get Workspace Path
# =============================================================================


class TestGetWorkspacePath:
    """Tests for get_workspace_path functionality."""

    @pytest.mark.integration
    def test_get_workspace_path_success(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test get_workspace_path returns correct path for existing workspace."""
        created = workspace_mgr.create_workspace(
            "zadacha-get",
            "agent/zadacha-get",
        )

        result = workspace_mgr.get_workspace_path("zadacha-get")

        assert result == created
        assert result.exists()

    def test_get_workspace_path_not_found(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test get_workspace_path raises when workspace does not exist."""
        with pytest.raises(
            WorkspaceNotFoundError,
            match="Workspace not found",
        ):
            workspace_mgr.get_workspace_path("nonexistent")


# =============================================================================
# Unit Tests: Workspace Exists
# =============================================================================


class TestWorkspaceExists:
    """Tests for workspace_exists functionality."""

    @pytest.mark.integration
    def test_workspace_exists_true(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test workspace_exists returns True for existing workspace."""
        workspace_mgr.create_workspace(
            "zadacha-exists",
            "agent/zadacha-exists",
        )

        assert workspace_mgr.workspace_exists("zadacha-exists") is True

    def test_workspace_exists_false(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test workspace_exists returns False for absent workspace."""
        assert workspace_mgr.workspace_exists("no-such-zadacha") is False


# =============================================================================
# Unit Tests: List Workspaces
# =============================================================================


class TestListWorkspaces:
    """Tests for list_workspaces functionality."""

    def test_list_workspaces_empty(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test list_workspaces returns empty list when base doesn't exist."""
        assert workspace_mgr.list_workspaces() == []

    @pytest.mark.integration
    def test_list_workspaces_with_workspaces(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test list_workspaces returns existing workspace directories."""
        workspace_mgr.create_workspace("zadacha-a", "agent/zadacha-a")
        workspace_mgr.create_workspace("zadacha-b", "agent/zadacha-b")

        workspaces = workspace_mgr.list_workspaces()

        names = [w.name for w in workspaces]
        assert "zadacha-a" in names
        assert "zadacha-b" in names
        assert len(workspaces) == 2

    def test_list_workspaces_base_not_exists(
        self,
        temp_dir: Path,
    ) -> None:
        """Test list_workspaces returns empty when base dir missing."""
        git_mgr = MagicMock(spec=GitManager)
        nonexistent_base = temp_dir / "missing_base"
        mgr = WorkspaceManager(
            git_manager=git_mgr,
            workspace_base=nonexistent_base,
        )

        result = mgr.list_workspaces()

        assert result == []

    @pytest.mark.integration
    def test_list_workspaces_ignores_hidden_dirs(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that list_workspaces skips directories starting with dot."""
        workspace_mgr.create_workspace("zadacha-vis", "agent/zadacha-vis")

        # Create a hidden directory manually
        hidden = workspace_mgr.workspace_base / ".hidden"
        hidden.mkdir()

        workspaces = workspace_mgr.list_workspaces()

        names = [w.name for w in workspaces]
        assert "zadacha-vis" in names
        assert ".hidden" not in names

    @pytest.mark.integration
    def test_list_workspaces_sorted(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that list_workspaces returns directories in sorted order."""
        workspace_mgr.create_workspace("zadacha-c", "agent/zadacha-c")
        workspace_mgr.create_workspace("zadacha-a", "agent/zadacha-a")
        workspace_mgr.create_workspace("zadacha-b", "agent/zadacha-b")

        workspaces = workspace_mgr.list_workspaces()
        names = [w.name for w in workspaces]

        assert names == sorted(names)


# =============================================================================
# Integration Tests: Cleanup All
# =============================================================================


class TestCleanupAll:
    """Tests for cleanup_all functionality."""

    @pytest.mark.integration
    def test_cleanup_all_removes_all_workspaces(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that cleanup_all removes every workspace."""
        workspace_mgr.create_workspace("zadacha-x", "agent/zadacha-x")
        workspace_mgr.create_workspace("zadacha-y", "agent/zadacha-y")

        assert len(workspace_mgr.list_workspaces()) == 2

        workspace_mgr.cleanup_all()

        assert len(workspace_mgr.list_workspaces()) == 0
        assert not workspace_mgr.workspace_exists("zadacha-x")
        assert not workspace_mgr.workspace_exists("zadacha-y")

    def test_cleanup_all_empty_is_noop(
        self,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        """Test that cleanup_all with no workspaces does nothing."""
        # Should not raise
        workspace_mgr.cleanup_all()

        assert workspace_mgr.list_workspaces() == []
