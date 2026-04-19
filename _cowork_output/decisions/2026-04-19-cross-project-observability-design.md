# Cross-Project Observability Design

**Date:** 2026-04-19 (rev. 2 after review)
**Status:** Draft — awaiting user approval before implementation planning
**Scope:** Maestro, spec-runner, arbiter, atp-platform, proctor-a

> **Revision note.** Revision 1 proposed a custom log schema and custom env-variable propagation protocol, with vendored Python reference code and parallel Rust implementation. Internal review rejected the custom protocol in favour of OpenTelemetry-native propagation and wire format; this revision adopts OTel Logs Data Model as the contract and W3C Trace Context as the propagation protocol, while keeping emitters custom and dependency footprint minimal. See §11 for the decision log.

---

## 1. Problem

Debugging a multi-project pipeline (Maestro → spec-runner → arbiter → ATP) is currently hard: no shared run identifier, logs land in disparate formats across 3 projects using stdlib `logging`, 2 using structlog, and one using Rust `tracing`. Subprocess stdout capture loses task context. Finding the root cause of a failure requires manual correlation by `task_id` across separately-formatted log files.

Audit (2026-04-19) confirmed:

- **Maestro** — stdlib logging + custom JSONL `EventLogger`; no correlation ID threaded through stdlib side.
- **spec-runner** — structlog JSON; no correlation ID bound.
- **arbiter** — stdlib Python + Rust `tracing`; no trace context, no `tracing-opentelemetry` bridge.
- **ATP** — structlog + OpenTelemetry SDK (OTLP exporter, TracerProvider). `correlation_id` ContextVar exists. Isolated — not hooked to other projects.
- **proctor-a** — stdlib logging, plain text; not yet integrated into ecosystem.

## 2. Goal

A unified observability contract that:

- Threads a **single trace through the entire pipeline**, including subprocess boundaries and the Rust component.
- Produces structured JSON logs with parent/child span relationships.
- Works today with zero external infrastructure (no Collector, no Jaeger, no Docker).
- Has a clean upgrade path to a full OpenTelemetry Collector + OTLP backend when the ecosystem moves from local dev to production-like environments.

**Non-goals (v1):** metrics/histograms, sampling, centralized UI, OTLP network export, distributed alerts, log rotation/retention. Deferred to v2 once v1 is proven in dogfooding.

## 3. Approach — OTel contract, custom emitters (path C)

**Contract = OpenTelemetry Logs Data Model + W3C Trace Context. Emitters = our code.**

- The **wire format** is OTel Logs Data Model (OTel spec v1.30+), with a small set of explicitly-marked custom attributes for features Logs DM does not cover (pipeline-wide human id, parent-span linkage in a logs-only export, our event taxonomy).
- The **propagation protocol** is W3C Trace Context (`traceparent` env var at subprocess boundaries).
- **Python emitters** are ~30-50 LOC structlog processors that render the Logs DM shape. `opentelemetry-sdk` is **not a v1 dependency** — we don't need its processors, samplers, or exporters while we write to a local file.
- **Rust emitter** (in arbiter-core) is `tracing-opentelemetry` + `opentelemetry_stdout` — the native path, well-supported, ~50 LOC of setup.
- The Python reference emitter is vendored into each Python project under `_vendor/obs.py` (same pattern as `coordination/arbiter_client.py` in Maestro).

### 3.1 Why this approach over the alternatives

| | Custom contract + custom code (A) | OTel SDK everywhere (B) | OTel contract + custom emitter (C, chosen) |
|---|---|---|---|
| Python LOC (new) | ~200 (vendored) | ~50 bridge + ~30 file-exporter | ~150 (vendored) |
| Rust LOC (new) | ~150 (custom) | ~50 via `tracing-opentelemetry` | ~50 via `tracing-opentelemetry` |
| Python deps | `structlog` | `structlog` + `opentelemetry-api` + `opentelemetry-sdk` | `structlog` |
| Contract to self-freeze | yes | no | no |
| ATP adapter required | yes (1-3d) | no | no |
| Upgrade to v2 | rewrite 5 places | swap exporter URL | swap one vendored file in Python; Rust unchanged |
| Risk of Python/Rust format drift | high | low | low (both emit the same OTel shape) |
| Estimate to M2 | 13-17 d | 6-9 d | 7-10 d |

