"""Tests for cost tracking: log parsing, cost calculation, and summary.

This module contains unit tests for the cost_tracker module covering
log parsing for different agent types, cost calculation, and summary
reporting. Also includes database integration tests for task_costs table.
"""

import json
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from maestro.cost_tracker import (
    CostSummary,
    TokenUsage,
    build_summary,
    calculate_cost,
    create_task_cost,
    format_summary,
    parse_and_create_cost,
    parse_claude_code_log,
    parse_log,
)
from maestro.database import Database, create_database
from maestro.models import AgentType, Task, TaskCost, TaskStatus


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def db(temp_db_path: Path) -> AsyncGenerator[Database, None]:
    """Provide a connected and initialized database."""
    database = await create_database(temp_db_path)
    yield database
    await database.close()


@pytest.fixture
def sample_task() -> Task:
    """Provide a sample task for cost testing."""
    return Task(
        id="task-001",
        title="Test Task",
        prompt="Test prompt",
        workdir="/tmp/test",
        agent_type=AgentType.CLAUDE_CODE,
        status=TaskStatus.PENDING,
    )


# =============================================================================
# Log Parsing Tests
# =============================================================================


class TestClaudeCodeLogParsing:
    """Tests for Claude Code JSON log parsing."""

    def test_parse_empty_log(self) -> None:
        """Empty log returns zero tokens."""
        usage = parse_claude_code_log("")
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0

    def test_parse_whitespace_log(self) -> None:
        """Whitespace-only log returns zero tokens."""
        usage = parse_claude_code_log("   \n\n  ")
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0

    def test_parse_top_level_tokens(self) -> None:
        """Parse tokens from top-level JSON fields."""
        log = json.dumps(
            {
                "result": "some output",
                "input_tokens": 1500,
                "output_tokens": 500,
            }
        )
        usage = parse_claude_code_log(log)
        assert usage.input_tokens == 1500
        assert usage.output_tokens == 500

    def test_parse_nested_usage(self) -> None:
        """Parse tokens from nested usage object."""
        log = json.dumps(
            {
                "result": "some output",
                "usage": {
                    "input_tokens": 2000,
                    "output_tokens": 800,
                },
            }
        )
        usage = parse_claude_code_log(log)
        assert usage.input_tokens == 2000
        assert usage.output_tokens == 800

    def test_parse_partial_usage(self) -> None:
        """Parse usage with only one token field."""
        log = json.dumps(
            {
                "usage": {
                    "input_tokens": 1000,
                },
            }
        )
        usage = parse_claude_code_log(log)
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 0

    def test_parse_last_json_line(self) -> None:
        """Parse tokens from last JSON line in multi-line output."""
        lines = [
            "Starting task...",
            "Processing...",
            json.dumps(
                {
                    "input_tokens": 3000,
                    "output_tokens": 1200,
                }
            ),
        ]
        log = "\n".join(lines)
        usage = parse_claude_code_log(log)
        assert usage.input_tokens == 3000
        assert usage.output_tokens == 1200

    def test_parse_invalid_json(self) -> None:
        """Invalid JSON returns zero tokens."""
        usage = parse_claude_code_log("not json at all")
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0

    def test_parse_json_without_tokens(self) -> None:
        """JSON without token fields returns zero tokens."""
        log = json.dumps({"result": "completed", "status": "ok"})
        usage = parse_claude_code_log(log)
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0

    def test_parse_json_array(self) -> None:
        """JSON array (non-dict) returns zero tokens."""
        log = json.dumps([1, 2, 3])
        usage = parse_claude_code_log(log)
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0

    def test_parse_mixed_output_with_json(self) -> None:
        """Mixed text and JSON, last line has usage."""
        lines = [
            "Running claude --print --output-format json",
            "Error on line 5",
            '{"usage": {"input_tokens": 500, "output_tokens": 200}}',
        ]
        log = "\n".join(lines)
        usage = parse_claude_code_log(log)
        assert usage.input_tokens == 500
        assert usage.output_tokens == 200


