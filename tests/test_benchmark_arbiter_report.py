import asyncio

import pytest
from pydantic import ValidationError

from maestro.benchmark.arbiter_report import WireTaskResult
from maestro.benchmark.models import BenchmarkResult, BenchmarkTaskResult
from tests.fakes.benchmark_score import evaluated_semantics


def _domain_task(**kwargs):
    defaults = {
        "task_index": 0,
        "prompt": "p",
        "response": "r",
        "duration_seconds": 1.0,
    }
    defaults.update(kwargs)
    return BenchmarkTaskResult(**defaults)


def test_wire_task_result_excludes_prompt_and_response():
    """Free-form strip: WireTaskResult must not carry prompt/response."""
    wire = WireTaskResult.from_domain(
        _domain_task(prompt="long prompt", response="long resp")
    )
    dumped = wire.model_dump()
    assert "prompt" not in dumped
    assert "response" not in dumped


def test_wire_task_result_maps_domain_fields():
    wire = WireTaskResult.from_domain(
        _domain_task(
            task_index=3,
            duration_seconds=4.2,
            tokens_used=1234,
            task_type="bugfix",
            score=0.9,
            error=None,
        )
    )
    assert wire.task_index == 3
    assert wire.duration_seconds == 4.2
    assert wire.tokens_used == 1234
    assert wire.task_type == "bugfix"
    assert wire.score == 0.9
    assert wire.error_class is None


def test_wire_task_result_error_bucketing():
    """Free-form error message → bounded enum bucket."""
    assert (
        WireTaskResult.from_domain(_domain_task(error="timeout after 30s")).error_class
        == "timeout"
    )
    assert (
        WireTaskResult.from_domain(_domain_task(error="subprocess crashed")).error_class
        == "crash"
    )
    assert (
        WireTaskResult.from_domain(_domain_task(error="2 test failures")).error_class
        == "test_failure"
    )
    assert (
        WireTaskResult.from_domain(_domain_task(error="something else")).error_class
        == "other"
    )
    assert WireTaskResult.from_domain(_domain_task(error=None)).error_class is None


def test_wire_task_result_forbids_extra_fields():
    with pytest.raises(ValidationError):
        WireTaskResult(
            task_index=0,
            task_type=None,
            score=None,
            tokens_used=None,
            duration_seconds=1.0,
            error_class=None,
            surprise="boom",  # type: ignore[unexpected-keyword]
        )


# ---------------------------------------------------------------------------
# Task 4.2 — ReportBenchmarkPayload, _sample_per_task, _build_wire_payload
# ---------------------------------------------------------------------------

from maestro.benchmark.arbiter_report import (  # noqa: E402
    _build_wire_payload,
)


def _result(
    run_id: str = "r",
    per_task: list | None = None,
    score: float = 0.5,
    semantics=None,
):
    return BenchmarkResult(
        run_id=run_id,
        benchmark_id="b",
        agent_id="a",
        score=score,
        semantics=semantics or evaluated_semantics(),
        per_task=per_task or [],
        duration_seconds=1.0,
    )


def _tasks(n: int) -> list[BenchmarkTaskResult]:
    return [
        BenchmarkTaskResult(
            task_index=i, prompt=f"p{i}", response=f"r{i}", duration_seconds=1.0
        )
        for i in range(n)
    ]


def test_payload_version_pinned_to_1_0_0():
    p = _build_wire_payload(_result(), max_per_task=200)
    assert p.payload_version == "1.0.0"


def test_payload_maps_all_aggregate_fields():
    result = _result(run_id="x", per_task=_tasks(3), score=0.85)
    p = _build_wire_payload(result, max_per_task=200)
    assert p.run_id == "x"
    assert p.benchmark_id == "b"
    assert p.agent_id == "a"
    assert p.score == 0.85
    assert p.per_task_total_count == 3
    assert p.per_task_truncated is False
    assert len(p.per_task) == 3


def test_truncation_under_cap_no_change():
    p = _build_wire_payload(_result(per_task=_tasks(50)), max_per_task=200)
    assert p.per_task_truncated is False
    assert len(p.per_task) == 50
    assert p.per_task_total_count == 50


def test_truncation_at_cap_boundary_not_truncated():
    p = _build_wire_payload(_result(per_task=_tasks(200)), max_per_task=200)
    assert p.per_task_truncated is False
    assert len(p.per_task) == 200


def test_truncation_above_cap_samples():
    p = _build_wire_payload(_result(per_task=_tasks(500)), max_per_task=200)
    assert p.per_task_truncated is True
    assert len(p.per_task) == 200
    assert p.per_task_total_count == 500


