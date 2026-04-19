# Contract v1 — Design Rationale

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
