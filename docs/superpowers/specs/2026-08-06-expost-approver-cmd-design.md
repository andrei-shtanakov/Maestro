# Ex-post gate: pluggable `approver_cmd` hook — design

**Status:** proposed (revision 2)
**Date:** 2026-08-06
**Issue:** #137 (`slug: expost-approver-cmd`), battle-testing pilot wave 2
**Owner decisions incorporated:** issue body (hard requirements from the
pilot) + owner verdict 2026-08-06 (scope boundary: pre-PR decision hook
only; review-bot comments belong to spec-runner#102 / a future
`post_pr_command`; shared transport envelope with notify channels is a
reuse note, not a unified spec).
**Revision 2 (owner review):** observations split from evaluation
attempts (kill-switch no longer burns the SHA slot); post-verdict
stale-SHA recheck + CAS-guarded PASS transaction; independent budgets
(authority vs execution vs escape-size, optional cost); bounded
subprocess output with canonical-serialization storage; mechanical
allowlist removed from v1; independence reworded as declared-provenance
validation; async evaluation lifecycle defined; §11 triggers made
concrete.

## 1. Problem

`maestro workstream-approve` is an operator gate. In autonomous runs
(nightly DAGs, cron) there is no live session to unblock `NEEDS_REVIEW`;
in the S2 pilot the session agent initially approved its own escapes —
one model was both analyzer and approver. The pilot validated a manual
two-agent rule over 3 real review rounds: author-side analysis plus an
independent critic, approve only on consensus, otherwise escalate to a
human. This spec turns that rule into a Maestro-side hook.

## 2. Scope and non-goals

**In scope:** one opt-in hook on exactly one seam — a gate-origin
`NEEDS_REVIEW` produced by the **ex-post** gate (the `RUNNING -> MERGING`
guard over the actual diff). The hook automates the *operator's existing
approval API*; it introduces no new authority path.

**Non-goals (recorded, not designed here):**

- **Ex-ante blocks stay human-only.** The ex-ante gate classifies the
  *declared scope* before any code exists — there is no diff for a critic
  to judge, so an LLM consensus adds nothing over the tier policy.
- **Review-bot / PR comments.** The temporal order is fixed: ex-post gate
  → domain verification → MERGING → PR creation. This hook runs before a
  PR exists; PR review loops are spec-runner#102's seam, bridged later by
  a thin `post_pr_command`.
- **`notify_cmd`.** Opposite semantics (async fail-open side effect vs
  this sync fail-closed decision gate). The transport envelope defined in
  §5 is written so a future notify/post-PR hook *may* reuse its shape,
  but nothing here depends on that.
- **Consensus policy.** Which critics run, how their votes combine, what
  counts as consensus — all live inside the command. Maestro defines only
  the contract and the fail-closed frame.
- **Mechanical allowlist.** Deliberately *out* of v1 (revision 2). An
  auto-approve branch that never reads the diff composes badly with
  path-globs (one allowed glob can hide an enormous or binary change),
  and genuinely harness-owned changes belong to the deterministic
  changed-path/scope layer, not to a semantic approver. If wanted later,
  it is a separate design: a deterministic gate rule with its own
  files/bytes limits and content-aware conditions. v1 is critic-path
  only.
- **Execution-layer integration.** v1 runs the command as a supervised
  subprocess with a durable attempt sentinel (§8). Moving it onto the
  shared execution layer (`execution_phase="approval"`, probe recovery,
  docker isolation) is a recorded follow-up — see §11 for the concrete
  triggers.

## 3. Design shape: an automated operator, not a new gate

The issue title says it precisely: a hook *for NEEDS_REVIEW*. The gate
machinery is untouched — a blocked workstream still routes to
`NEEDS_REVIEW` with the durable approval marker
(`gates:approval-required phase=ex_post sha=<sha>`), exactly as today.
The hook then acts where a human operator would:

```
ex-post gate blocks
  -> FAILED -> NEEDS_REVIEW + marker            (unchanged, durable)
  -> [eligible? guards, §6]                     (skips = observations)
      -> record attempt sentinel, then evaluate approver_cmd  (§5, §8)
          PASS  -> post-verdict recheck (§7.2): NEEDS_REVIEW still,
                   marker unchanged, worktree HEAD == marker SHA
                -> CAS transaction: attempt finalized + approval
                   (actor=agent) + NEEDS_REVIEW -> READY
                -> H-6 resume straight to the MERGING tail
          FAIL  -> stays NEEDS_REVIEW; critic findings recorded for the human
          ERROR -> stays NEEDS_REVIEW; audited; never softened to FAIL/PASS
```

Why this shape and not an inline hook at the gate edge:

- **Fail-closed by construction.** If the hook crashes, hangs, or the
  orchestrator dies mid-evaluation, the workstream is already parked in
  `NEEDS_REVIEW` — today's behavior is the failure mode.
- **One authority.** `gate_approvals` (written by
  `approve_workstream_with_gate_record` in one transaction) remains the
  single authority on "was this (workstream, phase, sha) approved". The
  hook is just another writer through the same API, distinguished by
  `actor`.
- **Resume machinery is free.** H-6 already resumes an approved ex-post
  block from `READY` straight to the MERGING tail when the worktree HEAD
  still matches the marker SHA — no regen, no respawn.

## 4. Configuration

```yaml
gates:
  # ... existing fields (steward_bin, profile, approval_tiers, ...) ...
  approver:
    cmd: ["./scripts/gate-approve.sh"]   # argv list, no shell; required
    timeout_seconds: 600                  # whole-command wall clock
    enabled: true                         # config kill-switch

    # --- budgets (independent; §6) ---
    max_auto_approvals: 2                 # AUTHORITY: agent approvals per workstream
    max_evaluations: 5                    # EXECUTION: attempts per workstream, any SHA/outcome
    max_escaped_paths: 20                 # more escapes -> straight to human
    max_cost_usd: null                    # optional; enforced over reported costs (§6)

    # --- input/output bounds (§5.4) ---
    max_diff_bytes: 262144                # bigger diff -> straight to human
    max_stdout_bytes: 1048576             # exceeded -> protocol ERROR
    max_stderr_bytes: 262144              # capture bound; never stored in DB
```

