"""Unit tests for the DAG Builder module.

Tests cover:
- Simple DAG construction
- Cycle detection
- Topological sort
- Ready tasks calculation
- Scope overlap warning
"""

import pytest

from maestro.dag import DAG, CycleError, DAGNode, ScopeWarning
from maestro.models import TaskConfig


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def simple_task() -> TaskConfig:
    """Create a simple task with no dependencies."""
    return TaskConfig(
        id="task-a",
        title="Task A",
        prompt="Do task A",
        scope=["src/a/**/*.py"],
    )


@pytest.fixture
def task_with_dependency() -> TaskConfig:
    """Create a task that depends on task-a."""
    return TaskConfig(
        id="task-b",
        title="Task B",
        prompt="Do task B",
        depends_on=["task-a"],
        scope=["src/b/**/*.py"],
    )


@pytest.fixture
def three_task_chain() -> list[TaskConfig]:
    """Create a chain of three tasks: A -> B -> C."""
    return [
        TaskConfig(
            id="task-a",
            title="Task A",
            prompt="Do task A",
            scope=["src/a/**/*.py"],
        ),
        TaskConfig(
            id="task-b",
            title="Task B",
            prompt="Do task B",
            depends_on=["task-a"],
            scope=["src/b/**/*.py"],
        ),
        TaskConfig(
            id="task-c",
            title="Task C",
            prompt="Do task C",
            depends_on=["task-b"],
            scope=["src/c/**/*.py"],
        ),
    ]


@pytest.fixture
def diamond_dag() -> list[TaskConfig]:
    """Create a diamond-shaped DAG: A -> B, A -> C, B -> D, C -> D."""
    return [
        TaskConfig(
            id="task-a",
            title="Task A",
            prompt="Do task A",
            scope=["src/a/**/*.py"],
        ),
        TaskConfig(
            id="task-b",
            title="Task B",
            prompt="Do task B",
            depends_on=["task-a"],
            scope=["src/b/**/*.py"],
        ),
        TaskConfig(
            id="task-c",
            title="Task C",
            prompt="Do task C",
            depends_on=["task-a"],
            scope=["src/c/**/*.py"],
        ),
        TaskConfig(
            id="task-d",
            title="Task D",
            prompt="Do task D",
            depends_on=["task-b", "task-c"],
            scope=["src/d/**/*.py"],
        ),
    ]


@pytest.fixture
def parallel_tasks_same_scope() -> list[TaskConfig]:
    """Create parallel tasks with overlapping scopes."""
    return [
        TaskConfig(
            id="task-a",
            title="Task A",
            prompt="Do task A",
            scope=["src/auth/**/*.py", "src/common/**/*.py"],
        ),
        TaskConfig(
            id="task-b",
            title="Task B",
            prompt="Do task B",
            scope=["src/auth/**/*.py", "tests/**/*.py"],
        ),
    ]


@pytest.fixture
def tasks_with_priorities() -> list[TaskConfig]:
    """Create independent tasks with different priorities."""
    return [
        TaskConfig(
            id="task-low",
            title="Low Priority",
            prompt="Low priority task",
            priority=-10,
        ),
        TaskConfig(
            id="task-high",
            title="High Priority",
            prompt="High priority task",
            priority=10,
        ),
        TaskConfig(
            id="task-medium",
            title="Medium Priority",
            prompt="Medium priority task",
            priority=0,
        ),
    ]


# =============================================================================
# Test: Simple DAG Construction
# =============================================================================