Rationale: the main thing OTel buys is a frozen, polyglot contract. We take it without dragging the SDK into every Python project for the v1 file-only case. If a bridge turns out brittle during implementation, the fallback is a file-level swap onto `opentelemetry-sdk` (still path B), not a redesign.

## 4. Contract v1

### 4.1 Log record schema

Every log line is one JSON object in OTel Logs Data Model shape. Example:

```json
{
  "Timestamp": "1713534191482913000",
  "ts_iso": "2026-04-19T14:23:11.482913Z",
  "SeverityText": "INFO",
  "SeverityNumber": 9,
  "TraceId": "3f2e8c1a9b7d450f6e2c8a1b9f4d730e",
  "SpanId": "3b7a9c2f1d8e4501",
  "TraceFlags": "01",
  "Body": "Starting verification of task T-042",
  "Resource": {"service.name": "spec-runner"},
  "Attributes": {
    "event": "task.started",
    "parent_span_id": "9f2e4a1b6c0d3387",
    "pipeline_id": "01HZKX3P9M7Q2VFGR8BNDAW5YT",
    "task_id": "T-042",
    "module": "execution",
    "retry": 0,
    "timeout_s": 30
  }
}
```

| Field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `Timestamp` | OTel Logs DM | string, nanoseconds since Unix epoch | yes | String (not JSON number) to preserve precision. OTel-canonical |
| `ts_iso` | our extension | ISO 8601 UTC with microseconds and `Z` | yes | Human/`jq`-friendly derived field. Ignored by OTel consumers |
| `SeverityText` | OTel Logs DM | `DEBUG`/`INFO`/`WARN`/`ERROR`/`FATAL` | yes | |
| `SeverityNumber` | OTel Logs DM | int 1-24 | yes | DEBUG=5, INFO=9, WARN=13, ERROR=17, FATAL=21 |
| `TraceId` | OTel Logs DM | 32 hex chars (16 random bytes) | yes | W3C-compliant random. Shared across a pipeline |
| `SpanId` | OTel Logs DM | 16 hex chars (8 random bytes) | yes | Per-operation. Unique even across processes |
| `TraceFlags` | OTel Logs DM | 2 hex | yes | Always `"01"` in v1 (sampled); no sampling logic |
| `Body` | OTel Logs DM | string | yes | Human-readable message. Falls back to `Attributes.event` if no message given |
| `Resource` | OTel Logs DM | object | yes | At minimum `{"service.name": "<project>"}` where `<project>` ∈ `maestro/spec-runner/arbiter/atp/proctor-a` |
| `Attributes` | OTel Logs DM | object | yes | Structured key-value map. See §4.2 for reserved keys |
| `InstrumentationScope` | OTel Logs DM | object | no | Optional: `{"name": "<module>"}` — we populate when available |
| `ObservedTimestamp` | OTel Logs DM | string, ns | no | Omitted in v1 (equal to `Timestamp` at write time) |

### 4.2 Reserved `Attributes` keys

OTel Attributes are free-form; we reserve the following keys so all projects emit them consistently:

| Key | Type | Required | Purpose |
|---|---|---|---|
| `event` | string matching `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$` | yes | Dotted event name (e.g. `task.started`). Regex enforced in CI |
| `parent_span_id` | 16 hex chars or omitted | no | Our span-tree linkage in a logs-only export. Absent for a root span |
| `pipeline_id` | ULID (26 chars Crockford base32) | yes | Human-readable pipeline identifier. Sortable, copy-paste-friendly |
| `task_id` | string | no | spec-runner task / Maestro step id |
| `module` | string | no | Logger/module name, mirrored from `InstrumentationScope.name` when set |
| `error` | object | no | Present when `SeverityNumber >= 17`. Shape: `{type: str, message: str, stack?: str, caused_by?: <error>}` (recursive via `caused_by`) |
| `*` (any) | any JSON | no | Free-form user attributes |