class TestParseLog:
    """Tests for the generic parse_log dispatcher."""

    def test_parse_log_claude_code(self) -> None:
        """Dispatch to Claude Code parser."""
        log = json.dumps({"input_tokens": 100, "output_tokens": 50})
        usage = parse_log(log, AgentType.CLAUDE_CODE)
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50

    def test_parse_log_codex(self) -> None:
        """Dispatch to Codex parser."""
        log = json.dumps({"input_tokens": 200, "output_tokens": 75})
        usage = parse_log(log, AgentType.CODEX)
        assert usage.input_tokens == 200
        assert usage.output_tokens == 75

    def test_parse_log_aider(self) -> None:
        """Dispatch to Aider parser."""
        log = json.dumps({"input_tokens": 300, "output_tokens": 100})
        usage = parse_log(log, AgentType.AIDER)
        assert usage.input_tokens == 300
        assert usage.output_tokens == 100

    def test_parse_log_announce(self) -> None:
        """Announce agent type returns zero tokens."""
        usage = parse_log("anything", AgentType.ANNOUNCE)
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0


# =============================================================================
# Cost Calculation Tests
# =============================================================================


class TestCostCalculation:
    """Tests for cost calculation from token usage."""

    def test_calculate_cost_claude_code(self) -> None:
        """Calculate cost for Claude Code usage."""
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=100_000)
        cost = calculate_cost(usage, AgentType.CLAUDE_CODE)
        # input: 1M * $3/1M = $3.00, output: 100K * $15/1M = $1.50
        assert abs(cost - 4.50) < 0.001

    def test_calculate_cost_codex(self) -> None:
        """Calculate cost for Codex usage."""
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=100_000)
        cost = calculate_cost(usage, AgentType.CODEX)
        # input: 1M * $2.5/1M = $2.50, output: 100K * $10/1M = $1.00
        assert abs(cost - 3.50) < 0.001

    def test_calculate_cost_zero_tokens(self) -> None:
        """Zero tokens yield zero cost."""
        usage = TokenUsage(input_tokens=0, output_tokens=0)
        cost = calculate_cost(usage, AgentType.CLAUDE_CODE)
        assert cost == 0.0

    def test_calculate_cost_announce(self) -> None:
        """Announce agent type always has zero cost."""
        usage = TokenUsage(input_tokens=1000, output_tokens=500)
        cost = calculate_cost(usage, AgentType.ANNOUNCE)
        assert cost == 0.0

    def test_calculate_cost_small_usage(self) -> None:
        """Calculate cost for small token counts."""
        usage = TokenUsage(input_tokens=1000, output_tokens=500)
        cost = calculate_cost(usage, AgentType.CLAUDE_CODE)
        # input: 1000 * $3/1M = $0.003, output: 500 * $15/1M = $0.0075
        expected = 0.003 + 0.0075
        assert abs(cost - expected) < 0.0001


class TestCreateTaskCost:
    """Tests for creating TaskCost records."""

    def test_create_task_cost(self) -> None:
        """Create TaskCost from token usage."""
        usage = TokenUsage(input_tokens=5000, output_tokens=2000)
        cost = create_task_cost("task-001", AgentType.CLAUDE_CODE, usage, attempt=1)
        assert cost.task_id == "task-001"
        assert cost.agent_type == AgentType.CLAUDE_CODE
        assert cost.input_tokens == 5000
        assert cost.output_tokens == 2000
        assert cost.estimated_cost_usd > 0.0
        assert cost.attempt == 1

    def test_create_task_cost_retry(self) -> None:
        """Create TaskCost with retry attempt number."""
        usage = TokenUsage(input_tokens=1000, output_tokens=500)
        cost = create_task_cost("task-002", AgentType.CODEX, usage, attempt=3)
        assert cost.attempt == 3