class TestDAGConstruction:
    """Tests for DAG construction from task configs."""

    def test_empty_dag(self) -> None:
        """DAG can be created with no tasks."""
        dag = DAG([])
        assert len(dag) == 0

    def test_single_task(self, simple_task: TaskConfig) -> None:
        """DAG can be created with a single task."""
        dag = DAG([simple_task])
        assert len(dag) == 1
        assert "task-a" in dag

    def test_single_task_node_structure(self, simple_task: TaskConfig) -> None:
        """Single task node has correct structure."""
        dag = DAG([simple_task])
        node = dag.get_node("task-a")
        assert node is not None
        assert node.task_id == "task-a"
        assert node.dependencies == set()
        assert node.dependents == set()

    def test_two_task_dependency(
        self, simple_task: TaskConfig, task_with_dependency: TaskConfig
    ) -> None:
        """DAG correctly captures dependency relationship."""
        dag = DAG([simple_task, task_with_dependency])
        assert len(dag) == 2

        node_a = dag.get_node("task-a")
        node_b = dag.get_node("task-b")

        assert node_a is not None
        assert node_b is not None
        assert node_a.dependents == {"task-b"}
        assert node_b.dependencies == {"task-a"}

    def test_chain_construction(self, three_task_chain: list[TaskConfig]) -> None:
        """DAG correctly builds a chain of dependencies."""
        dag = DAG(three_task_chain)
        assert len(dag) == 3

        node_a = dag.get_node("task-a")
        node_b = dag.get_node("task-b")
        node_c = dag.get_node("task-c")

        assert node_a is not None and node_b is not None and node_c is not None

        assert node_a.dependencies == set()
        assert node_a.dependents == {"task-b"}

        assert node_b.dependencies == {"task-a"}
        assert node_b.dependents == {"task-c"}

        assert node_c.dependencies == {"task-b"}
        assert node_c.dependents == set()

    def test_diamond_construction(self, diamond_dag: list[TaskConfig]) -> None:
        """DAG correctly builds a diamond-shaped dependency graph."""
        dag = DAG(diamond_dag)
        assert len(dag) == 4

        node_a = dag.get_node("task-a")
        node_b = dag.get_node("task-b")
        node_c = dag.get_node("task-c")
        node_d = dag.get_node("task-d")

        assert all(n is not None for n in [node_a, node_b, node_c, node_d])

        assert node_a.dependencies == set()  # type: ignore[union-attr]
        assert node_a.dependents == {"task-b", "task-c"}  # type: ignore[union-attr]

        assert node_b.dependencies == {"task-a"}  # type: ignore[union-attr]
        assert node_d.dependencies == {"task-b", "task-c"}  # type: ignore[union-attr]

    def test_get_all_nodes(self, diamond_dag: list[TaskConfig]) -> None:
        """get_all_nodes returns copy of all nodes."""
        dag = DAG(diamond_dag)
        nodes = dag.get_all_nodes()

        assert len(nodes) == 4
        assert set(nodes.keys()) == {"task-a", "task-b", "task-c", "task-d"}

    def test_contains(self, simple_task: TaskConfig) -> None:
        """DAG supports 'in' operator."""
        dag = DAG([simple_task])
        assert "task-a" in dag
        assert "task-nonexistent" not in dag

    def test_get_node_nonexistent(self, simple_task: TaskConfig) -> None:
        """get_node returns None for nonexistent task."""
        dag = DAG([simple_task])
        assert dag.get_node("nonexistent") is None


# =============================================================================
# Test: Cycle Detection
# =============================================================================


