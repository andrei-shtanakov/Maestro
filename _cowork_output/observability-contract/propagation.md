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