class TestParseAndCreateCost:
    """Tests for the parse_and_create_cost convenience function."""

    def test_parse_and_create_from_file(self, temp_dir: Path) -> None:
        """Parse log file and create TaskCost."""
        log_file = temp_dir / "task-001.log"
        log_data = json.dumps(
            {
                "input_tokens": 5000,
                "output_tokens": 2000,
            }
        )
        log_file.write_text(log_data)

        result = parse_and_create_cost("task-001", AgentType.CLAUDE_CODE, log_file)
        assert result is not None
        assert result.input_tokens == 5000
        assert result.output_tokens == 2000
        assert result.estimated_cost_usd > 0.0

    def test_parse_missing_file(self, temp_dir: Path) -> None:
        """Missing log file returns None."""
        log_file = temp_dir / "nonexistent.log"
        result = parse_and_create_cost("task-001", AgentType.CLAUDE_CODE, log_file)
        assert result is None

    def test_parse_empty_file(self, temp_dir: Path) -> None:
        """Empty log file returns None."""
        log_file = temp_dir / "empty.log"
        log_file.write_text("")
        result = parse_and_create_cost("task-001", AgentType.CLAUDE_CODE, log_file)
        assert result is None

    def test_parse_no_usage_data(self, temp_dir: Path) -> None:
        """Log without usage data returns None."""
        log_file = temp_dir / "no-usage.log"
        log_file.write_text('{"result": "done"}')
        result = parse_and_create_cost("task-001", AgentType.CLAUDE_CODE, log_file)
        assert result is None


# =============================================================================
# Summary Report Tests
# =============================================================================


class TestBuildSummary:
    """Tests for building cost summary."""

    def test_empty_costs(self) -> None:
        """Empty cost list produces zero summary."""
        summary = build_summary([])
        assert summary.total_input_tokens == 0
        assert summary.total_output_tokens == 0
        assert summary.total_cost_usd == 0.0
        assert summary.task_count == 0
        assert summary.costs_by_task == {}

    def test_single_cost(self) -> None:
        """Summary from a single cost record."""
        cost = TaskCost(
            task_id="task-001",
            agent_type=AgentType.CLAUDE_CODE,
            input_tokens=1000,
            output_tokens=500,
            estimated_cost_usd=0.0105,
            attempt=1,
        )
        summary = build_summary([cost])
        assert summary.total_input_tokens == 1000
        assert summary.total_output_tokens == 500
        assert summary.task_count == 1
        assert "task-001" in summary.costs_by_task

    def test_multiple_tasks(self) -> None:
        """Summary from costs across multiple tasks."""
        costs = [
            TaskCost(
                task_id="task-001",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=1000,
                output_tokens=500,
                estimated_cost_usd=0.01,
                attempt=1,
            ),
            TaskCost(
                task_id="task-002",
                agent_type=AgentType.CODEX,
                input_tokens=2000,
                output_tokens=800,
                estimated_cost_usd=0.02,
                attempt=1,
            ),
        ]
        summary = build_summary(costs)
        assert summary.total_input_tokens == 3000
        assert summary.total_output_tokens == 1300
        assert summary.task_count == 2
        assert len(summary.costs_by_task) == 2

    def test_multiple_attempts_same_task(self) -> None:
        """Summary aggregates costs from retry attempts."""
        costs = [
            TaskCost(
                task_id="task-001",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=1000,
                output_tokens=500,
                estimated_cost_usd=0.01,
                attempt=1,
            ),
            TaskCost(
                task_id="task-001",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=1200,
                output_tokens=600,
                estimated_cost_usd=0.012,
                attempt=2,
            ),
        ]
        summary = build_summary(costs)
        assert summary.total_input_tokens == 2200
        assert summary.total_output_tokens == 1100
        assert summary.task_count == 1
        assert abs(summary.costs_by_task["task-001"] - 0.022) < 0.001


class TestFormatSummary:
    """Tests for formatting cost summary as text."""

    def test_format_empty_summary(self) -> None:
        """Format an empty summary."""
        summary = CostSummary()
        report = format_summary(summary)
        assert "Cost Summary" in report
        assert "Tasks tracked: 0" in report
        assert "$0.0000" in report

    def test_format_with_data(self) -> None:
        """Format a summary with data."""
        summary = CostSummary(
            total_input_tokens=5000,
            total_output_tokens=2000,
            total_cost_usd=0.045,
            task_count=2,
            costs_by_task={
                "task-001": 0.025,
                "task-002": 0.020,
            },
        )
        report = format_summary(summary)
        assert "Tasks tracked: 2" in report
        assert "5,000" in report
        assert "2,000" in report
        assert "$0.0450" in report
        assert "task-001: $0.0250" in report
        assert "task-002: $0.0200" in report


