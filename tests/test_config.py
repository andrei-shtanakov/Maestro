"""Tests for the configuration parser module."""

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from maestro.config import (
    ConfigError,
    load_config,
    load_config_from_string,
    resolve_env_vars,
)
from maestro.models import AgentType, ProjectConfig


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def valid_config_dict() -> dict[str, Any]:
    """Provide a valid configuration dictionary."""
    return {
        "project": "test-project",
        "repo": "/tmp/test-repo",
        "max_concurrent": 3,
        "tasks": [
            {
                "id": "task-1",
                "title": "First Task",
                "prompt": "Do something",
            },
            {
                "id": "task-2",
                "title": "Second Task",
                "prompt": "Do something else",
                "depends_on": ["task-1"],
            },
        ],
    }


@pytest.fixture
def config_with_defaults() -> dict[str, Any]:
    """Configuration with defaults section."""
    return {
        "project": "test-project",
        "repo": "/tmp/test-repo",
        "defaults": {
            "timeout_minutes": 60,
            "max_retries": 5,
            "agent_type": "aider",
        },
        "tasks": [
            {
                "id": "task-1",
                "title": "First Task",
                "prompt": "Do something",
            },
            {
                "id": "task-2",
                "title": "Second Task",
                "prompt": "Do something else",
                "timeout_minutes": 30,  # Explicit override
            },
        ],
    }


@pytest.fixture
def config_yaml_file(temp_dir: Path, valid_config_dict: dict[str, Any]) -> Path:
    """Create a valid YAML config file."""
    config_path = temp_dir / "tasks.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(valid_config_dict, f)
    return config_path


# =============================================================================
# Test: Valid Config Parsing
# =============================================================================


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_valid_config_from_file(
        self, config_yaml_file: Path, valid_config_dict: dict[str, Any]
    ) -> None:
        """Test loading a valid configuration file."""
        config = load_config(config_yaml_file)

        assert isinstance(config, ProjectConfig)
        assert config.project == valid_config_dict["project"]
        assert config.repo == valid_config_dict["repo"]
        assert config.max_concurrent == valid_config_dict["max_concurrent"]
        assert len(config.tasks) == 2

    def test_load_config_with_string_path(self, config_yaml_file: Path) -> None:
        """Test loading config with string path instead of Path object."""
        config = load_config(str(config_yaml_file))

        assert isinstance(config, ProjectConfig)
        assert config.project == "test-project"

    def test_load_config_file_not_found(self, temp_dir: Path) -> None:
        """Test error when config file doesn't exist."""
        non_existent = temp_dir / "non_existent.yaml"

        with pytest.raises(ConfigError) as exc_info:
            load_config(non_existent)

        assert "not found" in str(exc_info.value)
        assert exc_info.value.path == non_existent

    def test_load_config_path_is_directory(self, temp_dir: Path) -> None:
        """Test error when path is a directory instead of file."""
        with pytest.raises(ConfigError) as exc_info:
            load_config(temp_dir)

        assert "not a file" in str(exc_info.value)

    def test_load_empty_config_file(self, temp_dir: Path) -> None:
        """Test error when config file is empty."""
        empty_file = temp_dir / "empty.yaml"
        empty_file.touch()

        with pytest.raises(ConfigError) as exc_info:
            load_config(empty_file)

        assert "empty" in str(exc_info.value).lower()

    def test_load_config_not_a_mapping(self, temp_dir: Path) -> None:
        """Test error when YAML content is not a mapping."""
        list_file = temp_dir / "list.yaml"
        list_file.write_text("- item1\n- item2\n")

        with pytest.raises(ConfigError) as exc_info:
            load_config(list_file)

        assert "mapping" in str(exc_info.value).lower()