def test_truncation_deterministic_same_run_id_same_sample():
    tasks = _tasks(500)
    p1 = _build_wire_payload(_result(run_id="same", per_task=tasks), max_per_task=200)
    p2 = _build_wire_payload(_result(run_id="same", per_task=tasks), max_per_task=200)
    assert [t.task_index for t in p1.per_task] == [t.task_index for t in p2.per_task]


def test_truncation_different_run_ids_different_samples():
    """Guard against global-seed regression (e.g. random.seed(0))."""
    tasks = _tasks(500)
    p1 = _build_wire_payload(_result(run_id="run-A", per_task=tasks), max_per_task=200)
    p2 = _build_wire_payload(_result(run_id="run-B", per_task=tasks), max_per_task=200)
    assert [t.task_index for t in p1.per_task] != [t.task_index for t in p2.per_task]


def test_empty_per_task_handled():
    p = _build_wire_payload(_result(per_task=[]), max_per_task=200)
    assert p.per_task == []
    assert p.per_task_total_count == 0
    assert p.per_task_truncated is False


def test_payload_excludes_free_form_in_per_task():
    p = _build_wire_payload(_result(per_task=_tasks(1)), max_per_task=200)
    dumped = p.per_task[0].model_dump()
    assert "prompt" not in dumped
    assert "response" not in dumped


def test_env_override_for_max_per_task(monkeypatch):
    monkeypatch.setenv("MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK", "5")
    # Re-import to pick up env at module load
    import importlib

    import maestro.benchmark.arbiter_report as ar

    importlib.reload(ar)
    try:
        assert ar.REPORT_MAX_PER_TASK == 5
    finally:
        # Restore default for subsequent tests
        monkeypatch.delenv("MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK", raising=False)
        importlib.reload(ar)


# ---------------------------------------------------------------------------
# Task 4.3 — _classify_error, ErrorClass, _ERROR_SEVERITY
# ---------------------------------------------------------------------------


from maestro.benchmark.arbiter_report import _classify_error  # noqa: E402
from maestro.coordination.arbiter_errors import (  # noqa: E402
    ArbiterContractError,
    ArbiterUnavailable,
)


def test_classify_timeout():
    assert _classify_error(TimeoutError()) == ("timeout", "report timed out")


def test_classify_contract_error_preserves_code_and_message():
    e = ArbiterContractError(-32602, "missing field")
    cls, msg = _classify_error(e)
    assert cls == "contract_break"
    assert "-32602" in msg
    assert "missing field" in msg


def test_classify_unavailable():
    e = ArbiterUnavailable("broken pipe")
    assert _classify_error(e) == ("unavailable", "arbiter unavailable")


def test_classify_unexpected_includes_type_name():
    e = ValueError("oops")
    cls, msg = _classify_error(e)
    assert cls == "unexpected"
    assert "ValueError" in msg
    assert "oops" in msg


def test_classify_dispatches_on_type_not_string():
    """Regression guard: an ArbiterUnavailable whose message contains 'timeout'
    must still classify as 'unavailable' (because dispatch is by type)."""
    e = ArbiterUnavailable("connection timeout after 30s")
    assert _classify_error(e) == ("unavailable", "arbiter unavailable")


# ---------------------------------------------------------------------------
# Task 4.4 — report_benchmark_to_arbiter happy + skipped paths
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from maestro.benchmark.arbiter_report import report_benchmark_to_arbiter  # noqa: E402


@pytest.mark.asyncio
async def test_helper_returns_skipped_when_client_none():
    result = _result(run_id="skip")
    returned = await report_benchmark_to_arbiter(result, client=None)
    assert returned.report_status == "skipped"
    assert returned.report_error is None
    # Immutability: input unchanged (it was the default "skipped" anyway)
    assert result.report_status == "skipped"


@pytest.mark.asyncio
async def test_helper_returns_ok_on_created():
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        return_value={"status": "created", "run_id": "x"}
    )
    result = _result(run_id="x")
    returned = await report_benchmark_to_arbiter(result, mock_client)
    assert returned.report_status == "ok"
    assert returned.report_error is None
    mock_client.report_benchmark_raw.assert_awaited_once()


@pytest.mark.asyncio
async def test_helper_returns_ok_on_duplicate():
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        return_value={"status": "duplicate", "run_id": "x"}
    )
    result = _result(run_id="x")
    returned = await report_benchmark_to_arbiter(result, mock_client)
    assert returned.report_status == "ok"


