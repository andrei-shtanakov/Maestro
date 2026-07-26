"""Cost tracking for Maestro task execution.

This module provides log parsing for token usage extraction,
cost calculation based on model pricing, and summary reporting.
Supports Claude Code JSON output format, with extensible parsing
for other agent types.
"""

import json
import logging
import math
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

# opencode is deliberately absent: it is an open-model harness whose price
# depends on the routed model, so Maestro cannot price it from a static
# per-harness table. Absence from PRICING == "unpriced" (see has_pricing);
# outcome reporting turns the resulting 0.0 into cost_usd=None so
# cost-aware routing reads it as *unknown*, never as *free*.


def has_pricing(agent_type: AgentType) -> bool:
    """True if the harness has a rate card in PRICING.

    announce's (0.0, 0.0) is an honest zero (it runs no model); a harness
    absent from PRICING (opencode) has UNKNOWN cost — callers reporting
    cost to the arbiter must send None, not 0.0.
    """
    return agent_type.value in PRICING


def effective_cost(cost: TaskCost) -> float | None:
    """The best-known cost of one TaskCost row, or None if unknown.

    Agent-reported cost wins; the PRICING estimate is trusted only for
    priced harnesses (announce's 0.0 is an honest zero); an unpriced
    harness with no report is UNKNOWN — callers reporting to the arbiter
    must propagate None, never 0.0.
    """
    if cost.reported_cost_usd is not None:
        return cost.reported_cost_usd
    if has_pricing(cost.agent_type):
        return cost.estimated_cost_usd
    return None


@dataclass(frozen=True)
class CostGroup:
    """Aggregated cost/usage for one grouping key (or the grand total)."""

    label: str
    known_cost_usd: float
    input_tokens: int
    output_tokens: int
    tasks: int
    attempts: int
    unknown_attempts: int
    unknown_tasks: int


@dataclass(frozen=True)
class CostReport:
    """Aggregated cost report: total, by harness, by task, by phase, by model."""

    total: CostGroup
    by_harness: list[CostGroup]
    by_task: list[CostGroup]
    by_phase: list[CostGroup]
    by_model: list[CostGroup]


class _Acc:
    """Mutable accumulator; frozen into a CostGroup at the end."""

    def __init__(self) -> None:
        self.known = 0.0
        self.inp = 0
        self.out = 0
        self.attempts = 0
        self.unknown_attempts = 0
        self._task_ids: set[str] = set()
        self._unknown_task_ids: set[str] = set()

    def add(self, cost: TaskCost) -> None:
        self.attempts += 1
        self.inp += cost.input_tokens
        self.out += cost.output_tokens
        self._task_ids.add(cost.task_id)
        eff = effective_cost(cost)
        if eff is None:
            self.unknown_attempts += 1
            self._unknown_task_ids.add(cost.task_id)
        else:
            self.known += eff

    def freeze(self, label: str) -> CostGroup:
        return CostGroup(
            label=label,
            known_cost_usd=self.known,
            input_tokens=self.inp,
            output_tokens=self.out,
            tasks=len(self._task_ids),
            attempts=self.attempts,
            unknown_attempts=self.unknown_attempts,
            unknown_tasks=len(self._unknown_task_ids),
        )


def summarize_costs(costs: list[TaskCost]) -> CostReport:
    """Database-wide cost summary: TOTAL + per-harness + per-task + per-phase
    + per-model.

    Known/unknown per row is decided by `effective_cost` (SSOT). `known_cost_usd`
    is a known subtotal; unknown attempts/tasks are reported alongside, never
    folded into the dollar figure. Tokens are summed over all supplied rows.
    A row with no recorded `model` (pre-T4 rows, or a harness that never
    filled it in) groups under the `"UNKNOWN"` label rather than being
    dropped, so the by-model totals still cover every row.
    """
    total = _Acc()
    by_harness: dict[str, _Acc] = {}
    by_task: dict[str, _Acc] = {}
    by_phase: dict[str, _Acc] = {}
    by_model: dict[str, _Acc] = {}
    for cost in costs:
        total.add(cost)
        by_harness.setdefault(cost.agent_type.value, _Acc()).add(cost)
        by_task.setdefault(cost.task_id, _Acc()).add(cost)
        by_phase.setdefault(cost.execution_phase, _Acc()).add(cost)
        by_model.setdefault(cost.model or "UNKNOWN", _Acc()).add(cost)
    return CostReport(
        total=total.freeze("TOTAL"),
        by_harness=[acc.freeze(k) for k, acc in sorted(by_harness.items())],
        by_task=[acc.freeze(k) for k, acc in sorted(by_task.items())],
        by_phase=[acc.freeze(k) for k, acc in sorted(by_phase.items())],
        by_model=[acc.freeze(k) for k, acc in sorted(by_model.items())],
    )


# =========================================================================
# Token Usage Data
# =========================================================================


