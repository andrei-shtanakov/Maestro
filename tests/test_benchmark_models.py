"""Tests for R-06b M4 additive benchmark model fields."""

from maestro.benchmark.models import BenchmarkResult, BenchmarkTaskResult
from tests.fakes.benchmark_score import evaluated_semantics


def test_benchmark_result_has_default_report_status_skipped():
    r = BenchmarkResult(
        run_id="x",
        benchmark_id="b",
        agent_id="a",
        score=0.5,
        semantics=evaluated_semantics(),
        per_task=[],
        duration_seconds=1.0,
    )
    assert r.report_status == "skipped"
    assert r.report_error is None


def test_benchmark_result_report_status_accepts_ok_failed_skipped():
    for status in ("ok", "failed", "skipped"):
        r = BenchmarkResult(
            run_id="x",
            benchmark_id="b",
            agent_id="a",
            score=0.5,
            semantics=evaluated_semantics(),
            per_task=[],
            duration_seconds=1.0,
            report_status=status,
        )
        assert r.report_status == status


def test_benchmark_task_result_additive_task_type_and_score():
    t = BenchmarkTaskResult(
        task_index=0,
        prompt="p",
        response="r",
        duration_seconds=1.0,
        task_type="bugfix",
        score=0.9,
    )
    assert t.task_type == "bugfix"
    assert t.score == 0.9


def test_benchmark_task_result_additive_defaults_none():
    t = BenchmarkTaskResult(
        task_index=0, prompt="p", response="r", duration_seconds=1.0
    )
    assert t.task_type is None
    assert t.score is None