@pytest.mark.asyncio
async def test_helper_returns_new_object_not_mutated():
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        return_value={"status": "created", "run_id": "x"}
    )
    result = _result()
    returned = await report_benchmark_to_arbiter(result, mock_client)
    assert returned is not result
    assert result.report_status == "skipped"  # default — helper did NOT mutate input
    assert returned.report_status == "ok"


# ---------------------------------------------------------------------------
# Task 4.5 — error paths: fire-and-forget, classified failures, never raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_helper_failed_on_unavailable():
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        side_effect=ArbiterUnavailable("broken pipe")
    )
    returned = await report_benchmark_to_arbiter(_result(), mock_client)
    assert returned.report_status == "failed"
    assert returned.report_error == "unavailable: arbiter unavailable"


@pytest.mark.asyncio
async def test_helper_failed_on_contract_break():
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        side_effect=ArbiterContractError(-32602, "missing agent_id")
    )
    returned = await report_benchmark_to_arbiter(_result(), mock_client)
    assert returned.report_status == "failed"
    assert isinstance(returned.report_error, str)
    assert "contract_break: -32602" in returned.report_error
    assert "missing agent_id" in returned.report_error


@pytest.mark.asyncio
async def test_helper_failed_on_timeout(monkeypatch):
    """When client hangs past REPORT_TIMEOUT_S, helper times out and classifies."""

    async def hang(_payload):
        await asyncio.sleep(60)

    mock_client = MagicMock()
    mock_client.report_benchmark_raw = hang
    # Use tiny timeout for the test
    import maestro.benchmark.arbiter_report as ar

    monkeypatch.setattr(ar, "REPORT_TIMEOUT_S", 0.05)
    returned = await report_benchmark_to_arbiter(_result(), mock_client)
    assert returned.report_status == "failed"
    assert isinstance(returned.report_error, str)
    assert returned.report_error.startswith("timeout:")


@pytest.mark.asyncio
async def test_helper_failed_on_unexpected_exception():
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(side_effect=ValueError("oops"))
    returned = await report_benchmark_to_arbiter(_result(), mock_client)
    assert returned.report_status == "failed"
    assert isinstance(returned.report_error, str)
    assert "unexpected: ValueError" in returned.report_error
    assert "oops" in returned.report_error


@pytest.mark.asyncio
async def test_helper_does_not_catch_cancelled():
    """CancelledError is BaseException; must propagate (not return failed)."""
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await report_benchmark_to_arbiter(_result(), mock_client)


# ---------------------------------------------------------------------------
# Task 4.6 — obs instrumentation: distinct event names per outcome
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_obs_events(monkeypatch):
    """Capture structlog log calls on arbiter_report._obs_log."""
    events: list[dict[str, object]] = []

    class _CapturingLogger:
        def _capture(self, level: str, event: str, **kw):
            events.append({"level": level, "event": event, **kw})

        def info(self, event: str, **kw):
            self._capture("info", event, **kw)

        def warning(self, event: str, **kw):
            self._capture("warning", event, **kw)

        def error(self, event: str, **kw):
            self._capture("error", event, **kw)

        # span() calls log.info / log.error directly under the hood, so we don't
        # need to mock obs.span itself for these tests.

    import maestro.benchmark.arbiter_report as ar

    monkeypatch.setattr(ar, "_obs_log", _CapturingLogger())
    return events


@pytest.mark.asyncio
async def test_emits_skipped_when_client_none(captured_obs_events):
    await report_benchmark_to_arbiter(_result(run_id="x"), None)
    assert any(
        e["event"] == "benchmark.report.skipped" and e.get("run_id") == "x"
        for e in captured_obs_events
    )


@pytest.mark.asyncio
async def test_emits_succeeded_on_created(captured_obs_events):
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        return_value={"status": "created", "run_id": "x"}
    )
    await report_benchmark_to_arbiter(_result(run_id="x"), mock_client)
    assert any(e["event"] == "benchmark.report.succeeded" for e in captured_obs_events)


@pytest.mark.asyncio
async def test_emits_duplicate_on_duplicate(captured_obs_events):
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        return_value={"status": "duplicate", "run_id": "x"}
    )
    await report_benchmark_to_arbiter(_result(run_id="x"), mock_client)
    assert any(e["event"] == "benchmark.report.duplicate" for e in captured_obs_events)
    # Must NOT also emit succeeded (these are mutually exclusive)
    assert not any(
        e["event"] == "benchmark.report.succeeded" for e in captured_obs_events
    )