**Note on custom attributes.** `parent_span_id` and `pipeline_id` are not part of OTel Logs DM. They are explicitly-marked extensions (see §11 for rationale). They are discarded safely by any OTel consumer that does not know about them; they are used by our local `merge-logs` tooling. If/when we export real Span records (deferred), `parent_span_id` becomes redundant for operators that read span data, but remains harmless.

### 4.3 Propagation — W3C Trace Context

**Env variables:**

| Variable | Required | Purpose |
|---|---|---|
| `TRACEPARENT` | yes at subprocess boundary | W3C Trace Context: `00-<trace_id_32hex>-<span_id_16hex>-<flags_2hex>`. Root process generates it if the env is empty at startup; every non-root process MUST receive it from its parent |
| `ORCHESTRA_PIPELINE_ID` | no (follows `TRACEPARENT`) | ULID. Root generates, children inherit. In a future v2 this can migrate to `TRACESTATE` as `orchestra=pipeline_id=...` |
| `ORCHESTRA_LOG_DIR` | no | Absolute path to output directory. Default: `<cwd>/logs/<pipeline_id>/` at root; Maestro always injects an absolute path when spawning subprocesses |
| `ORCHESTRA_LOG_LEVEL` | no | Default: `INFO` |
| `ORCHESTRA_LOG_FORMAT` | no | `json` (default, always) or `console` (opt-in for interactive dev) |

**Subprocess-boundary rule:**

1. Parent opens a span around the `Popen` call.
2. Parent writes `TRACEPARENT = 00-<current_trace_id>-<current_span_id>-01` into child env.
3. Parent writes `ORCHESTRA_PIPELINE_ID`, `ORCHESTRA_LOG_DIR` (absolute) into child env.
4. Child's `init_logging()` parses `TRACEPARENT`, extracts `trace_id` and the **parent** `span_id` (to fill its root span's `Attributes.parent_span_id`), generates a fresh `span_id` for its own root span.
5. Child's stdout may be captured by the parent; parent does not transform it (the child already writes contract-compliant JSONL to `ORCHESTRA_LOG_DIR`).

**Parser robustness:** the `TRACEPARENT` parser MUST tolerate empty strings (treated as "I am root") and malformed values (logged as a warning, treated as root). No crash on bad input.

### 4.4 Sinks — file-per-process

- Each process writes to `$ORCHESTRA_LOG_DIR/<service.name>-<pid>.jsonl` (append, O_APPEND).
- **Rationale:** POSIX atomicity of `write(O_APPEND)` is guaranteed only up to `PIPE_BUF` (4096 B on Linux, 512 B on macOS). A single stack trace can exceed this; concurrent writes from multiple Maestro-spawned spec-runner workers to a single `spec-runner.jsonl` would interleave. Per-process files avoid the problem entirely. The slight proliferation of files is handled by the merge step.
- `stderr` is reserved for human-readable crash output that occurs before `init_logging()` succeeds.
- `maestro merge-logs <pipeline_id>` is a **standalone CLI** (not a pipeline-end-only hook) that:
  - accepts a directory or a pipeline_id
  - globs `*.jsonl` under it
  - produces `merged.jsonl` — time-sorted union by `Timestamp` (the tool converts the ns-string to int for sorting)
  - works on incomplete runs (after SIGKILL / OOM)
  - Maestro invokes it in its own `finally` / shutdown path; users invoke it manually for anything else.

### 4.5 Redaction (PII / secrets)

The v1 policy: **these are local dev logs, not for export.** This is stated explicitly in the contract README. In addition, the emitter runs a **redaction processor** before JSON rendering:

