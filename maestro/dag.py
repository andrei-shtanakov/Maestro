"""DAG Builder for task dependency management.

This module provides the DAG (Directed Acyclic Graph) implementation for
managing task dependencies, detecting cycles, computing execution order,
and checking for scope overlaps between parallel tasks.
"""

from collections import deque
from dataclasses import dataclass, field
from fnmatch import fnmatch

from maestro.models import TaskConfig


class CycleError(Exception):
    """Raised when a cycle is detected in the task dependency graph."""

    def __init__(self, cycle_path: list[str]) -> None:
        """Initialize CycleError with the detected cycle path.

        Args:
            cycle_path: List of task IDs forming the cycle.
        """
        self.cycle_path = cycle_path
        cycle_str = " -> ".join(cycle_path)
        super().__init__(f"Cyclic dependency detected: {cycle_str}")


@dataclass
class ScopeWarning:
    """Warning for overlapping scopes between parallel tasks.

    Attributes:
        task_ids: Tuple of task IDs with overlapping scopes.
        overlapping_patterns: List of glob patterns that overlap.
        suggestion: Suggested action to resolve the overlap.
    """

    task_ids: tuple[str, str]
    overlapping_patterns: list[tuple[str, str]]
    suggestion: str = field(default="")

    def __post_init__(self) -> None:
        """Set default suggestion if not provided."""
        if not self.suggestion:
            self.suggestion = (
                f"Consider adding a dependency between '{self.task_ids[0]}' and "
                f"'{self.task_ids[1]}', or split the tasks to avoid conflicts."
            )


@dataclass
class DAGNode:
    """A node in the task dependency DAG.

    Attributes:
        task_id: Unique identifier for the task.
        dependencies: Set of task IDs this task depends on.
        dependents: Set of task IDs that depend on this task.
    """

    task_id: str
    dependencies: set[str] = field(default_factory=set)
    dependents: set[str] = field(default_factory=set)


