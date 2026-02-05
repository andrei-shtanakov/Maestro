"""Tests for the SpawnerRegistry module."""

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from maestro.models import Task
from maestro.spawners import (
    AgentSpawner,
    ClaudeCodeSpawner,
    SpawnerNotFoundError,
    SpawnerRegistry,
    create_default_registry,
)


# =============================================================================
# Test Fixtures
# =============================================================================


class MockSpawner(AgentSpawner):
    """Mock spawner for testing."""

    def __init__(self, agent_type_value: str = "mock") -> None:
        self._agent_type = agent_type_value

    @property
    def agent_type(self) -> str:
        return self._agent_type

    def is_available(self) -> bool:
        return True

    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
    ) -> subprocess.Popen[bytes]:
        return MagicMock()


class AnotherMockSpawner(AgentSpawner):
    """Another mock spawner for testing."""

    @property
    def agent_type(self) -> str:
        return "another_mock"

    def is_available(self) -> bool:
        return False

    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
    ) -> subprocess.Popen[bytes]:
        return MagicMock()


@pytest.fixture
def registry() -> SpawnerRegistry:
    """Provide an empty SpawnerRegistry instance."""
    return SpawnerRegistry()


@pytest.fixture
def mock_spawner() -> MockSpawner:
    """Provide a mock spawner instance."""
    return MockSpawner()


@pytest.fixture
def another_mock_spawner() -> AnotherMockSpawner:
    """Provide another mock spawner instance."""
    return AnotherMockSpawner()


# =============================================================================
# Unit Tests: Registry Initialization
# =============================================================================


class TestRegistryInitialization:
    """Tests for SpawnerRegistry initialization."""

    def test_empty_registry(self, registry: SpawnerRegistry) -> None:
        """Test that new registry is empty."""
        assert len(registry) == 0
        assert registry.spawner_count == 0
        assert registry.agent_types == []

    def test_registry_no_fallback(self, registry: SpawnerRegistry) -> None:
        """Test that new registry has no fallback."""
        assert registry.get_fallback() is None


# =============================================================================
# Unit Tests: Registration
# =============================================================================


class TestRegistration:
    """Tests for spawner registration."""

    def test_register_spawner(
        self,
        registry: SpawnerRegistry,
        mock_spawner: MockSpawner,
    ) -> None:
        """Test registering a spawner."""
        registry.register(mock_spawner)

        assert len(registry) == 1
        assert registry.spawner_count == 1
        assert "mock" in registry.agent_types
        assert "mock" in registry

    def test_register_multiple_spawners(
        self,
        registry: SpawnerRegistry,
        mock_spawner: MockSpawner,
        another_mock_spawner: AnotherMockSpawner,
    ) -> None:
        """Test registering multiple spawners."""
        registry.register(mock_spawner)
        registry.register(another_mock_spawner)

        assert len(registry) == 2
        assert "mock" in registry
        assert "another_mock" in registry

    def test_register_replaces_existing(
        self,
        registry: SpawnerRegistry,
    ) -> None:
        """Test that registering with same agent type replaces existing."""
        spawner1 = MockSpawner("test")
        spawner2 = MockSpawner("test")

        registry.register(spawner1)
        registry.register(spawner2)

        assert len(registry) == 1
        assert registry.get_spawner("test") is spawner2

    def test_register_non_spawner_raises_error(
        self,
        registry: SpawnerRegistry,
    ) -> None:
        """Test that registering non-spawner raises TypeError."""
        with pytest.raises(TypeError, match="Expected AgentSpawner"):
            registry.register("not a spawner")  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="Expected AgentSpawner"):
            registry.register(123)  # type: ignore[arg-type]

    def test_unregister_spawner(
        self,
        registry: SpawnerRegistry,
        mock_spawner: MockSpawner,
    ) -> None:
        """Test unregistering a spawner."""
        registry.register(mock_spawner)
        assert "mock" in registry

        result = registry.unregister("mock")

        assert result is True
        assert "mock" not in registry
        assert len(registry) == 0

    def test_unregister_nonexistent_returns_false(
        self,
        registry: SpawnerRegistry,
    ) -> None:
        """Test unregistering nonexistent spawner returns False."""
        result = registry.unregister("nonexistent")
        assert result is False