@pytest.mark.asyncio
async def test_emits_contract_break_event_distinct(captured_obs_events):
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        side_effect=ArbiterContractError(-32602, "missing")
    )
    await report_benchmark_to_arbiter(_result(run_id="x"), mock_client)
    assert any(
        e["event"] == "benchmark.report.contract_break" and e["level"] == "error"
        for e in captured_obs_events
    )
    # Must NOT emit the generic failed event when it's a contract break
    assert not any(e["event"] == "benchmark.report.failed" for e in captured_obs_events)


@pytest.mark.asyncio
async def test_emits_failed_on_unavailable_with_warning_severity(captured_obs_events):
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(side_effect=ArbiterUnavailable("x"))
    await report_benchmark_to_arbiter(_result(run_id="x"), mock_client)
    assert any(
        e["event"] == "benchmark.report.failed"
        and e["level"] == "warning"
        and e.get("error_class") == "unavailable"
        for e in captured_obs_events
    )


# ---------------------------------------------------------------------------
# Copilot follow-up #1 — strict status whitelist (forward-compat hole)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_helper_treats_unknown_status_as_contract_break(captured_obs_events):
    """Forward-compat hole: arbiter v1.2 returning 'rejected' must not silently
    map to 'ok'."""
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        return_value={"status": "rejected", "run_id": "x"}
    )
    returned = await report_benchmark_to_arbiter(_result(run_id="x"), mock_client)
    assert returned.report_status == "failed"
    assert returned.report_error is not None
    assert "contract_break" in returned.report_error
    assert "rejected" in returned.report_error
    assert any(
        e["event"] == "benchmark.report.contract_break" for e in captured_obs_events
    )


@pytest.mark.asyncio
async def test_helper_treats_missing_status_as_contract_break(captured_obs_events):
    """Malformed response (status field absent) → contract break."""
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        return_value={"run_id": "x"}  # no status
    )
    returned = await report_benchmark_to_arbiter(_result(run_id="x"), mock_client)
    assert returned.report_status == "failed"
    assert returned.report_error is not None
    assert "contract_break" in returned.report_error


# ---------------------------------------------------------------------------
# Copilot follow-up #2 — reset report_error on ok/skipped paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_helper_resets_stale_report_error_on_success():
    """A BenchmarkResult with a prior failure's report_error must come back clean on retry-success."""
    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        return_value={"status": "created", "run_id": "x"}
    )
    # Simulate prior-failure state:
    prior_result = _result(run_id="x").model_copy(
        update={
            "report_status": "failed",
            "report_error": "unavailable: arbiter unavailable",
        }
    )
    returned = await report_benchmark_to_arbiter(prior_result, mock_client)
    assert returned.report_status == "ok"
    assert returned.report_error is None, f"stale: {returned.report_error}"


@pytest.mark.asyncio
async def test_helper_resets_stale_report_error_on_skipped():
    """Same for skipped path."""
    prior_result = _result(run_id="x").model_copy(
        update={"report_status": "failed", "report_error": "previous error"}
    )
    returned = await report_benchmark_to_arbiter(prior_result, None)
    assert returned.report_status == "skipped"
    assert returned.report_error is None


# ---------------------------------------------------------------------------
# Copilot polish #3 — defensive REPORT_MAX_PER_TASK env parsing
# ---------------------------------------------------------------------------


def test_env_parser_returns_default_when_unset(monkeypatch):
    from maestro.benchmark.arbiter_report import _parse_max_per_task_env

    monkeypatch.delenv("MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK", raising=False)
    assert _parse_max_per_task_env(200) == 200


def test_env_parser_returns_int_when_valid(monkeypatch):
    from maestro.benchmark.arbiter_report import _parse_max_per_task_env

    monkeypatch.setenv("MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK", "50")
    assert _parse_max_per_task_env(200) == 50


def test_env_parser_falls_back_on_garbage(monkeypatch, caplog):
    from maestro.benchmark.arbiter_report import _parse_max_per_task_env

    monkeypatch.setenv("MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK", "not-an-int")
    import logging

    caplog.set_level(logging.WARNING)
    result = _parse_max_per_task_env(200)
    assert result == 200
    assert "Invalid" in caplog.text


def test_env_parser_clamps_zero_to_one(monkeypatch, caplog):
    from maestro.benchmark.arbiter_report import _parse_max_per_task_env

    monkeypatch.setenv("MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK", "0")
    import logging

    caplog.set_level(logging.WARNING)
    result = _parse_max_per_task_env(200)
    assert result == 1
    assert "clamping" in caplog.text


def test_env_parser_clamps_negative(monkeypatch):
    from maestro.benchmark.arbiter_report import _parse_max_per_task_env

    monkeypatch.setenv("MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK", "-5")
    assert _parse_max_per_task_env(200) == 1