class TestLoadConfigFromString:
    """Tests for load_config_from_string function."""

    def test_load_valid_config_from_string(self) -> None:
        """Test loading a valid configuration from YAML string."""
        yaml_content = """
project: test-project
repo: /tmp/test-repo
tasks:
  - id: task-1
    title: Test Task
    prompt: Do something
"""
        config = load_config_from_string(yaml_content)

        assert isinstance(config, ProjectConfig)
        assert config.project == "test-project"
        assert len(config.tasks) == 1
        assert config.tasks[0].id == "task-1"

    def test_load_empty_string(self) -> None:
        """Test error when YAML string is empty."""
        with pytest.raises(ConfigError) as exc_info:
            load_config_from_string("")

        assert "empty" in str(exc_info.value).lower()

    def test_load_with_custom_path(self) -> None:
        """Test loading with custom path for error messages."""
        yaml_content = """
project: test
repo: /tmp/test
tasks: []
"""
        custom_path = Path("/custom/path/config.yaml")
        config = load_config_from_string(yaml_content, path=custom_path)

        assert config.project == "test"


# =============================================================================
# Test: Defaults Merging
# =============================================================================


class TestDefaultsMerging:
    """Tests for defaults merging from project to task level."""

    def test_defaults_applied_to_tasks(
        self, temp_dir: Path, config_with_defaults: dict[str, Any]
    ) -> None:
        """Test that defaults are applied to tasks without explicit values."""
        config_path = temp_dir / "config.yaml"
        with config_path.open("w") as f:
            yaml.safe_dump(config_with_defaults, f)

        config = load_config(config_path)

        # Task 1 should have defaults applied
        task1 = config.get_task_by_id("task-1")
        assert task1 is not None
        assert task1.timeout_minutes == 60
        assert task1.max_retries == 5
        assert task1.agent_type == AgentType.AIDER

    def test_defaults_do_not_override_explicit(
        self, temp_dir: Path, config_with_defaults: dict[str, Any]
    ) -> None:
        """Test that explicit task values override defaults."""
        config_path = temp_dir / "config.yaml"
        with config_path.open("w") as f:
            yaml.safe_dump(config_with_defaults, f)

        config = load_config(config_path)

        # Task 2 has explicit timeout_minutes, should not be overridden
        task2 = config.get_task_by_id("task-2")
        assert task2 is not None
        assert task2.timeout_minutes == 30  # Explicit value
        assert task2.max_retries == 5  # From defaults
        assert task2.agent_type == AgentType.AIDER  # From defaults

    def test_no_defaults_section(self, config_yaml_file: Path) -> None:
        """Test that config works without defaults section."""
        config = load_config(config_yaml_file)

        # Tasks should have their own defaults (from model)
        task1 = config.get_task_by_id("task-1")
        assert task1 is not None
        assert task1.timeout_minutes == 30  # Model default
        assert task1.max_retries == 2  # Model default
        assert task1.agent_type == AgentType.CLAUDE_CODE  # Model default

    def test_partial_defaults(self, temp_dir: Path) -> None:
        """Test config with partial defaults section."""
        config_dict = {
            "project": "test",
            "repo": "/tmp/test",
            "defaults": {
                "timeout_minutes": 120,
                # max_retries and agent_type not specified
            },
            "tasks": [
                {
                    "id": "task-1",
                    "title": "Test",
                    "prompt": "Do it",
                }
            ],
        }
        config_path = temp_dir / "config.yaml"
        with config_path.open("w") as f:
            yaml.safe_dump(config_dict, f)

        config = load_config(config_path)
        task = config.tasks[0]

        assert task.timeout_minutes == 120  # From defaults
        assert task.max_retries == 2  # DefaultsConfig model default
        assert task.agent_type == AgentType.CLAUDE_CODE  # DefaultsConfig default


# =============================================================================
# Test: Environment Variable Substitution
# =============================================================================