class TestCycleDetection:
    """Tests for cycle detection in the DAG."""

    def test_no_cycle_simple(self, simple_task: TaskConfig) -> None:
        """No cycle error for single task."""
        dag = DAG([simple_task])
        assert len(dag) == 1

    def test_no_cycle_chain(self, three_task_chain: list[TaskConfig]) -> None:
        """No cycle error for linear chain."""
        dag = DAG(three_task_chain)
        assert len(dag) == 3

    def test_no_cycle_diamond(self, diamond_dag: list[TaskConfig]) -> None:
        """No cycle error for diamond DAG."""
        dag = DAG(diamond_dag)
        assert len(dag) == 4

    def test_direct_cycle_two_tasks(self) -> None:
        """Detect direct cycle between two tasks: A -> B -> A."""
        tasks = [
            TaskConfig(
                id="task-a",
                title="Task A",
                prompt="Do A",
                depends_on=["task-b"],
            ),
            TaskConfig(
                id="task-b",
                title="Task B",
                prompt="Do B",
                depends_on=["task-a"],
            ),
        ]
        with pytest.raises(CycleError) as exc_info:
            DAG(tasks)

        assert len(exc_info.value.cycle_path) >= 2
        assert "Cyclic dependency detected" in str(exc_info.value)

    def test_indirect_cycle_three_tasks(self) -> None:
        """Detect indirect cycle: A -> B -> C -> A."""
        tasks = [
            TaskConfig(
                id="task-a",
                title="Task A",
                prompt="Do A",
                depends_on=["task-c"],
            ),
            TaskConfig(
                id="task-b",
                title="Task B",
                prompt="Do B",
                depends_on=["task-a"],
            ),
            TaskConfig(
                id="task-c",
                title="Task C",
                prompt="Do C",
                depends_on=["task-b"],
            ),
        ]
        with pytest.raises(CycleError) as exc_info:
            DAG(tasks)

        assert len(exc_info.value.cycle_path) >= 3

    def test_cycle_error_attributes(self) -> None:
        """CycleError contains the cycle path."""
        tasks = [
            TaskConfig(
                id="a",
                title="A",
                prompt="Do A",
                depends_on=["b"],
            ),
            TaskConfig(
                id="b",
                title="B",
                prompt="Do B",
                depends_on=["a"],
            ),
        ]
        with pytest.raises(CycleError) as exc_info:
            DAG(tasks)

        error = exc_info.value
        assert hasattr(error, "cycle_path")
        assert isinstance(error.cycle_path, list)
        assert len(error.cycle_path) >= 2

    def test_cycle_with_multiple_components(self) -> None:
        """Detect cycle even when there are independent components."""
        tasks = [
            # Independent task
            TaskConfig(id="independent", title="Independent", prompt="Do it"),
            # Cycle: cycle-a -> cycle-b -> cycle-a
            TaskConfig(
                id="cycle-a",
                title="Cycle A",
                prompt="Do A",
                depends_on=["cycle-b"],
            ),
            TaskConfig(
                id="cycle-b",
                title="Cycle B",
                prompt="Do B",
                depends_on=["cycle-a"],
            ),
        ]
        with pytest.raises(CycleError):
            DAG(tasks)


# =============================================================================
# Test: Topological Sort
# =============================================================================


class TestTopologicalSort:
    """Tests for topological sorting of the DAG."""

    def test_empty_dag(self) -> None:
        """Topological sort of empty DAG returns empty list."""
        dag = DAG([])
        assert dag.topological_sort() == []

    def test_single_task(self, simple_task: TaskConfig) -> None:
        """Topological sort of single task returns that task."""
        dag = DAG([simple_task])
        assert dag.topological_sort() == ["task-a"]

    def test_chain_order(self, three_task_chain: list[TaskConfig]) -> None:
        """Topological sort respects chain ordering."""
        dag = DAG(three_task_chain)
        result = dag.topological_sort()

        # A must come before B, B must come before C
        assert result.index("task-a") < result.index("task-b")
        assert result.index("task-b") < result.index("task-c")

    def test_diamond_order(self, diamond_dag: list[TaskConfig]) -> None:
        """Topological sort respects diamond DAG ordering."""
        dag = DAG(diamond_dag)
        result = dag.topological_sort()

        # A must come first
        # B and C must come after A
        # D must come after B and C
        assert result.index("task-a") < result.index("task-b")
        assert result.index("task-a") < result.index("task-c")
        assert result.index("task-b") < result.index("task-d")
        assert result.index("task-c") < result.index("task-d")

    def test_independent_tasks(self) -> None:
        """Independent tasks can appear in any order."""
        tasks = [
            TaskConfig(id="task-a", title="A", prompt="Do A"),
            TaskConfig(id="task-b", title="B", prompt="Do B"),
            TaskConfig(id="task-c", title="C", prompt="Do C"),
        ]
        dag = DAG(tasks)
        result = dag.topological_sort()

        # All tasks should appear exactly once
        assert set(result) == {"task-a", "task-b", "task-c"}
        assert len(result) == 3

    def test_complex_dag(self) -> None:
        """Topological sort handles complex DAG structure."""
        # Structure:
        #   A -> B -> D -> F
        #   A -> C -> E -> F
        #   B -> E
        tasks = [
            TaskConfig(id="a", title="A", prompt="Do A"),
            TaskConfig(id="b", title="B", prompt="Do B", depends_on=["a"]),
            TaskConfig(id="c", title="C", prompt="Do C", depends_on=["a"]),
            TaskConfig(id="d", title="D", prompt="Do D", depends_on=["b"]),
            TaskConfig(id="e", title="E", prompt="Do E", depends_on=["b", "c"]),
            TaskConfig(id="f", title="F", prompt="Do F", depends_on=["d", "e"]),
        ]
        dag = DAG(tasks)
        result = dag.topological_sort()

        # Verify all ordering constraints
        assert result.index("a") < result.index("b")
        assert result.index("a") < result.index("c")
        assert result.index("b") < result.index("d")
        assert result.index("b") < result.index("e")
        assert result.index("c") < result.index("e")
        assert result.index("d") < result.index("f")
        assert result.index("e") < result.index("f")


