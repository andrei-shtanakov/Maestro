# approver_cmd (#137) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved spec
`docs/superpowers/specs/2026-08-06-expost-approver-cmd-design.md`
(revision 4, approved at `b61f45f`): an opt-in ex-post-gate approver hook
that acts as an automated operator over the existing approval API.

**Architecture:** New pure-logic module `maestro/approver.py` (contract
models, guards, bounded subprocess, verdict validation) + migration 20
(`gate_approvals.actor`/`approval_run_id`, `gate_approver_runs`,
`gate_block_contexts`) + orchestrator wiring (persist-at-block, tracked
evaluation tasks, PASS path per spec §7.2, startup finalization of
interrupted runs, shutdown grace). Evidence via the existing
`GateVerdictRecord` family extended with `not_run` and an explicit
schema discriminator.

**Tech Stack:** Python 3.12, pydantic v2, aiosqlite, asyncio
subprocess (process groups), pytest (asyncio auto mode).

## Global Constraints (from the spec — exact values)

- Config defaults: `timeout_seconds=600`, `max_auto_approvals=2`,
  `max_evaluations=5`, `max_escaped_paths=20`, `max_cost_usd=None`,
  `max_diff_bytes=262144`, `max_stdout_bytes=1048576`,
  `max_stderr_bytes=262144`, `enabled=True`; env kill-switch
  `MAESTRO_APPROVER_DISABLED=1`.
- Envelope `maestro.approval-request/v1`; verdict
  `maestro.approval-verdict/v1`; evidence discriminator
  `maestro.gate-verdict-record/v1`.
- Verdict field limits: summary ≤ 2000; findings ≤ 50 (detail ≤ 4000);
  critics ≤ 8 (bounded name/model ≤ 200); verdict ∈ {PASS, FAIL, ERROR}.
- Echo handshake fields: approval_run_id, workstream_id, phase, sha —
  any mismatch = protocol ERROR. Declared critic model == author model
  (when author model known) = protocol ERROR. Empty critics = ERROR.
- Guard order (§6): disabled → not-a-gate-block → no_block_context →
  stale_sha → approval_budget_exhausted → evaluation_budget_exhausted →
  cost_budget_unknown/exhausted → too_many_escapes →
  oversize_diff/diff_error → already_attempted. Skips are observations
  (`not_run`, advisory), never `gate_approver_runs` rows; deduped
  in-process per (workstream, sha, reason).
- PASS path (§7.2): post-verdict cost authority check → re-read
  workstream → NEEDS_REVIEW check → marker unchanged → HEAD == sha →
  one txn CAS-guarded on exact prior error_message → post-txn HEAD
  confirm. Any failure → `error/stale_after_evaluation` (or the two
  cost reasons), no approval.
- Author provenance v1: `{"harness": "spec-runner", "model": null}`
  (Mode-2 workstreams carry no per-workstream model; the null model
  vacuously passes the independence comparison — documented).
- Subprocess: argv exec (no shell), `start_new_session=True`, kill via
  process group; env = PATH/HOME/USER passthrough + 4 `MAESTRO_*` vars;
  stdin = envelope JSON; stdout/stderr bounded streaming capture.
- stderr never in DB; ≤500-char tail in evidence note only.
  `verdict_json` = canonical `model_dump_json()`, never raw bytes.

---

### Task 1: Contract models + pure validation (`maestro/approver.py`)

**Files:** Create `maestro/approver.py`; Test `tests/test_approver.py`.

**Produces:** `ApproverCritic`, `ApproverFinding`, `ApprovalVerdict`
(field `schema_version` serialized/parsed under alias `schema`),
`BlockContext` (tier, flags, block_reason, declared_scope,
changed_paths, escaped_paths, author), `EchoFields(approval_run_id,
workstream_id, phase, sha)`, `validate_verdict(raw: bytes, expected:
EchoFields, author_model: str | None) -> ApprovalVerdict | str` (str =
protocol-error text), `build_request_envelope(...) -> dict`.

- [ ] Failing tests: handshake matrix (4 echo mismatches, malformed
      JSON, trailing garbage, empty stdout, over-limit summary/findings/
      critics, empty critics, critic==author model, author model None
      passes), envelope shape (schema key, counters), canonical
      round-trip.
- [ ] Implement; run; commit.

### Task 2: Migration 20 + DB APIs

**Files:** Modify `maestro/database.py` (base schema + migration 20 +
APIs); Test additions in `tests/test_approver.py` (DB section) and
journal-list updates in `tests/test_database.py`.

**Produces:** `record_gate_block_context`, `get_gate_block_context`,
`insert_approver_run_started` (IntegrityError → caller treats as
already_attempted), `finalize_approver_run(approval_run_id, state,
reason=None, verdict_json=None, cost_usd=None)` (guarded
`state='started'`), `count_agent_approvals`, `count_approver_runs`,
`approver_cost_stats -> (known_sum, has_unknown)`,
`list_started_approver_runs`, `has_approver_run`,
`approve_workstream_agent(workstream_id, phase, sha, approval_run_id,
verdict_json, cost_usd, expected_error_message)` — ONE txn: finalize
started→pass + INSERT gate_approvals(actor='agent', approval_run_id) +
`UPDATE workstreams SET status='ready' WHERE id=? AND
status='needs_review' AND error_message=?`; rowcount 0 → ValueError →
rollback (started row survives for a separate stale finalize).