class TestEnvVarSubstitution:
    """Tests for environment variable substitution."""

    def test_resolve_env_vars_in_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test resolving env vars in a string."""
        monkeypatch.setenv("TEST_VAR", "test_value")

        result = resolve_env_vars("prefix_${TEST_VAR}_suffix")

        assert result == "prefix_test_value_suffix"

    def test_resolve_multiple_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test resolving multiple env vars in one string."""
        monkeypatch.setenv("VAR1", "first")
        monkeypatch.setenv("VAR2", "second")

        result = resolve_env_vars("${VAR1} and ${VAR2}")

        assert result == "first and second"

    def test_resolve_env_vars_in_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test resolving env vars in a dictionary."""
        monkeypatch.setenv("PROJECT_PATH", "/home/user/project")

        data = {"repo": "${PROJECT_PATH}", "name": "test"}
        result = resolve_env_vars(data)

        assert result["repo"] == "/home/user/project"
        assert result["name"] == "test"

    def test_resolve_env_vars_in_nested_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test resolving env vars in nested structures."""
        monkeypatch.setenv("TOKEN", "secret123")

        data = {
            "notifications": {
                "telegram": {
                    "token": "${TOKEN}",
                }
            }
        }
        result = resolve_env_vars(data)

        assert result["notifications"]["telegram"]["token"] == "secret123"

    def test_resolve_env_vars_in_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test resolving env vars in a list."""
        monkeypatch.setenv("SCOPE1", "src/")
        monkeypatch.setenv("SCOPE2", "tests/")

        data = ["${SCOPE1}", "${SCOPE2}"]
        result = resolve_env_vars(data)

        assert result == ["src/", "tests/"]

    def test_resolve_env_vars_undefined_raises_error(self) -> None:
        """Test that undefined env var raises ConfigError."""
        # Make sure variable doesn't exist
        os.environ.pop("UNDEFINED_VAR_12345", None)

        with pytest.raises(ConfigError) as exc_info:
            resolve_env_vars("${UNDEFINED_VAR_12345}")

        assert "UNDEFINED_VAR_12345" in str(exc_info.value)
        assert "not defined" in str(exc_info.value)

    def test_resolve_non_string_values_unchanged(self) -> None:
        """Test that non-string values pass through unchanged."""
        assert resolve_env_vars(42) == 42
        assert resolve_env_vars(3.14) == 3.14
        assert resolve_env_vars(True) is True
        assert resolve_env_vars(None) is None

    def test_env_var_in_config_file(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test env var substitution in actual config file loading."""
        monkeypatch.setenv("MAESTRO_REPO_PATH", "/home/user/myproject")

        config_content = """
project: test-project
repo: ${MAESTRO_REPO_PATH}
tasks:
  - id: task-1
    title: Test Task
    prompt: Do something
"""
        config_path = temp_dir / "config.yaml"
        config_path.write_text(config_content)

        config = load_config(config_path)

        assert config.repo == "/home/user/myproject"

    def test_env_var_with_underscores_and_numbers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test env vars with valid naming patterns."""
        monkeypatch.setenv("MY_VAR_123", "value")
        monkeypatch.setenv("_PRIVATE_VAR", "private")

        result = resolve_env_vars("${MY_VAR_123} ${_PRIVATE_VAR}")

        assert result == "value private"


# =============================================================================
# Test: Validation Errors
# =============================================================================


class TestValidationErrors:
    """Tests for validation error handling."""

    def test_invalid_yaml_syntax(self, temp_dir: Path) -> None:
        """Test error handling for invalid YAML syntax."""
        invalid_yaml = temp_dir / "invalid.yaml"
        invalid_yaml.write_text("project: test\n  invalid: indentation")

        with pytest.raises(ConfigError) as exc_info:
            load_config(invalid_yaml)

        error = exc_info.value
        assert error.path == invalid_yaml
        # YAML errors should include line information
        assert error.line is not None

    def test_missing_required_field(self, temp_dir: Path) -> None:
        """Test error when required field is missing."""
        config_dict = {
            "project": "test",
            # Missing 'repo' field
            "tasks": [],
        }
        config_path = temp_dir / "config.yaml"
        with config_path.open("w") as f:
            yaml.safe_dump(config_dict, f)

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path)

        assert "repo" in str(exc_info.value).lower()

    def test_invalid_task_id_format(self, temp_dir: Path) -> None:
        """Test error for invalid task ID format."""
        config_dict = {
            "project": "test",
            "repo": "/tmp/test",
            "tasks": [
                {
                    "id": "invalid id with spaces",
                    "title": "Test",
                    "prompt": "Do something",
                }
            ],
        }
        config_path = temp_dir / "config.yaml"
        with config_path.open("w") as f:
            yaml.safe_dump(config_dict, f)

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path)

        assert "alphanumeric" in str(exc_info.value).lower()

    def test_duplicate_task_ids(self, temp_dir: Path) -> None:
        """Test error when task IDs are duplicated."""
        config_dict = {
            "project": "test",
            "repo": "/tmp/test",
            "tasks": [
                {"id": "task-1", "title": "First", "prompt": "Do it"},
                {"id": "task-1", "title": "Duplicate", "prompt": "Do it again"},
            ],
        }
        config_path = temp_dir / "config.yaml"
        with config_path.open("w") as f:
            yaml.safe_dump(config_dict, f)

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path)

        assert "duplicate" in str(exc_info.value).lower()

    def test_unknown_dependency(self, temp_dir: Path) -> None:
        """Test error when task depends on non-existent task."""
        config_dict = {
            "project": "test",
            "repo": "/tmp/test",
            "tasks": [
                {
                    "id": "task-1",
                    "title": "Test",
                    "prompt": "Do it",
                    "depends_on": ["non-existent-task"],
                }
            ],
        }
        config_path = temp_dir / "config.yaml"
        with config_path.open("w") as f:
            yaml.safe_dump(config_dict, f)

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path)

        assert "unknown dependencies" in str(exc_info.value).lower()
        assert "non-existent-task" in str(exc_info.value)

    def test_self_dependency(self, temp_dir: Path) -> None:
        """Test error when task depends on itself."""
        config_dict = {
            "project": "test",
            "repo": "/tmp/test",
            "tasks": [
                {
                    "id": "task-1",
                    "title": "Test",
                    "prompt": "Do it",
                    "depends_on": ["task-1"],
                }
            ],
        }
        config_path = temp_dir / "config.yaml"
        with config_path.open("w") as f:
            yaml.safe_dump(config_dict, f)

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path)

        assert "itself" in str(exc_info.value).lower()

    def test_relative_repo_path(self, temp_dir: Path) -> None:
        """Test error when repo path is relative."""
        config_dict = {
            "project": "test",
            "repo": "relative/path",  # Not absolute
            "tasks": [],
        }
        config_path = temp_dir / "config.yaml"
        with config_path.open("w") as f:
            yaml.safe_dump(config_dict, f)

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path)

        assert "absolute" in str(exc_info.value).lower()

    def test_invalid_agent_type(self, temp_dir: Path) -> None:
        """Test error for invalid agent type."""
        config_dict = {
            "project": "test",
            "repo": "/tmp/test",
            "tasks": [
                {
                    "id": "task-1",
                    "title": "Test",
                    "prompt": "Do it",
                    "agent_type": "invalid_agent",
                }
            ],
        }
        config_path = temp_dir / "config.yaml"
        with config_path.open("w") as f:
            yaml.safe_dump(config_dict, f)

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path)

        # Pydantic validation error for enum
        assert "agent_type" in str(exc_info.value).lower()

    def test_timeout_out_of_range(self, temp_dir: Path) -> None:
        """Test error when timeout_minutes is out of valid range."""
        config_dict = {
            "project": "test",
            "repo": "/tmp/test",
            "tasks": [
                {
                    "id": "task-1",
                    "title": "Test",
                    "prompt": "Do it",
                    "timeout_minutes": 9999,  # Max is 1440
                }
            ],
        }
        config_path = temp_dir / "config.yaml"
        with config_path.open("w") as f:
            yaml.safe_dump(config_dict, f)

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path)

        assert "timeout" in str(exc_info.value).lower()


class TestConfigErrorFormatting:
    """Tests for ConfigError formatting."""

    def test_error_with_all_location_info(self) -> None:
        """Test error message with full location info."""
        error = ConfigError(
            "Something went wrong",
            path=Path("/path/to/config.yaml"),
            line=42,
            column=10,
        )

        message = str(error)
        assert "/path/to/config.yaml" in message
        assert "line 42" in message
        assert "column 10" in message
        assert "Something went wrong" in message

    def test_error_with_path_only(self) -> None:
        """Test error message with path only."""
        error = ConfigError(
            "Error message",
            path=Path("/config.yaml"),
        )

        message = str(error)
        assert "/config.yaml" in message
        assert "line" not in message

    def test_error_without_location(self) -> None:
        """Test error message without any location."""
        error = ConfigError("Just an error")

        assert str(error) == "Just an error"


# =============================================================================
# Test: Complex Configuration
# =============================================================================


class TestComplexConfiguration:
    """Tests for complex configuration scenarios."""

    def test_full_config_with_all_sections(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test loading a full configuration with all optional sections."""
        monkeypatch.setenv("TELEGRAM_TOKEN", "bot123:token")
        monkeypatch.setenv("TELEGRAM_CHAT", "12345")

        config_content = """
project: feature-auth-jwt
repo: /home/user/projects/myapp
max_concurrent: 5

defaults:
  timeout_minutes: 45
  max_retries: 3
  agent_type: claude_code

git:
  base_branch: develop
  auto_push: true
  branch_prefix: feature/

notifications:
  desktop: true
  telegram_token: "${TELEGRAM_TOKEN}"
  telegram_chat_id: "${TELEGRAM_CHAT}"
  webhook_url: https://hooks.example.com/notify

tasks:
  - id: prepare-interfaces
    title: Extract auth interfaces
    prompt: |
      Create auth interfaces:
      1. AuthProvider ABC
      2. Token types
    scope:
      - src/auth/interfaces.py
      - src/auth/tokens.py
    validation_cmd: pytest tests/auth/ -x

  - id: implement-jwt
    title: Implement JWT auth
    prompt: Implement JWTAuthProvider
    depends_on:
      - prepare-interfaces
    scope:
      - src/auth/jwt_provider.py
    timeout_minutes: 60
    max_retries: 5
    requires_approval: true
    priority: 10
"""
        config_path = temp_dir / "full_config.yaml"
        config_path.write_text(config_content)

        config = load_config(config_path)

        # Check project settings
        assert config.project == "feature-auth-jwt"
        assert config.repo == "/home/user/projects/myapp"
        assert config.max_concurrent == 5

        # Check git config
        assert config.git is not None
        assert config.git.base_branch == "develop"
        assert config.git.auto_push is True
        assert config.git.branch_prefix == "feature/"

        # Check notifications with env vars resolved
        assert config.notifications is not None
        assert config.notifications.desktop is True
        assert config.notifications.telegram_token == "bot123:token"
        assert config.notifications.telegram_chat_id == "12345"
        assert config.notifications.webhook_url == "https://hooks.example.com/notify"

        # Check tasks
        assert len(config.tasks) == 2

        task1 = config.get_task_by_id("prepare-interfaces")
        assert task1 is not None
        assert task1.title == "Extract auth interfaces"
        assert "AuthProvider ABC" in task1.prompt
        assert task1.scope == ["src/auth/interfaces.py", "src/auth/tokens.py"]
        assert task1.validation_cmd == "pytest tests/auth/ -x"
        assert task1.timeout_minutes == 45  # From defaults
        assert task1.max_retries == 3  # From defaults

        task2 = config.get_task_by_id("implement-jwt")
        assert task2 is not None
        assert task2.depends_on == ["prepare-interfaces"]
        assert task2.timeout_minutes == 60  # Explicit override
        assert task2.max_retries == 5  # Explicit override
        assert task2.requires_approval is True
        assert task2.priority == 10

    def test_minimal_valid_config(self, temp_dir: Path) -> None:
        """Test loading a minimal but valid configuration."""
        config_content = """
project: minimal
repo: /tmp/project
tasks: []
"""
        config_path = temp_dir / "minimal.yaml"
        config_path.write_text(config_content)

        config = load_config(config_path)

        assert config.project == "minimal"
        assert config.repo == "/tmp/project"
        assert config.max_concurrent == 3  # Default
        assert config.tasks == []
        assert config.defaults is None
        assert config.git is None
        assert config.notifications is None

    def test_config_with_tilde_path(self, temp_dir: Path) -> None:
        """Test that repo paths with ~ are accepted."""
        config_content = """
project: test
repo: ~/projects/myapp
tasks: []
"""
        config_path = temp_dir / "tilde.yaml"
        config_path.write_text(config_content)

        config = load_config(config_path)

        assert config.repo == "~/projects/myapp"