# =============================================================================
# Unit Tests: Lookup
# =============================================================================


class TestLookup:
    """Tests for spawner lookup."""

    def test_get_spawner(
        self,
        registry: SpawnerRegistry,
        mock_spawner: MockSpawner,
    ) -> None:
        """Test getting a registered spawner."""
        registry.register(mock_spawner)

        spawner = registry.get_spawner("mock")

        assert spawner is mock_spawner

    def test_get_spawner_not_found_raises_error(
        self,
        registry: SpawnerRegistry,
        mock_spawner: MockSpawner,
    ) -> None:
        """Test getting non-existent spawner raises error."""
        registry.register(mock_spawner)

        with pytest.raises(SpawnerNotFoundError) as exc_info:
            registry.get_spawner("nonexistent")

        assert "nonexistent" in str(exc_info.value)
        assert exc_info.value.agent_type == "nonexistent"
        assert "mock" in exc_info.value.available

    def test_has_spawner(
        self,
        registry: SpawnerRegistry,
        mock_spawner: MockSpawner,
    ) -> None:
        """Test has_spawner method."""
        registry.register(mock_spawner)

        assert registry.has_spawner("mock") is True
        assert registry.has_spawner("nonexistent") is False

    def test_iteration(
        self,
        registry: SpawnerRegistry,
        mock_spawner: MockSpawner,
        another_mock_spawner: AnotherMockSpawner,
    ) -> None:
        """Test iterating over registry."""
        registry.register(mock_spawner)
        registry.register(another_mock_spawner)

        agent_types = list(registry)

        assert "mock" in agent_types
        assert "another_mock" in agent_types


# =============================================================================
# Unit Tests: Fallback Handling
# =============================================================================


class TestFallbackHandling:
    """Tests for fallback spawner handling."""

    def test_set_fallback(
        self,
        registry: SpawnerRegistry,
        mock_spawner: MockSpawner,
    ) -> None:
        """Test setting a fallback spawner."""
        registry.set_fallback(mock_spawner)

        assert registry.get_fallback() is mock_spawner

    def test_fallback_used_for_unknown_type(
        self,
        registry: SpawnerRegistry,
        mock_spawner: MockSpawner,
    ) -> None:
        """Test that fallback is used for unknown agent types."""
        registry.set_fallback(mock_spawner)

        spawner = registry.get_spawner("unknown_type")

        assert spawner is mock_spawner

    def test_fallback_not_used_for_known_type(
        self,
        registry: SpawnerRegistry,
        mock_spawner: MockSpawner,
        another_mock_spawner: AnotherMockSpawner,
    ) -> None:
        """Test that fallback is not used when spawner exists."""
        registry.register(mock_spawner)
        registry.set_fallback(another_mock_spawner)

        spawner = registry.get_spawner("mock")

        assert spawner is mock_spawner

    def test_clear_fallback(
        self,
        registry: SpawnerRegistry,
        mock_spawner: MockSpawner,
    ) -> None:
        """Test clearing the fallback spawner."""
        registry.set_fallback(mock_spawner)
        registry.set_fallback(None)

        assert registry.get_fallback() is None

        with pytest.raises(SpawnerNotFoundError):
            registry.get_spawner("unknown_type")

    def test_set_fallback_non_spawner_raises_error(
        self,
        registry: SpawnerRegistry,
    ) -> None:
        """Test that setting non-spawner as fallback raises TypeError."""
        with pytest.raises(TypeError, match="Expected AgentSpawner"):
            registry.set_fallback("not a spawner")  # type: ignore[arg-type]