@dataclass
class TokenUsage:
    """Parsed token usage from agent logs."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    """Agent-reported cost in USD (e.g. opencode's per-step ``part.cost``,
    Claude Code's result ``total_cost_usd``).

    None means the agent did not report a cost — never collapse to 0.0.
    The opencode and claude parsers fill this; codex/aider are priced from
    PRICING downstream unless their log happens to be JSON carrying a cost
    (they share ``parse_claude_code_log``).
    """


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
        if (
            usage.input_tokens > 0
            or usage.output_tokens > 0
            or usage.cost_usd is not None
        ):
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
    """Extract token usage and reported cost from a parsed JSON dict.

    Handles multiple token formats:
    - Top-level: {"input_tokens": N, "output_tokens": N}
    - Nested usage: {"usage": {"input_tokens": N, "output_tokens": N}}
    - Result format: {"result": ..., "usage": {...}}
    Cost (Claude's ``total_cost_usd``, or ``cost_usd``) is read from the
    top level of the object and attached regardless of the token format.
    """
    if not isinstance(data, dict):
        return TokenUsage()

    usage = TokenUsage()

    if "input_tokens" in data and "output_tokens" in data:
        usage.input_tokens = int(data["input_tokens"])
        usage.output_tokens = int(data["output_tokens"])
    else:
        nested = data.get("usage")
        if isinstance(nested, dict):
            usage.input_tokens = int(nested.get("input_tokens", 0))
            usage.output_tokens = int(nested.get("output_tokens", 0))

    # Claude's result JSON carries the cost at the top level.
    cost = data.get("total_cost_usd")
    if cost is None:
        cost = data.get("cost_usd")
    # bool is an int subclass (JSON true must not read as $1.00); NaN/Infinity
    # and negatives must not leak (NaN fails TaskCost's ge=0.0 check and would
    # silently drop the whole row).
    if (
        isinstance(cost, (int, float))
        and not isinstance(cost, bool)
        and math.isfinite(cost)
        and cost >= 0.0
    ):
        usage.cost_usd = float(cost)

    return usage


def parse_opencode_log(log_content: str) -> TokenUsage:
    """Parse opencode ``run --format json`` JSONL output for token usage.

    opencode emits one JSON event per line; ``step_finish`` events carry
    per-step usage in ``part.tokens`` (verified against a captured real run:
    values are per-step increments, so they are summed across events).

    ``part.tokens.cache.read`` / ``part.tokens.cache.write`` are intentionally
    dropped: Maestro never computes opencode cost from tokens, so cache reads
    are never billed at input price. ``part.cost`` IS extracted (summed
    per-step, same fixture-proven semantics) into ``TokenUsage.cost_usd`` —
    opencode's own number already prices cache correctly.

    Args:
        log_content: Raw log file content (stderr shares the fd, so
            non-JSON noise lines are expected and skipped).

    Returns:
        TokenUsage with input and output (+ reasoning) token sums, and
        reported cost if any.
    """
    usage = TokenUsage()
    saw_step_finish = False
    saw_cost = False
    cost_total = 0.0
    for raw_line in log_content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "step_finish":
            continue
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        saw_step_finish = True
        cost = part.get("cost")
        # bool is an int subclass: JSON true must not leak in as $1.00.
        # Infinity/NaN/negative must not leak in either: Infinity poisons
        # summaries, and NaN or a negative sum fails the
        # TaskCost.reported_cost_usd ge=0.0 check downstream and silently
        # drops the whole row (including tokens). Matches the claude parser's
        # guard.
        if (
            isinstance(cost, (int, float))
            and not isinstance(cost, bool)
            and math.isfinite(cost)
            and cost >= 0.0
        ):
            saw_cost = True
            cost_total += float(cost)
        tokens = part.get("tokens")
        if not isinstance(tokens, dict):
            continue
        usage.input_tokens += int(tokens.get("input") or 0)
        usage.output_tokens += int(tokens.get("output") or 0) + int(
            tokens.get("reasoning") or 0
        )
    if saw_cost:
        usage.cost_usd = cost_total
    if log_content.strip() and not saw_step_finish:
        # Format-drift canary: opencode renaming/removing step_finish would
        # otherwise zero out token tracking with no signal at all.
        logger.debug("opencode log had no step_finish events — format drift?")
    return usage


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
        AgentType.OPENCODE: parse_opencode_log,
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
        reported_cost_usd=usage.cost_usd,
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
    if usage.input_tokens == 0 and usage.output_tokens == 0 and usage.cost_usd is None:
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
        # COALESCE semantics (same as get_cost_summary's SQL): reported
        # wins; the estimate is the fallback. Summaries stay non-nullable
        # floats — the None-vs-0 distinction lives only at the
        # arbiter-outcome boundary (effective_cost).
        row_cost = (
            cost.reported_cost_usd
            if cost.reported_cost_usd is not None
            else cost.estimated_cost_usd
        )
        summary.total_cost_usd += row_cost
        task_ids.add(cost.task_id)

        if cost.task_id not in summary.costs_by_task:
            summary.costs_by_task[cost.task_id] = 0.0
        summary.costs_by_task[cost.task_id] += row_cost

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
        f"Total cost: ${summary.total_cost_usd:.4f}",
    ]

    if summary.costs_by_task:
        lines.append("")
        lines.append("Per-task costs:")
        for task_id, cost in sorted(summary.costs_by_task.items()):
            lines.append(f"  {task_id}: ${cost:.4f}")

    return "\n".join(lines)