- **Opt-in at two levels:** no `gates:` block — nothing changes (as
  today); `gates:` without `approver:` — gate blocks wait for a human
  (today's behavior, byte-identical).
- **Kill-switches:** `enabled: false` in config, plus the environment
  override `MAESTRO_APPROVER_DISABLED=1` for operational shutoff without
  a config edit. Either one disables auto-approve entirely; blocks then
  wait for the operator. Disabling is always reversible: a skip is an
  observation, never an attempt (§6), so re-enabling re-arms the same
  SHA.
- `cmd` is an argv list executed without a shell (same stance as
  `VerifierSpec.argv`). Placeholders are NOT supported in v1 — all
  context arrives via stdin envelope and `MAESTRO_*` env (§5), so the
  argv stays a small fixed template with no per-run identity in it (same
  rationale as Stage B: argv is shared surface, identity goes in env).

## 5. Contract: request envelope and verdict document

### 5.1 Request (stdin, JSON)

Minted per evaluation: `approval_run_id` (ULID). The envelope:

```json
{
  "schema": "maestro.approval-request/v1",
  "approval_run_id": "01K1X...",
  "workstream_id": "ws-006",
  "phase": "ex_post",
  "sha": "<worktree HEAD the gate evaluated>",
  "base_branch": "main",
  "tier": "high",
  "flags": ["scope_violation"],
  "block_reason": "<the gate's block reason verbatim>",
  "declared_scope": ["src/auth/**"],
  "changed_paths": ["src/auth/x.py", "docs/notes.md"],
  "escaped_paths": ["docs/notes.md"],
  "diff": "<unified diff, bounded by max_diff_bytes>",
  "worktree": "/path/to/worktree",
  "author": {"harness": "claude_code", "model": "claude-sonnet-5"},
  "auto_approvals_used": 0,
  "evaluations_used": 1
}
```

Notes:

- The diff is computed by Maestro (scope-bounded, `base_branch..HEAD`)
  and embedded; an oversize or unproducible diff never reaches the
  command (§6). `worktree` is provided for read-only deeper inspection.
- `author` identifies the workstream's authoring harness/model so the
  command can enforce critic independence; Maestro validates the
  *declared* provenance on the way back (§5.3).

### 5.2 Environment

Like `CommandVerifier`: `inherit_env=False`; the subprocess env is
exactly the explicit `PATH`/`HOME`/`USER` passthrough (so CLI critics
like `claude`/`codex` resolve binaries and keychain identity) plus the
echo-checked identity fields:

```
MAESTRO_APPROVAL_RUN_ID, MAESTRO_WORKSTREAM_ID,
MAESTRO_GATE_PHASE, MAESTRO_GATE_SHA
```

This env never reaches the author; the author's env never reaches the
critic. Identity travels via env, never argv.

### 5.3 Verdict document (stdout, JSON)

```json
{
  "schema": "maestro.approval-verdict/v1",
  "approval_run_id": "01K1X...",
  "workstream_id": "ws-006",
  "phase": "ex_post",
  "sha": "<echoed>",
  "verdict": "PASS",
  "summary": "consensus: benign docs-only escape",
  "findings": [
    {"severity": "major", "title": "...", "detail": "..."}
  ],
  "critics": [
    {"name": "codex-critic", "harness": "codex_cli",
     "model": "gpt-5.4", "verdict": "PASS"},
    {"name": "claude-critic", "harness": "claude_code",
     "model": "claude-opus-5", "verdict": "PASS"}
  ],
  "cost_usd": 0.42
}
```

Strict run-keyed handshake, mirroring verdict v2:

- All four identity fields (`approval_run_id`, `workstream_id`, `phase`,
  `sha`) must echo exactly; any mismatch or malformed/unparseable
  document is a **protocol ERROR** — never softened to FAIL, never
  approved.
- `verdict` ∈ {PASS, FAIL, ERROR}. The command reporting its own ERROR
  is respected as ERROR.
- **Declared-provenance validation (honest framing, revision 2):**
  `critics` must be non-empty, and no critic's declared `model` may
  equal the author's model — a violation is a protocol ERROR. This is
  *validation of what the command declares*, not proof of independence:
  Maestro cannot verify which models actually ran, and model aliases
  may differ as strings. In v1 `approver_cmd` is explicitly a **trusted
  policy boundary** — the check catches misconfiguration, not malice.
  Verified critic identity (config-pinned critics executed by a
  Maestro-managed execution layer) is a possible follow-up (§11).
- `cost_usd` is optional and self-reported; used for the cost budget
  (§6) when present, recorded as unknown (never 0) when absent — the
  same stance `maestro costs` takes on unpriced usage.

### 5.4 Bounds (revision 2)

The subprocess's output is bounded, not just its input:

- stdout is captured with a hard `max_stdout_bytes` cap via bounded
  streaming reads (never an unbounded `communicate()`); exceeding the
  cap kills the process group and finalizes the attempt as protocol
  ERROR. Partial output is never interpreted.
- stderr is captured up to `max_stderr_bytes` and then discarded at the
  pipe level. stderr **never** reaches the DB; at most a truncated tail
  (≤500 chars) appears in the run-scoped evidence note for debugging.
- Field limits on the validated document: `summary` ≤ 2000 chars;
  `findings` ≤ 50 entries, each `detail` ≤ 4000 chars; `critics` ≤ 8
  entries with bounded `name`/`model` strings. Anything over limit is a
  protocol ERROR (not silent truncation — a critic drowning the operator
  in output is itself a signal).
- The DB stores the **canonical re-serialization of the validated
  document** (pydantic `model_dump_json`), never the raw stdout bytes.

## 6. Eligibility: observations vs evaluation attempts

**Revision 2 core split.** A *skip* is an **observation** — recorded as
evidence (a `GateVerdictRecord` with `gate_id="agent.approver"`,
`verdict="waived"`, note = reason, in `logs/<ULID>/gate_verdicts.jsonl`)
plus a structured event; it occupies **no** durable evaluation slot and
is deduplicated in-process per `(workstream_id, sha, reason)` so loop
passes don't spam. An **evaluation attempt** is the durable
`gate_approver_runs` row (§7), written only after all guards pass and
the decision to actually run the command has been made. Consequently a
block that was skipped (approver disabled, budget reached, oversize
diff, …) is re-examined from scratch on every pass and on startup —
disabling the approver never burns the SHA.

Guards, in order (any failure → observation, workstream stays
`NEEDS_REVIEW` for the human):

1. **Kill-switch** (config `enabled: false` or env) — reason `disabled`.
2. **Not a gate block:** `NEEDS_REVIEW` without a parseable
   `phase=ex_post` approval marker (recovery parks, rework ambiguity,
   verification failures…) — never evaluated. Recovery-origin
   NEEDS_REVIEW is out of reach by construction.
3. **Stale SHA:** marker SHA != current worktree HEAD (or worktree
   missing) — reason `stale_sha`. A new commit invalidates everything,
   same as for human approvals.
4. **Authority budget:** `gate_approvals` rows with `actor='agent'` for
   this workstream `>= max_auto_approvals` — reason
   `approval_budget_exhausted`. Beyond it a human must approve.
5. **Execution budget:** `gate_approver_runs` rows for this workstream
   (any SHA, any outcome — including `error`) `>= max_evaluations` —
   reason `evaluation_budget_exhausted`. This bounds *spend*: without
   it, each new commit re-arms the hook and a workstream could fund an
   unbounded sequence of paid FAIL/ERROR critic runs. Authority and
   execution budgets are deliberately independent.
6. **Cost budget** (only when `max_cost_usd` is set): the sum of
   reported `cost_usd` over this workstream's attempts `>= max_cost_usd`
   — reason `cost_budget_exhausted`. Attempts that reported no cost
   count as unknown and are surfaced in the observation note (the bound
   is then advisory for them — honest limitation, same as `maestro
   costs`).
7. **Escape size:** `len(escaped_paths) > max_escaped_paths` — reason
   `too_many_escapes` (the accepted TODO's ">N escapes → human"
   requirement).
8. **Oversize / unproducible diff:** scope-bounded diff exceeds
   `max_diff_bytes`, or cannot be produced (binary, git error) — reason
   `oversize_diff` / `diff_error`. An unreadable diff never reaches a
   critic.
9. **Already attempted:** a `gate_approver_runs` row exists for this
   `(workstream_id, phase, sha)` — reason `already_attempted`. One paid
   evaluation per SHA; a human (or a new commit) breaks the tie.

## 7. Audit: both verdicts, durably

Two layers, mirroring the existing split (durable DB authority + per-run
evidence).

### 7.1 Schema (migration 20)

- `gate_approvals` gains `actor TEXT NOT NULL DEFAULT 'human'`
  (`'human' | 'agent'`) and `approval_run_id TEXT NULL` (FK-by-value to
  the attempt row). Append-only stays; UNIQUE(workstream_id, phase, sha)
  stays — an agent approval occupies the same slot a human approval
  would; there is never a second, competing approval for one SHA.
- New append-only table `gate_approver_runs` — **evaluation attempts
  only** (observations never land here, §6):

  ```sql
  CREATE TABLE gate_approver_runs (
      approval_run_id TEXT PRIMARY KEY,          -- ULID
      workstream_id   TEXT NOT NULL,
      phase           TEXT NOT NULL CHECK (phase IN ('ex_post')),
      sha             TEXT NOT NULL,
      state           TEXT NOT NULL CHECK (state IN
                        ('started','pass','fail','error')),
      reason          TEXT,                      -- error cause / stale detail
      verdict_json    TEXT,                      -- canonical serialization (§5.4)
      cost_usd        REAL,                      -- reported; NULL = unknown
      created_at      TEXT NOT NULL,
      finished_at     TEXT,
      UNIQUE (workstream_id, phase, sha)
  );
  ```

  The `started` row is written and committed **synchronously before the
  subprocess spawns** (the crash sentinel, §8). The full validated
  verdict document — including every critic's individual verdict — lands
  in `verdict_json` in canonical serialization: "both verdicts
  persisted" is satisfied in the DB, not only in run-scoped logs.

### 7.2 PASS path: post-verdict recheck + CAS (revision 2)

A critic run can take up to `timeout_seconds`; the worktree may change
underneath it. A PASS verdict for SHA-A must never mutate state after
the world has moved to SHA-B. Before the PASS transaction, the hook:

1. re-reads the workstream row;
2. confirms status is still `NEEDS_REVIEW`;
3. confirms the approval marker is unchanged: `{phase=ex_post, sha=A}`;
4. re-reads the worktree HEAD and confirms `HEAD == A`;
5. only then executes the transaction, whose workstream UPDATE is
   CAS-guarded on `status='needs_review' AND error_message = <the exact
   prior value re-read in step 1>` (rowcount 0 → abort + rollback).

The transaction (extending `approve_workstream_with_gate_record` with an
agent-path variant): finalize the `gate_approver_runs` row
(`started -> pass`) → INSERT `gate_approvals(actor='agent',
approval_run_id)` → workstream `NEEDS_REVIEW -> READY`. Same atomicity
as the operator path; a crash between steps cannot leave an approval
without its verdict or vice versa.

Any recheck failure (status moved, marker changed, HEAD != A, CAS
rowcount 0) finalizes the attempt as
`state='error', reason='stale_after_evaluation'` — **no approval, no
READY**, workstream untouched. The attempt row for SHA-A remains (the
money was spent); the new SHA arms a fresh evaluation within budgets.

**FAIL path:** finalize `started -> fail` with the document in
`verdict_json`; workstream stays `NEEDS_REVIEW`; the human sees the
critic's findings (evidence note + `verdict_json`) and can still
`workstream-approve` manually — an agent FAIL never blocks the
operator.

### 7.3 Evidence (per-run logs)

Every observation and every attempt appends `GateVerdictRecord`s to
`logs/<ULID>/gate_verdicts.jsonl` with `gate_id="agent.approver"` —
verdict `waived` (observation) / `pass` / `fail` / `error`, note
carrying the summary, skip reason, or truncated stderr tail (§5.4). The
existing record shape is reused; no new evidence format.

## 8. Lifecycle, crash-safety, and recovery

### 8.1 Scheduling (revision 2)

- **Trigger points:** immediately after `_route_gate_block` parks an
  ex-post block, and on each main-loop pass / startup for any
  `NEEDS_REVIEW` workstream whose guards (§6) all pass — this covers
  blocks that happened while the approver was disabled and restarts
  that lost the in-flight task.
- **Tracked tasks, deduplicated:** evaluations run as asyncio tasks held
  in a tracked set keyed by `workstream_id`; a workstream with an
  in-flight evaluation is never scheduled twice in-process. (Cross-crash
  dedup is the sentinel row, §8.2.)
- **Ordering:** the `started` sentinel is written and committed
  *synchronously, before* `create_task` — there is no window where an
  evaluation is scheduled but no attempt is recorded.
- **Shutdown:** the main loop counts pending/in-flight evaluations as
  active work — an autonomous run does not exit right after parking a
  block; it waits for in-flight evaluations up to their remaining
  `timeout_seconds` (bounded grace). On cancellation (grace expired,
  operator interrupt) the subprocess's process group is killed and the
  attempt is finalized `error/interrupted`.

### 8.2 Crash and recovery

- **Crash mid-evaluation:** the `started` sentinel row exists without a
  terminal state. On startup it is finalized as
  `state='error', reason='interrupted'` — **fail-closed to the human**,
  never re-run automatically (a critic run costs money and the first
  run's effects are unknown; mirrors the #124 stance that ambiguity
  resolves to a human, not to a retry loop). A new commit (new SHA)
  naturally re-arms the hook, within the execution budget.
- **Timeout:** `timeout_seconds` wall clock over the whole command; on
  expiry the **process group** is killed (the command spawns critic
  subprocesses of its own) and the attempt is finalized as ERROR. No
  partial reads of stdout are ever interpreted.
- **The hook never touches the worktree.** Read-only by contract; the
  post-verdict recheck (§7.2) plus H-6's own HEAD == marker-SHA check
  make a misbehaving command unable to smuggle code through an approval
  minted for an older SHA.

## 9. Governance fit (ADR-ECO-004 I1–I4)

The ex-post edge guards `RUNNING -> MERGING` — the merge into the
**integration branch** (`base_branch`) inside Maestro's own delivery
tail, before any PR exists. Auto-approve therefore extends the agent's
authority exactly to where ADR-ECO-004 already places it: the agent
remains PR-authority, and merging to master stays with the human at the
PR stage. No change to that boundary is made or needed here.

## 10. Testing plan (implementation PR)

- **Handshake matrix:** each of the four echo fields mismatched → ERROR,
  no approval row; malformed JSON / empty stdout / trailing garbage →
  ERROR.
- **Fail-closed matrix:** timeout (process group killed), non-zero exit,
  command not found, declared critic model == author model, empty
  `critics` with verdict PASS, stdout over `max_stdout_bytes`,
  over-limit `summary`/`findings`/`critics` → all ERROR → workstream
  stays `NEEDS_REVIEW`, terminal attempt row written, no
  `gate_approvals` row.
- **Observations vs attempts:** every §6 skip → evidence record +
  event, **no** `gate_approver_runs` row; disable → block → re-enable →
  same SHA gets evaluated (the kill-switch does not burn the slot);
  observation dedup per (workstream, sha, reason) within a process.
- **Budgets, independently:** authority budget counts only
  `actor='agent'` approvals; execution budget counts all attempts incl.
  `error` across SHAs (N failed evaluations on N commits stop the
  spend); `max_escaped_paths` boundary; cost budget with reported /
  absent `cost_usd` (absent = unknown, surfaced, never 0).
- **PASS path:** post-verdict recheck passes → single CAS transaction
  (attempt `pass` + approval `actor='agent'` + `approval_run_id` +
  READY), then H-6 resume reaches MERGING without respawn
  (`generate_spec` not awaited, PR created from existing worktree).
- **Stale-after-evaluation:** commit lands during the critic run → HEAD
  recheck fails → attempt `error/stale_after_evaluation`, no approval,
  no READY, workstream untouched; CAS rowcount-0 path (concurrent
  operator action mid-transaction) behaves identically.
- **FAIL path:** stays `NEEDS_REVIEW`, findings visible in
  `verdict_json` and evidence note; human `workstream-approve` still
  works afterwards.
- **stderr hygiene:** stderr never lands in any DB column; evidence note
  carries at most the truncated tail; `verdict_json` is canonical
  serialization, not raw bytes.
- **Lifecycle:** sentinel committed before `create_task` (kill between
  → startup finalizes `error/interrupted`); in-process dedup (no double
  evaluation for one workstream); main loop waits for in-flight
  evaluations at shutdown, bounded by remaining timeout; cancellation →
  `error/interrupted`.
- **Kill-switches:** config and env — no invocation, `disabled`
  observation only; reversible (see above).
- **Migration 20:** fresh DB and upgrade path; existing `gate_approvals`
  rows read back as `actor='human'`; journal tripwire lists updated.
- **Zero-change guarantee:** `gates:` without `approver:` — existing
  gates tests byte-identical; no `gates:` at all — untouched.

## 11. v1 subprocess supervision, and the execution-layer triggers

v1 runs the command as a directly supervised subprocess — acceptable
because the failure domain is inherently safe (every failure mode ends
at "a human reviews", never at a wrong approval), and revision 2 closes
the supervision gaps that made this risky: process-group termination,
bounded stdout/stderr capture, the durable sentinel committed before
spawn, a tracked task lifecycle with bounded shutdown, and the
post-verdict stale-SHA recheck.

Moving onto the shared execution layer (`execution_phase="approval"`) is
the recorded follow-up, triggered by any of these concrete needs — not
by a vague "operational maturity":

- running the approver on a **remote or docker backend** (isolation or
  placement requirements);
- **recovery-continuation** of an unfinished critic run (today an
  interrupted run is terminal by design);
- **unified cost accounting** through `maestro costs` instead of
  self-reported `cost_usd`;
- config-pinned, Maestro-executed critics (turning §5.3's declared
  provenance into verified identity).

The §5 contract does not change under that move.