# =============================================================================
# Test: Ready Tasks Calculation
# =============================================================================


class TestReadyTasks:
    """Tests for get_ready_tasks() calculation."""

    def test_empty_dag(self) -> None:
        """Empty DAG returns empty ready list."""
        dag = DAG([])
        assert dag.get_ready_tasks(set()) == []

    def test_single_task_ready(self, simple_task: TaskConfig) -> None:
        """Single task with no dependencies is ready."""
        dag = DAG([simple_task])
        ready = dag.get_ready_tasks(set())
        assert ready == ["task-a"]

    def test_single_task_completed(self, simple_task: TaskConfig) -> None:
        """Completed task is not in ready list."""
        dag = DAG([simple_task])
        ready = dag.get_ready_tasks({"task-a"})
        assert ready == []

    def test_chain_first_ready(self, three_task_chain: list[TaskConfig]) -> None:
        """Only first task in chain is initially ready."""
        dag = DAG(three_task_chain)
        ready = dag.get_ready_tasks(set())
        assert ready == ["task-a"]

    def test_chain_second_ready_after_first(
        self, three_task_chain: list[TaskConfig]
    ) -> None:
        """Second task becomes ready after first completes."""
        dag = DAG(three_task_chain)
        ready = dag.get_ready_tasks({"task-a"})
        assert ready == ["task-b"]

    def test_chain_third_ready(self, three_task_chain: list[TaskConfig]) -> None:
        """Third task becomes ready after first two complete."""
        dag = DAG(three_task_chain)
        ready = dag.get_ready_tasks({"task-a", "task-b"})
        assert ready == ["task-c"]

    def test_chain_all_completed(self, three_task_chain: list[TaskConfig]) -> None:
        """No tasks ready when all are completed."""
        dag = DAG(three_task_chain)
        ready = dag.get_ready_tasks({"task-a", "task-b", "task-c"})
        assert ready == []

    def test_diamond_initial_ready(self, diamond_dag: list[TaskConfig]) -> None:
        """Only root task is ready initially in diamond DAG."""
        dag = DAG(diamond_dag)
        ready = dag.get_ready_tasks(set())
        assert ready == ["task-a"]

    def test_diamond_parallel_ready(self, diamond_dag: list[TaskConfig]) -> None:
        """Both B and C become ready after A completes."""
        dag = DAG(diamond_dag)
        ready = dag.get_ready_tasks({"task-a"})
        assert set(ready) == {"task-b", "task-c"}

    def test_diamond_d_not_ready_partial(self, diamond_dag: list[TaskConfig]) -> None:
        """D is not ready until both B and C complete."""
        dag = DAG(diamond_dag)

        # Only B completed
        ready = dag.get_ready_tasks({"task-a", "task-b"})
        assert "task-d" not in ready
        assert "task-c" in ready

        # Only C completed
        ready = dag.get_ready_tasks({"task-a", "task-c"})
        assert "task-d" not in ready
        assert "task-b" in ready

    def test_diamond_d_ready(self, diamond_dag: list[TaskConfig]) -> None:
        """D becomes ready after both B and C complete."""
        dag = DAG(diamond_dag)
        ready = dag.get_ready_tasks({"task-a", "task-b", "task-c"})
        assert ready == ["task-d"]

    def test_priority_ordering(self, tasks_with_priorities: list[TaskConfig]) -> None:
        """Ready tasks are sorted by priority (highest first)."""
        dag = DAG(tasks_with_priorities)
        ready = dag.get_ready_tasks(set())

        assert ready[0] == "task-high"
        assert ready[1] == "task-medium"
        assert ready[2] == "task-low"

    def test_ready_tasks_with_multiple_dependencies(self) -> None:
        """Task with multiple dependencies only ready when all complete."""
        tasks = [
            TaskConfig(id="a", title="A", prompt="Do A"),
            TaskConfig(id="b", title="B", prompt="Do B"),
            TaskConfig(id="c", title="C", prompt="Do C"),
            TaskConfig(id="d", title="D", prompt="Do D", depends_on=["a", "b", "c"]),
        ]
        dag = DAG(tasks)

        # Initially A, B, C are ready
        assert set(dag.get_ready_tasks(set())) == {"a", "b", "c"}

        # D not ready until all dependencies complete
        assert dag.get_ready_tasks({"a"}) == ["b", "c"]
        assert dag.get_ready_tasks({"a", "b"}) == ["c"]
        assert dag.get_ready_tasks({"a", "b", "c"}) == ["d"]


