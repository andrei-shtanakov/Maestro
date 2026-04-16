"""Cost tracking for Maestro task execution.

This module provides log parsing for token usage extraction,
cost calculation based on model pricing, and summary reporting.
Supports Claude Code JSON output format, with extensible parsing
for other agent types.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from maestro.models import AgentType, TaskCost


logger = logging.getLogger(__name__)


# =========================================================================
# Pricing Configuration
# =========================================================================

# USD per token for each agent type (input, output)
# Claude Code uses Claude Sonnet pricing by default
PRICING: dict[str, tuple[float, float]] = {
    "claude_code": (3.0 / 1_000_000, 15.0 / 1_000_000),
    "codex_cli": (2.5 / 1_000_000, 10.0 / 1_000_000),
    "aider": (3.0 / 1_000_000, 15.0 / 1_000_000),
    "announce": (0.0, 0.0),
}


# =========================================================================
# Token Usage Data
# =========================================================================


@dataclass
class TokenUsage:
    """Parsed token usage from agent logs."""

    input_tokens: int = 0
    output_tokens: int = 0


# =========================================================================
# Log Parsing
# =========================================================================


def parse_claude_code_log(log_content: str) -> TokenUsage:
    """Parse Claude Code JSON log output for token usage.

    Claude Code with --output-format json produces a JSON object
    that may contain usage information with input_tokens and
    output_tokens fields.

    Args:
        log_content: Raw log file content.

    Returns:
        TokenUsage with extracted token counts.
    """
    if not log_content.strip():
        return TokenUsage()

    # Claude Code JSON output may have the result as the last JSON object
    # Try parsing the entire content as JSON first
    for parser in (_parse_json_object, _parse_last_json_line):
        usage = parser(log_content)
        if usage.input_tokens > 0 or usage.output_tokens > 0:
            return usage

    return TokenUsage()


def _parse_json_object(content: str) -> TokenUsage:
    """Try to parse the entire content as a single JSON object."""
    try:
        data = json.loads(content.strip())
        return _extract_usage_from_dict(data)
    except (json.JSONDecodeError, TypeError):
        return TokenUsage()


def _parse_last_json_line(content: str) -> TokenUsage:
    """Try to parse the last non-empty line as JSON."""
    lines = content.strip().splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            return _extract_usage_from_dict(data)
        except (json.JSONDecodeError, TypeError):
            continue
    return TokenUsage()


def _extract_usage_from_dict(data: object) -> TokenUsage:
    """Extract token usage from a parsed JSON dictionary.

    Handles multiple formats:
    - Top-level: {"input_tokens": N, "output_tokens": N}
    - Nested usage: {"usage": {"input_tokens": N, "output_tokens": N}}
    - Result format: {"result": ..., "usage": {...}}
    """
    if not isinstance(data, dict):
        return TokenUsage()

    # Check for direct fields
    if "input_tokens" in data and "output_tokens" in data:
        return TokenUsage(
            input_tokens=int(data["input_tokens"]),
            output_tokens=int(data["output_tokens"]),
        )

    # Check nested "usage" key
    usage = data.get("usage")
    if isinstance(usage, dict):
        return TokenUsage(
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )

    return TokenUsage()


def parse_log(log_content: str, agent_type: AgentType) -> TokenUsage:
    """Parse agent log content to extract token usage.

    Dispatches to agent-specific parsers based on agent type.

    Args:
        log_content: Raw log file content.
        agent_type: Type of agent that produced the log.

    Returns:
        TokenUsage with extracted token counts.
    """
    parsers = {
        AgentType.CLAUDE_CODE: parse_claude_code_log,
        AgentType.CODEX: parse_claude_code_log,
        AgentType.AIDER: parse_claude_code_log,
    }

    parser = parsers.get(agent_type)
    if parser is None:
        return TokenUsage()

    try:
        return parser(log_content)
    except Exception:
        logger.warning("Failed to parse log for agent type %s", agent_type)
        return TokenUsage()


# =========================================================================
# Cost Calculation
# =========================================================================


def calculate_cost(
    usage: TokenUsage,
    agent_type: AgentType,
) -> float:
    """Calculate estimated cost in USD from token usage.

    Args:
        usage: Token usage counts.
        agent_type: Agent type for pricing lookup.

    Returns:
        Estimated cost in USD.
    """
    input_price, output_price = PRICING.get(agent_type.value, (0.0, 0.0))
    return usage.input_tokens * input_price + usage.output_tokens * output_price


def create_task_cost(
    task_id: str,
    agent_type: AgentType,
    usage: TokenUsage,
    attempt: int = 1,
) -> TaskCost:
    """Create a TaskCost record from parsed token usage.

    Args:
        task_id: Associated task identifier.
        agent_type: Agent type that executed the task.
        usage: Parsed token usage.
        attempt: Retry attempt number.

    Returns:
        TaskCost model ready for database storage.
    """
    cost = calculate_cost(usage, agent_type)
    return TaskCost(
        task_id=task_id,
        agent_type=agent_type,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        estimated_cost_usd=cost,
        attempt=attempt,
    )


def parse_and_create_cost(
    task_id: str,
    agent_type: AgentType,
    log_file: Path,
    attempt: int = 1,
) -> TaskCost | None:
    """Parse a log file and create a TaskCost record.

    Convenience function that reads a log file, parses token usage,
    and creates a TaskCost record. Returns None if the log file
    cannot be read or contains no usage data.

    Args:
        task_id: Associated task identifier.
        agent_type: Agent type that produced the log.
        log_file: Path to the agent log file.
        attempt: Retry attempt number.

    Returns:
        TaskCost record, or None if no usage data found.
    """
    try:
        log_content = log_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        logger.warning("Could not read log file: %s", log_file)
        return None

    usage = parse_log(log_content, agent_type)
    if usage.input_tokens == 0 and usage.output_tokens == 0:
        return None

    return create_task_cost(task_id, agent_type, usage, attempt)


# =========================================================================
# Summary Report
# =========================================================================


@dataclass
class CostSummary:
    """Aggregated cost summary across tasks."""

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    task_count: int = 0
    costs_by_task: dict[str, float] = field(default_factory=dict)


def build_summary(costs: list[TaskCost]) -> CostSummary:
    """Build a cost summary from a list of TaskCost records.

    Args:
        costs: List of TaskCost records.

    Returns:
        Aggregated CostSummary.
    """
    summary = CostSummary()
    task_ids: set[str] = set()

    for cost in costs:
        summary.total_input_tokens += cost.input_tokens
        summary.total_output_tokens += cost.output_tokens
        summary.total_cost_usd += cost.estimated_cost_usd
        task_ids.add(cost.task_id)

        if cost.task_id not in summary.costs_by_task:
            summary.costs_by_task[cost.task_id] = 0.0
        summary.costs_by_task[cost.task_id] += cost.estimated_cost_usd

    summary.task_count = len(task_ids)
    return summary


def format_summary(summary: CostSummary) -> str:
    """Format a CostSummary as a human-readable report.

    Args:
        summary: Cost summary to format.

    Returns:
        Formatted report string.
    """
    lines = [
        "Cost Summary",
        "=" * 40,
        f"Tasks tracked: {summary.task_count}",
        f"Total input tokens: {summary.total_input_tokens:,}",
        f"Total output tokens: {summary.total_output_tokens:,}",
        f"Total estimated cost: ${summary.total_cost_usd:.4f}",
    ]

    if summary.costs_by_task:
        lines.append("")
        lines.append("Per-task costs:")
        for task_id, cost in sorted(summary.costs_by_task.items()):
            lines.append(f"  {task_id}: ${cost:.4f}")

    return "\n".join(lines)
