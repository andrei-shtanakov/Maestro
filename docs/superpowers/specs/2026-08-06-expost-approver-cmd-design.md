# Ex-post gate: pluggable `approver_cmd` hook — design

**Status:** proposed
**Date:** 2026-08-06
**Issue:** #137 (`slug: expost-approver-cmd`), battle-testing pilot wave 2
**Owner decisions incorporated:** issue body (hard requirements from the
pilot) + owner verdict 2026-08-06 (scope boundary: pre-PR decision hook
only; review-bot comments belong to spec-runner#102 / a future
`post_pr_command`; shared transport envelope with notify channels is a
reuse note, not a unified spec).

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
- **Execution-layer integration.** v1 runs the command as a supervised
  subprocess with a durable attempt sentinel (§8). Moving it onto the
  shared execution layer (`execution_phase="approval"`, probe recovery,
  docker isolation) is a recorded follow-up — see Open question in §11.

## 3. Design shape: an automated operator, not a new gate

The issue title says it precisely: a hook *for NEEDS_REVIEW*. The gate
machinery is untouched — a blocked workstream still routes to
`NEEDS_REVIEW` with the durable approval marker
(`gates:approval-required phase=ex_post sha=<sha>`), exactly as today.
The hook then acts where a human operator would:

```
ex-post gate blocks
  -> FAILED -> NEEDS_REVIEW + marker            (unchanged, durable)
  -> [approver configured & armed?]             (guards, §6)
      -> evaluate approver_cmd                  (contract, §5)
          PASS  -> approve_workstream_with_gate_record(actor=agent)
                   (same single-txn API the operator uses)
                -> READY + marker + unchanged HEAD
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
    max_diff_bytes: 262144                # bigger diff -> straight to human
    max_auto_approvals: 2                 # per workstream; beyond -> human
    mechanical_allowlist: []              # path globs approvable w/o critic
    enabled: true                         # config kill-switch
```

- **Opt-in at two levels:** no `gates:` block — nothing changes (as
  today); `gates:` without `approver:` — gate blocks wait for a human
  (today's behavior, byte-identical).
- **Kill-switches:** `enabled: false` in config, plus the environment
  override `MAESTRO_APPROVER_DISABLED=1` for operational shutoff without
  a config edit. Either one disables auto-approve entirely; blocks then
  wait for the operator.
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
  "auto_approvals_used": 0
}
```

Notes:

- The diff is computed by Maestro (scope-bounded, `base_branch..HEAD`)
  and embedded; an oversize or unproducible diff never reaches the
  command (§6). `worktree` is provided for read-only deeper inspection.
- `author` identifies the workstream's authoring harness/model so the
  command can enforce critic independence — and Maestro re-checks it on
  the way back (§5.3).

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
  ]
}
```

Strict run-keyed handshake, mirroring verdict v2:

- All four identity fields (`approval_run_id`, `workstream_id`, `phase`,
  `sha`) must echo exactly; any mismatch or malformed/unparseable
  document is a **protocol ERROR** — never softened to FAIL, never
  approved.
- `verdict` ∈ {PASS, FAIL, ERROR}. The command reporting its own ERROR
  is respected as ERROR.
- **Independence is enforced on the way back:** `critics` must be
  non-empty, and no critic's `model` may equal the author's model. A
  violation is a protocol ERROR (the pilot's hard requirement enforced
  mechanically, not by convention). Consensus itself is the command's
  business; Maestro only checks identity and shape.

## 6. Guards: when the hook does NOT run

Evaluated in order; every skip is audited (§7) and leaves the workstream
in `NEEDS_REVIEW` for the human:

1. **Kill-switch** (config `enabled: false` or env) — skip, reason
   `disabled`.
2. **Not a gate block:** `NEEDS_REVIEW` without a parseable
   `phase=ex_post` approval marker (recovery parks, rework ambiguity,
   verification failures…) — never evaluated. Recovery-origin
   NEEDS_REVIEW is out of reach by construction.
3. **Stale SHA:** marker SHA != current worktree HEAD (or worktree
   missing) — skip, reason `stale_sha`. A new commit invalidates
   everything, same as for human approvals.
4. **Budget:** `auto_approvals_used >= max_auto_approvals` (counted as
   `gate_approvals` rows with `actor='agent'` for this workstream) —
   skip, reason `budget_exhausted`. Unbounded critic loops are the
   pilot's explicit fear; beyond the budget a human must look.
5. **Oversize diff:** scope-bounded diff exceeds `max_diff_bytes`, or the
   diff cannot be produced (binary, git error) — skip, reason
   `oversize_diff` / `diff_error`. An unreadable diff never reaches a
   critic (hard requirement: unreadable diff = escalation, never
   approve).
6. **Already evaluated:** an attempt row for this
   `(workstream_id, phase, sha)` already exists (§8) — skip, reason
   `already_attempted`. One evaluation per SHA; a human (or a new
   commit) breaks the tie.

**Mechanical allowlist (runs after guards 1–4 and 6, before invoking the
command; the oversize-diff guard does not apply — no diff is sent
anywhere):** if `escaped_paths` is non-empty and *every* escaped path matches
`mechanical_allowlist` globs, the hook may approve without invoking a
critic — recorded as `verdict=PASS`, `critics=[]`,
`summary=mechanical-allowlist`, and it still consumes the
`max_auto_approvals` budget and writes the same audit rows. Path-match
only in v1 (no content awareness); default empty list means the branch
is dead unless explicitly configured. Anything not fully covered goes to
the critic path — semantic judgment is never mechanical.

## 7. Audit: both verdicts, durably

Two layers, mirroring the existing split (durable DB authority + per-run
evidence):

