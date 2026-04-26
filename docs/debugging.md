# Debugging a pipeline with jq

Every process in a Maestro-spawned pipeline writes OpenTelemetry Logs Data
Model JSONL to `$ORCHESTRA_LOG_DIR/<service>-<pid>.jsonl`. After the run,
`maestro merge-logs <pipeline_id>` sorts all per-pid files by nanosecond
`Timestamp` into `merged.jsonl`. From there `jq` is the main debugger.

Contract: `Maestro/_cowork_output/observability-contract/log-schema.json`.
Services in v1: `maestro`, `spec-runner`, `arbiter`, `atp`.

All recipes below assume you have run `maestro merge-logs <pipeline_id>` and
are in the run directory — paths are relative to
`logs/<pipeline_id>/merged.jsonl`.

---

## 1. What went wrong in this run?

Every error or warning across the whole pipeline, newest last:

```bash
jq -c 'select(.SeverityNumber >= 13)
       | {ts_iso, svc: .Resource."service.name", ev: .Attributes.event,
          body: .Body, err: .Attributes.error}' \
    merged.jsonl
```

`SeverityNumber >= 13` covers WARN (13), ERROR (17), FATAL (21). Drop to 17
for errors only.

---

## 2. Reconstruct the span tree of one task

Given a `task_id` (e.g. `T-042`), show every record that touched it with
indentation by span depth:

```bash
jq -r '
  select(.Attributes.task_id == "T-042")
  | "\(.ts_iso)  \(.Attributes.parent_span_id // "-")/\(.SpanId)
     \(.Resource."service.name")  \(.Attributes.event)  \(.Body)"
' merged.jsonl
```

`parent_span_id/SpanId` lets you eyeball the tree: each record whose
`SpanId` equals some other record's `parent_span_id` is that other record's
parent. For a structured tree use recipe 3.

---

## 3. Follow one TraceId across all four services

A pipeline has one `TraceId`; pull every record for it and group by
service:

```bash
TRACE_ID=$(jq -r '.TraceId' merged.jsonl | head -1)

jq -c --arg tid "$TRACE_ID" '
  select(.TraceId == $tid)
  | {svc: .Resource."service.name", ts_iso, ev: .Attributes.event,
     span: .SpanId, parent: .Attributes.parent_span_id}
' merged.jsonl
```

Useful after you extracted the `TraceId` from a single known-bad record —
this shows what every other service was doing at the same point.

---

## 4. Slowest N spans (using `.started` / `.ended` pairs)

Span lifecycle is emitted as synthetic `<name>.started` and `<name>.ended`
events carrying the same `SpanId`. Their timestamp delta is the span
duration. Top 10 longest spans in the run:

```bash
jq -c 'select(.Attributes.event | endswith(".started") or endswith(".ended"))
       | {sid: .SpanId, ts: (.Timestamp|tonumber), ev: .Attributes.event}' \
    merged.jsonl \
  | jq -s '
      group_by(.sid)
      | map(select(length == 2))
      | map({
          span_id: .[0].sid,
          event: (.[0].ev | rtrimstr(".started")),
          duration_ms: ((.[1].ts - .[0].ts) / 1e6 | floor)
        })
      | sort_by(-.duration_ms)
      | .[:10]
  '
```

`ns → ms` via `/1e6`. A span that started but never closed shows up in the
group-by as `length == 1` and is filtered out — investigate those
separately with recipe 1 (likely crashed mid-span).

---

## 5. Cross-project timeline for a failed task

Combine 1 + 2 + 3: for a task that failed in `arbiter`, show the
chronological cross-service context five seconds before and after the
error:

```bash
TASK_ID=T-042

# Find the error record for the task
ERR_NS=$(jq -r --arg tid "$TASK_ID" '
  select(.Attributes.task_id == $tid and .SeverityNumber >= 17)
  | .Timestamp
' merged.jsonl | head -1)

# Window: +/- 5 seconds around the error
jq -c --arg tid "$TASK_ID" --argjson err "$ERR_NS" '
  select(
    (.Timestamp|tonumber) >= ($err|tonumber) - 5e9
    and (.Timestamp|tonumber) <= ($err|tonumber) + 5e9
  )
  | {ts: .ts_iso, svc: .Resource."service.name", ev: .Attributes.event,
     sev: .SeverityText, body: .Body, task: .Attributes.task_id}
' merged.jsonl
```

Widen the window (`5e9` → `60e9` for a full minute) if the error is slow
to surface.

---

## 6. Error chains (`error.caused_by` walk)

When an emitter records an exception via the `error` attribute, nested
causes live under `error.caused_by`. Flatten the chain:

```bash
jq -c 'select(.Attributes.error != null)
       | {ts: .ts_iso, svc: .Resource."service.name",
          chain: [.Attributes.error |
                  recurse(.caused_by; . != null) |
                  {type, message}]}' \
    merged.jsonl
```

Each record's `chain` array reads outer-to-inner, so the last element is
the root cause.

---

## 7. Verify redaction didn't leak a secret

Every value matching a default-redacted key must be literally
`"<redacted>"`. A quick audit:

```bash
jq -c 'select(.Attributes | to_entries[]
               | select(.key | ascii_downcase |
                        test("api_key|token|password|secret|authorization|cookie|private_key"))
               | select(.value != "<redacted>"))
       | {ts: .ts_iso, svc: .Resource."service.name",
          ev: .Attributes.event, body: .Body}' \
    merged.jsonl
```

Any hits mean the blocklist is incomplete for your call sites — add the
offending key via `ORCHESTRA_REDACT_KEYS=key1,key2` and re-run, or extend
`obs.DEFAULT_REDACT_KEYS` (Python) /
`arbiter_core::obs::DEFAULT_REDACT_KEYS` (Rust) and re-vendor.

---

## 8. Per-service record count (sanity check)

After M1/M2 you expect at least the four services to show up:

```bash
jq -r '.Resource."service.name"' merged.jsonl | sort | uniq -c | sort -rn
```

If `arbiter` is missing, `arbiter-mcp`'s stdout is going to the MCP
protocol but `init_logging` couldn't open the sink — check the parent
process for `WARNING: obs::init_logging failed` on stderr.

---

## Tips

- `jq -c` (compact) is better for piping; drop `-c` for pretty output.
- `tonumber` on `.Timestamp` is needed because the contract stores it as
  a string (OTel canonical — preserves ns precision).
- `jq -s` (slurp) reads the whole file into an array — use sparingly on
  large runs; prefer line-oriented `jq -c` where possible.
- Merging JSONL across the whole monorepo: `maestro merge-logs <dir>`
  handles multiple per-pid files + is tolerant of malformed lines
  (SIGKILL / OOM mid-write).
