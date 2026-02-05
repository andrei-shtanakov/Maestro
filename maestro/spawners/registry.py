"""Spawner registry for dynamic agent spawner discovery and management.

This module provides the SpawnerRegistry class that enables:
- Manual registration of spawner instances
- Auto-discovery of spawners via entry points
- Auto-discovery of spawners via directory scanning
- Lookup of spawners by agent type
- Fallback handling for unknown agent types
"""

import importlib
import importlib.metadata
import inspect
import logging
import pkgutil
from pathlib import Path
from typing import TYPE_CHECKING

from maestro.spawners.base import AgentSpawner


if TYPE_CHECKING:
    from collections.abc import Iterator


logger = logging.getLogger(__name__)

# Entry point group name for spawner plugins
SPAWNER_ENTRY_POINT_GROUP = "maestro.spawners"


class SpawnerNotFoundError(Exception):
    """Raised when a spawner for a given agent type is not found."""

    def __init__(self, agent_type: str, available: list[str] | None = None) -> None:
        self.agent_type = agent_type
        self.available = available or []
        msg = f"No spawner found for agent type '{agent_type}'"
        if self.available:
            msg += f". Available types: {', '.join(self.available)}"
        super().__init__(msg)


class SpawnerRegistry:
    """Registry for agent spawners with auto-discovery support.

    The registry manages spawner instances and provides lookup by agent type.
    It supports multiple discovery mechanisms:
    1. Manual registration via register()
    2. Entry point discovery via discover_entry_points()
    3. Directory scanning via discover_from_directory()

    Example:
        >>> registry = SpawnerRegistry()
        >>> registry.discover_entry_points()
        >>> spawner = registry.get_spawner("claude_code")
    """

    def __init__(self) -> None:
        """Initialize an empty spawner registry."""
        self._spawners: dict[str, AgentSpawner] = {}
        self._fallback: AgentSpawner | None = None

    @property
    def agent_types(self) -> list[str]:
        """Return list of registered agent types."""
        return list(self._spawners.keys())

    @property
    def spawner_count(self) -> int:
        """Return number of registered spawners."""
        return len(self._spawners)

    def register(self, spawner: AgentSpawner) -> None:
        """Register a spawner instance.

        The spawner's agent_type property determines its registration key.
        If a spawner for the same agent type is already registered,
        it will be replaced with a warning.

        Args:
            spawner: The spawner instance to register.

        Raises:
            TypeError: If spawner is not an AgentSpawner instance.
        """
        if not isinstance(spawner, AgentSpawner):
            raise TypeError(
                f"Expected AgentSpawner instance, got {type(spawner).__name__}"
            )

        agent_type = spawner.agent_type
        if agent_type in self._spawners:
            logger.warning("Replacing existing spawner for agent type '%s'", agent_type)

        self._spawners[agent_type] = spawner
        logger.debug("Registered spawner for agent type '%s'", agent_type)

    def unregister(self, agent_type: str) -> bool:
        """Unregister a spawner by agent type.

        Args:
            agent_type: The agent type to unregister.

        Returns:
            True if a spawner was unregistered, False if not found.
        """
        if agent_type in self._spawners:
            del self._spawners[agent_type]
            logger.debug("Unregistered spawner for agent type '%s'", agent_type)
            return True
        return False

    def get_spawner(self, agent_type: str) -> AgentSpawner:
        """Get a spawner by agent type.

        If the agent type is not found and a fallback is set,
        returns the fallback spawner.

        Args:
            agent_type: The agent type to look up.

        Returns:
            The spawner for the given agent type.

        Raises:
            SpawnerNotFoundError: If no spawner is found for the agent type
                and no fallback is configured.
        """
        spawner = self._spawners.get(agent_type)
        if spawner is not None:
            return spawner

        if self._fallback is not None:
            logger.debug(
                "Using fallback spawner for unknown agent type '%s'", agent_type
            )
            return self._fallback

        raise SpawnerNotFoundError(agent_type, self.agent_types)

    def has_spawner(self, agent_type: str) -> bool:
        """Check if a spawner is registered for the given agent type.

        Args:
            agent_type: The agent type to check.

        Returns:
            True if a spawner is registered, False otherwise.
        """
        return agent_type in self._spawners

    def set_fallback(self, spawner: AgentSpawner | None) -> None:
        """Set the fallback spawner for unknown agent types.

        Args:
            spawner: The fallback spawner instance, or None to disable fallback.

        Raises:
            TypeError: If spawner is not an AgentSpawner instance or None.
        """
        if spawner is not None and not isinstance(spawner, AgentSpawner):
            raise TypeError(
                f"Expected AgentSpawner instance or None, got {type(spawner).__name__}"
            )
        self._fallback = spawner
        if spawner is not None:
            logger.debug(
                "Set fallback spawner with agent type '%s'", spawner.agent_type
            )
        else:
            logger.debug("Cleared fallback spawner")

    def get_fallback(self) -> AgentSpawner | None:
        """Get the current fallback spawner.

        Returns:
            The fallback spawner if set, None otherwise.
        """
        return self._fallback

    def discover_entry_points(self, group: str | None = None) -> int:
        """Discover and register spawners from entry points.

        This method scans Python entry points for spawner classes and
        instantiates them. Entry points should return AgentSpawner subclasses.

        Args:
            group: Entry point group name. Defaults to 'maestro.spawners'.

        Returns:
            Number of spawners discovered and registered.
        """
        group = group or SPAWNER_ENTRY_POINT_GROUP
        count = 0

        eps = importlib.metadata.entry_points(group=group)

        for ep in eps:
            try:
                spawner_class = ep.load()
                if not (
                    inspect.isclass(spawner_class)
                    and issubclass(spawner_class, AgentSpawner)
                    and spawner_class is not AgentSpawner
                ):
                    logger.warning(
                        "Entry point '%s' did not return an AgentSpawner subclass",
                        ep.name,
                    )
                    continue

                spawner = spawner_class()
                self.register(spawner)
                count += 1
                logger.info(
                    "Discovered spawner '%s' from entry point '%s'",
                    spawner.agent_type,
                    ep.name,
                )
            except Exception as e:
                logger.warning(
                    "Failed to load spawner from entry point '%s': %s", ep.name, e
                )

        return count

    def discover_from_directory(
        self,
        directory: Path | None = None,
        package: str | None = None,
    ) -> int:
        """Discover and register spawners from a directory.

        Scans Python modules in the given directory for AgentSpawner subclasses
        and instantiates them. This provides a fallback discovery mechanism
        when entry points are not available.

        Security Note:
            This method imports and executes Python code from the specified
            directory. Only use with trusted directories that you control.
            Do not pass user-provided or untrusted paths to this method.

        Args:
            directory: Directory to scan. Defaults to the spawners package directory.
            package: Package name for importing. Defaults to 'maestro.spawners'.

        Returns:
            Number of spawners discovered and registered.
        """
        package = package or "maestro.spawners"

        if directory is None:
            # Use the spawners package directory
            directory = Path(__file__).parent

        count = 0
        discovered_classes: set[type] = set()

        for module_info in pkgutil.iter_modules([str(directory)]):
            # Skip private modules and this module
            if module_info.name.startswith("_") or module_info.name in (
                "base",
                "registry",
            ):
                continue

            module_name = f"{package}.{module_info.name}"

            try:
                module = importlib.import_module(module_name)

                # Find AgentSpawner subclasses in the module
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    if (
                        issubclass(obj, AgentSpawner)
                        and obj is not AgentSpawner
                        and obj not in discovered_classes
                    ):
                        # Check that the class is defined in this module
                        if obj.__module__ != module_name:
                            continue

                        discovered_classes.add(obj)
                        try:
                            spawner = obj()
                            self.register(spawner)
                            count += 1
                            logger.info(
                                "Discovered spawner '%s' from module '%s'",
                                spawner.agent_type,
                                module_name,
                            )
                        except Exception as e:
                            logger.warning(
                                "Failed to instantiate spawner class '%s' from '%s': %s",
                                name,
                                module_name,
                                e,
                            )
            except Exception as e:
                logger.warning(
                    "Failed to import module '%s' for spawner discovery: %s",
                    module_name,
                    e,
                )

        return count

    def discover_all(self) -> int:
        """Run all discovery mechanisms.

        This method runs entry point discovery first, then directory scanning.
        Spawners discovered via entry points take precedence.

        Returns:
            Total number of spawners discovered.
        """
        count = self.discover_entry_points()
        count += self.discover_from_directory()
        return count

    def to_dict(self) -> dict[str, AgentSpawner]:
        """Return spawners as a dictionary.

        This is useful for passing to the Scheduler which expects
        a dict[str, SpawnerProtocol].

        Returns:
            Dictionary mapping agent types to spawner instances.
        """
        return dict(self._spawners)

    def clear(self) -> None:
        """Remove all registered spawners and clear the fallback."""
        self._spawners.clear()
        self._fallback = None
        logger.debug("Cleared all spawners from registry")

    def __len__(self) -> int:
        """Return the number of registered spawners."""
        return len(self._spawners)

    def __contains__(self, agent_type: str) -> bool:
        """Check if an agent type is registered."""
        return agent_type in self._spawners

    def __iter__(self) -> "Iterator[str]":
        """Iterate over registered agent types."""
        return iter(self._spawners)


def create_default_registry() -> SpawnerRegistry:
    """Create a registry with default spawners.

    This function creates a new SpawnerRegistry and runs auto-discovery
    to find all available spawners.

    Returns:
        A SpawnerRegistry populated with discovered spawners.
    """
    registry = SpawnerRegistry()
    registry.discover_all()
    return registry