**DB (migration 20):**

- `gate_approvals` gains `actor TEXT NOT NULL DEFAULT 'human'`
  (`'human' | 'agent'`) and `approval_run_id TEXT NULL` (FK-by-value to
  the attempt row). Append-only stays; UNIQUE(workstream_id, phase, sha)
  stays — so an agent approval occupies the same slot a human approval
  would, and there is never a second, competing approval for the same
  SHA.
- New append-only table `gate_approver_runs`:

  ```sql
  CREATE TABLE gate_approver_runs (
      approval_run_id TEXT PRIMARY KEY,          -- ULID
      workstream_id   TEXT NOT NULL,
      phase           TEXT NOT NULL CHECK (phase IN ('ex_post')),
      sha             TEXT NOT NULL,
      state           TEXT NOT NULL CHECK (state IN
                        ('started','pass','fail','error','skipped')),
      skip_reason     TEXT,                      -- guards, §6
      verdict_json    TEXT,                      -- full document, verbatim
      created_at      TEXT NOT NULL,
      finished_at     TEXT,
      UNIQUE (workstream_id, phase, sha)
  );
  ```

  The `started` row is written **before** the subprocess spawns (the
  crash sentinel, §8). The full verdict document — including every
  critic's individual verdict — lands in `verdict_json` verbatim: "both
  verdicts persisted" is satisfied in the DB, not only in run-scoped
  logs.

**Evidence (per-run logs):** every evaluation appends
`GateVerdictRecord`s to `logs/<ULID>/gate_verdicts.jsonl` with
`gate_id="agent.approver"` — verdict `pass`/`fail`/`error`, note carrying
the summary or skip reason. The existing record shape is reused; no new
evidence format.

**On PASS, one transaction** (extending
`approve_workstream_with_gate_record`): finalize the
`gate_approver_runs` row → INSERT `gate_approvals(actor='agent',
approval_run_id)` → workstream `NEEDS_REVIEW -> READY`. The same
atomicity the operator path has; a crash between steps cannot leave an
approval without its verdict or vice versa.

## 8. Lifecycle, crash-safety, and recovery

- **Trigger point:** immediately after `_route_gate_block` parks an
  ex-post block in `NEEDS_REVIEW`, the orchestrator schedules one
  evaluation (async task). Additionally, on startup and on each main-loop
  pass, a `NEEDS_REVIEW` workstream with an ex-post marker, an armed
  approver, and **no attempt row** for its (phase, sha) is scheduled —
  this covers blocks that happened while the approver was disabled and
  orchestrator restarts that lost the in-flight task.
- **Crash mid-evaluation:** the `started` sentinel row exists without a
  terminal state. On startup this is finalized as
  `state='error', skip_reason='interrupted'` — **fail-closed to the
  human**, never re-run automatically (a critic run costs money and the
  first run's effects are unknown; mirrors the #124 stance that
  ambiguity resolves to a human, not to a retry loop). A new commit
  (new SHA) naturally re-arms the hook.
- **Timeout:** `timeout_seconds` wall clock over the whole command; on
  expiry the process group is killed and the attempt is finalized as
  ERROR. No partial reads of stdout are ever interpreted.
- **The hook never touches the worktree.** Read-only by contract; the
  H-6 resume re-verifies HEAD == marker SHA before the MERGING tail, so
  even a misbehaving command that commits cannot smuggle code through an
  approval minted for the old SHA.

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
- **Fail-closed matrix:** timeout, non-zero exit, command not found,
  critic model == author model, empty `critics` with verdict PASS → all
  ERROR → workstream stays `NEEDS_REVIEW`, `gate_approver_runs` terminal
  row written, no `gate_approvals` row.
- **PASS path:** single transaction (approval `actor='agent'` +
  `approval_run_id` + READY), then H-6 resume reaches MERGING without
  respawn (`generate_spec` not awaited, PR created from existing
  worktree).
- **FAIL path:** stays `NEEDS_REVIEW`, findings visible in
  `verdict_json` and evidence note; human `workstream-approve` still
  works afterwards (agent attempt does not block the operator).
- **Guards:** each skip reason (§6) → correct `skipped` row + no
  invocation; budget counts only `actor='agent'` rows; stale-SHA skip
  after a new commit; `already_attempted` prevents double invocation on
  restart.
- **Mechanical allowlist:** full coverage → PASS without invocation,
  budget consumed; partial coverage → critic path.
- **Kill-switches:** config and env — no invocation, `disabled` skip
  row.
- **Crash sentinel:** `started` row without terminal state at startup →
  finalized `error/interrupted`, no re-run; new SHA re-arms.
- **Migration 20:** fresh DB and upgrade path; existing `gate_approvals`
  rows read back as `actor='human'`; journal tripwire lists updated.
- **Zero-change guarantee:** `gates:` without `approver:` — existing
  gates tests byte-identical; no `gates:` at all — untouched.

## 11. Open question for review

**Subprocess supervision (v1) vs execution layer.** This spec runs the
command as a directly supervised subprocess with the `started`-sentinel
providing crash evidence. The alternative — routing through the shared
execution layer (`execution_phase="approval"`, durable handles, probe
recovery, optional docker isolation like the Mode-1 verifier's
`backend: docker`) — buys uniform recovery and isolation at the cost of
a heavier slice. v1 chooses the subprocess because the failure domain is
inherently safe (worst case is always "a human reviews", never a wrong
approval), and the sentinel row already gives durable attempt evidence.
If the approver becomes an operationally mandatory channel (nightly
autonomous runs at scale), the execution-layer move is the follow-up —
the contract in §5 does not change.