# =============================================================================
# Test: Scope Overlap Warning
# =============================================================================


class TestScopeOverlapWarning:
    """Tests for scope overlap detection."""

    def test_no_overlap_different_scopes(self) -> None:
        """No warning when scopes don't overlap."""
        tasks = [
            TaskConfig(
                id="task-a",
                title="A",
                prompt="Do A",
                scope=["src/module_a/**/*.py"],
            ),
            TaskConfig(
                id="task-b",
                title="B",
                prompt="Do B",
                scope=["src/module_b/**/*.py"],
            ),
        ]
        dag = DAG(tasks)
        warnings = dag.check_scope_overlaps()
        assert warnings == []

    def test_no_overlap_empty_scope(self) -> None:
        """No warning when tasks have empty scopes."""
        tasks = [
            TaskConfig(id="task-a", title="A", prompt="Do A", scope=[]),
            TaskConfig(id="task-b", title="B", prompt="Do B", scope=[]),
        ]
        dag = DAG(tasks)
        warnings = dag.check_scope_overlaps()
        assert warnings == []

    def test_overlap_exact_match(
        self, parallel_tasks_same_scope: list[TaskConfig]
    ) -> None:
        """Warning when parallel tasks have same scope."""
        dag = DAG(parallel_tasks_same_scope)
        warnings = dag.check_scope_overlaps()

        assert len(warnings) == 1
        warning = warnings[0]
        assert set(warning.task_ids) == {"task-a", "task-b"}
        assert len(warning.overlapping_patterns) >= 1

    def test_no_overlap_dependent_tasks(self) -> None:
        """No warning for dependent tasks with same scope."""
        tasks = [
            TaskConfig(
                id="task-a",
                title="A",
                prompt="Do A",
                scope=["src/auth/**/*.py"],
            ),
            TaskConfig(
                id="task-b",
                title="B",
                prompt="Do B",
                depends_on=["task-a"],
                scope=["src/auth/**/*.py"],
            ),
        ]
        dag = DAG(tasks)
        warnings = dag.check_scope_overlaps()
        assert warnings == []

    def test_overlap_wildcard_patterns(self) -> None:
        """Detect overlap between wildcard patterns."""
        tasks = [
            TaskConfig(
                id="task-a",
                title="A",
                prompt="Do A",
                scope=["src/**/*.py"],
            ),
            TaskConfig(
                id="task-b",
                title="B",
                prompt="Do B",
                scope=["src/auth/*.py"],
            ),
        ]
        dag = DAG(tasks)
        warnings = dag.check_scope_overlaps()

        assert len(warnings) == 1

    def test_warning_structure(
        self, parallel_tasks_same_scope: list[TaskConfig]
    ) -> None:
        """ScopeWarning has correct structure."""
        dag = DAG(parallel_tasks_same_scope)
        warnings = dag.check_scope_overlaps()

        assert len(warnings) == 1
        warning = warnings[0]

        assert isinstance(warning, ScopeWarning)
        assert isinstance(warning.task_ids, tuple)
        assert len(warning.task_ids) == 2
        assert isinstance(warning.overlapping_patterns, list)
        assert isinstance(warning.suggestion, str)
        assert len(warning.suggestion) > 0

    def test_multiple_overlaps(self) -> None:
        """Detect multiple scope overlaps."""
        tasks = [
            TaskConfig(id="a", title="A", prompt="Do A", scope=["src/common/**/*.py"]),
            TaskConfig(id="b", title="B", prompt="Do B", scope=["src/common/**/*.py"]),
            TaskConfig(id="c", title="C", prompt="Do C", scope=["src/common/**/*.py"]),
        ]
        dag = DAG(tasks)
        warnings = dag.check_scope_overlaps()

        # Should have 3 warnings: (a,b), (a,c), (b,c)
        assert len(warnings) == 3

    def test_transitive_dependency_no_overlap(self) -> None:
        """No warning for tasks connected through transitive dependency."""
        # A -> B -> C: A and C are transitively dependent
        tasks = [
            TaskConfig(
                id="a",
                title="A",
                prompt="Do A",
                scope=["src/shared/**/*.py"],
            ),
            TaskConfig(
                id="b",
                title="B",
                prompt="Do B",
                depends_on=["a"],
                scope=["src/other/**/*.py"],
            ),
            TaskConfig(
                id="c",
                title="C",
                prompt="Do C",
                depends_on=["b"],
                scope=["src/shared/**/*.py"],
            ),
        ]
        dag = DAG(tasks)
        warnings = dag.check_scope_overlaps()

        # A and C share scope but are transitively dependent
        assert warnings == []


