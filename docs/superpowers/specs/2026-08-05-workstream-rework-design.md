# `maestro workstream-rework` — state-machine design (issue #124)

Date: 2026-08-05
Status: approved (design only; implementation is a separate PR)
Issue: #124 `workstream-rework-command` (battle-testing pilot, S2 TASK-006 —
three review rounds, two improvised raw-DB reworks)

## Problem

When an ex-post-blocked (or otherwise stuck) workstream should be REWORKED —
new instructions, re-execution — rather than approved, there is no supported
operation. `workstream-approve` merges as-is; the Stage B rework/respawn
path fires only on a verification FAIL, never on a gate block. The pilot's
improvisation against the DB showed the sharp edges:

- `status='failed'` does NOT rework: on resume the orchestrator merely
  re-evaluates the ex-post gate on the unchanged HEAD;
- actual rework required `status='pending'` (forcing re-decomposition from
  an updated description) plus manually clearing the worktree's spec-runner
  state files — stale done-entries would otherwise mask the regenerated
  subtasks.

## Key discovery

The heavy machinery already exists. The READY-processing path in
`orchestrator.py` dispatches on `resume_reason`; the `RESUME_REWORK` branch
re-enters DECOMPOSING **in the same worktree** (same branch, same lineage),
re-runs `setup_spec_runner` — which already idempotently unlinks every
stale spec-runner state file (`workspace.py`, prefixed and unprefixed
variants) — and regenerates the spec with an addendum appended to the
description. The pilot's manual state-file cleanup is already built into
the re-decomposition path on current master.

The design therefore adds **no new statuses and no new transition edges**.
It gives the operator a sanctioned entrance into the existing path.

## Decision summary (owner, 2026-08-05)

- Both input channels: mandatory `--reason`, optional
  `--refresh-from <project.yaml>`.
- `--reason` (audit) and the next-attempt prompt are semantically separate:
  a distinct optional `--instructions` flag feeds the addendum; `--reason`
  never enters the prompt.
- Separate `operator_rework_count` column; the automatic Stage B
  `rework_attempt` / `rework_budget` are untouched.
- All seven constraints below are part of the accepted design.

## CLI contract

```
maestro workstream-rework <workstream-id>
    --reason "<text>"              # mandatory: immutable audit explanation
    [--instructions "<text>"]      # optional: addendum for the next attempt
    [--refresh-from <project.yaml>]  # optional: re-read description/scope
    [--db <path>]
```

- `--reason` is the operator's immutable explanation of the decision. It is
  recorded in the audit row and is **never** used as a prompt instruction.
- `--instructions` is the author-facing channel: it becomes the rework
  addendum appended to the description at spec regeneration (symmetric to
  `build_rework_addendum` for verification FAILs). Omitted → no addendum;
  the change then has to be carried by `--refresh-from`.
- `--refresh-from <config>` re-reads **only the workstream with the same
  ID** from the given project.yaml and updates `description` and `scope`.
  Topological fields — `depends_on`, priority, base branch, anything that
  alters the DAG — are **refused**: changing them requires re-validating
  the whole DAG, which is not a local rework.

## State machine

No new states, no new edges. The command performs `NEEDS_REVIEW -> READY`
or `FAILED -> READY` (both edges already valid) and sets
`resume_reason='operator_rework'`.

Allowed source states — exactly two:

| Source | Allowed | Note |
| --- | --- | --- |
| NEEDS_REVIEW | yes | gate block, budget exhaustion, recovery ambiguity |
| FAILED | yes | retry-exhausted failures |
| PENDING / DECOMPOSING / READY / RUNNING / VERIFYING / MERGING / PR_CREATED | no | in-flight or not-yet-attempted |
| DONE / ABANDONED | no | terminal; rework-after-DONE is a different feature |

Fail-closed refusals (no DB change, no audit row):

- source status not in {NEEDS_REVIEW, FAILED};
- `process_pid` or `generation_pid` not NULL — including the spawning
  sentinel: a possibly-live process means "wait for recovery or
  investigate", never "reset under it";
- worktree missing, or its HEAD cannot be reliably determined (nothing to
  rework / no trustworthy prior state to record);
- `--refresh-from` names a config without this workstream ID, or the
  refreshed entry changes a forbidden (topological) field;
- refreshed scope fails re-validation (see below);
- a second invocation after a successful transition (the row is READY —
  not an allowed source).

## Validation before the transaction

Order matters: everything fallible happens **before** the DB transaction,
so a refused rework leaves zero trace in the accepted lineage (operator
output only — no audit record of a rework that did not happen).

1. Load the workstream row; check status and both pids (preliminary —
   re-confirmed inside the transaction).
