"""Pytest configuration and fixtures for Maestro tests."""

import shutil
import sqlite3
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
    monkeypatch.delenv("ATP_CATALOG", raising=False)


@pytest.fixture
def catalog_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point $ATP_CATALOG at the test fixture catalog; return its path."""
    fixture = Path(__file__).parent / "fixtures" / "agents-catalog.toml"
    monkeypatch.setenv("ATP_CATALOG", str(fixture))
    return fixture


# =============================================================================
# External Binary Stubs
# =============================================================================


@pytest.fixture(autouse=True)
def _stub_spec_runner_help(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_project(check_fs=True) shells out to `spec-runner run
    --help` (H-7 contract guard) and `spec-runner --version` (#122 version
    gate). Stub both to passing responses so the suite doesn't depend on a
    locally installed spec-runner binary/version. Other subprocess.run
    calls (e.g. real `git` invocations made directly by tests) pass
    through untouched. Tests that want to exercise a guard itself override
    this via their own monkeypatch.setattr call, which wins because it
    runs later in the same test.
    """
    import subprocess

    from maestro import preflight
    from maestro.spec_runner import SPEC_RUNNER_REQUIRED_VERSION

    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["spec-runner", "run", "--help"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="usage: ... --spec-prefix SPEC_PREFIX ...", stderr=""
            )
        if cmd[:2] == ["spec-runner", "--version"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=f"spec-runner {SPEC_RUNNER_REQUIRED_VERSION}\n",
                stderr="",
            )
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)


@pytest.fixture
def seed_postmortem():
    """Seed a committed post-mortem archive for a workstream (#164).

    Delivery is gated on evidence existing, so any test that drives
    `_handle_success` through to DONE has to have captured an archive first —
    exactly as a real run does inside finalization. This writes a real one via
    the production `capture_archive`, so the gate reads what it would read in
    production rather than a hand-built stand-in.
    """
    from maestro.models import PostmortemConfig
    from maestro.postmortem import capture_archive

    async def _seed(
        db,
        workstream_id: str,
        *,
        done: int = 1,
        planned: int | None = 1,
        noop_done: int = 0,
        execution_id: str = "exec-1",
        head_sha: str = "c" * 40,
        stop_reason: str | None = None,
        spec_dir: Path | None = None,
    ) -> Path:
        root = Path(db.db_path).parent / "postmortem"
        source = spec_dir if spec_dir is not None else root.parent / "seed-spec"
        source.mkdir(parents=True, exist_ok=True)
        state_db = source / ".executor-maestro-state.db"
        if not state_db.exists():
            conn = sqlite3.connect(str(state_db))
            try:
                conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY)")
                conn.commit()
            finally:
                conn.close()
        archive = capture_archive(
            spec_dir=source,
            root=root,
            identity={
                "workstream_id": workstream_id,
                "execution_id": execution_id,
                "attempt": 0,
                "backend_id": "local",
                "transport": "local",
                "exit_code": 0,
                "branch": f"feature/{workstream_id}",
                "head_sha": head_sha,
                "captured_at": "2026-08-11T00:00:00Z",
                "last_run_stop_reason": stop_reason,
                "last_run_stop_detail": None,
            },
            counters={
                "done": done,
                "planned": planned,
                "noop_done": noop_done,
                "state_total": done,
            },
            config=PostmortemConfig(),
        )
        await db.record_postmortem_archive(
            workstream_id,
            execution_id,
            path=str(archive.path),
            bytes_written=archive.bytes_written,
            truncated=archive.truncated,
        )
        return archive.path

    return _seed
