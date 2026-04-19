# Observability M1 reached — 2026-04-19

## Summary
Cross-project observability Plan 1 completed. Maestro and spec-runner share a unified trace via W3C TRACEPARENT propagation; logs emit OpenTelemetry Logs Data Model JSONL; `merge-logs` assembles a single time-sorted merged.jsonl.

## M1 criteria verification

| Criterion | Status | Evidence |
|---|---|---|
| One TraceId across maestro + spec-runner | PASS | integration test `test_trace_continuity_across_subprocess` — single TraceId `979b4685d0362bebfbdcc5f2fd459a2b` across all 7 records |
| parent_span_id linkage correct | PASS | child's root span `parent_span_id=f9a6e1e6b7cbfaeb` matches Maestro's spawning `task.execute` SpanId |
| merge-logs works on live and partial runs | PASS | unit tests (Maestro cc8c89d) + integration test produces 7-record `merged.jsonl` |
| Contract tests green in both projects | PASS | spec-runner `test_obs_contract.py` (1bcf9eb) + contract schema (be29b16) |
| Redaction of sensitive keys | PASS | spec-runner `test_redaction_*` (b07153b) |

## Live pipeline run

- Example: `examples/hello.yaml` (agent_type: announce)
- Pipeline_id: `01KPK9TNBXKQGXSQ5X96HNS5VQ`
- Command: `uv run maestro run examples/hello.yaml --clean`
- Exit code: 0
- Log files produced: `logs/01KPK9TNBXKQGXSQ5X96HNS5VQ/maestro-81316.jsonl` (0 bytes)
- merged.jsonl: NOT produced
- Concern: The `run` command path (`_run_scheduler`) calls `init_logging("maestro")` at entry, which creates the per-PID JSONL file. However, the scheduler internals use stdlib `logging`, not `obs.span()` / `obs.get_logger()`. No OTel-shaped records are emitted during a `run`-mode pipeline; the JSONL file is created but remains empty. `merge_logs_dir` is also not called in the `_run_scheduler` finally block (only in `Orchestrator.run()` for the `orchestrate` command). The live run therefore demonstrates that `init_logging` is wired to the CLI entry point, but cross-process trace continuity via `announce`-only pipelines is not meaningful since `echo` has no OTel instrumentation.

## Integration test details (M1 proven)

The cross-process M1 criterion is fully demonstrated by `tests/test_obs_integration.py::test_trace_continuity_across_subprocess`:

- Maestro parent spawns a real Python subprocess (`tests/_obs_child.py`) acting as spec-runner
- `child_env()` injects `TRACEPARENT` and `ORCHESTRA_PIPELINE_ID` + `ORCHESTRA_LOG_DIR`
- Both services write to the same `logs/run/` directory
- `merge_logs_dir` produces a 7-record `merged.jsonl`
- All 7 records share TraceId `979b4685d0362bebfbdcc5f2fd459a2b`
- unique services: `['maestro', 'spec-runner']`
- Span tree reconstructed:

```
maestro    pipeline.run.started    SpanId=da12a54205ac7288  parent=65aa6689b82c1047
maestro    task.execute.started    SpanId=f9a6e1e6b7cbfaeb  parent=da12a54205ac7288
spec-runner child.work.started     SpanId=028bc34676fc3c29  parent=f9a6e1e6b7cbfaeb  <-- cross-process link
spec-runner child.doing.stuff      SpanId=028bc34676fc3c29  parent=f9a6e1e6b7cbfaeb
spec-runner child.work.ended       SpanId=028bc34676fc3c29  parent=f9a6e1e6b7cbfaeb
maestro    task.execute.ended      SpanId=f9a6e1e6b7cbfaeb  parent=da12a54205ac7288
maestro    pipeline.run.ended      SpanId=da12a54205ac7288  parent=none
```

- Timestamps monotonic: PASS
- parent_span_id linkage: PASS (child root's parent = Maestro spawning span)
- Log files: `maestro-<pid>.jsonl`, `spec-runner-<pid>.jsonl`, `merged.jsonl`

## Commit trail

spec-runner (master):
- ead7070 feat(obs): init_logging and get_logger skeleton
- 788b77f feat(obs): TRACEPARENT parsing with graceful fallback
- 208938c test(obs): timestamp format (ns-string + ISO micros)
- 31e4cdd feat(obs): span context manager with error chains
- b07153b feat(obs): redaction processor with default + env-extended blocklist
- 1cd18f9 feat(obs): child_env() for subprocess trace propagation
- 1bcf9eb test(obs): contract validation against shared schema and fixtures
- 52c3ce6 style(obs): ruff format + lint cleanup
- 641b9b8 refactor(obs): cutover logging.py to obs.py shim
- fa6b106 fix(obs): use TRACEPARENT parent span_id as initial _span_id

Maestro (master):
- c5e1a9f docs(obs): observability v1 design, review, plan-1-to-M1
- be29b16 feat(obs): contract v1 — JSON Schema + propagation.md + fixtures
- 66d7003 feat(obs): vendor obs.py from spec-runner@641b9b8
- c5a3b00 feat(obs): Maestro init_logging + child_env in subprocess spawning
- cc8c89d feat(obs): standalone merge-logs CLI with tolerant parser
- 62f5f58 feat(obs): auto-merge logs in pipeline finally block
- e3feefd test(obs): cross-process trace continuity (M1)
- 4688633 chore(obs): re-vendor obs.py from spec-runner@fa6b106

## Known limitations (for Plan 2)

- **scheduler `run` path not instrumented**: The `run` command's `_run_scheduler` path calls `init_logging("maestro")` at CLI entry (file created) but the Scheduler internals use stdlib `logging`. No `obs.span()` calls wrap task dispatch, so the JSONL is empty in `announce`-only pipelines. The `orchestrate` command (Orchestrator) IS instrumented with `obs.span("task.execute")` wrapping each `spec-runner` spawn. Plan 2 should add `obs.span()` to the scheduler's `_spawn_task` / `_monitor_tasks` loop. `merge_logs_dir` is also not called in `_run_scheduler` finally block.
- Span LIFO `parent_span_id` contextvar not restored after nested span exits (logs in B after C's exit don't show A as parent). Not M1-breaking; next span call re-establishes linkage.
- `sp._attrs` could collide with `error` key if user explicitly sets `sp.set_attrs(error=...)`. Theoretical — no in-code caller does this.
- Redaction is O(N) per log record (reconstructs attr tree). Acceptable for dev; could be cached for hot-path.
- `WriteLoggerFactory` file handle not explicitly closed on `init_logging` re-init (OS reclaims at process exit). Suppressed in test filterwarnings.
- Vendor ruff-reformat divergence: Maestro's ruff config wraps lines slightly differently from spec-runner's. Freeze marker records SHA source, but byte-exact parity is not preserved.

## Next steps

Plan 2 (to M2) — separate doc:
- Add `obs.span()` instrumentation to `Scheduler._spawn_task()` and `_run_scheduler` finally block (`merge_logs_dir`) for full `run`-mode observability
- Rust `arbiter-core/src/obs.rs` via `tracing-opentelemetry`
- Vendor `obs.py` into arbiter Python client
- ATP structlog chain extension (no vendor — integrate with existing OTel)
- jq cookbook (`docs/debugging.md`)