- A configurable blocklist of `Attributes` keys — default: `api_key`, `token`, `password`, `secret`, `authorization`, `cookie`, `private_key`. Matched case-insensitive, recursively in nested objects.
- Matched values are replaced with `"<redacted>"`.
- The default list is overridable via `obs.init_logging(..., redact_keys=[...])` and extendable via env `ORCHESTRA_REDACT_KEYS` (comma-separated).
- Redaction runs on `Attributes` and `Body` is **not scanned** (structured logging, not free-text, is the expected pattern).

## 5. Python reference API (`obs.py`)

Single file, ~150 LOC, dependency: `structlog`. Vendored into Maestro, arbiter Python client, proctor-a; ATP extends its existing structlog chain with our processor but keeps its file layout.

```python
def init_logging(
    project: str,
    *,
    level: str | None = None,
    log_dir: Path | None = None,
    redact_keys: list[str] | None = None,  # extends default blocklist
    extra_sinks: list[Sink] = (),           # e.g. Maestro EventLogger
) -> None: ...

def get_logger(module: str | None = None) -> structlog.BoundLogger: ...

@contextmanager
def span(event: str, **attrs) -> Iterator[Span]: ...

class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    pipeline_id: str
    def set_attrs(self, **attrs: Any) -> None: ...
    def record_exception(self, exc: BaseException) -> None: ...

def traced(event: str | None = None): ...  # decorator form of span()

def child_env() -> dict[str, str]: ...      # TRACEPARENT + ORCHESTRA_PIPELINE_ID + LOG_DIR

def current_trace_id() -> str | None: ...
def current_span_id() -> str | None: ...
def current_pipeline_id() -> str | None: ...

def bind(**kv: Any) -> None: ...            # binds into structlog contextvars
def unbind(*keys: str) -> None: ...
```

### 5.1 Implementation notes

structlog processor chain (order matters):

```
merge_contextvars
  → add_log_level                           # fills SeverityText/Number
  → _inject_trace_context                   # TraceId, SpanId, parent_span_id from contextvars
  → _inject_pipeline_id                     # from contextvars
  → _timestamp                              # Timestamp (ns string) + ts_iso
  → _redact                                 # before rendering
  → _to_otel_logs_shape                     # reshape dict → Logs DM JSON
  → JSONRenderer(sort_keys=False)
```

- `trace_id`, `span_id`, `parent_span_id`, `pipeline_id` live in `structlog.contextvars`. `span()` pushes a new `span_id`, records the previous as `parent_span_id`, restores on exit.
- `Timestamp` is built from `time.time_ns()` — single call, formatted as string. `ts_iso` is derived by `datetime.fromtimestamp(ns/1e9, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")` (microseconds, never nanoseconds — OTel exporters read `Timestamp`, humans read `ts_iso`).
- File writer: opens `$LOG_DIR/<project>-<pid>.jsonl` with `O_APPEND`, line-buffered. One file per process, no locks, no contention.
- `init_logging()` is idempotent (safe to call twice; second call is a no-op).
- Works in sync and async — contextvars are async-safe.
- `TRACEPARENT` parser tolerates empty and malformed input (logs a warning, generates a new root trace).

### 5.2 Per-project usage

**Maestro (pipeline root, spawner):**

```python
init_logging("maestro", extra_sinks=[event_logger_sink])
with span("pipeline.run", dag_name=dag.name, n_tasks=len(dag.tasks)):
    for task in dag.topological_order():
        with span("task.execute", task_id=task.id) as s:
            proc = subprocess.run(
                task.cmd,
                env={**os.environ, **child_env()},
                capture_output=True, text=True,
            )
            s.set_attrs(exit_code=proc.returncode)
```

**spec-runner / arbiter-client / proctor-a (subprocess):**

```python
init_logging("spec-runner")  # reads TRACEPARENT from env
log = get_logger("execution")
with span("spec.verify", task_id=task.id):
    log.info("check.started", check_type=check.kind)
```

**ATP (keeps its existing OTel TracerProvider):**