- [ ] Failing tests: fresh-DB + upgrade paths, actor default 'human',
      INSERT OR IGNORE immutability of contexts, unique attempt per
      (ws, phase, sha), CAS matrix of `approve_workstream_agent`
      (wrong status / changed error_message / already-finalized run),
      cost stats with NULLs. Update journal tripwires (…, 20; count 20).
- [ ] Implement; run; commit.

### Task 3: Evidence record extension (`maestro/gates.py`)

**Files:** Modify `maestro/gates.py`; test additions in
`tests/test_gates.py`.

**Produces:** `GateVerdictRecord.verdict` gains `"not_run"`; new field
`record_schema: str = "maestro.gate-verdict-record/v1"` serialized as
`schema` (serialization_alias; `_write` switches to
`model_dump(by_alias=True, exclude_none=True)`); public
`GateKeeper.append_records(records)` wrapping `_write` for the
orchestrator's approver records.

- [ ] Failing tests: not_run accepted, every appended line carries
      `"schema": "maestro.gate-verdict-record/v1"`, existing records
      unchanged otherwise.
- [ ] Implement; run; commit.

### Task 4: Config surface (`maestro/models.py` + schema regen)

**Files:** Modify `maestro/models.py` (ApproverConfig, GatesConfig
field); regenerate `maestro/schemas/*.json`; test additions in
`tests/test_models.py` or `tests/test_approver.py`.

- [ ] Failing tests: defaults per Global Constraints; extra=forbid;
      cmd non-empty; GatesConfig without approver unchanged.
- [ ] Implement + `uv run python -m maestro.schemas.generate`; commit.

### Task 5: Bounded subprocess runner (`maestro/approver.py`)

**Produces:** `run_approver_cmd(argv, envelope_json, timeout_seconds,
max_stdout_bytes, max_stderr_bytes, env) -> CmdOutcome(stdout: bytes |
None, stderr_tail: str, error: str | None)` — asyncio subprocess,
`start_new_session=True`, envelope on stdin, bounded concurrent
stdout/stderr readers, wall-clock timeout → killpg → error="timeout",
overflow → killpg → error="stdout_overflow", non-zero exit →
error=f"exit {rc}", command-not-found → error="spawn: FileNotFoundError".

- [ ] Failing tests with real tiny python scripts: success JSON,
      timeout (killpg observed), stdout overflow, non-zero exit, absent
      binary, stderr tail truncation at 500.
- [ ] Implement; run; commit.

### Task 6: Orchestrator wiring

**Files:** Modify `maestro/orchestrator.py`; Test
`tests/test_approver_orchestrator.py`.

Sub-steps, each TDD:

- [ ] **6a Persist-at-block:** `GateDecision` gains optional
      `tier/flags/paths` (set in `_decide`/`evaluate_ex_post`);
      `_gate_ex_post` block path computes
      `escaped = find_escapes(paths, normalize(scope))` and writes
      `gate_block_contexts` (INSERT OR IGNORE) right after
      `_route_gate_block`, regardless of approver enablement.
- [ ] **6b Guards + observations:** `_approver_eligible(ws) -> str |
      None` implementing §6 order against DB/config/env/worktree;
      observation → `append_records([not_run record])` + event, deduped
      via `self._approver_observed: set[tuple[str, str, str]]`.
- [ ] **6c Scheduling:** main-loop pass `_schedule_approver()` (before
      the completeness check): eligible → `insert_approver_run_started`
      (synchronous, IntegrityError → observation already_attempted) →
      tracked `self._approver_tasks[ws_id] = create_task(...)`;
      in-process dedup by ws_id; `_all_workstreams_complete` returns
      False while `self._approver_tasks` is non-empty.
- [ ] **6d Evaluation task:** build envelope from persisted context +
      fresh HEAD + bounded diff (`git diff base...HEAD` via helper; size
      guard rechecked here as diff is produced now); run Task-5 runner;
      validate via Task-1; FAIL → finalize fail + evidence; ERROR →
      finalize error + evidence; PASS → §7.2 sequence: cost authority
      check → re-read/marker/HEAD rechecks under a per-workstream
      asyncio.Lock (the in-process reservation) →
      `approve_workstream_agent` CAS txn → post-txn HEAD confirm
      (mismatch: log + evidence; approval stays, H-6 refuses) →
      evidence pass record. All finalization inside try/finally;
      task removed from `_approver_tasks` in finally.
- [ ] **6e Startup + shutdown:** `run()` calls
      `_finalize_interrupted_approver_runs()` (started rows →
      error/interrupted + evidence) before the main loop; after
      `_main_loop`, graceful path awaits in-flight approver tasks
      (they self-bound by timeout); signal-shutdown path cancels them —
      the task's CancelledError handler finalizes error/interrupted.
- [ ] Integration tests: full PASS path with a real fake-approver
      script through `_run_approver_evaluation` (workstream ends READY
      + approval actor=agent + H-6-resumable state), stale-after
      (commit lands mid-run), each budget boundary, disable→enable
      re-arm, interrupted-run finalization, zero-change guarantee
      (gates without approver: existing gates tests untouched).

### Task 7: Docs + bookkeeping in-PR

- [ ] CHANGELOG entry (Added), CLAUDE.md gates bullet extension (one
      paragraph), commented `approver:` block in the gates example
      config if one exists (else skip).
- [ ] `uv run ruff format/check`, `uv run pyrefly check`, targeted
      foreground pytest of all touched files; commit.