# =============================================================================
# Database Integration Tests
# =============================================================================


class TestTaskCostDatabase:
    """Tests for task_costs database operations."""

    @pytest.mark.anyio
    async def test_save_and_get_task_cost(
        self, db: Database, sample_task: Task
    ) -> None:
        """Save and retrieve a task cost."""
        await db.create_task(sample_task)

        cost = TaskCost(
            task_id="task-001",
            agent_type=AgentType.CLAUDE_CODE,
            input_tokens=5000,
            output_tokens=2000,
            estimated_cost_usd=0.045,
            attempt=1,
        )
        saved = await db.save_task_cost(cost)
        assert saved.id is not None

        costs = await db.get_task_costs("task-001")
        assert len(costs) == 1
        assert costs[0].input_tokens == 5000
        assert costs[0].output_tokens == 2000
        assert costs[0].estimated_cost_usd == 0.045

    @pytest.mark.anyio
    async def test_multiple_attempts(self, db: Database, sample_task: Task) -> None:
        """Save costs for multiple retry attempts."""
        await db.create_task(sample_task)

        for attempt in range(1, 4):
            cost = TaskCost(
                task_id="task-001",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=1000 * attempt,
                output_tokens=500 * attempt,
                estimated_cost_usd=0.01 * attempt,
                attempt=attempt,
            )
            await db.save_task_cost(cost)

        costs = await db.get_task_costs("task-001")
        assert len(costs) == 3
        assert costs[0].attempt == 1
        assert costs[2].attempt == 3

    @pytest.mark.anyio
    async def test_get_all_costs(self, db: Database, sample_task: Task) -> None:
        """Get all cost records across tasks."""
        await db.create_task(sample_task)

        task2 = Task(
            id="task-002",
            title="Task Two",
            prompt="Second task",
            workdir="/tmp/test",
            agent_type=AgentType.CODEX,
        )
        await db.create_task(task2)

        await db.save_task_cost(
            TaskCost(
                task_id="task-001",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=1000,
                output_tokens=500,
                estimated_cost_usd=0.01,
            )
        )
        await db.save_task_cost(
            TaskCost(
                task_id="task-002",
                agent_type=AgentType.CODEX,
                input_tokens=2000,
                output_tokens=800,
                estimated_cost_usd=0.02,
            )
        )

        all_costs = await db.get_all_costs()
        assert len(all_costs) == 2

    @pytest.mark.anyio
    async def test_get_cost_summary(self, db: Database, sample_task: Task) -> None:
        """Get aggregated cost summary."""
        await db.create_task(sample_task)

        await db.save_task_cost(
            TaskCost(
                task_id="task-001",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=1000,
                output_tokens=500,
                estimated_cost_usd=0.01,
            )
        )
        await db.save_task_cost(
            TaskCost(
                task_id="task-001",
                agent_type=AgentType.CLAUDE_CODE,
                input_tokens=2000,
                output_tokens=800,
                estimated_cost_usd=0.02,
            )
        )

        summary = await db.get_cost_summary()
        assert summary["total_input_tokens"] == 3000
        assert summary["total_output_tokens"] == 1300
        assert abs(summary["total_cost_usd"] - 0.03) < 0.001
        assert summary["task_count"] == 1

    @pytest.mark.anyio
    async def test_get_cost_summary_empty(self, db: Database) -> None:
        """Empty database returns zero summary."""
        summary = await db.get_cost_summary()
        assert summary["total_input_tokens"] == 0
        assert summary["total_output_tokens"] == 0
        assert summary["total_cost_usd"] == 0.0
        assert summary["task_count"] == 0

    @pytest.mark.anyio
    async def test_get_task_costs_empty(self, db: Database) -> None:
        """No costs for a task returns empty list."""
        costs = await db.get_task_costs("nonexistent-task")
        assert costs == []