ATP does **not** vendor `obs.py`. It adds the same structlog processors (`_inject_trace_context`, `_inject_pipeline_id`, `_timestamp`, `_redact`, `_to_otel_logs_shape`) to its existing chain. ATP's `_correlation_id` ContextVar is unchanged — it's an internal concept. Our processors read `TRACEPARENT` / current OTel span context via the OTel API that ATP already has, and emit `TraceId`/`SpanId` accordingly. ATP's existing OTLP exporter stays; it becomes the first OTLP-capable participant for v2.

### 5.3 Rust mirror (`arbiter-core/src/obs.rs`)

Uses the native OTel path — no hand-rolled Layer needed:

```rust
pub fn init_logging(project: &str) -> Result<()>;  // sets up tracing-subscriber
                                                    // with tracing-opentelemetry layer
                                                    // pointing at opentelemetry_stdout
                                                    // writing to file-per-pid
pub fn child_env() -> HashMap<String, String>;     // TRACEPARENT + ORCHESTRA_*
```

Crates: `tracing` (already in `arbiter-core`), `tracing-subscriber`, `tracing-opentelemetry`, `opentelemetry`, `opentelemetry_sdk`, `opentelemetry-stdout`. The OTel crates are new but lightweight (pure-Rust, no system dependencies). No gRPC/protobuf pulled in for v1.

`opentelemetry_stdout` emits OTel Logs DM JSON natively; we configure it to write to `$LOG_DIR/arbiter-<pid>.jsonl`. The `parent_span_id` extension is added via a custom `Layer` that intercepts log events and adds the current span's parent id into `Attributes`.

## 6. Testing strategy

One shared contract, five CI checks.

### 6.1 JSON Schema + golden fixtures

The contract repo `_cowork_output/observability-contract/` ships:
- `log-schema.json` — JSON Schema covering OTel Logs DM core fields + our reserved Attributes keys.
- `fixtures/` — golden `*.jsonl` files demonstrating: root span, nested span, cross-process propagation, error with `caused_by` chain, redacted attribute.

### 6.2 Per-project contract test

Each Python project runs:

```python
def test_obs_contract_v1(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("TRACEPARENT",
        "00-3f2e8c1a9b7d450f6e2c8a1b9f4d730e-9f2e4a1b6c0d3387-01")
    init_logging("spec-runner")
    with span("test.event", x=1):
        get_logger().info("hello")
    lines = list(tmp_path.glob("spec-runner-*.jsonl"))[0].read_text().splitlines()
    for line in lines:
        record = json.loads(line)
        jsonschema.validate(record, CONTRACT_SCHEMA_V1)
```

arbiter-core runs an equivalent Rust test with `serde_json` + the `jsonschema` crate against the same fixture.

**Important:** comparisons are on **parsed JSON values**, never byte strings. Python `structlog`/`json.dumps` and Rust `serde_json` do not produce identical byte output (field order differs), and that's fine — the Schema validates structure.

### 6.3 Cross-process integration test

A pytest in Maestro that:
1. runs `maestro run` on a minimal DAG with a spec-runner task and an arbiter call;
2. collects `logs/<pipeline_id>/*.jsonl`;
3. asserts all records share one `TraceId`;
4. asserts the child root span's `parent_span_id` equals the Maestro span's `SpanId`;
5. runs `merge-logs` and asserts timestamps monotonic.

## 7. Migration plan