# =============================================================================
# TestArbiterSection — arbiter block parsing in orchestrator YAML
# =============================================================================


_ORCH_MINIMAL_HEADER = """\
project: test
repo_url: https://github.com/test/repo
repo_path: /tmp/test-repo
workspace_base: /tmp/test-ws
zadachi: []
"""


class TestArbiterSection:
    """Verify OrchestratorConfig.arbiter is populated from YAML."""

    def test_no_arbiter_section_defaults_to_none(self, tmp_path: Path) -> None:
        from maestro.config import load_orchestrator_config

        yaml_path = tmp_path / "p.yaml"
        yaml_path.write_text(_ORCH_MINIMAL_HEADER)
        cfg = load_orchestrator_config(yaml_path)
        assert cfg.arbiter is None

    def test_arbiter_section_parses_to_pydantic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maestro.config import load_orchestrator_config
        from maestro.models import ArbiterMode

        monkeypatch.setenv("ARBITER_BIN", "/opt/arbiter/arbiter-mcp")
        monkeypatch.setenv("ARBITER_CONFIG", "/etc/arbiter")
        monkeypatch.setenv("ARBITER_TREE", "/etc/arbiter/tree.json")

        yaml_path = tmp_path / "p.yaml"
        yaml_path.write_text(
            _ORCH_MINIMAL_HEADER
            + """\
arbiter:
  enabled: true
  mode: authoritative
  binary_path: ${ARBITER_BIN}
  config_dir: ${ARBITER_CONFIG}
  tree_path: ${ARBITER_TREE}
  timeout_ms: 750
"""
        )
        cfg = load_orchestrator_config(yaml_path)
        assert cfg.arbiter is not None
        assert cfg.arbiter.enabled is True
        assert cfg.arbiter.mode is ArbiterMode.AUTHORITATIVE
        assert cfg.arbiter.binary_path == "/opt/arbiter/arbiter-mcp"
        assert cfg.arbiter.timeout_ms == 750

    def test_unknown_arbiter_field_is_rejected(self, tmp_path: Path) -> None:
        """Typos in the arbiter block should fail loudly, not be silently dropped."""
        from maestro.config import load_orchestrator_config

        yaml_path = tmp_path / "p.yaml"
        yaml_path.write_text(
            _ORCH_MINIMAL_HEADER
            + """\
arbiter:
  enabled: false
  timeout_ms_typo: 123
"""
        )
        with pytest.raises(ConfigError, match="timeout_ms_typo"):
            load_orchestrator_config(yaml_path)
