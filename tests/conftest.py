"""Pytest configuration and fixtures for Maestro tests."""

import shutil
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest


# =============================================================================
# Async Fixtures
# =============================================================================


@pytest.fixture
def anyio_backend() -> str:
    """Configure anyio to use asyncio backend."""
    return "asyncio"


# =============================================================================
# Path Fixtures
# =============================================================================


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Provide a temporary directory that is cleaned up after the test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_file(temp_dir: Path) -> Generator[Path, None, None]:
    """Provide a temporary file path within the temp directory."""
    file_path = temp_dir / "test_file.txt"
    file_path.touch()
    yield file_path


@pytest.fixture
def project_root() -> Path:
    """Return the project root directory."""
    return Path(__file__).parent.parent


@pytest.fixture
def test_data_dir(project_root: Path) -> Path:
    """Return the test data directory, creating it if it doesn't exist."""
    data_dir = project_root / "tests" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


# =============================================================================
# Database Fixtures
# =============================================================================


@pytest.fixture
def temp_db_path(temp_dir: Path) -> Path:
    """Provide a temporary database file path."""
    return temp_dir / "test_maestro.db"


# =============================================================================
# Git Fixtures
# =============================================================================


@pytest.fixture
def git_repo(temp_dir: Path) -> Generator[Path, None, None]:
    """Create a temporary git repository for testing.

    Skips if git is not available.
    """
    import subprocess

    # Check if git is available
    if shutil.which("git") is None:
        pytest.skip("git is not available")

    repo_dir = temp_dir / "test_repo"
    repo_dir.mkdir()

    # Initialize git repo
    subprocess.run(
        ["git", "init"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    # Create initial commit
    readme = repo_dir / "README.md"
    readme.write_text("# Test Repository\n")
    subprocess.run(
        ["git", "add", "."],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    yield repo_dir


# =============================================================================
# Configuration Fixtures
# =============================================================================


@pytest.fixture
def sample_task_config() -> dict[str, Any]:
    """Provide a sample task configuration dictionary."""
    return {
        "id": "test-task-001",
        "title": "Test Task",
        "prompt": "This is a test task prompt.",
        "agent_type": "claude_code",
        "scope": ["src/**/*.py"],
        "depends_on": [],
        "timeout_minutes": 30,
        "max_retries": 2,
        "validation_cmd": None,
        "requires_approval": False,
    }


@pytest.fixture
def sample_project_config(sample_task_config: dict[str, Any]) -> dict[str, Any]:
    """Provide a sample project configuration dictionary."""
    return {
        "project": "test-project",
        "repo": "/tmp/test-repo",
        "max_concurrent": 3,
        "tasks": [sample_task_config],
    }


@pytest.fixture
def sample_yaml_config(temp_dir: Path, sample_project_config: dict[str, Any]) -> Path:
    """Create a sample YAML config file and return its path."""
    import yaml

    config_path = temp_dir / "tasks.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(sample_project_config, f)
    return config_path


# =============================================================================
# Mock Fixtures
# =============================================================================


@pytest.fixture
def mock_subprocess(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Mock subprocess.run and subprocess.Popen for testing spawners."""
    from unittest.mock import MagicMock

    mock_run = MagicMock()
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = b""
    mock_run.return_value.stderr = b""

    mock_popen = MagicMock()
    mock_popen.return_value.returncode = 0
    mock_popen.return_value.pid = 12345
    mock_popen.return_value.poll.return_value = None

    import subprocess

    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    return {"run": mock_run, "Popen": mock_popen}


# =============================================================================
# Cleanup Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def cleanup_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clean up environment variables that might affect tests."""
    # Remove any MAESTRO_ prefixed env vars that could affect tests
    import os

    for key in list(os.environ.keys()):
        if key.startswith("MAESTRO_"):
            monkeypatch.delenv(key, raising=False)