| # | Step | Location | Deliverable | Done criterion | Est. |
|---|---|---|---|---|---|
| 0 | Write contract | `_cowork_output/observability-contract/` | `log-schema.json`, `propagation.md`, `fixtures/*.jsonl`, `rationale.md` (decisions in §11) | JSON Schema validates; golden fixtures cover required fields, nesting, error chain, redaction | 1d |
| 1 | Reference `obs.py` | spec-runner | `src/spec_runner/obs.py` + unit tests + contract test + redaction tests | All spec-runner logs flow through `obs.py`; old structlog setup removed; `pytest -k obs` passes | 2d |
| 2 | Vendor to Maestro + `child_env()` + standalone `merge-logs` CLI | Maestro | `maestro/_vendor/obs.py`, init at entrypoint, `env={…, **child_env()}` in all `Popen`, `maestro merge-logs` subcommand | E2E: `maestro run` produces `logs/<pipeline_id>/{maestro,spec-runner}-<pid>.jsonl` with shared `TraceId` and correct `parent_span_id`; `merge-logs` handles incomplete runs | 2d |
| **M1** | **Milestone: cross-process correlation** | | | `jq 'select(.TraceId=="...")' logs/*/merged.jsonl` shows the full pipeline history, span tree reconstructible from `Attributes.parent_span_id` | |
| 3a | Rust emitter via `tracing-opentelemetry` + `opentelemetry_stdout` | arbiter-core | `arbiter-core/src/obs.rs`, custom Layer for `parent_span_id`, file-per-pid writer | Rust output passes the same JSON Schema and golden fixtures as Python | 2d |
| 3b | Vendor `obs.py` in arbiter Python client | arbiter | `orchestrator/_vendor/obs.py` | Python client logs and Rust logs share `TraceId`, have distinct `SpanId` | 0.5d |
| 4 | ATP integration (no vendor) | atp-platform | Extend ATP's structlog chain with our processors; ensure `TraceId`/`SpanId` pulled from existing OTel context | ATP in pipeline emits contract-compliant JSONL; all 301 ATP tests still pass; ATP's existing OTLP exporter unchanged | 1d |
| 5 | `jq` cookbook | Maestro | `docs/debugging.md` with 5+ recipes (all errors, span tree of one task, slowest N, cross-project timeline, redaction check) | Cookbook on fixture pipeline produces expected output | 0.5d |
| **M2** | **Milestone: unified debugging** | | | Any error in any of the 4 core projects is findable with one `jq` query on `merged.jsonl` | |
| — | proctor-a | proctor-a | Deferred until proctor-a enters the pipeline (currently standalone). Moved to backlog. | | — |

