# Cross-Project Observability — Plan 1 (to M1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship milestone M1 of the observability spec — a Maestro pipeline spawning spec-runner produces `logs/<pipeline_id>/*.jsonl` where every record shares one `TraceId`, child spans link to parents via `Attributes.parent_span_id`, and `merge-logs` assembles a time-sorted `merged.jsonl` usable with `jq`.

**Architecture:** OpenTelemetry Logs Data Model as wire format + W3C Trace Context (`TRACEPARENT` env var) for propagation. Python reference emitter `obs.py` built on `structlog` (no `opentelemetry-sdk` dependency in v1), vendored into Maestro with the same pattern as `coordination/arbiter_client.py`. File-per-pid output to avoid concurrent-append corruption.

**Tech Stack:** Python 3.11+, `structlog`, `pytest`, `uv`, `jsonschema` (for contract tests), `ulid-py` (for pipeline_id).

**Scope (this plan):** contract artifacts, reference `obs.py` in spec-runner, spec-runner cutover from `logging.py` to `obs.py` (as a shim), vendor to Maestro, `merge-logs` CLI, cross-process integration test **and a live pipeline test** showing one `TraceId` across `maestro` and real `spec-runner` records. Rust arbiter, arbiter Python client, ATP, and the jq cookbook are **Plan 2 (to M2)** — separate document.

**Reference spec:** `Maestro/_cowork_output/decisions/2026-04-19-cross-project-observability-design.md` (rev. 2, path C).

**Repo layout:** Maestro is the ecosystem hub repo — it owns `_cowork_output/` for cross-project artifacts (contract, specs, plans). File-path bullets below are umbrella-relative (e.g., `Maestro/_cowork_output/...`). Inside task bodies, shell commands run from `cd Maestro` by default, so in commands `_cowork_output/...` means `Maestro/_cowork_output/...`.

**Environment notes (from Maestro/CLAUDE.md):** Python **3.12+**, uv for packages, Pyrefly for types, Ruff for format+lint, **Typer** (not Click) for CLI. spec-runner is consumed by Maestro as an installed package.

---

## File Structure

**Created:**
- `Maestro/_cowork_output/observability-contract/log-schema.json` — JSON Schema v1
- `Maestro/_cowork_output/observability-contract/propagation.md` — env-var protocol
- `Maestro/_cowork_output/observability-contract/rationale.md` — design decisions (mirrors §11 of spec)
- `Maestro/_cowork_output/observability-contract/fixtures/root-span.jsonl` — golden fixtures
- `Maestro/_cowork_output/observability-contract/fixtures/nested-span.jsonl`
- `Maestro/_cowork_output/observability-contract/fixtures/error-chain.jsonl`
- `Maestro/_cowork_output/observability-contract/fixtures/redacted.jsonl`
- `spec-runner/src/spec_runner/obs.py` — reference emitter (~150 LOC)
- `spec-runner/tests/test_obs.py` — unit tests for obs.py
- `spec-runner/tests/test_obs_contract.py` — contract test against schema + fixtures
- `Maestro/maestro/_vendor/__init__.py` — empty
- `Maestro/maestro/_vendor/obs.py` — vendored copy with freeze marker
- `Maestro/maestro/merge_logs.py` — standalone merge CLI module
- `Maestro/tests/test_merge_logs.py` — unit tests for merge
- `Maestro/tests/test_obs_integration.py` — cross-process integration test

**Modified:**
- `spec-runner/src/spec_runner/logging.py` — delegate to `obs.py`, keep as shim (back-compat for callers)
- `spec-runner/pyproject.toml` — add `ulid-py` dep
- `Maestro/maestro/orchestrator.py` — replace raw `subprocess.run` with `env={**os.environ, **child_env()}`, wrap task execution in `span()`
- `Maestro/maestro/cli.py` — `init_logging()` at entry; register `merge-logs` subcommand
- `Maestro/pyproject.toml` — add `structlog`, `ulid-py` deps

---

## Test Strategy

- **Unit tests** for every processor and public function in `obs.py` (Task 2-9).
- **Contract tests** validate emitted JSONL against `log-schema.json` + match golden fixtures structurally (parsed JSON, not bytes).
- **Integration test** (Task 15) spawns a child Python process from Maestro's test harness, verifies `TraceId` matches across parent/child `.jsonl` files and `parent_span_id` linkage is correct.
- Run full spec-runner test suite after Task 10 cutover: `cd spec-runner && uv run pytest` → 0 regressions (or known-bounded test updates documented in the task).
- Run full Maestro test suite after Task 14: `cd Maestro && uv run pytest` → 0 regressions.

---

## Task 1: Contract artifacts

**Files:**
- Create: `Maestro/_cowork_output/observability-contract/log-schema.json`
- Create: `Maestro/_cowork_output/observability-contract/propagation.md`
- Create: `Maestro/_cowork_output/observability-contract/rationale.md`
- Create: `Maestro/_cowork_output/observability-contract/fixtures/root-span.jsonl`
- Create: `Maestro/_cowork_output/observability-contract/fixtures/nested-span.jsonl`
- Create: `Maestro/_cowork_output/observability-contract/fixtures/error-chain.jsonl`
- Create: `Maestro/_cowork_output/observability-contract/fixtures/redacted.jsonl`