class DAG:
    """Directed Acyclic Graph for task dependency management.

    This class builds a DAG from task configurations, validates that there
    are no cycles, and provides methods for determining task execution order
    and checking for scope overlaps.
    """

    def __init__(self, tasks: list[TaskConfig]) -> None:
        """Build DAG from task configs.

        Args:
            tasks: List of task configurations to build the DAG from.

        Raises:
            CycleError: If a cycle is detected in the dependency graph.
        """
        self._nodes: dict[str, DAGNode] = {}
        self._tasks: dict[str, TaskConfig] = {}

        self._build_graph(tasks)
        self._detect_cycles()

    def _build_graph(self, tasks: list[TaskConfig]) -> None:
        """Build the DAG from task configurations.

        Args:
            tasks: List of task configurations.
        """
        # First pass: create all nodes
        for task in tasks:
            self._nodes[task.id] = DAGNode(task_id=task.id)
            self._tasks[task.id] = task

        # Second pass: add edges (dependencies and dependents)
        for task in tasks:
            node = self._nodes[task.id]
            for dep_id in task.depends_on:
                if dep_id in self._nodes:
                    node.dependencies.add(dep_id)
                    self._nodes[dep_id].dependents.add(task.id)

    def _detect_cycles(self) -> None:
        """Detect cycles using Kahn's algorithm.

        Kahn's algorithm works by repeatedly removing nodes with no incoming
        edges (in-degree 0). If the graph is acyclic, all nodes will be
        removed. If nodes remain, they form a cycle.

        Raises:
            CycleError: If a cycle is detected.
        """
        # Calculate in-degrees
        in_degree: dict[str, int] = {
            node_id: len(node.dependencies) for node_id, node in self._nodes.items()
        }

        # Queue of nodes with no incoming edges
        queue: deque[str] = deque()
        for node_id, degree in in_degree.items():
            if degree == 0:
                queue.append(node_id)

        processed_count = 0

        while queue:
            node_id = queue.popleft()
            processed_count += 1

            # Reduce in-degree for all dependents
            for dependent_id in self._nodes[node_id].dependents:
                in_degree[dependent_id] -= 1
                if in_degree[dependent_id] == 0:
                    queue.append(dependent_id)

        # If not all nodes processed, there's a cycle
        if processed_count != len(self._nodes):
            cycle_path = self._find_cycle_path()
            raise CycleError(cycle_path)

    def _find_cycle_path(self) -> list[str]:
        """Find and return the path of a cycle using DFS.

        Returns:
            List of task IDs forming the cycle.
        """
        visited: set[str] = set()
        rec_stack: set[str] = set()

        def dfs(node_id: str, path: list[str]) -> list[str] | None:
            visited.add(node_id)
            rec_stack.add(node_id)
            path.append(node_id)

            for dep_id in self._nodes[node_id].dependencies:
                if dep_id not in visited:
                    result = dfs(dep_id, path)
                    if result:
                        return result
                elif dep_id in rec_stack:
                    # Found cycle
                    cycle_start = path.index(dep_id)
                    return [*path[cycle_start:], dep_id]

            path.pop()
            rec_stack.remove(node_id)
            return None

        for node_id in self._nodes:
            if node_id not in visited:
                result = dfs(node_id, [])
                if result:
                    return result

        return []

    def topological_sort(self) -> list[str]:
        """Return tasks in topological order (execution order).

        Uses Kahn's algorithm to produce a topological ordering where
        tasks with no dependencies come first, followed by tasks whose
        dependencies have been satisfied.

        Returns:
            List of task IDs in topological order.
        """
        # Calculate in-degrees
        in_degree: dict[str, int] = {
            node_id: len(node.dependencies) for node_id, node in self._nodes.items()
        }

        # Queue of nodes with no incoming edges
        queue: deque[str] = deque()
        for node_id, degree in in_degree.items():
            if degree == 0:
                queue.append(node_id)

        result: list[str] = []

        while queue:
            node_id = queue.popleft()
            result.append(node_id)

            # Reduce in-degree for all dependents
            for dependent_id in self._nodes[node_id].dependents:
                in_degree[dependent_id] -= 1
                if in_degree[dependent_id] == 0:
                    queue.append(dependent_id)

        return result

    def get_ready_tasks(self, completed: set[str]) -> list[str]:
        """Return task IDs that are ready to run.

        A task is ready when all its dependencies are in the completed set.

        Args:
            completed: Set of task IDs that have been completed.

        Returns:
            List of task IDs that are ready to run, sorted by priority
            (highest first).
        """
        ready: list[str] = []

        for node_id, node in self._nodes.items():
            # Skip already completed tasks
            if node_id in completed:
                continue

            # Check if all dependencies are completed
            if node.dependencies <= completed:
                ready.append(node_id)

        # Sort by priority (highest first)
        ready.sort(key=lambda tid: self._tasks[tid].priority, reverse=True)

        return ready

    def check_scope_overlaps(self) -> list[ScopeWarning]:
        """Check for overlapping scopes between parallel tasks.

        Two tasks are considered parallel if neither depends on the other
        (directly or transitively). If parallel tasks have overlapping
        scopes, there's a risk of git conflicts.

        Returns:
            List of ScopeWarning objects for each detected overlap.
        """
        warnings: list[ScopeWarning] = []
        task_ids = list(self._nodes.keys())

        # Find all pairs of parallel tasks
        for i, task_id_a in enumerate(task_ids):
            for task_id_b in task_ids[i + 1 :]:
                if self._are_parallel(task_id_a, task_id_b):
                    overlaps = self._find_scope_overlaps(task_id_a, task_id_b)
                    if overlaps:
                        warnings.append(
                            ScopeWarning(
                                task_ids=(task_id_a, task_id_b),
                                overlapping_patterns=overlaps,
                            )
                        )

        return warnings

    def _are_parallel(self, task_a: str, task_b: str) -> bool:
        """Check if two tasks can run in parallel.

        Two tasks are parallel if neither depends on the other
        (directly or transitively).

        Args:
            task_a: First task ID.
            task_b: Second task ID.

        Returns:
            True if tasks can run in parallel.
        """
        # Get all transitive closures (dependencies and dependents)
        closure_a = self._get_transitive_closure(task_a)
        closure_b = self._get_transitive_closure(task_b)

        # Tasks are parallel if neither is in the other's transitive closure
        return task_b not in closure_a and task_a not in closure_b

    def _get_transitive_closure(self, task_id: str) -> set[str]:
        """Get all tasks in the transitive closure (dependencies and dependents).

        This includes both upstream (dependencies) and downstream (dependents)
        to determine if two tasks are in the same dependency chain.

        Args:
            task_id: Task ID to get closure for.

        Returns:
            Set of all task IDs connected to this task in either direction.
        """
        result: set[str] = set()
        queue: deque[str] = deque([task_id])
        visited: set[str] = set()

        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)

            for dep_id in self._nodes[current].dependencies:
                result.add(dep_id)
                queue.append(dep_id)

            for dependent_id in self._nodes[current].dependents:
                result.add(dependent_id)
                queue.append(dependent_id)

        return result

    def _find_scope_overlaps(self, task_a: str, task_b: str) -> list[tuple[str, str]]:
        """Find overlapping scope patterns between two tasks.

        Args:
            task_a: First task ID.
            task_b: Second task ID.

        Returns:
            List of tuples containing overlapping pattern pairs.
        """
        scope_a = self._tasks[task_a].scope
        scope_b = self._tasks[task_b].scope

        overlaps: list[tuple[str, str]] = []

        for pattern_a in scope_a:
            for pattern_b in scope_b:
                if self._patterns_overlap(pattern_a, pattern_b):
                    overlaps.append((pattern_a, pattern_b))

        return overlaps

    def _patterns_overlap(self, pattern_a: str, pattern_b: str) -> bool:
        """Check if two glob patterns could match the same files.

        This is a simplified overlap check that handles common cases:
        - Exact match: "src/foo.py" and "src/foo.py"
        - One matches the other: "src/*" matches "src/foo.py"
        - Both could match same files: "src/*.py" and "src/foo.*"

        Args:
            pattern_a: First glob pattern.
            pattern_b: Second glob pattern.

        Returns:
            True if patterns could match the same files.
        """
        # Exact match
        if pattern_a == pattern_b:
            return True

        # Check if one pattern matches the other (treating one as a path)
        if fnmatch(pattern_a, pattern_b) or fnmatch(pattern_b, pattern_a):
            return True

        # Check for common prefix overlap with wildcards
        # Split patterns into parts
        parts_a = pattern_a.replace("\\", "/").split("/")
        parts_b = pattern_b.replace("\\", "/").split("/")

        # Check if patterns share a common directory structure
        min_len = min(len(parts_a), len(parts_b))
        for i in range(min_len):
            part_a = parts_a[i]
            part_b = parts_b[i]

            # If both parts are wildcards or match each other, continue
            if part_a == part_b:
                continue

            # Check if either part is a wildcard that could match
            if "*" in part_a or "*" in part_b:
                # Check if the wildcard could match the other
                if fnmatch(part_a, part_b) or fnmatch(part_b, part_a):
                    continue
                # Check if wildcards could match same things
                if "*" in part_a and "*" in part_b:
                    # Both have wildcards, might overlap
                    continue

            # Parts don't match and aren't compatible wildcards
            return False

        return True

    def get_node(self, task_id: str) -> DAGNode | None:
        """Get a DAG node by task ID.

        Args:
            task_id: Task ID to look up.

        Returns:
            The DAGNode for the task, or None if not found.
        """
        return self._nodes.get(task_id)

    def get_all_nodes(self) -> dict[str, DAGNode]:
        """Get all DAG nodes.

        Returns:
            Dictionary mapping task IDs to DAGNode objects.
        """
        return self._nodes.copy()

    def __len__(self) -> int:
        """Return the number of tasks in the DAG."""
        return len(self._nodes)

    def __contains__(self, task_id: str) -> bool:
        """Check if a task is in the DAG."""
        return task_id in self._nodes
