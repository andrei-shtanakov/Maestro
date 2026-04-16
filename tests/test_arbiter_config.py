"""Tests for ArbiterConfig pydantic model."""

import pytest
from pydantic import ValidationError

from maestro.models import ArbiterConfig, ArbiterMode


class TestArbiterMode:
    def test_values(self) -> None:
        assert ArbiterMode.ADVISORY.value == "advisory"
        assert ArbiterMode.AUTHORITATIVE.value == "authoritative"


class TestArbiterConfigDefaults:
    def test_disabled_by_default(self) -> None:
        cfg = ArbiterConfig()
        assert cfg.enabled is False
        assert cfg.mode is ArbiterMode.ADVISORY
        assert cfg.optional is False
        assert cfg.timeout_ms == 500
        assert cfg.reconnect_interval_s == 60
        assert cfg.abandon_outcome_after_s == 300
        assert cfg.binary_path is None

    def test_disabled_allows_missing_paths(self) -> None:
        ArbiterConfig(enabled=False, binary_path=None, tree_path=None)


class TestArbiterConfigValidationWhenEnabled:
    def test_missing_binary_path_rejected(self) -> None:
        with pytest.raises(ValidationError, match="binary_path"):
            ArbiterConfig(enabled=True, config_dir="/c", tree_path="/t")

    def test_missing_config_dir_rejected(self) -> None:
        with pytest.raises(ValidationError, match="config_dir"):
            ArbiterConfig(enabled=True, binary_path="/b", tree_path="/t")

    def test_missing_tree_path_rejected(self) -> None:
        with pytest.raises(ValidationError, match="tree_path"):
            ArbiterConfig(enabled=True, binary_path="/b", config_dir="/c")

    def test_fully_populated_passes(self) -> None:
        cfg = ArbiterConfig(
            enabled=True,
            binary_path="/usr/local/bin/arbiter",
            config_dir="/etc/arbiter",
            tree_path="/var/lib/arbiter/tree.json",
        )
        assert cfg.binary_path == "/usr/local/bin/arbiter"


class TestArbiterConfigUnresolvedEnvVar:
    """Config parser only supports ${VAR}; ${VAR:-default} leaks through unresolved."""

    def test_unresolved_default_syntax_rejected_in_binary_path(self) -> None:
        with pytest.raises(ValidationError, match="env var substitution"):
            ArbiterConfig(
                enabled=True,
                binary_path="${ARBITER_BIN:-/fallback}",
                config_dir="/c",
                tree_path="/t",
            )

    def test_unresolved_plain_var_rejected(self) -> None:
        with pytest.raises(ValidationError, match="env var substitution"):
            ArbiterConfig(
                enabled=True,
                binary_path="${ARBITER_BIN}",  # did not get resolved
                config_dir="/c",
                tree_path="/t",
            )

    def test_absolute_path_no_dollar_passes(self) -> None:
        ArbiterConfig(
            enabled=True,
            binary_path="/opt/arbiter/arbiter-mcp",
            config_dir="/etc/arbiter",
            tree_path="/etc/arbiter/tree.json",
        )
