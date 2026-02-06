"""Configuration file parsing for Maestro.

This module provides functionality to load and validate YAML configuration files
for the Maestro orchestrator. It handles:
- YAML parsing with PyYAML
- Environment variable substitution (${VAR} syntax)
- Defaults merging from project-level to task-level
- Schema validation through Pydantic models
- Detailed error messages with position information
"""

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from maestro.models import OrchestratorConfig, ProjectConfig


class ConfigError(Exception):
    """Configuration parsing or validation error.

    Attributes:
        message: Human-readable error description
        path: Path to the config file (if available)
        line: Line number where error occurred (if available)
        column: Column number where error occurred (if available)
    """

    def __init__(
        self,
        message: str,
        path: Path | None = None,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        self.message = message
        self.path = path
        self.line = line
        self.column = column
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        """Format the error message with location information."""
        parts = []
        if self.path:
            parts.append(str(self.path))
        if self.line is not None:
            parts.append(f"line {self.line}")
        if self.column is not None:
            parts.append(f"column {self.column}")

        if parts:
            location = ":".join(parts)
            return f"{location}: {self.message}"
        return self.message


# Regex pattern for environment variable substitution: ${VAR_NAME}
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_env_vars(
    value: Any,
    path: Path | None = None,
) -> Any:
    """Recursively resolve environment variables in configuration values.

    Supports the ${VAR_NAME} syntax for environment variable substitution.
    Variables are replaced with their values from os.environ.

    Args:
        value: The configuration value to process (can be dict, list, or scalar)
        path: Path to the config file for error messages

    Returns:
        The value with all environment variables resolved

    Raises:
        ConfigError: If an environment variable is not defined
    """
    if isinstance(value, str):
        return _resolve_string_env_vars(value, path)
    elif isinstance(value, dict):
        return {k: resolve_env_vars(v, path) for k, v in value.items()}
    elif isinstance(value, list):
        return [resolve_env_vars(item, path) for item in value]
    else:
        return value


def _resolve_string_env_vars(value: str, path: Path | None = None) -> str:
    """Resolve environment variables in a string value.

    Args:
        value: String potentially containing ${VAR} patterns
        path: Path to the config file for error messages

    Returns:
        String with all environment variables resolved

    Raises:
        ConfigError: If an environment variable is not defined
    """

    def replace_env_var(match: re.Match[str]) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ConfigError(
                f"Environment variable '{var_name}' is not defined",
                path=path,
            )
        return env_value

    return ENV_VAR_PATTERN.sub(replace_env_var, value)


def _format_yaml_error(exc: yaml.YAMLError, path: Path) -> ConfigError:
    """Convert a PyYAML error to a ConfigError with position information.

    Args:
        exc: The YAML error from PyYAML
        path: Path to the config file

    Returns:
        ConfigError with formatted message and position
    """
    line = None
    column = None

    # Extract position information from the YAML error
    if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
        mark = exc.problem_mark
        line = mark.line + 1  # Convert to 1-based line numbers
        column = mark.column + 1

    message = str(exc.problem) if hasattr(exc, "problem") else str(exc)
    return ConfigError(message, path=path, line=line, column=column)


def _format_validation_error(exc: ValidationError, path: Path) -> ConfigError:
    """Convert a Pydantic ValidationError to a ConfigError with details.

    Args:
        exc: The validation error from Pydantic
        path: Path to the config file

    Returns:
        ConfigError with formatted validation errors
    """
    errors = exc.errors()
    if len(errors) == 1:
        error = errors[0]
        location = ".".join(str(loc) for loc in error["loc"])
        message = f"Validation error at '{location}': {error['msg']}"
    else:
        error_messages = []
        for error in errors:
            location = ".".join(str(loc) for loc in error["loc"])
            error_messages.append(f"  - {location}: {error['msg']}")
        message = "Multiple validation errors:\n" + "\n".join(error_messages)

    return ConfigError(message, path=path)


def load_config(path: Path | str) -> ProjectConfig:
    """Load and validate a YAML configuration file.

    This function performs the following steps:
    1. Load the YAML file from disk
    2. Resolve environment variables (${VAR} syntax)
    3. Validate the configuration through Pydantic models
    4. Apply defaults to tasks (handled by Pydantic model validators)

    Args:
        path: Path to the YAML configuration file

    Returns:
        ProjectConfig: The validated configuration

    Raises:
        ConfigError: If the file cannot be read, parsed, or validated
    """
    if isinstance(path, str):
        path = Path(path)

    # Check if file exists
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}", path=path)

    if not path.is_file():
        raise ConfigError(f"Path is not a file: {path}", path=path)

    # Read and parse YAML
    try:
        with path.open("r", encoding="utf-8") as f:
            raw_config = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise _format_yaml_error(exc, path) from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read file: {exc}", path=path) from exc

    # Handle empty files
    if raw_config is None:
        raise ConfigError("Configuration file is empty", path=path)

    if not isinstance(raw_config, dict):
        raise ConfigError(
            f"Configuration must be a YAML mapping, got {type(raw_config).__name__}",
            path=path,
        )

    # Resolve environment variables
    resolved_config = resolve_env_vars(raw_config, path)

    # Validate through Pydantic
    try:
        return ProjectConfig(**resolved_config)
    except ValidationError as exc:
        raise _format_validation_error(exc, path) from exc


def load_orchestrator_config(
    path: Path | str,
) -> OrchestratorConfig:
    """Load and validate an orchestrator YAML config.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        Validated OrchestratorConfig.

    Raises:
        ConfigError: If file cannot be read or validated.
    """
    if isinstance(path, str):
        path = Path(path)

    if not path.exists():
        raise ConfigError(
            f"Configuration file not found: {path}",
            path=path,
        )

    if not path.is_file():
        raise ConfigError(f"Path is not a file: {path}", path=path)

    try:
        with path.open("r", encoding="utf-8") as f:
            raw_config = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise _format_yaml_error(exc, path) from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read file: {exc}", path=path) from exc

    if raw_config is None:
        raise ConfigError("Configuration file is empty", path=path)

    if not isinstance(raw_config, dict):
        raise ConfigError(
            f"Configuration must be a YAML mapping, got {type(raw_config).__name__}",
            path=path,
        )

    resolved_config = resolve_env_vars(raw_config, path)

    try:
        return OrchestratorConfig(**resolved_config)
    except ValidationError as exc:
        raise _format_validation_error(exc, path) from exc


def load_config_from_string(content: str, path: Path | None = None) -> ProjectConfig:
    """Load and validate configuration from a YAML string.

    Useful for testing or when configuration is provided inline.

    Args:
        content: YAML configuration string
        path: Optional path for error messages

    Returns:
        ProjectConfig: The validated configuration

    Raises:
        ConfigError: If the content cannot be parsed or validated
    """
    dummy_path = path or Path("<string>")

    try:
        raw_config = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise _format_yaml_error(exc, dummy_path) from exc

    if raw_config is None:
        raise ConfigError("Configuration is empty", path=dummy_path)

    if not isinstance(raw_config, dict):
        raise ConfigError(
            f"Configuration must be a YAML mapping, got {type(raw_config).__name__}",
            path=dummy_path,
        )

    # Resolve environment variables
    resolved_config = resolve_env_vars(raw_config, dummy_path)

    # Validate through Pydantic
    try:
        return ProjectConfig(**resolved_config)
    except ValidationError as exc:
        raise _format_validation_error(exc, dummy_path) from exc