# =============================================================================
# Unit Tests: Clear and To Dict
# =============================================================================


class TestClearAndToDict:
    """Tests for clear and to_dict methods."""

    def test_clear(
        self,
        registry: SpawnerRegistry,
        mock_spawner: MockSpawner,
    ) -> None:
        """Test clearing the registry."""
        registry.register(mock_spawner)
        registry.set_fallback(mock_spawner)

        registry.clear()

        assert len(registry) == 0
        assert registry.get_fallback() is None

    def test_to_dict(
        self,
        registry: SpawnerRegistry,
        mock_spawner: MockSpawner,
        another_mock_spawner: AnotherMockSpawner,
    ) -> None:
        """Test converting registry to dict."""
        registry.register(mock_spawner)
        registry.register(another_mock_spawner)

        spawners_dict = registry.to_dict()

        assert isinstance(spawners_dict, dict)
        assert "mock" in spawners_dict
        assert "another_mock" in spawners_dict
        assert spawners_dict["mock"] is mock_spawner
        assert spawners_dict["another_mock"] is another_mock_spawner


# =============================================================================
# Unit Tests: Entry Point Discovery
# =============================================================================


class TestEntryPointDiscovery:
    """Tests for entry point discovery."""

    def test_discover_entry_points_empty(
        self,
        registry: SpawnerRegistry,
    ) -> None:
        """Test discovery when no entry points exist."""
        with patch("importlib.metadata.entry_points", return_value=[]):
            count = registry.discover_entry_points()

        assert count == 0
        assert len(registry) == 0

    def test_discover_entry_points_with_spawner(
        self,
        registry: SpawnerRegistry,
    ) -> None:
        """Test discovery finds valid spawner entry points."""
        mock_ep = MagicMock()
        mock_ep.name = "test_spawner"
        mock_ep.load.return_value = MockSpawner

        with patch("importlib.metadata.entry_points", return_value=[mock_ep]):
            count = registry.discover_entry_points()

        assert count == 1
        assert "mock" in registry

    def test_discover_entry_points_skips_invalid(
        self,
        registry: SpawnerRegistry,
    ) -> None:
        """Test discovery skips invalid entry points."""
        mock_ep = MagicMock()
        mock_ep.name = "invalid_spawner"
        mock_ep.load.return_value = str  # Not an AgentSpawner

        with patch("importlib.metadata.entry_points", return_value=[mock_ep]):
            count = registry.discover_entry_points()

        assert count == 0
        assert len(registry) == 0

    def test_discover_entry_points_handles_load_error(
        self,
        registry: SpawnerRegistry,
    ) -> None:
        """Test discovery handles entry point load errors gracefully."""
        mock_ep = MagicMock()
        mock_ep.name = "broken_spawner"
        mock_ep.load.side_effect = ImportError("Module not found")

        with patch("importlib.metadata.entry_points", return_value=[mock_ep]):
            count = registry.discover_entry_points()

        assert count == 0
        assert len(registry) == 0


# =============================================================================
# Unit Tests: Directory Discovery
# =============================================================================


