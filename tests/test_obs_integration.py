"""Integration test — parent/child trace continuity.

Verifies M1 criteria:
- Same TraceId across parent and child .jsonl files
- Child's root span has parent_span_id = parent's current span_id
- merge-logs produces a time-sorted merged.jsonl
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path


def test_trace_continuity_across_subprocess(tmp_path, monkeypatch):
    log_dir = tmp_path / "run"
    log_dir.mkdir()
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(log_dir))
    monkeypatch.delenv("TRACEPARENT", raising=False)

    from maestro._vendor import obs

    importlib.reload(obs)
    obs.init_logging("maestro")

    child_script = Path(__file__).parent / "_obs_child.py"
    parent_trace_id_holder: dict[str, str] = {}
    parent_span_id_holder: dict[str, str] = {}

    with obs.span("pipeline.run", dag_name="test"):  # noqa: SIM117
        with obs.span("task.execute", task_id="T-test") as inner:
            parent_trace_id_holder["v"] = inner.trace_id
            parent_span_id_holder["v"] = inner.span_id
            proc = subprocess.run(
                [sys.executable, str(child_script)],
                env={**os.environ, **obs.child_env()},
                capture_output=True,
                text=True,
                check=False,
            )
            assert proc.returncode == 0, proc.stderr

    from maestro.merge_logs import merge_logs_dir

    merge_logs_dir(log_dir)

    merged = [
        json.loads(line) for line in (log_dir / "merged.jsonl").read_text().splitlines()
    ]
    assert merged, "merged.jsonl is empty"

    # All records share the same TraceId
    trace_ids = {r["TraceId"] for r in merged}
    assert len(trace_ids) == 1
    assert parent_trace_id_holder["v"] in trace_ids

    # Child's root span (first spec-runner record) links to parent's current span
    child_records = [
        r for r in merged if r["Resource"]["service.name"] == "spec-runner"
    ]
    assert child_records, "no spec-runner records"
    first_child = child_records[0]
    assert first_child["Attributes"].get("parent_span_id") == parent_span_id_holder["v"]

    # Timestamps are monotonic in merged output
    ts = [int(r["Timestamp"]) for r in merged]
    assert ts == sorted(ts)
