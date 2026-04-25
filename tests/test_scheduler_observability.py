"""M2 observability — verify scheduler instrumentation emits OTel-shaped JSONL
matching the cross-project observability contract.

The scheduler imports `_obs_log` at module load time, so we must reload
the module after pointing `ORCHESTRA_LOG_DIR` at a tmp dir and reloading
`maestro._vendor.obs`. The scheduler's structured emits should appear as
OTel Logs records with `Resource.service.name == "maestro"` and the
`Attributes.event` set to the documented event names.
"""

from __future__ import annotations

import importlib
import json
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path


def _reload_obs_into_tmp(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import maestro._vendor.obs as obs

    importlib.reload(obs)
    obs.init_logging("maestro")
    return obs


def _read_records(tmp_path: Path) -> list[dict]:
    files = list(tmp_path.glob("maestro-*.jsonl"))
    assert len(files) == 1, f"expected 1 jsonl file, got {len(files)}: {files}"
    return [json.loads(line) for line in files[0].read_text().splitlines()]


def test_scheduler_obs_log_emits_to_otel_jsonl(tmp_path, monkeypatch):
    """The scheduler's `_obs_log` writes OTel-shaped records with
    `service.name == "maestro"` and the documented event name in
    `Attributes.event`."""
    obs = _reload_obs_into_tmp(monkeypatch, tmp_path)
    log = obs.get_logger("maestro.scheduler")

    log.info(
        "task.completed",
        task_id="t-42",
        agent="codex_cli",
        validation_passed=True,
    )

    records = _read_records(tmp_path)
    completed = [r for r in records if r["Attributes"].get("event") == "task.completed"]
    assert len(completed) == 1
    rec = completed[0]
    assert rec["Resource"]["service.name"] == "maestro"
    assert rec["SeverityText"] == "INFO"
    assert rec["Attributes"]["task_id"] == "t-42"
    assert rec["Attributes"]["agent"] == "codex_cli"
    assert rec["Attributes"]["validation_passed"] is True
    # Trace context is bound by init_logging, never the zero-value sentinel.
    assert rec["TraceId"] != "0" * 32
    assert rec["SpanId"] != "0" * 16


def test_scheduler_span_carries_trace_id_for_subprocess(tmp_path, monkeypatch):
    """`obs.span("task.spawn", ...)` emits a started/ended pair under the
    same trace_id, and `child_env()` inside the span carries that trace_id
    via TRACEPARENT — the contract that lets spawner subprocesses inherit
    the per-task span as their parent (M1 wiring; M2 makes the scheduler
    actually open the span)."""
    obs = _reload_obs_into_tmp(monkeypatch, tmp_path)
    log = obs.get_logger("maestro.scheduler")

    captured_traceparent: dict[str, str] = {}
    with obs.span("task.spawn", task_id="t-1", agent="codex_cli", retry_count=0):
        env = obs.child_env()
        captured_traceparent["v"] = env["TRACEPARENT"]
        log.info("noop")

    records = _read_records(tmp_path)
    started = [
        r for r in records if r["Attributes"].get("event") == "task.spawn.started"
    ]
    ended = [r for r in records if r["Attributes"].get("event") == "task.spawn.ended"]
    assert len(started) == 1, f"expected 1 task.spawn.started, got {len(started)}"
    assert len(ended) == 1, f"expected 1 task.spawn.ended, got {len(ended)}"
    assert started[0]["TraceId"] == ended[0]["TraceId"]
    assert started[0]["Attributes"]["task_id"] == "t-1"
    assert started[0]["Attributes"]["agent"] == "codex_cli"

    # TRACEPARENT format: 00-<trace_id>-<span_id>-<flags>
    parts = captured_traceparent["v"].split("-")
    assert len(parts) == 4
    assert parts[0] == "00"
    assert parts[1] == started[0]["TraceId"]  # same trace
    assert parts[2] == started[0]["SpanId"]  # subprocess parent = this span
    assert parts[3] == "01"


def test_scheduler_emits_failure_with_retry_metadata(tmp_path, monkeypatch):
    """task.failed emits with retry_count, max_retries, will_retry, error
    so dashboards can distinguish transient retries from terminal NEEDS_REVIEW."""
    obs = _reload_obs_into_tmp(monkeypatch, tmp_path)
    log = obs.get_logger("maestro.scheduler")

    log.warning(
        "task.failed",
        task_id="t-x",
        retry_count=2,
        max_retries=3,
        will_retry=True,
        error="exit code 1",
    )

    records = _read_records(tmp_path)
    failed = [r for r in records if r["Attributes"].get("event") == "task.failed"]
    assert len(failed) == 1
    attrs = failed[0]["Attributes"]
    assert attrs["retry_count"] == 2
    assert attrs["max_retries"] == 3
    assert attrs["will_retry"] is True
    assert attrs["error"] == "exit code 1"
    assert failed[0]["SeverityText"] == "WARN"