- [ ] **Step 1.1: Write `log-schema.json`**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://all_ai_orchestrators/observability-contract/v1",
  "title": "Orchestra Observability Log Record v1",
  "type": "object",
  "required": [
    "Timestamp", "ts_iso", "SeverityText", "SeverityNumber",
    "TraceId", "SpanId", "TraceFlags", "Body", "Resource", "Attributes"
  ],
  "properties": {
    "Timestamp": {"type": "string", "pattern": "^[0-9]+$"},
    "ObservedTimestamp": {"type": "string", "pattern": "^[0-9]+$"},
    "ts_iso": {
      "type": "string",
      "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{6}Z$"
    },
    "SeverityText": {"enum": ["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]},
    "SeverityNumber": {"type": "integer", "minimum": 1, "maximum": 24},
    "TraceId": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
    "SpanId": {"type": "string", "pattern": "^[0-9a-f]{16}$"},
    "TraceFlags": {"type": "string", "pattern": "^[0-9a-f]{2}$"},
    "Body": {"type": "string"},
    "Resource": {
      "type": "object",
      "required": ["service.name"],
      "properties": {
        "service.name": {
          "enum": ["maestro", "spec-runner", "arbiter", "atp", "proctor-a"]
        }
      }
    },
    "InstrumentationScope": {
      "type": "object",
      "properties": {"name": {"type": "string"}}
    },
    "Attributes": {
      "type": "object",
      "required": ["event", "pipeline_id"],
      "properties": {
        "event": {
          "type": "string",
          "pattern": "^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$"
        },
        "pipeline_id": {"type": "string", "pattern": "^[0-9A-HJKMNP-TV-Z]{26}$"},
        "parent_span_id": {"type": "string", "pattern": "^[0-9a-f]{16}$"},
        "task_id": {"type": "string"},
        "module": {"type": "string"},
        "error": {"$ref": "#/$defs/error"}
      },
      "additionalProperties": true
    }
  },
  "$defs": {
    "error": {
      "type": "object",
      "required": ["type", "message"],
      "properties": {
        "type": {"type": "string"},
        "message": {"type": "string"},
        "stack": {"type": "string"},
        "caused_by": {"$ref": "#/$defs/error"}
      }
    }
  }
}
```

- [ ] **Step 1.2: Write `propagation.md`**

```markdown
# Propagation Protocol — Orchestra Observability v1

## Trace Context
Use W3C Trace Context env var `TRACEPARENT` at every subprocess boundary:
`00-<trace_id_32hex>-<span_id_16hex>-<flags_2hex>`

Flags are always `01` in v1 (sampled; no sampling logic).

## Parent → Child Rules
1. Parent opens a span around the `Popen`/`subprocess.run` call.
2. Parent injects the following env vars into child:
   - `TRACEPARENT` — built from current trace_id and span_id.
   - `ORCHESTRA_PIPELINE_ID` — inherited ULID.
   - `ORCHESTRA_LOG_DIR` — absolute path.
3. Child's `init_logging()` parses `TRACEPARENT`, records parent `span_id` for
   `Attributes.parent_span_id` on the child's root span, and generates a fresh
   `span_id` for itself.

## Parser Robustness
`TRACEPARENT` parser MUST accept:
- Empty/missing → child becomes root (fresh trace_id, pipeline_id, no parent).
- Malformed → log warning, treat as root.

## Local Env Vars
- `ORCHESTRA_LOG_DIR` — absolute path. Default `<cwd>/logs/<pipeline_id>/` at root.
- `ORCHESTRA_LOG_LEVEL` — default `INFO`.
- `ORCHESTRA_LOG_FORMAT` — `json` (default) | `console`.
- `ORCHESTRA_REDACT_KEYS` — comma-separated, extends default blocklist.
```

- [ ] **Step 1.3: Write `rationale.md`** — copy §11 of the spec file
  verbatim (see `_cowork_output/decisions/2026-04-19-cross-project-observability-design.md` §11).

- [ ] **Step 1.4: Write `fixtures/root-span.jsonl`** (single record)

```jsonl
{"Timestamp":"1713534191482913000","ts_iso":"2026-04-19T14:23:11.482913Z","SeverityText":"INFO","SeverityNumber":9,"TraceId":"3f2e8c1a9b7d450f6e2c8a1b9f4d730e","SpanId":"3b7a9c2f1d8e4501","TraceFlags":"01","Body":"pipeline starting","Resource":{"service.name":"maestro"},"Attributes":{"event":"pipeline.started","pipeline_id":"01HZKX3P9M7Q2VFGR8BNDAW5YT","dag_name":"demo"}}
```

- [ ] **Step 1.5: Write `fixtures/nested-span.jsonl`** (parent + child span start events)

```jsonl
{"Timestamp":"1713534191482913000","ts_iso":"2026-04-19T14:23:11.482913Z","SeverityText":"INFO","SeverityNumber":9,"TraceId":"3f2e8c1a9b7d450f6e2c8a1b9f4d730e","SpanId":"3b7a9c2f1d8e4501","TraceFlags":"01","Body":"task execute","Resource":{"service.name":"maestro"},"Attributes":{"event":"task.started","pipeline_id":"01HZKX3P9M7Q2VFGR8BNDAW5YT","task_id":"T-042"}}
{"Timestamp":"1713534191483100000","ts_iso":"2026-04-19T14:23:11.483100Z","SeverityText":"INFO","SeverityNumber":9,"TraceId":"3f2e8c1a9b7d450f6e2c8a1b9f4d730e","SpanId":"9a2b1c0d5e4f3260","TraceFlags":"01","Body":"verifying task","Resource":{"service.name":"spec-runner"},"Attributes":{"event":"spec.verify.started","pipeline_id":"01HZKX3P9M7Q2VFGR8BNDAW5YT","task_id":"T-042","parent_span_id":"3b7a9c2f1d8e4501"}}
```

- [ ] **Step 1.6: Write `fixtures/error-chain.jsonl`**

```jsonl
{"Timestamp":"1713534191500000000","ts_iso":"2026-04-19T14:23:11.500000Z","SeverityText":"ERROR","SeverityNumber":17,"TraceId":"3f2e8c1a9b7d450f6e2c8a1b9f4d730e","SpanId":"9a2b1c0d5e4f3260","TraceFlags":"01","Body":"task failed","Resource":{"service.name":"spec-runner"},"Attributes":{"event":"task.failed","pipeline_id":"01HZKX3P9M7Q2VFGR8BNDAW5YT","task_id":"T-042","error":{"type":"RuntimeError","message":"verify failed","caused_by":{"type":"FileNotFoundError","message":"manifest.yaml missing"}}}}
```

- [ ] **Step 1.7: Write `fixtures/redacted.jsonl`**

```jsonl
{"Timestamp":"1713534191600000000","ts_iso":"2026-04-19T14:23:11.600000Z","SeverityText":"INFO","SeverityNumber":9,"TraceId":"3f2e8c1a9b7d450f6e2c8a1b9f4d730e","SpanId":"3b7a9c2f1d8e4501","TraceFlags":"01","Body":"api call","Resource":{"service.name":"spec-runner"},"Attributes":{"event":"http.request","pipeline_id":"01HZKX3P9M7Q2VFGR8BNDAW5YT","api_key":"<redacted>","url":"https://api.example"}}
```

- [ ] **Step 1.8: Validate all fixtures against schema**

Run:
```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/Maestro
uv run --with jsonschema python -c "
import json, pathlib
schema = json.loads(pathlib.Path('_cowork_output/observability-contract/log-schema.json').read_text())
from jsonschema import validate
for f in pathlib.Path('_cowork_output/observability-contract/fixtures').glob('*.jsonl'):
    for line in f.read_text().splitlines():
        validate(json.loads(line), schema)
    print(f'{f.name}: OK')
"
```
Expected: each file prints `OK`.

- [ ] **Step 1.9: Commit (in Maestro repo)**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/Maestro
git add _cowork_output/observability-contract/
git commit -m "feat(obs): contract v1 — JSON Schema + propagation.md + fixtures"
```

---

## Task 2: `obs.py` skeleton — `init_logging` + `get_logger`

**Files:**
- Create: `spec-runner/src/spec_runner/obs.py`
- Create: `spec-runner/tests/test_obs.py`
- Modify: `spec-runner/pyproject.toml` (add `ulid-py`)

- [ ] **Step 2.1: Add `ulid-py` dependency**

```bash
cd spec-runner
uv add ulid-py
```

Expected: `pyproject.toml` gains `ulid-py` under `dependencies`.

- [ ] **Step 2.2: Write failing test for `init_logging` + `get_logger`**

Create `spec-runner/tests/test_obs.py`:

```python
"""Unit tests for spec_runner.obs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_init_logging_creates_logger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)

    from spec_runner import obs

    obs.init_logging("spec-runner")
    log = obs.get_logger("test")
    log.info("hello.world", x=1)

    files = list(tmp_path.glob("spec-runner-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["Resource"]["service.name"] == "spec-runner"
    assert record["Attributes"]["event"] == "hello.world"
    assert record["Attributes"]["x"] == 1
    assert record["SeverityText"] == "INFO"
```

- [ ] **Step 2.3: Run the test, verify it fails**

```bash
cd spec-runner
uv run pytest tests/test_obs.py::test_init_logging_creates_logger -v
```
Expected: FAIL with `ImportError` or `AttributeError` (module/functions don't exist yet).

- [ ] **Step 2.4: Write minimal `obs.py`**

Create `spec-runner/src/spec_runner/obs.py`:

```python
"""Orchestra observability emitter — reference implementation.

Source of truth for `obs.py` (vendored into other Python projects).
Produces OpenTelemetry Logs Data Model JSONL, one file per PID.

Contract: see _cowork_output/observability-contract/log-schema.json
"""
from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import structlog
import ulid

_SEVERITY_NUMBER = {
    "debug": 5, "info": 9, "warning": 13, "warn": 13,
    "error": 17, "critical": 21, "fatal": 21,
}
_SEVERITY_TEXT = {
    5: "DEBUG", 9: "INFO", 13: "WARN", 17: "ERROR", 21: "FATAL",
}

_initialized = False


def _now_ns() -> int:
    return time.time_ns()


def _iso_micros(ns: int) -> str:
    dt = datetime.fromtimestamp(ns / 1_000_000_000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _reshape_to_otel(project: str):
    """Final processor: rearrange structlog dict into OTel Logs DM shape."""
    def processor(logger, method_name, event_dict):
        ns = event_dict.pop("_ts_ns", _now_ns())
        # structlog passes method_name: "info", "error", etc. Use it, not event_dict.
        sev_num = _SEVERITY_NUMBER.get(method_name.lower(), 9)
        event_dict.pop("level", None)   # drop if present; method_name is authoritative
        event_name = event_dict.pop("event")

        attrs = {"event": event_name}
        for key in ("pipeline_id", "parent_span_id", "task_id", "module"):
            if key in event_dict:
                attrs[key] = event_dict.pop(key)
        attrs.update(event_dict)

        return {
            "Timestamp": str(ns),
            "ts_iso": _iso_micros(ns),
            "SeverityText": _SEVERITY_TEXT[sev_num],
            "SeverityNumber": sev_num,
            "TraceId": attrs.pop("_trace_id", "0" * 32),
            "SpanId": attrs.pop("_span_id", "0" * 16),
            "TraceFlags": "01",
            "Body": attrs.pop("_body", event_name),
            "Resource": {"service.name": project},
            "Attributes": attrs,
        }
    return processor


def _default_log_dir() -> Path:
    env_dir = os.environ.get("ORCHESTRA_LOG_DIR")
    if env_dir:
        return Path(env_dir)
    pid = os.environ.get("ORCHESTRA_PIPELINE_ID") or str(ulid.new())
    return Path.cwd() / "logs" / pid


def init_logging(
    project: str,
    *,
    level: str | None = None,
    log_dir: Path | None = None,
) -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    log_dir = log_dir or _default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    output_path = log_dir / f"{project}-{os.getpid()}.jsonl"

    pipeline_id = os.environ.get("ORCHESTRA_PIPELINE_ID") or str(ulid.new())
    structlog.contextvars.bind_contextvars(
        pipeline_id=pipeline_id,
        _trace_id=secrets.token_hex(16),
        _span_id=secrets.token_hex(8),
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _reshape_to_otel(project),
            structlog.processors.JSONRenderer(sort_keys=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            {"debug": 10, "info": 20, "warning": 30, "error": 40}.get(
                (level or os.environ.get("ORCHESTRA_LOG_LEVEL") or "info").lower(), 20
            )
        ),
        logger_factory=structlog.WriteLoggerFactory(file=output_path.open("a")),
        cache_logger_on_first_use=True,
    )


def get_logger(module: str | None = None) -> structlog.BoundLogger:
    return structlog.get_logger(module=module) if module else structlog.get_logger()
```

- [ ] **Step 2.5: Run the test, verify it passes**

```bash
cd spec-runner
uv run pytest tests/test_obs.py::test_init_logging_creates_logger -v
```
Expected: PASS.

- [ ] **Step 2.6: Commit**

```bash
cd spec-runner
git add src/spec_runner/obs.py tests/test_obs.py pyproject.toml uv.lock
git commit -m "feat(obs): init_logging and get_logger skeleton"
```

---

## Task 3: `TRACEPARENT` parsing

**Files:**
- Modify: `spec-runner/src/spec_runner/obs.py`
- Modify: `spec-runner/tests/test_obs.py`

- [ ] **Step 3.1: Write failing tests**

Append to `spec-runner/tests/test_obs.py`:

```python
def test_traceparent_valid_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    tid = "3f2e8c1a9b7d450f6e2c8a1b9f4d730e"
    pid_span = "9f2e4a1b6c0d3387"
    monkeypatch.setenv("TRACEPARENT", f"00-{tid}-{pid_span}-01")
    monkeypatch.setenv("ORCHESTRA_PIPELINE_ID", "01HZKX3P9M7Q2VFGR8BNDAW5YT")

    # Re-import fresh to reset module state
    import importlib, spec_runner.obs as mod
    importlib.reload(mod)
    mod.init_logging("spec-runner")
    mod.get_logger().info("child.started")

    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    assert rec["TraceId"] == tid
    assert rec["Attributes"]["parent_span_id"] == pid_span
    assert rec["Attributes"]["pipeline_id"] == "01HZKX3P9M7Q2VFGR8BNDAW5YT"
    assert rec["SpanId"] != pid_span  # fresh span for this process


def test_traceparent_empty_means_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("TRACEPARENT", "")

    import importlib, spec_runner.obs as mod
    importlib.reload(mod)
    mod.init_logging("spec-runner")
    mod.get_logger().info("root.started")

    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    assert len(rec["TraceId"]) == 32
    assert "parent_span_id" not in rec["Attributes"]


def test_traceparent_malformed_warns_and_roots(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("TRACEPARENT", "garbage")

    import importlib, spec_runner.obs as mod
    importlib.reload(mod)
    mod.init_logging("spec-runner")
    mod.get_logger().info("recovered")

    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    assert len(rec["TraceId"]) == 32
    assert "parent_span_id" not in rec["Attributes"]
```

- [ ] **Step 3.2: Run the tests, verify they fail**

```bash
cd spec-runner
uv run pytest tests/test_obs.py -k traceparent -v
```
Expected: FAIL — parser doesn't exist / parent_span_id never set.

- [ ] **Step 3.3: Add `_parse_traceparent` and wire into `init_logging`**

In `spec-runner/src/spec_runner/obs.py`, add near the top-level helpers:

```python
import logging as _stdlib_logging
import re

_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def _parse_traceparent() -> tuple[str, str | None]:
    """Return (trace_id, parent_span_id). parent_span_id is None at root."""
    raw = os.environ.get("TRACEPARENT", "").strip()
    if not raw:
        return secrets.token_hex(16), None
    m = _TRACEPARENT_RE.match(raw)
    if not m:
        _stdlib_logging.getLogger(__name__).warning(
            "malformed TRACEPARENT=%r, treating as root", raw
        )
        return secrets.token_hex(16), None
    return m.group(1), m.group(2)
```

Replace the `structlog.contextvars.bind_contextvars(...)` call inside
`init_logging` with:

```python
    trace_id, parent_span_id = _parse_traceparent()
    bind_kwargs: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "_trace_id": trace_id,
        "_span_id": secrets.token_hex(8),
    }
    if parent_span_id is not None:
        bind_kwargs["parent_span_id"] = parent_span_id
    structlog.contextvars.bind_contextvars(**bind_kwargs)
```

Also, set the global `_initialized` flag back to `False` at the top of
`init_logging` for test re-import flow (tests reload the module). Add just
below the function signature:

```python
    global _initialized
    _initialized = False   # tests rely on reload; make idempotent by reset
    structlog.contextvars.clear_contextvars()
```

- [ ] **Step 3.4: Run the tests, verify they pass**

```bash
cd spec-runner
uv run pytest tests/test_obs.py -k traceparent -v
```
Expected: all 3 PASS.

- [ ] **Step 3.5: Commit**

```bash
cd spec-runner
git add src/spec_runner/obs.py tests/test_obs.py
git commit -m "feat(obs): TRACEPARENT parsing with graceful fallback"
```

---

## Task 4: Timestamp normalization (`Timestamp` ns-string + `ts_iso` micros)

**Files:**
- Modify: `spec-runner/tests/test_obs.py`
- Already covered by Task 2 code; only verification + explicit test here.

- [ ] **Step 4.1: Write test validating both timestamp fields**

Append to `spec-runner/tests/test_obs.py`:

```python
def test_timestamp_formats(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import importlib, spec_runner.obs as mod
    importlib.reload(mod)
    mod.init_logging("spec-runner")
    mod.get_logger().info("ts.check")

    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    # Timestamp: ns since epoch, string, ~19 digits in 2026
    assert rec["Timestamp"].isdigit() and 18 <= len(rec["Timestamp"]) <= 20
    # ts_iso: microseconds, Z suffix
    assert rec["ts_iso"].endswith("Z")
    assert len(rec["ts_iso"].split(".")[1]) == 7   # "NNNNNNZ" = 6 digits + Z
```

- [ ] **Step 4.2: Run test, verify it passes**

```bash
cd spec-runner
uv run pytest tests/test_obs.py::test_timestamp_formats -v
```
Expected: PASS.

- [ ] **Step 4.3: Commit**

```bash
cd spec-runner
git add tests/test_obs.py
git commit -m "test(obs): timestamp format (ns-string + ISO micros)"
```

---

## Task 5: Span management — `span()` context manager

**Files:**
- Modify: `spec-runner/src/spec_runner/obs.py`
- Modify: `spec-runner/tests/test_obs.py`

- [ ] **Step 5.1: Write failing test**

Append to `spec-runner/tests/test_obs.py`:

```python
def test_span_nesting_linkage(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import importlib, spec_runner.obs as mod
    importlib.reload(mod)
    mod.init_logging("spec-runner")
    log = mod.get_logger()

    with mod.span("outer.op") as outer:
        log.info("inside.outer")
        with mod.span("inner.op") as inner:
            log.info("inside.inner")
            assert inner.parent_span_id == outer.span_id

    lines = [json.loads(l)
             for l in list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()]
    inner_records = [r for r in lines if r["Body"] in ("inside.inner", "inner.op.started")]
    for r in inner_records:
        assert r["Attributes"].get("parent_span_id") == outer.span_id


def test_span_emits_started_and_ended(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import importlib, spec_runner.obs as mod
    importlib.reload(mod)
    mod.init_logging("spec-runner")
    with mod.span("op.do", x=1):
        pass
    events = [json.loads(l)["Attributes"]["event"]
              for l in list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()]
    assert "op.do.started" in events
    assert "op.do.ended" in events


def test_span_failure_emits_failed_and_reraises(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import importlib, spec_runner.obs as mod
    importlib.reload(mod)
    mod.init_logging("spec-runner")
    with pytest.raises(RuntimeError):
        with mod.span("op.do"):
            raise RuntimeError("boom")
    lines = [json.loads(l)
             for l in list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()]
    failed = [r for r in lines if r["Attributes"]["event"] == "op.do.failed"]
    assert len(failed) == 1
    assert failed[0]["Attributes"]["error"]["type"] == "RuntimeError"
    assert failed[0]["Attributes"]["error"]["message"] == "boom"
```

- [ ] **Step 5.2: Run tests, verify they fail**

```bash
cd spec-runner
uv run pytest tests/test_obs.py -k span -v
```
Expected: FAIL — `mod.span` doesn't exist.

- [ ] **Step 5.3: Implement `span()` and `Span`**

Append to `spec-runner/src/spec_runner/obs.py`:

```python
class Span:
    def __init__(self, span_id: str, parent_span_id: str | None, trace_id: str):
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.trace_id = trace_id
        self._attrs: dict[str, Any] = {}

    def set_attrs(self, **attrs: Any) -> None:
        self._attrs.update(attrs)


def _exc_to_dict(exc: BaseException) -> dict[str, Any]:
    d: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        d["caused_by"] = _exc_to_dict(cause)
    return d


@contextmanager
def span(event: str, **attrs: Any) -> Iterator[Span]:
    log = get_logger()
    ctx = structlog.contextvars.get_contextvars()
    parent_span_id = ctx.get("_span_id")
    trace_id = ctx.get("_trace_id", "0" * 32)

    new_span_id = secrets.token_hex(8)
    sp = Span(new_span_id, parent_span_id, trace_id)

    # push new span, parent_span_id
    structlog.contextvars.bind_contextvars(
        _span_id=new_span_id,
        parent_span_id=parent_span_id,
    )
    log.info(f"{event}.started", **attrs)
    try:
        yield sp
    except BaseException as exc:
        log.error(f"{event}.failed", error=_exc_to_dict(exc), **sp._attrs)
        raise
    else:
        log.info(f"{event}.ended", **sp._attrs)
    finally:
        # restore previous span context
        structlog.contextvars.unbind_contextvars("_span_id", "parent_span_id")
        if parent_span_id is not None:
            structlog.contextvars.bind_contextvars(_span_id=parent_span_id)
```

- [ ] **Step 5.4: Run tests, verify they pass**

```bash
cd spec-runner
uv run pytest tests/test_obs.py -k span -v
```
Expected: 3 tests PASS.

- [ ] **Step 5.5: Commit**

```bash
cd spec-runner
git add src/spec_runner/obs.py tests/test_obs.py
git commit -m "feat(obs): span context manager with error chains"
```

---

## Task 6: Redaction processor

**Files:**
- Modify: `spec-runner/src/spec_runner/obs.py`
- Modify: `spec-runner/tests/test_obs.py`

- [ ] **Step 6.1: Write failing test**

Append to `spec-runner/tests/test_obs.py`:

```python
def test_redaction_default_blocklist(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import importlib, spec_runner.obs as mod
    importlib.reload(mod)
    mod.init_logging("spec-runner")
    mod.get_logger().info(
        "http.request", api_key="sk-secret", password="p", url="https://x"
    )
    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    assert rec["Attributes"]["api_key"] == "<redacted>"
    assert rec["Attributes"]["password"] == "<redacted>"
    assert rec["Attributes"]["url"] == "https://x"


def test_redaction_nested(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import importlib, spec_runner.obs as mod
    importlib.reload(mod)
    mod.init_logging("spec-runner")
    mod.get_logger().info("ctx", headers={"Authorization": "Bearer t", "X-Req": "1"})
    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    assert rec["Attributes"]["headers"]["Authorization"] == "<redacted>"
    assert rec["Attributes"]["headers"]["X-Req"] == "1"


def test_redaction_extended_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    monkeypatch.setenv("ORCHESTRA_REDACT_KEYS", "ssn,pin")
    import importlib, spec_runner.obs as mod
    importlib.reload(mod)
    mod.init_logging("spec-runner")
    mod.get_logger().info("pii", ssn="123", pin="1234", name="Alice")
    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    assert rec["Attributes"]["ssn"] == "<redacted>"
    assert rec["Attributes"]["pin"] == "<redacted>"
    assert rec["Attributes"]["name"] == "Alice"
```

- [ ] **Step 6.2: Run tests, verify they fail**

```bash
cd spec-runner
uv run pytest tests/test_obs.py -k redaction -v
```
Expected: FAIL (no redaction yet).

- [ ] **Step 6.3: Implement redaction processor**

In `spec-runner/src/spec_runner/obs.py`, add before `_reshape_to_otel`:

```python
_DEFAULT_REDACT_KEYS = frozenset({
    "api_key", "apikey", "token", "password", "secret",
    "authorization", "cookie", "private_key",
})


def _redact(keys: frozenset[str]):
    def _walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: ("<redacted>" if k.lower() in keys else _walk(v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    def processor(logger, method_name, event_dict):
        return {
            k: ("<redacted>" if k.lower() in keys else _walk(v))
            for k, v in event_dict.items()
        }
    return processor
```

Extend `init_logging` signature with a `redact_keys` parameter and
assemble the full blocklist. Replace the existing `init_logging`
body with:

```python
def init_logging(
    project: str,
    *,
    level: str | None = None,
    log_dir: Path | None = None,
    redact_keys: list[str] | None = None,
) -> None:
    global _initialized
    _initialized = False
    structlog.contextvars.clear_contextvars()
    _initialized = True

    log_dir = log_dir or _default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    output_path = log_dir / f"{project}-{os.getpid()}.jsonl"

    pipeline_id = os.environ.get("ORCHESTRA_PIPELINE_ID") or str(ulid.new())
    trace_id, parent_span_id = _parse_traceparent()

    bind_kwargs: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "_trace_id": trace_id,
        "_span_id": secrets.token_hex(8),
    }
    if parent_span_id is not None:
        bind_kwargs["parent_span_id"] = parent_span_id
    structlog.contextvars.bind_contextvars(**bind_kwargs)

    env_extra = os.environ.get("ORCHESTRA_REDACT_KEYS", "")
    env_keys = {k.strip().lower() for k in env_extra.split(",") if k.strip()}
    param_keys = {k.lower() for k in (redact_keys or [])}
    all_redact = frozenset(_DEFAULT_REDACT_KEYS | env_keys | param_keys)

    level_name = (level or os.environ.get("ORCHESTRA_LOG_LEVEL") or "info").lower()
    min_level = {"debug": 10, "info": 20, "warning": 30, "error": 40}.get(level_name, 20)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _redact(all_redact),
            _reshape_to_otel(project),
            structlog.processors.JSONRenderer(sort_keys=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(min_level),
        logger_factory=structlog.WriteLoggerFactory(file=output_path.open("a")),
        cache_logger_on_first_use=True,
    )
```

- [ ] **Step 6.4: Run tests, verify they pass**

```bash
cd spec-runner
uv run pytest tests/test_obs.py -k redaction -v
```
Expected: 3 tests PASS.

- [ ] **Step 6.5: Commit**

```bash
cd spec-runner
git add src/spec_runner/obs.py tests/test_obs.py
git commit -m "feat(obs): redaction processor with default + env-extended blocklist"
```

---

## Task 7: `child_env()` for subprocess propagation

**Files:**
- Modify: `spec-runner/src/spec_runner/obs.py`
- Modify: `spec-runner/tests/test_obs.py`

- [ ] **Step 7.1: Write failing test**

Append to `spec-runner/tests/test_obs.py`:

```python
def test_child_env_contains_traceparent(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import importlib, spec_runner.obs as mod
    importlib.reload(mod)
    mod.init_logging("spec-runner")
    with mod.span("outer") as s:
        env = mod.child_env()
    tp = env["TRACEPARENT"]
    assert tp.startswith("00-")
    parts = tp.split("-")
    assert len(parts[1]) == 32   # trace_id
    assert parts[2] == s.span_id  # parent for the child = current span
    assert parts[3] == "01"
    assert "ORCHESTRA_PIPELINE_ID" in env
    assert env["ORCHESTRA_LOG_DIR"] == str(tmp_path)
```

- [ ] **Step 7.2: Run test, verify it fails**

```bash
cd spec-runner
uv run pytest tests/test_obs.py::test_child_env_contains_traceparent -v
```
Expected: FAIL — `child_env` doesn't exist.

- [ ] **Step 7.3: Implement `child_env()`**

Append to `spec-runner/src/spec_runner/obs.py`:

```python
def child_env() -> dict[str, str]:
    """Env-var dict to merge into subprocess env= for trace propagation."""
    ctx = structlog.contextvars.get_contextvars()
    trace_id = ctx.get("_trace_id", "0" * 32)
    span_id = ctx.get("_span_id", "0" * 16)
    pipeline_id = ctx.get("pipeline_id", "")
    env = {
        "TRACEPARENT": f"00-{trace_id}-{span_id}-01",
        "ORCHESTRA_PIPELINE_ID": pipeline_id,
    }
    log_dir = os.environ.get("ORCHESTRA_LOG_DIR")
    if log_dir:
        env["ORCHESTRA_LOG_DIR"] = str(Path(log_dir).resolve())
    return env


def current_trace_id() -> str | None:
    return structlog.contextvars.get_contextvars().get("_trace_id")


def current_span_id() -> str | None:
    return structlog.contextvars.get_contextvars().get("_span_id")


def current_pipeline_id() -> str | None:
    return structlog.contextvars.get_contextvars().get("pipeline_id")
```

- [ ] **Step 7.4: Run test, verify it passes**

```bash
cd spec-runner
uv run pytest tests/test_obs.py::test_child_env_contains_traceparent -v
```
Expected: PASS.

- [ ] **Step 7.5: Commit**

```bash
cd spec-runner
git add src/spec_runner/obs.py tests/test_obs.py
git commit -m "feat(obs): child_env() for subprocess trace propagation"
```

---

## Task 8: Contract test — validate `obs.py` output against schema

**Files:**
- Create: `spec-runner/tests/test_obs_contract.py`

- [ ] **Step 8.1: Add `jsonschema` dev dep**

```bash
cd spec-runner
uv add --dev jsonschema
```

- [ ] **Step 8.2: Write the contract test**

Create `spec-runner/tests/test_obs_contract.py`:

```python
"""Validates obs.py output against _cowork_output/observability-contract/."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import jsonschema
import pytest

_SPEC_RUNNER_ROOT = Path(__file__).resolve().parents[2]
_UMBRELLA = _SPEC_RUNNER_ROOT.parent  # all_ai_orchestrators/
_CONTRACT = _UMBRELLA / "Maestro" / "_cowork_output" / "observability-contract"
_SCHEMA = json.loads((_CONTRACT / "log-schema.json").read_text())


@pytest.fixture
def obs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv(
        "TRACEPARENT",
        "00-3f2e8c1a9b7d450f6e2c8a1b9f4d730e-9f2e4a1b6c0d3387-01",
    )
    monkeypatch.setenv("ORCHESTRA_PIPELINE_ID", "01HZKX3P9M7Q2VFGR8BNDAW5YT")
    import spec_runner.obs as mod
    importlib.reload(mod)
    mod.init_logging("spec-runner")
    return mod, tmp_path


def test_emits_schema_valid_records(obs_env):
    mod, tmp_path = obs_env
    with mod.span("spec.verify.task", task_id="T-042"):
        mod.get_logger("execution").info("check.started", check_type="syntax")
    for line in list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines():
        jsonschema.validate(json.loads(line), _SCHEMA)


def test_fixture_root_span_is_schema_valid():
    for line in (_CONTRACT / "fixtures" / "root-span.jsonl").read_text().splitlines():
        jsonschema.validate(json.loads(line), _SCHEMA)


def test_fixture_nested_span_is_schema_valid():
    for line in (_CONTRACT / "fixtures" / "nested-span.jsonl").read_text().splitlines():
        jsonschema.validate(json.loads(line), _SCHEMA)
```

- [ ] **Step 8.3: Run tests, verify they pass**

```bash
cd spec-runner
uv run pytest tests/test_obs_contract.py -v
```
Expected: 3 tests PASS.

- [ ] **Step 8.4: Commit**

```bash
cd spec-runner
git add tests/test_obs_contract.py pyproject.toml uv.lock
git commit -m "test(obs): contract validation against shared schema and fixtures"
```

---

## Task 9: Run full spec-runner test suite (regression check)

- [ ] **Step 9.1: Run full test suite**

```bash
cd spec-runner
uv run pytest
```
Expected: all tests PASS. `test_logging.py` still passes (we haven't touched the existing `logging.py` module yet).

If anything fails: investigate before proceeding. Do not go to Task 10.
(Note: `test_logging.py` may still use the old API — that is expected and handled in Task 10's cutover.)

- [ ] **Step 9.2: Run type checker**

```bash
cd spec-runner
uv run pyrefly check
```
Expected: 0 errors introduced by `obs.py`. Fix any new errors before proceeding.

- [ ] **Step 9.3: Run formatter + linter**

```bash
cd spec-runner
uv run ruff format src/spec_runner/obs.py tests/test_obs.py tests/test_obs_contract.py
uv run ruff check src/spec_runner/obs.py tests/test_obs.py tests/test_obs_contract.py
```
Expected: clean. If ruff makes changes, commit them.

---

## Task 10: Spec-runner cutover — `logging.py` → shim over `obs.py`

**Rationale:** without this, real spec-runner logs do not carry `TraceId`, so M1 is only demonstrable via the stand-in child in Task 15 — not on a live pipeline. This task makes spec-runner emit contract-compliant records end-to-end.

**Files:**
- Modify: `spec-runner/src/spec_runner/logging.py` — replace body with a thin shim delegating to `obs.py`
- Modify: `spec-runner/tests/test_logging.py` — update assertions to OTel Logs DM shape where format-sensitive
- Possibly modify: call sites of `setup_logging` (CLI entry) — only if the shim's signature cannot absorb existing kwargs

- [ ] **Step 10.1: Inspect current `setup_logging` callers**

```bash
cd spec-runner
grep -rn "setup_logging\|from spec_runner.logging\|from spec_runner\.logging" src tests | grep -v __pycache__
```

Record the call sites — typically `cli.py` / entry points. Note the exact kwargs used.

- [ ] **Step 10.2: Inspect current `test_logging.py` to see which assertions are format-sensitive**

```bash
cd spec-runner
cat tests/test_logging.py
```

Classify each test:
- **Shape-sensitive** (asserts `event` key at top level, or specific processor ordering) — will need to be rewritten in Step 10.5 to assert OTel Logs DM shape.
- **API-sensitive** (calls `setup_logging(level=..., json_output=..., log_file=..., tui_mode=...)`) — keep as-is after the shim preserves that signature.

- [ ] **Step 10.3: Rewrite `setup_logging` as a shim**

Replace the entire body of `spec-runner/src/spec_runner/logging.py` with:

```python
"""Back-compat shim over spec_runner.obs.

The canonical entrypoint is now `spec_runner.obs.init_logging`. This module
remains for existing callers that import `setup_logging`.
"""
from __future__ import annotations

from pathlib import Path

from spec_runner import obs


def setup_logging(
    level: str = "info",
    json_output: bool = True,   # ignored — obs always emits JSON
    log_file: Path | None = None,
    tui_mode: bool = False,     # ignored — obs writes to file; stdout stays free
) -> None:
    """Delegate to obs.init_logging; preserved signature for back-compat."""
    log_dir = log_file.parent if log_file else None
    obs.init_logging("spec-runner", level=level, log_dir=log_dir)


__all__ = ["setup_logging"]
```

- [ ] **Step 10.4: Run `test_logging.py`, classify failures**

```bash
cd spec-runner
uv run pytest tests/test_logging.py -v
```

Expected: some failures. For each failure:
- If it asserts `event` at top level → rewrite to assert `Attributes.event`.
- If it asserts specific processor pipeline — tests were testing the old implementation, not a contract. Either rewrite to assert OTel Logs DM shape, or delete the test if it was purely internal.
- If it asserts `setup_logging()` signature — should still pass.

- [ ] **Step 10.5: Rewrite format-sensitive assertions**

For each shape-sensitive test, change assertions from:

```python
# OLD
assert record["event"] == "foo.bar"
assert record["task_id"] == "T-1"
```

to:

```python
# NEW (OTel Logs DM)
assert record["Attributes"]["event"] == "foo.bar"
assert record["Attributes"]["task_id"] == "T-1"
```

Timestamps:
```python
# OLD
assert "timestamp" in record
# NEW
assert record["Timestamp"].isdigit()
assert record["ts_iso"].endswith("Z")
```

Service identity:
```python
# OLD (if present)
assert record["module"] == "execution"
# NEW
assert record["Resource"]["service.name"] == "spec-runner"
# module still available as Attributes.module where set
```

- [ ] **Step 10.6: Run updated `test_logging.py`**

```bash
cd spec-runner
uv run pytest tests/test_logging.py -v
```

Expected: all PASS. If anything still fails because it was asserting a truly removed behavior (e.g., a specific legacy processor that no longer exists), delete that test with a one-line commit note and move on.

- [ ] **Step 10.7: Run full spec-runner test suite**

```bash
cd spec-runner
uv run pytest
```

Expected: all PASS. If unrelated tests fail, they are likely asserting log output format elsewhere — apply the same rewrite pattern.

- [ ] **Step 10.8: Run pyrefly + ruff**

```bash
cd spec-runner
uv run ruff format src/spec_runner/logging.py tests/test_logging.py
uv run ruff check src/spec_runner/logging.py tests/test_logging.py
uv run pyrefly check
```

Expected: clean.

- [ ] **Step 10.9: Smoke-check at the CLI level**

```bash
cd spec-runner
rm -rf /tmp/spec-runner-cutover-check
mkdir /tmp/spec-runner-cutover-check
ORCHESTRA_LOG_DIR=/tmp/spec-runner-cutover-check \
  uv run python -c "
from spec_runner.logging import setup_logging
import structlog
setup_logging(level='info')
structlog.get_logger().info('smoke.check', k='v')
"
ls /tmp/spec-runner-cutover-check/
cat /tmp/spec-runner-cutover-check/spec-runner-*.jsonl
```

Expected: one file `spec-runner-<pid>.jsonl` containing one OTel Logs DM record with `Attributes.event == "smoke.check"`.

- [ ] **Step 10.10: Commit**

```bash
cd spec-runner
git add src/spec_runner/logging.py tests/test_logging.py
git commit -m "refactor(obs): cutover logging.py to obs.py shim"
```

---

## Task 11: Vendor `obs.py` into Maestro

**Files:**
- Create: `Maestro/maestro/_vendor/__init__.py`
- Create: `Maestro/maestro/_vendor/obs.py`
- Modify: `Maestro/pyproject.toml`
- Create: `Maestro/tests/test_vendor_obs.py`

- [ ] **Step 11.1: Add Maestro dependencies**

```bash
cd Maestro
uv add structlog ulid-py
uv add --dev jsonschema
```

- [ ] **Step 11.2: Get current spec-runner commit SHA**

```bash
cd spec-runner
git rev-parse HEAD
```
Copy the hash (call it `<SHA>` below).

- [ ] **Step 11.3: Create `_vendor` package**

Create `Maestro/maestro/_vendor/__init__.py` (empty file).

- [ ] **Step 11.4: Copy `obs.py` with freeze marker**

Copy file contents from `spec-runner/src/spec_runner/obs.py` into
`Maestro/maestro/_vendor/obs.py`. At the very top, before the existing
docstring, insert:

```python
# Vendored from spec-runner@<SHA> — observability contract v1
# Do not edit locally. To update: re-copy from spec-runner and bump marker.
```

Replace `<SHA>` with the hash from Step 11.2.

- [ ] **Step 11.5: Write smoke test for vendor**

Create `Maestro/tests/test_vendor_obs.py`:

```python
"""Smoke test — vendored obs.py behaves identically for trace plumbing."""
from __future__ import annotations

import importlib
import json


def test_vendored_obs_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import maestro._vendor.obs as obs
    importlib.reload(obs)
    obs.init_logging("maestro")
    with obs.span("t.op"):
        obs.get_logger().info("hello")
    files = list(tmp_path.glob("maestro-*.jsonl"))
    assert len(files) == 1
    assert len(files[0].read_text().splitlines()) >= 3  # started + hello + ended
    for line in files[0].read_text().splitlines():
        rec = json.loads(line)
        assert rec["Resource"]["service.name"] == "maestro"
```

- [ ] **Step 11.6: Run test**

```bash
cd Maestro
uv run pytest tests/test_vendor_obs.py -v
```
Expected: PASS.

- [ ] **Step 11.7: Commit**

```bash
cd Maestro
git add maestro/_vendor/ tests/test_vendor_obs.py pyproject.toml uv.lock
git commit -m "feat(obs): vendor obs.py from spec-runner@<SHA>"
```

---

## Task 12: Maestro — `init_logging()` at CLI entry + `child_env()` in spawner

**Files:**
- Modify: `Maestro/maestro/cli.py`
- Modify: `Maestro/maestro/orchestrator.py`

- [ ] **Step 12.1: Locate the subprocess call site**

Read `Maestro/maestro/orchestrator.py` — find the `subprocess.run` or
`Popen` call (around line 360 per earlier audit). Note the line numbers
and the `env=` argument if any.

```bash
cd Maestro
grep -n "subprocess\.\(run\|Popen\)" maestro/orchestrator.py
```

Expected output: one or more hits. Record each line for Step 12.3.

- [ ] **Step 12.2: Add `init_logging` to CLI entry**

Open `Maestro/maestro/cli.py`. At the top-level entry function (the
`main()` or Click command that starts a run), add near the top of the
function body:

```python
from maestro._vendor.obs import init_logging

init_logging("maestro")
```

Place it **before** any orchestrator code executes.

- [ ] **Step 12.3: Wrap task execution in a span + inject `child_env()`**

In `Maestro/maestro/orchestrator.py`, at the top add:

```python
from maestro._vendor.obs import child_env, span
```

Find each `subprocess.run(...)` / `subprocess.Popen(...)` call. For each:
- Wrap it in a `with span("task.execute", task_id=task.id):` context
  (use the appropriate task id variable from the surrounding code).
- Change `env=…` to `env={**os.environ, **child_env(), **(custom_env or {})}`.
  If there was no `env=` kwarg, add it: `env={**os.environ, **child_env()}`.

If `os` is not imported, add `import os` at the top of the file.

- [ ] **Step 12.4: Run Maestro test suite**

```bash
cd Maestro
uv run pytest
```
Expected: all existing tests PASS. No regressions.

If there are failures: check whether tests mock subprocess and need
adjustment for the new `env=` kwarg. Fix those tests to accept the
extra env keys (e.g. `env=ANY` or checking `TRACEPARENT in env`).

- [ ] **Step 12.5: Commit**

```bash
cd Maestro
git add maestro/cli.py maestro/orchestrator.py
git commit -m "feat(obs): Maestro init_logging + child_env in subprocess spawning"
```

---

## Task 13: `merge-logs` CLI

**Files:**
- Create: `Maestro/maestro/merge_logs.py`
- Modify: `Maestro/maestro/cli.py` (register subcommand)
- Create: `Maestro/tests/test_merge_logs.py`

- [ ] **Step 13.1: Write failing test**

Create `Maestro/tests/test_merge_logs.py`:

```python
"""Unit tests for maestro.merge_logs."""
from __future__ import annotations

import json
from pathlib import Path

from maestro.merge_logs import merge_logs_dir


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_merge_sorts_by_timestamp_across_files(tmp_path):
    pipeline_dir = tmp_path / "01HZKX"
    pipeline_dir.mkdir()
    _write_jsonl(
        pipeline_dir / "maestro-100.jsonl",
        [{"Timestamp": "1000000000000000002", "Body": "b"},
         {"Timestamp": "1000000000000000004", "Body": "d"}],
    )
    _write_jsonl(
        pipeline_dir / "spec-runner-200.jsonl",
        [{"Timestamp": "1000000000000000001", "Body": "a"},
         {"Timestamp": "1000000000000000003", "Body": "c"}],
    )
    merge_logs_dir(pipeline_dir)
    merged = (pipeline_dir / "merged.jsonl").read_text().splitlines()
    bodies = [json.loads(line)["Body"] for line in merged]
    assert bodies == ["a", "b", "c", "d"]


def test_merge_tolerates_malformed_lines(tmp_path):
    pipeline_dir = tmp_path / "01HZKY"
    pipeline_dir.mkdir()
    (pipeline_dir / "maestro-1.jsonl").write_text(
        '{"Timestamp": "1", "Body": "ok"}\n'
        'garbage line\n'
        '{"Timestamp": "2", "Body": "ok2"}\n'
    )
    merge_logs_dir(pipeline_dir)
    merged = (pipeline_dir / "merged.jsonl").read_text().splitlines()
    assert len(merged) == 2  # garbage dropped


def test_merge_on_empty_dir_writes_empty_merged(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    merge_logs_dir(d)
    assert (d / "merged.jsonl").exists()
    assert (d / "merged.jsonl").read_text() == ""
```

- [ ] **Step 13.2: Run test, verify it fails**

```bash
cd Maestro
uv run pytest tests/test_merge_logs.py -v
```
Expected: FAIL — module doesn't exist.

- [ ] **Step 13.3: Implement `merge_logs.py`**

Create `Maestro/maestro/merge_logs.py`:

```python
"""Standalone merge-logs CLI — time-sorts per-pid JSONL into merged.jsonl.

Works on partial runs (after SIGKILL). Tolerates malformed lines
(drops them with a stderr warning).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _load_records(path: Path) -> list[dict]:
    records: list[dict] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            sys.stderr.write(f"warning: {path}:{lineno} malformed JSON, skipped\n")
    return records


def merge_logs_dir(pipeline_dir: Path) -> Path:
    """Merge all *.jsonl under pipeline_dir (excluding merged.jsonl) by Timestamp."""
    pipeline_dir = Path(pipeline_dir)
    all_records: list[dict] = []
    for jsonl in sorted(pipeline_dir.glob("*.jsonl")):
        if jsonl.name == "merged.jsonl":
            continue
        all_records.extend(_load_records(jsonl))
    all_records.sort(key=lambda r: int(r.get("Timestamp", "0")))
    out = pipeline_dir / "merged.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in all_records) + ("\n" if all_records else ""))
    return out


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        sys.stderr.write("usage: maestro merge-logs <pipeline_dir-or-id>\n")
        return 2
    target = Path(argv[0])
    if not target.exists():
        # allow passing a pipeline_id; resolve under ./logs/
        candidate = Path("logs") / argv[0]
        if candidate.exists():
            target = candidate
        else:
            sys.stderr.write(f"error: {argv[0]} not found\n")
            return 1
    out = merge_logs_dir(target)
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 13.4: Run tests, verify they pass**

```bash
cd Maestro
uv run pytest tests/test_merge_logs.py -v
```
Expected: 3 tests PASS.

- [ ] **Step 13.5: Register `merge-logs` subcommand in CLI**

In `Maestro/maestro/cli.py`, add a new command. Pattern depends on the
CLI framework — Maestro uses Click per convention; the exact insertion
looks like:

```python
from maestro import merge_logs as _merge_logs


@cli.command("merge-logs")
@click.argument("target", type=str)
def merge_logs_cmd(target: str) -> None:
    """Time-sort per-pid JSONL under a pipeline directory into merged.jsonl."""
    raise SystemExit(_merge_logs.main([target]))
```

Place this adjacent to other `@cli.command(...)` definitions. If
Maestro uses argparse instead of Click, adapt to the argparse pattern
present in the file.

- [ ] **Step 13.6: Smoke test via CLI**

```bash
cd Maestro
mkdir -p /tmp/merge-test/01HZ
echo '{"Timestamp":"2","Body":"b"}' > /tmp/merge-test/01HZ/a-1.jsonl
echo '{"Timestamp":"1","Body":"a"}' > /tmp/merge-test/01HZ/b-2.jsonl
uv run python -m maestro.cli merge-logs /tmp/merge-test/01HZ
cat /tmp/merge-test/01HZ/merged.jsonl
```
Expected output (order by Timestamp):
```
{"Timestamp": "1", "Body": "a"}
{"Timestamp": "2", "Body": "b"}
```

- [ ] **Step 13.7: Commit**

```bash
cd Maestro
git add maestro/merge_logs.py maestro/cli.py tests/test_merge_logs.py
git commit -m "feat(obs): standalone merge-logs CLI with tolerant parser"
```

---

## Task 14: Register `merge-logs` in Maestro's pipeline finally block

**Files:**
- Modify: `Maestro/maestro/orchestrator.py` (or wherever pipeline teardown lives)

- [ ] **Step 14.1: Locate pipeline teardown**

```bash
cd Maestro
grep -n "finally\|shutdown\|cleanup" maestro/orchestrator.py | head -20
```

Identify the function that owns "pipeline complete" lifecycle. This is
the place where a `try: ... finally: merge_logs_dir(...)` wraps the
whole run.

- [ ] **Step 14.2: Import and call merge at end-of-run**

At the top of `orchestrator.py`:

```python
from maestro.merge_logs import merge_logs_dir
from maestro._vendor.obs import current_pipeline_id
```

In the top-level run function, wrap the body in:

```python
try:
    # existing pipeline logic
    ...
finally:
    pid = current_pipeline_id()
    if pid:
        from pathlib import Path
        merge_logs_dir(Path(os.environ.get("ORCHESTRA_LOG_DIR", f"logs/{pid}")))
```

- [ ] **Step 14.3: Run Maestro test suite**

```bash
cd Maestro
uv run pytest
```
Expected: all tests PASS.

- [ ] **Step 14.4: Commit**

```bash
cd Maestro
git add maestro/orchestrator.py
git commit -m "feat(obs): auto-merge logs in pipeline finally block"
```

---

## Task 15: Cross-process integration test

**Files:**
- Create: `Maestro/tests/test_obs_integration.py`
- Create: `Maestro/tests/_obs_child.py` — stand-in child script

- [ ] **Step 15.1: Create child stand-in script**

Create `Maestro/tests/_obs_child.py`:

```python
"""Stand-in child for obs integration test — spawned by test_obs_integration."""
from __future__ import annotations

import sys

# Re-use vendored obs from Maestro so we don't need spec-runner installed
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from maestro._vendor import obs


def main() -> int:
    obs.init_logging("spec-runner")  # pretend to be spec-runner
    log = obs.get_logger("child")
    with obs.span("child.work", task_id="T-test"):
        log.info("child.doing.stuff", step=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 15.2: Write integration test**

Create `Maestro/tests/test_obs_integration.py`:

```python
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
    parent_trace_id_holder = {}
    parent_span_id_holder = {}

    with obs.span("pipeline.run", dag_name="test") as outer:
        with obs.span("task.execute", task_id="T-test") as inner:
            parent_trace_id_holder["v"] = inner.trace_id
            parent_span_id_holder["v"] = inner.span_id
            proc = subprocess.run(
                [sys.executable, str(child_script)],
                env={**os.environ, **obs.child_env()},
                capture_output=True, text=True, check=False,
            )
            assert proc.returncode == 0, proc.stderr

    from maestro.merge_logs import merge_logs_dir
    merge_logs_dir(log_dir)

    merged = [
        json.loads(line)
        for line in (log_dir / "merged.jsonl").read_text().splitlines()
    ]
    assert merged, "merged.jsonl is empty"

    # All records share the same TraceId
    trace_ids = {r["TraceId"] for r in merged}
    assert len(trace_ids) == 1
    assert parent_trace_id_holder["v"] in trace_ids

    # Child's root span (first spec-runner record) links to parent's current span
    child_records = [r for r in merged if r["Resource"]["service.name"] == "spec-runner"]
    assert child_records, "no spec-runner records"
    first_child = child_records[0]
    assert first_child["Attributes"].get("parent_span_id") == parent_span_id_holder["v"]

    # Timestamps are monotonic in merged output
    ts = [int(r["Timestamp"]) for r in merged]
    assert ts == sorted(ts)
```

- [ ] **Step 15.3: Run integration test**

```bash
cd Maestro
uv run pytest tests/test_obs_integration.py -v
```
Expected: PASS.

If it fails: diagnose by reading `logs/` output directly. Common causes:
- `TRACEPARENT` not reset between test runs — check `monkeypatch` scope.
- `merge-logs` sorting comparator wrong — check Task 13.
- Child's `init_logging` not idempotent — check Task 3.5 reset logic.

- [ ] **Step 15.4: Run full Maestro suite**

```bash
cd Maestro
uv run pytest
```
Expected: all tests PASS, no regressions.

- [ ] **Step 15.5: Run type checker + linter**

```bash
cd Maestro
uv run ruff format maestro/_vendor/ maestro/merge_logs.py maestro/cli.py maestro/orchestrator.py tests/test_obs_integration.py tests/test_merge_logs.py tests/test_vendor_obs.py tests/_obs_child.py
uv run ruff check maestro/_vendor/ maestro/merge_logs.py tests/
uv run pyrefly check
```
Expected: clean. Commit formatter diffs if any.

- [ ] **Step 15.6: Commit**

```bash
cd Maestro
git add tests/test_obs_integration.py tests/_obs_child.py
git commit -m "test(obs): cross-process trace continuity (M1)"
```

---

## Task 16: M1 milestone verification

With Task 10 complete, real spec-runner emits contract-compliant records — so this step now verifies M1 on a **live pipeline**, not just on the stand-in child.

- [ ] **Step 16.1: Run a real Maestro pipeline**

Pick a small existing example (under `Maestro/examples/`) that invokes spec-runner. Run it:

```bash
cd Maestro
uv run python -m maestro.cli run examples/<pick-smallest>.yaml
```

Expected: pipeline succeeds. `logs/<pipeline_id>/merged.jsonl` exists and contains records from both `maestro-<pid>.jsonl` and `spec-runner-<pid>.jsonl`.

- [ ] **Step 16.2: Verify with `jq`**

```bash
# Pick the most recent pipeline_id
PIPELINE_ID=$(ls -t Maestro/logs/ | head -1)
MERGED="Maestro/logs/$PIPELINE_ID/merged.jsonl"

# All records share one TraceId
jq -r '.TraceId' "$MERGED" | sort -u
# Expected: exactly one line

# Both projects emit records
jq -r '.Resource["service.name"]' "$MERGED" | sort -u
# Expected: two lines — "maestro" and "spec-runner"

# Span tree: find a task.execute span and its children
jq 'select(.Attributes.event == "task.execute.started") | .SpanId' "$MERGED"
# Expected: one or more span IDs printed

# For one such span id, find children
jq --arg pid "<span_id_from_above>" \
   'select(.Attributes.parent_span_id == $pid) | .Body' "$MERGED"
# Expected: logs from the task body, including spec-runner records
```

- [ ] **Step 16.3: Record milestone achievement**

Write `_cowork_output/status/obs-m1-reached-<YYYY-MM-DD>.md`:

```markdown
# Observability M1 reached — <date>

Pipeline `<pipeline_id>` on `<example>.yaml`:
- One TraceId across maestro + spec-runner (live, not stand-in): ✅
- parent_span_id linkage correct: ✅
- merge-logs on partial runs works: ✅
- Contract tests green in spec-runner and Maestro: ✅

Next: Plan 2 (to M2) — Rust arbiter obs.rs, arbiter Python client vendor,
ATP integration, jq cookbook.
```

- [ ] **Step 16.4: Commit milestone note**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators
git add _cowork_output/status/obs-m1-reached-*.md
git commit -m "milestone(obs): M1 reached — cross-process trace continuity"
```

---

## Done criteria (Plan 1)

- `Maestro/_cowork_output/observability-contract/` has schema, propagation doc, rationale, fixtures.
- `spec-runner/src/spec_runner/obs.py` emits schema-valid JSONL.
- `spec-runner/src/spec_runner/logging.py` is a back-compat shim over `obs.py`; all spec-runner logs flow through the new emitter.
- Maestro vendors `obs.py`, uses `child_env()` at subprocess boundaries, merges at end.
- `maestro merge-logs` CLI works on live and incomplete runs.
- Integration test proves trace continuity across one subprocess boundary.
- Live `maestro run` on a real example produces `merged.jsonl` with one `TraceId` across `maestro` and `spec-runner` records (Task 16).
- spec-runner + Maestro test suites green, no regressions.
- M1 status note recorded.

## Out of scope (Plan 2)

- Rust `arbiter-core/src/obs.rs`.
- Vendor `obs.py` into arbiter Python client.
- ATP structlog chain extension.
- `jq` cookbook (`docs/debugging.md`).
- proctor-a integration (backlog).
