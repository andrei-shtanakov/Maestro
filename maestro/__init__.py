"""Maestro - AI Agent Orchestrator for parallel coding agent coordination."""

__version__ = "0.1.0"

from maestro.config import ConfigError, load_config, load_config_from_string


__all__ = [
    "ConfigError",
    "load_config",
    "load_config_from_string",
]