# =============================================================================
# Test: DAGNode Dataclass
# =============================================================================


class TestDAGNode:
    """Tests for DAGNode dataclass."""

    def test_default_values(self) -> None:
        """DAGNode has correct default values."""
        node = DAGNode(task_id="test")
        assert node.task_id == "test"
        assert node.dependencies == set()
        assert node.dependents == set()

    def test_with_values(self) -> None:
        """DAGNode stores provided values."""
        node = DAGNode(
            task_id="test",
            dependencies={"dep1", "dep2"},
            dependents={"child1"},
        )
        assert node.task_id == "test"
        assert node.dependencies == {"dep1", "dep2"}
        assert node.dependents == {"child1"}


# =============================================================================
# Test: ScopeWarning Dataclass
# =============================================================================


class TestScopeWarning:
    """Tests for ScopeWarning dataclass."""

    def test_default_suggestion(self) -> None:
        """ScopeWarning generates default suggestion."""
        warning = ScopeWarning(
            task_ids=("task-a", "task-b"),
            overlapping_patterns=[("src/*", "src/*")],
        )
        assert "task-a" in warning.suggestion
        assert "task-b" in warning.suggestion

    def test_custom_suggestion(self) -> None:
        """ScopeWarning allows custom suggestion."""
        warning = ScopeWarning(
            task_ids=("task-a", "task-b"),
            overlapping_patterns=[("src/*", "src/*")],
            suggestion="Custom suggestion",
        )
        assert warning.suggestion == "Custom suggestion"


# =============================================================================
# Test: CycleError Exception
# =============================================================================


class TestCycleError:
    """Tests for CycleError exception."""

    def test_error_message(self) -> None:
        """CycleError has correct message format."""
        error = CycleError(["a", "b", "a"])
        assert "Cyclic dependency detected" in str(error)
        assert "a -> b -> a" in str(error)

    def test_cycle_path_attribute(self) -> None:
        """CycleError stores cycle path."""
        path = ["x", "y", "z", "x"]
        error = CycleError(path)
        assert error.cycle_path == path
