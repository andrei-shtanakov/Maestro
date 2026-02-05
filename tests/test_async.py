"""Tests for async functionality using pytest-asyncio."""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest


class TestAsyncBasics:
    """Tests to verify pytest-asyncio configuration works correctly."""

    async def test_simple_async_function(self) -> None:
        """Test that a simple async function works."""
        result = await asyncio.sleep(0.001, result="done")
        assert result == "done"

    async def test_async_gather(self) -> None:
        """Test that asyncio.gather works correctly."""

        async def slow_add(a: int, b: int) -> int:
            await asyncio.sleep(0.001)
            return a + b

        results = await asyncio.gather(
            slow_add(1, 2),
            slow_add(3, 4),
            slow_add(5, 6),
        )
        assert results == [3, 7, 11]

    async def test_async_timeout(self) -> None:
        """Test that async timeouts work correctly."""

        async def slow_operation() -> str:
            await asyncio.sleep(0.001)
            return "completed"

        result = await asyncio.wait_for(slow_operation(), timeout=1.0)
        assert result == "completed"


class TestAsyncFixtures:
    """Tests for async fixture functionality."""

    async def test_temp_db_path_fixture(self, temp_db_path: Path) -> None:
        """Test that async temp_db_path fixture works."""
        assert temp_db_path.suffix == ".db"
        assert "test_maestro" in temp_db_path.name


class TestAsyncContextManagers:
    """Tests for async context managers."""

    async def test_async_context_manager(self) -> None:
        """Test that async context managers work correctly."""

        class AsyncResource:
            def __init__(self) -> None:
                self.opened = False
                self.closed = False

            async def __aenter__(self) -> "AsyncResource":
                await asyncio.sleep(0.001)
                self.opened = True
                return self

            async def __aexit__(self, *args: object) -> None:
                await asyncio.sleep(0.001)
                self.closed = True

        async with AsyncResource() as resource:
            assert resource.opened
            assert not resource.closed

        assert resource.closed


class TestAsyncGenerators:
    """Tests for async generators."""

    async def test_async_generator(self) -> None:
        """Test that async generators work correctly."""

        async def async_range(n: int) -> AsyncGenerator[int, None]:
            for i in range(n):
                await asyncio.sleep(0.001)
                yield i

        results = []
        async for value in async_range(5):
            results.append(value)

        assert results == [0, 1, 2, 3, 4]


@pytest.mark.slow
class TestAsyncSlowOperations:
    """Tests marked as slow for async operations."""

    async def test_multiple_concurrent_tasks(self) -> None:
        """Test running multiple concurrent async tasks."""
        completed = []

        async def task(name: str, delay: float) -> str:
            await asyncio.sleep(delay)
            completed.append(name)
            return name

        tasks = [
            asyncio.create_task(task("a", 0.01)),
            asyncio.create_task(task("b", 0.01)),
            asyncio.create_task(task("c", 0.01)),
        ]

        results = await asyncio.gather(*tasks)

        assert len(results) == 3
        assert set(results) == {"a", "b", "c"}
        assert len(completed) == 3