class TestDirectoryDiscovery:
    """Tests for directory scanning discovery."""

    def test_discover_from_directory_finds_claude_code(
        self,
        registry: SpawnerRegistry,
    ) -> None:
        """Test that directory discovery finds ClaudeCodeSpawner."""
        count = registry.discover_from_directory()

        assert count >= 1
        assert "claude_code" in registry
        assert isinstance(registry.get_spawner("claude_code"), ClaudeCodeSpawner)

    def test_discover_from_directory_custom_path(
        self,
        registry: SpawnerRegistry,
        temp_dir: Path,
    ) -> None:
        """Test discovery from custom directory path."""
        # Create a mock spawner module
        spawner_code = '''
"""Test spawner module."""
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from maestro.spawners.base import AgentSpawner
from maestro.models import Task


class CustomTestSpawner(AgentSpawner):
    """Custom test spawner."""

    @property
    def agent_type(self) -> str:
        return "custom_test"

    def is_available(self) -> bool:
        return True

    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
    ) -> subprocess.Popen[bytes]:
        return MagicMock()
'''
        spawner_file = temp_dir / "custom_spawner.py"
        spawner_file.write_text(spawner_code)

        # Create __init__.py to make it a package
        init_file = temp_dir / "__init__.py"
        init_file.write_text("")

        # Patch pkgutil.iter_modules to return our custom module
        import pkgutil

        original_iter = pkgutil.iter_modules

        def mock_iter(paths: list[str]) -> Any:
            if str(temp_dir) in paths:
                mock_info = MagicMock()
                mock_info.name = "custom_spawner"
                return [mock_info]
            return original_iter(paths)

        with (
            patch("pkgutil.iter_modules", side_effect=mock_iter),
            patch(
                "importlib.import_module",
                side_effect=ImportError("Test module import"),
            ),
        ):
            count = registry.discover_from_directory(
                directory=temp_dir, package="test_package"
            )

        # Import error is handled gracefully
        assert count == 0


# =============================================================================
# Unit Tests: Discover All
# =============================================================================


class TestDiscoverAll:
    """Tests for discover_all method."""

    def test_discover_all_combines_discoveries(
        self,
        registry: SpawnerRegistry,
    ) -> None:
        """Test that discover_all combines both discovery methods."""
        with patch("importlib.metadata.entry_points", return_value=[]):
            count = registry.discover_all()

        # Should at least find ClaudeCodeSpawner from directory scan
        assert count >= 1
        assert "claude_code" in registry


# =============================================================================
# Unit Tests: Create Default Registry
# =============================================================================


class TestCreateDefaultRegistry:
    """Tests for create_default_registry function."""

    def test_create_default_registry(self) -> None:
        """Test creating a default registry with discovered spawners."""
        with patch("importlib.metadata.entry_points", return_value=[]):
            registry = create_default_registry()

        assert isinstance(registry, SpawnerRegistry)
        assert "claude_code" in registry


# =============================================================================
# Unit Tests: SpawnerNotFoundError
# =============================================================================


class TestSpawnerNotFoundError:
    """Tests for SpawnerNotFoundError exception."""

    def test_error_message_without_available(self) -> None:
        """Test error message without available types."""
        error = SpawnerNotFoundError("unknown")

        assert "unknown" in str(error)
        assert error.agent_type == "unknown"
        assert error.available == []

    def test_error_message_with_available(self) -> None:
        """Test error message with available types."""
        error = SpawnerNotFoundError("unknown", ["mock", "claude_code"])

        assert "unknown" in str(error)
        assert "mock" in str(error)
        assert "claude_code" in str(error)
        assert error.available == ["mock", "claude_code"]


# =============================================================================
# Integration Tests
# =============================================================================


class TestRegistryIntegration:
    """Integration tests for the registry with real spawners."""

    def test_registry_with_claude_code_spawner(
        self,
        registry: SpawnerRegistry,
    ) -> None:
        """Test registry with actual ClaudeCodeSpawner."""
        spawner = ClaudeCodeSpawner()
        registry.register(spawner)

        retrieved = registry.get_spawner("claude_code")

        assert isinstance(retrieved, ClaudeCodeSpawner)
        assert retrieved.agent_type == "claude_code"

    def test_registry_to_dict_works_with_scheduler(
        self,
        registry: SpawnerRegistry,
    ) -> None:
        """Test that to_dict output is compatible with Scheduler."""
        registry.discover_all()

        spawners_dict = registry.to_dict()

        # Verify dict structure matches what Scheduler expects
        assert isinstance(spawners_dict, dict)
        for agent_type, spawner in spawners_dict.items():
            assert isinstance(agent_type, str)
            assert hasattr(spawner, "agent_type")
            assert hasattr(spawner, "is_available")
            assert hasattr(spawner, "spawn")