2. Resolve the worktree; read `prior_head_sha` (`git rev-parse HEAD`).
   Unreadable → fail closed.
3. If `--refresh-from`: load the config, find the same-ID workstream,
   verify only `description`/`scope` differ (topology refusal otherwise),
   run the new scope through the normal normalization and the preflight
   overlap validation against the other workstreams of the config. Any
   error → refuse, nothing written.

## Transactional reset

One DB transaction:

1. **Conditional UPDATE** (constraint: no read-then-update): the row is
   updated only where `status` is still the previously read source status
   AND `process_pid IS NULL` AND `generation_pid IS NULL`. Zero rows
   affected → the world changed under the operator → refuse (fail closed);
   the transaction rolls back and nothing is recorded. This re-confirms
   inside the transaction the expected state read in step 1, including the
   window between the pre-transaction checks and the commit.
2. Append the **audit row** (same transaction) to the new append-only
   `workstream_reworks` table:
   - `workstream_id`, `seq` (per-workstream, monotonically increasing),
     `initiated_at`, `initiator` (OS user);
   - `reason` (mandatory), `instructions` (nullable);
   - rejected context: `prior_status`, `prior_error_message` (the block
     verdict/marker text in full), `prior_head_sha`;
   - refresh evidence when `--refresh-from` was used: config path, config
     file hash, old and new `description`, old and new `scope`.
   Rows are never updated or deleted.
3. Update the workstream row: `status=READY`,
   `resume_reason='operator_rework'`, `operator_rework_count += 1`,
   `error_message=NULL` (clears the H-6 approval marker),
   `verification_run_id=NULL` (a new attempt is a new run), refreshed
   `description`/`scope` when applicable.

Explicitly NOT written: anything in `gate_approvals`. Rework is not an
approval — the new work produces a new SHA and the ex-post gate evaluates
it from scratch; old SHA-pinned approvals are inert by construction and
must not be applied to the new attempt.

## Resume dispatch

- The READY handler's `resume_reason` dispatch becomes **exhaustive**.
  The complete set of allowed values is: `verification_reverify`,
  `verification_rework`, `operator_rework`, and NULL (a plain,
  non-resume READY). Any other value is an **error** (fail-closed to
  NEEDS_REVIEW), never a silent plain resume.
- `operator_rework` follows the existing re-decomposition path: same
  worktree, same branch, `setup_spec_runner` cleans stale harness state
  idempotently, spec regenerates. The addendum is built from the
  **latest audit row's `instructions`** (not from the verification
  ledger, and never from `reason`).
- The reason/instructions must survive until the new attempt exists: the
  addendum is loaded from the audit table (durable) at DECOMPOSING time,
  not carried in memory — a crash between the transition and spec
  generation loses nothing.
- Crash after commit, before resume: the row is a consistent
  READY+`operator_rework`; the next `maestro orchestrate --resume` picks
  it up. File-state cleanup is owned by the (idempotent) decompose path,
  so no partial file mutation can be stranded by the CLI.

## Counter and visibility

- `operator_rework_count` is **not** limited by any automatic budget:
  unbounded explicit operator rework is an administrative capability.
- It must not be invisible: `maestro workstreams` displays the count, and
  both the CLI (on invocation) and the status display emit a warning once
  the count reaches a threshold (default 3) — "N operator reworks; consider
  whether this workstream needs redesign instead".
- Stage B's `rework_attempt` and `rework_budget` are untouched by this
  command in both directions.

## Acceptance criteria (design-level)

- NEEDS_REVIEW and FAILED transition to READY; all other states are
  refused.
- Live or sentinel pids block the command (fail closed).
- A second invocation after a successful transition is refused.
- A crash after commit is safely picked up by resume.
- Stale spec-runner state is cleaned by the existing idempotent path — the
  command itself performs no file mutations.
- The audit table is append-only and preserves the rejected context
  (prior status, prior error/verdict text, prior HEAD SHA; refresh
  evidence with config path+hash and old/new description/scope).
- No approval is created; old SHA-pinned approvals are not applied to the
  new attempt.
- A refreshed scope passes normalization + overlap re-validation before
  anything is written; a failed refresh leaves no trace in the lineage.
- Automatic `rework_attempt` and its budget are unchanged.
- Unknown `resume_reason` values are an error, not a plain resume.

## Out of scope

- Implementation (separate PR).
- Rework of DONE/ABANDONED workstreams (revert semantics).
- Editing topological fields (`depends_on`, priority, base branch) — a
  config change plus full re-validation, not a rework.
- Any change to gate semantics or the Stage B rework budget.