**Total to M2:** 7-10 person-days. M1 reachable in ~5 days. (Revised from rev. 1's 7-10 days, which the reviewer correctly flagged as optimistic for the custom-everything path; path C pulls the estimate back down because there is no parallel Rust implementation and no ATP adapter.)

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Python and Rust emit slightly different OTel Logs DM shapes in edge cases | Shared JSON Schema + same golden fixtures validated in both CIs; comparison on parsed JSON, never byte strings |
| `parent_span_id` as a custom Attribute becomes non-portable to strict OTel consumers | Documented as an extension in `rationale.md`; consumers that don't know about it drop it safely. When we export real Span records in v2, the extension becomes redundant but not harmful |
| Python structlog-to-OTel-Logs-DM bridge turns out fragile | Fallback: swap the vendored `obs.py` to a thin wrapper over `opentelemetry-sdk` + a custom file `LogRecordExporter` (path B). Swap is one file in each vendor copy, not a redesign |
| TRACEPARENT parser crashes on malformed input and brings down a subprocess | Parser is explicitly tolerant (empty → root; malformed → warning + root); tested with bad inputs |
| Concurrent writes interleave | Solved structurally: file-per-pid (§4.4) |
| Time-format drift between Python (microseconds) and Rust (nanoseconds) | Contract specifies `Timestamp` as nanoseconds string; `ts_iso` as microseconds with `Z`. Both emitters follow the same formatting rules; unit tests cover the conversion |
| Vendored `obs.py` copies drift after freeze | Freeze marker with `@sha` in each vendor copy + CI check "`obs.py` matches upstream" (same pattern as `arbiter_client.py`) |
| Secrets leak into logs via `Attributes` | Redaction processor with default blocklist (§4.5); policy stated: v1 logs are dev-only, not for export |
| OTel Logs DM spec evolves and breaks us | We pin to a specific OTel spec version in `rationale.md`; upgrade is a minor schema tweak, not a rewrite |
| `merge-logs` not run after SIGKILL / OOM | `merge-logs` is standalone, not a pipeline-end hook; can be run manually on any `logs/<pipeline_id>/` at any time |

## 9. Explicitly deferred (not in v1)

- `opentelemetry-sdk` + OTLP exporter (v2 upgrade). When added, swap one vendored Python file; Rust unchanged.
- OTel Collector, Jaeger, Docker-compose observability stack.
- Metrics and histograms.
- Sampling.
- Emitting real Span records (separate trace stream); `Attributes.parent_span_id` fills the span-tree need for v1.
- `TRACESTATE`-encoded `pipeline_id` (v1 uses a separate env var; migration to tracestate is mechanical).
- Centralized UI / dashboard.
- Log rotation and retention.
- proctor-a integration (waits until proctor-a enters the pipeline).

## 10. Success criteria

- **M1** — a Maestro pipeline that invokes spec-runner produces `merged.jsonl` where every record shares one `TraceId`, the child's root span's `Attributes.parent_span_id` equals Maestro's spawning span's `SpanId`, and `jq` can reconstruct the span tree.
- **M2** — a deliberate failure in arbiter surfaces in `merged.jsonl` alongside surrounding Maestro/spec-runner/ATP context, findable with one `jq` query on `TraceId`. Error chain (`Attributes.error.caused_by`) traversable.
- Zero regressions in ATP's 301 tests; zero regressions in Maestro, spec-runner, arbiter test suites after integration.
- CI in all four core projects runs the contract test against the shared schema and golden fixtures.
- Redaction verified: a test putting `api_key` and `password` in `Attributes` confirms they are replaced with `"<redacted>"` in the output file.

## 11. Decision log

**Why OTel Logs Data Model as the contract (vs our own schema).**
We would have frozen our own v1 schema and maintained it ourselves. OTel Logs DM is already frozen by a standards body, tooling understands it, and upgrade to a Collector is a format-preserving transport change. The schema we actually write is OTel's schema plus a few Attributes extensions we document explicitly.

**Why custom emitters (vs `opentelemetry-sdk` in every Python project).**
For v1 we only need file output. `opentelemetry-sdk` is designed for a richer pipeline (processors, samplers, exporters) that we don't use. Adding the dep buys nothing concrete at v1 and costs ~3 MB per venv. When v2 adds a Collector, a single vendored file swaps for an SDK-based implementation; the wire format does not change.

**Why W3C `TRACEPARENT` (vs our own env-var namespace).**
Our own env vars duplicated the W3C standard with no upside. `TRACEPARENT` is parsed by any OTel-aware library for free. ATP already consumes it natively.

**Why a separate env var for `pipeline_id` (vs encoding in `TRACESTATE`).**
Simplicity in v1. `TRACESTATE` is the right long-term home and the migration is mechanical (string manipulation); documenting it as deferred is cheaper than implementing it now.

**Why random 16 bytes for `TraceId` + separate ULID `pipeline_id` (vs ULID as `TraceId`).**
OTel recommends random trace_ids. Some backends treat the upper bits as random in sampling decisions. A ULID has 48 bits of timestamp prefix that would skew any such logic. We pay the cost of a separate field (`pipeline_id`) to get both clean W3C/OTel compatibility and human-readable identifiers for filenames, copy-paste, and jq queries.

**Why file-per-pid (vs a single per-project file).**
POSIX `write(O_APPEND)` atomicity is limited to `PIPE_BUF` (4096 B on Linux, 512 B on macOS). A Python traceback exceeds this and would interleave with concurrent writes from sibling spec-runner workers spawned by Maestro. File-per-pid removes the class of bug entirely with no locking.

**Why keep `ts_iso` alongside `Timestamp`.**
`Timestamp` is OTel-canonical (nanoseconds as string) but hostile to `jq`: string-sorting requires fixed width, arithmetic requires conversion. `ts_iso` costs ~20 bytes per record and makes local debugging pleasant. OTel consumers ignore unknown top-level keys.

**Why `parent_span_id` as a custom Attribute (vs emitting real Span records).**
Real Span records are a separate OTLP signal with their own schema, requiring a second exporter and a second merge path. For v1 (logs-only, local files, span tree reconstruction for debugging) a custom Attribute on log records is enough. v2 can add real spans for timing/tree UIs without removing the Attribute.
