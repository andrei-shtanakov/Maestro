"""Tests for the maestro package."""

from pathlib import Path
from typing import Any

import pytest

import maestro
from main import main


class TestVersion:
    """Tests for package version."""

    def test_version_is_defined(self) -> None:
        """Test that version is defined."""
        assert maestro.__version__ == "0.1.0"

    def test_version_is_string(self) -> None:
        """Test that version is a string."""
        assert isinstance(maestro.__version__, str)

    def test_version_follows_semver(self) -> None:
        """Test that version follows semantic versioning format."""
        parts = maestro.__version__.split(".")
        assert len(parts) == 3
        for part in parts:
            assert part.isdigit()


class TestSubpackages:
    """Tests for subpackage imports."""

    def test_spawners_importable(self) -> None:
        """Test that spawners subpackage can be imported."""
        import maestro.spawners

        assert maestro.spawners is not None

    def test_coordination_importable(self) -> None:
        """Test that coordination subpackage can be imported."""
        import maestro.coordination

        assert maestro.coordination is not None

    def test_notifications_importable(self) -> None:
        """Test that notifications subpackage can be imported."""
        import maestro.notifications

        assert maestro.notifications is not None


class TestMain:
    """Tests for main entry point."""

    def test_main_is_callable(self) -> None:
        """Test that main() is callable."""
        assert callable(main)

    def test_main_with_help_returns_zero(self) -> None:
        """Test that main() with --help returns exit code 0."""
        import sys
        from unittest.mock import patch as mock_patch

        with mock_patch.object(sys, "argv", ["maestro", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0


class TestFixtures:
    """Tests for conftest fixtures to ensure they work correctly."""

    def test_temp_dir_fixture(self, temp_dir: Path) -> None:
        """Test that temp_dir fixture provides a valid directory."""
        assert temp_dir.exists()
        assert temp_dir.is_dir()

    def test_temp_dir_is_writable(self, temp_dir: Path) -> None:
        """Test that temp_dir is writable."""
        test_file = temp_dir / "test.txt"
        test_file.write_text("test content")
        assert test_file.exists()
        assert test_file.read_text() == "test content"

    def test_temp_file_fixture(self, temp_file: Path) -> None:
        """Test that temp_file fixture provides a valid file."""
        assert temp_file.exists()
        assert temp_file.is_file()

    def test_project_root_fixture(self, project_root: Path) -> None:
        """Test that project_root points to correct directory."""
        assert project_root.exists()
        assert (project_root / "pyproject.toml").exists()

    def test_test_data_dir_fixture(self, test_data_dir: Path) -> None:
        """Test that test_data_dir is created."""
        assert test_data_dir.exists()
        assert test_data_dir.is_dir()

    def test_sample_task_config_fixture(
        self, sample_task_config: dict[str, Any]
    ) -> None:
        """Test that sample_task_config has required fields."""
        assert "id" in sample_task_config
        assert "title" in sample_task_config
        assert "prompt" in sample_task_config
        assert "agent_type" in sample_task_config

    def test_sample_project_config_fixture(
        self, sample_project_config: dict[str, Any]
    ) -> None:
        """Test that sample_project_config has required fields."""
        assert "project" in sample_project_config
        assert "repo" in sample_project_config
        assert "max_concurrent" in sample_project_config
        assert "tasks" in sample_project_config
        assert len(sample_project_config["tasks"]) > 0

    def test_sample_yaml_config_fixture(self, sample_yaml_config: Path) -> None:
        """Test that sample_yaml_config creates valid YAML file."""
        import yaml

        assert sample_yaml_config.exists()
        content = yaml.safe_load(sample_yaml_config.read_text())
        assert "project" in content
        assert "tasks" in content


@pytest.mark.integration
class TestGitFixture:
    """Tests for git repository fixture."""

    def test_git_repo_fixture(self, git_repo: Path) -> None:
        """Test that git_repo creates a valid git repository."""
        assert git_repo.exists()
        assert (git_repo / ".git").exists()
        assert (git_repo / "README.md").exists()

    def test_git_repo_has_initial_commit(self, git_repo: Path) -> None:
        """Test that git_repo has an initial commit."""
        import subprocess

        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "Initial commit" in result.stdout


class TestMockSubprocess:
    """Tests for mock_subprocess fixture."""

    def test_mock_subprocess_fixture(self, mock_subprocess: dict[str, Any]) -> None:
        """Test that mock_subprocess provides run and Popen mocks."""
        assert "run" in mock_subprocess
        assert "Popen" in mock_subprocess

    def test_mock_subprocess_run_returns_success(
        self, mock_subprocess: dict[str, Any]
    ) -> None:
        """Test that mocked run returns success by default."""
        import subprocess

        result = subprocess.run(["test"])
        assert result.returncode == 0

    def test_mock_subprocess_popen_returns_pid(
        self, mock_subprocess: dict[str, Any]
    ) -> None:
        """Test that mocked Popen returns a PID."""
        import subprocess

        proc = subprocess.Popen(["test"])
        assert proc.pid == 12345


@pytest.mark.unit
class TestEnvironmentCleanup:
    """Tests for environment cleanup fixture."""

    def test_maestro_env_vars_cleaned(self) -> None:
        """Test that MAESTRO_ prefixed env vars are cleaned up."""
        import os

        # The autouse cleanup_environment fixture should have run
        maestro_vars = [k for k in os.environ if k.startswith("MAESTRO_")]
        assert len(maestro_vars) == 0
