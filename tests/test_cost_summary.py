from datetime import UTC, datetime

import pytest

from maestro.cost_tracker import CostReport, summarize_costs
from maestro.models import AgentType, TaskCost


def _c(
    task_id,
    agent,
    *,
    inp=0,
    out=0,
    est=0.0,
    reported=None,
    attempt=1,
    execution_phase="task",
    model=None,
):
    return TaskCost(
        task_id=task_id,
        agent_type=agent,
        input_tokens=inp,
        output_tokens=out,
        estimated_cost_usd=est,
        reported_cost_usd=reported,
        attempt=attempt,
        execution_phase=execution_phase,
        model=model,
        created_at=datetime.now(UTC),
    )


def test_empty():
    r = summarize_costs([])
    assert isinstance(r, CostReport)
    assert r.total.known_cost_usd == 0.0
    assert r.total.tasks == 0 and r.total.attempts == 0
    assert r.by_harness == [] and r.by_task == []
    assert r.by_phase == [] and r.by_model == []


def test_announce_is_known_zero_not_unknown():
    r = summarize_costs([_c("t1", AgentType.ANNOUNCE, est=0.0)])
    assert r.total.known_cost_usd == 0.0
    assert r.total.unknown_attempts == 0 and r.total.unknown_tasks == 0


def test_opencode_unpriced_unreported_is_unknown():
    # opencode absent from PRICING; estimated 0.0 must NOT be summed as known
    r = summarize_costs([_c("t1", AgentType.OPENCODE, inp=100, out=50, est=0.0)])
    assert r.total.known_cost_usd == 0.0
    assert r.total.unknown_attempts == 1 and r.total.unknown_tasks == 1
    # tokens still counted even though $ is unknown
    assert r.total.input_tokens == 100 and r.total.output_tokens == 50


def test_reported_cost_is_known():
    r = summarize_costs([_c("t1", AgentType.OPENCODE, reported=0.42)])
    assert r.total.known_cost_usd == 0.42
    assert r.total.unknown_attempts == 0


def test_priced_estimate_is_known():
    r = summarize_costs([_c("t1", AgentType.CLAUDE_CODE, est=0.10)])
    assert r.total.known_cost_usd == 0.10
    assert r.total.unknown_attempts == 0


def test_mixed_known_and_unknown_attempts_on_one_task():
    rows = [
        _c("t1", AgentType.CLAUDE_CODE, est=0.20, attempt=1),
        _c("t1", AgentType.OPENCODE, est=0.0, attempt=2),  # unknown
    ]
    r = summarize_costs(rows)
    assert r.total.known_cost_usd == 0.20  # known subtotal preserved
    assert r.total.tasks == 1 and r.total.attempts == 2
    assert r.total.unknown_attempts == 1 and r.total.unknown_tasks == 1


def test_two_tasks_same_harness():
    r = summarize_costs(
        [
            _c("t1", AgentType.CLAUDE_CODE, est=0.1),
            _c("t2", AgentType.CLAUDE_CODE, est=0.2),
        ]
    )
    assert r.total.tasks == 2 and r.total.attempts == 2
    assert len(r.by_harness) == 1
    assert r.by_harness[0].label == "claude_code"
    assert r.by_harness[0].known_cost_usd == pytest.approx(0.30)
    assert r.by_harness[0].tasks == 2


def test_retry_with_different_harness_splits_by_group():
    rows = [
        _c("t1", AgentType.CLAUDE_CODE, est=0.1, attempt=1),
        _c("t1", AgentType.CODEX, est=0.2, attempt=2),
    ]
    r = summarize_costs(rows)
    labels = {g.label: g for g in r.by_harness}
    assert set(labels) == {"claude_code", "codex_cli"}
    assert labels["claude_code"].attempts == 1
    assert labels["codex_cli"].attempts == 1
    # by task: t1 aggregates both attempts
    assert len(r.by_task) == 1 and r.by_task[0].attempts == 2
    assert r.by_task[0].known_cost_usd == pytest.approx(0.30)


def test_by_phase_breakdown() -> None:
    rows = [
        _c("t1", AgentType.CLAUDE_CODE, est=0.20, attempt=1, execution_phase="task"),
        _c(
            "t1",
            AgentType.CLAUDE_CODE,
            est=0.05,
            attempt=1,
            execution_phase="verification",
        ),
    ]
    r = summarize_costs(rows)
    phases = {g.label: g for g in r.by_phase}
    assert set(phases) == {"task", "verification"}
    assert phases["task"].known_cost_usd == pytest.approx(0.20)
    assert phases["verification"].known_cost_usd == pytest.approx(0.05)
    # totals still sum ALL rows regardless of phase
    assert r.total.known_cost_usd == pytest.approx(0.25)


def test_by_model_breakdown() -> None:
    rows = [
        _c("t1", AgentType.CLAUDE_CODE, est=0.20, attempt=1, model="claude-opus"),
        _c("t1", AgentType.CLAUDE_CODE, est=0.05, attempt=1, model="claude-haiku"),
        _c("t2", AgentType.CLAUDE_CODE, est=0.01, attempt=1, model=None),
    ]
    r = summarize_costs(rows)
    models = {g.label: g for g in r.by_model}
    assert set(models) == {"claude-opus", "claude-haiku", "UNKNOWN"}
    assert models["claude-opus"].known_cost_usd == pytest.approx(0.20)
    assert models["claude-haiku"].known_cost_usd == pytest.approx(0.05)
    assert models["UNKNOWN"].known_cost_usd == pytest.approx(0.01)
    assert r.total.known_cost_usd == pytest.approx(0.26)
