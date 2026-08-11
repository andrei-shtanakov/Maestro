# Per-workstream quarantine + resume without regeneration (#166) — design

**Status:** approved (revision 4, 2026-08-11 — recovery capture is placed
AFTER the isolation-aware classification, not before it: a live orphan is still
writing its state and logs, and an archive taken mid-write is a torn snapshot
that looks like evidence (§4.4). Revision 3 — quarantine does **not**
terminate a live handle, which reverses revision 2's "no new durable state"
conclusion and reinstates a migration; §3 rewritten accordingly. Revision 2
answered all four §8 questions, one of which corrected freeze from
process-wide to per-workstream.) Boundary with #164 was fixed
when that shipped: #164 gives approve (accept an incomplete result, execute
nothing) and rework (ordinary re-decomposition); **catching up the remaining
work is this document's subject**, and #164 deliberately left the naming free.
Builds on the whole of wave 4: `stop_reason` is guaranteed by the 2.24.0 pin
(#169b), the post-mortem archive machinery exists and is reused here (#164) —
though it does **not** yet cover an interrupted run, because a hard kill skips
the finalization that writes it (§2.3, §4.4) — and the retry classifier
already distinguishes failures worth re-running (#165).

## 1. What the pilot did, and why both halves of the fix are separate

An external forensic watchdog found a false DONE on `w-adapters` and had to
kill `maestro orchestrate`, because leaving it running meant dependents would
start from a false base. The process owned three concurrent executions
(`max_concurrent: 3`), so healthy `w-events` and `w-verifier` died with it —
mid-task, with uncommitted work in their worktrees. Recovery was manual:
inventory the worktrees, make defensive WIP commits, then decide per
workstream. Reproduced twice in one wave (2026-08-09 ~09:15 and ~12:35).

The issue's own boundary statement is right and this spec keeps it: **this is
not about the watchdog** (an external kill is sometimes necessary) **and not
about #164** (the false DONE that prompted it, now fixed). It is about a
process-wide stop having no smaller unit than "everything".

Two independent defects hide behind that, and they need different fixes:

- **A. There is no way to stop one workstream.** The operator's only lever is
  the process, so "quarantine this one" is spelled "kill all three".
- **B. An interrupted workstream cannot resume; it can only start over.**
  Whatever ends a run, the workstream lands in READY, and READY means
  "Always regenerate" (the comment of that name in
  `orchestrator.py::_spawn_workstream`) — a fresh spec, a fresh LLM
  lottery, fresh money, and the partial work discarded.

A is worth fixing because it removes the need to kill. B is worth fixing
because kills still happen — SIGKILL, OOM, a watchdog with no patience — and
B is what makes the acceptance criterion's second branch true.

## 2. What is actually true today (verified, not assumed)

| Path | What happens | Evidence |
|---|---|---|
| Graceful stop (SIGTERM/SIGINT) | `_cleanup` terminates **every** running handle, then walks each RUNNING → FAILED → READY | `orchestrator.py::_cleanup` |
| Hard kill (SIGKILL, watchdog) | Nothing runs; rows stay RUNNING until the next start | — |
| Next start after a hard kill | `_recover_stranded_workstreams` reconciles: a dead RUNNING → READY; a live orphan or possibly-live handle → NEEDS_REVIEW with an ambiguity marker | `orchestrator.py::_recover_stranded_workstreams` |
| Either way | READY → `_spawn_workstream` → DECOMPOSING → **regenerate** | the "Always regenerate" comment in `_spawn_workstream` |
| Interrupted evidence | The post-mortem archive exists **only if finalization ran**; a hard kill skips it | #164, `finalize.py` |

Three consequences the design has to respect:

1. **Graceful shutdown is already a collateral kill.** Fixing only the hard
   path would leave `maestro stop` destroying healthy work.
2. **The recovery path already distinguishes "provably dead" from
   "possibly alive"** and parks the latter fail-closed. Resume-without-regen
   must inherit that discipline rather than invent a parallel one — a
   resumable state that re-dispatches over a live orphan is worse than a
   regeneration.
3. **A hard kill leaves no archive.** "Preserve the logs" (the issue's
   acceptance) is therefore *not* already solved by #164 for this case: #164
   captures during finalization, which a SIGKILL skips. This spec must say
   where an interrupted run's evidence comes from.

## 3. Half A — quarantine one workstream instead of the process

### 3.1 What quarantine is, and what it deliberately is not

`maestro workstream-quarantine <id> --reason "<why>"` forbids a workstream's
result from progressing any further. **It does not terminate a live handle**
(owner decision, revision 3).

That distinction is the whole point rather than a nicety. Killing a running
execution to isolate it is exactly the loss of uncommitted work this issue
was filed about; a quarantine that kills would reproduce the defect in a
politer form. So:

- a RUNNING quarantined workstream **keeps running** to its natural end;
- **no new dispatch** happens for it — not now, not after a restart;
- when it finishes, it does **not** enter the delivery tail: it goes to
  NEEDS_REVIEW with the quarantine reason;
- **forced termination stays a separate, explicit operation.** Quarantine must
  never be the thing that happens to kill work, because an operator reaching
  for "isolate this" is not asking for that.

Lifting a quarantine is its own audited action
(`maestro workstream-unquarantine <id> --reason "<why>"`), never a side effect
of another verb. It records who lifted it and why, for the same reason the
approval path does: an operator undoing a safety decision is a decision.

`maestro stop` keeps its own meaning — see §4.6 — and quarantine is not a
scoped variant of it.

### 3.2 Why this needs durable state after all

Revision 2 concluded that no new storage was required, because the dispatcher
only picks up READY and so a transition into NEEDS_REVIEW *is* the freeze.
**That conclusion is void under revision 3.** With the process left running,
the row must stay RUNNING while it runs — the finalization path CASes with
`expected_status=RUNNING` (`_handle_completion` → `_handle_success` /
`_handle_failure`) and would raise `ConcurrentModificationError` against a row
someone had quietly moved to NEEDS_REVIEW.

Overloading NEEDS_REVIEW would therefore mix two unrelated facts — *what the
process is doing* and *what the operator has forbidden* — and break the CAS
the whole state machine relies on. A migration is cheaper than that: one
additive, nullable column,

```
workstreams.quarantined_at   TIMESTAMP   -- NULL = not quarantined
workstreams.quarantine_reason TEXT
```

with the quarantine audit row alongside (actor, reason, lifted_at,
lifted_by), mirroring `workstream_reworks` (#124).

### 3.3 The race with completion, resolved by one CAS

Quarantine and a finishing workstream can collide, and the resolution must be
atomic in the strong sense: **either delivery has irreversibly begun and
quarantine refuses, or quarantine wins and delivery is guaranteed not to
start.** No third outcome, no "mostly quarantined" workstream whose branch
lands anyway.

The boundary is the MERGING transition — the first step that touches the base
branch and the git host. Two writes, one row, existing machinery:

1. **Quarantine's write** requires the workstream to be in a state that has
   not begun delivering (`READY`, `RUNNING`, `DECOMPOSING`, `PENDING`,
   `VERIFYING`, `FAILED`, `NEEDS_REVIEW`). A row already in `MERGING`,
   `PR_CREATED` or `DONE` refuses with "delivery already started" — the
   operator's remedy there is a revert, not a quarantine, and pretending
   otherwise would be a lie about what was prevented.
2. **The MERGING transition's CAS gains `AND quarantined_at IS NULL`.** If
   quarantine landed first, that CAS fails and the completion path routes to
   NEEDS_REVIEW instead of delivering.

Because both are single-row writes under the same lock, exactly one wins, and
the loser learns it did from its own failed CAS rather than from a read that
could be stale. The gates (completeness, scope, ex-post) all run *before*
MERGING, so a quarantine racing them simply blocks at the same edge.

### 3.4 Why this removes the reason to kill

The pilot's sequence becomes: quarantine `w-adapters` (its result stops
progressing; if it is still running it finishes, and lands in NEEDS_REVIEW
rather than delivery) → `w-events` and `w-verifier` finish and deliver
normally → dependents of `w-adapters` stay blocked because it is not DONE. No
healthy execution is interrupted, no uncommitted work is lost, and the
watchdog never needs the process-level hammer.

## 4. Half B — resume an interrupted workstream without regeneration

### 4.1 The second meaning of READY, stated once

Today READY has exactly one meaning at dispatch: *(re)generate a spec and
spawn the author*. `resume_reason` already carves out five exceptions
(`verification_rework`, `verification_reverify`, `operator_rework`,
`completeness_accept_partial`, `postmortem_recapture`), and the dispatch is
exhaustive over that set — an unknown value is routed fail-closed, never
treated as a plain resume (the `KNOWN_RESUME_REASONS` guard at the head of
the resume dispatch in `_spawn_workstream`).

This spec adds one more member rather than a new status:
`RESUME_CONTINUE_TASKS` — *re-dispatch spec-runner against the existing
`spec/maestro-tasks.md`, with no regeneration and no author respawn*.

The invariant that keeps this from becoming a third undocumented entry into
RUNNING: **every non-plain READY is a named `resume_reason`, and the dispatch
switch is total over them.** #164 already pays into this discipline; #166
should extend it, not fork it.

### 4.2 Preconditions, all fail-closed

Continuation is only safe when the world matches what the interrupted run
left behind. Refuse — with a distinct reason — unless all hold:

1. **The worktree exists** and is the one the workstream recorded.
2. **No live process/handle**, proven by the same probe recovery already uses
   (`_probe_open_handle`, the isolation-aware `probe()` boundary). A
   possibly-live execution goes to NEEDS_REVIEW with an ambiguity marker, as
   today — continuation must never race an orphan.
3. **`spec/maestro-tasks.md` exists and validates** — the #165 dangling-dep
   check runs here too; continuing against a file that spec-runner will reject
   wastes the very spawn this feature exists to save.
4. **The executor state DB is present and readable.** spec-runner resumes from
   its own state; without it "continue" is indistinguishable from "start".

Refusal falls back to the operator's choice (rework, i.e. today's behaviour),
never to a silent regeneration.

### 4.3 What continuation does NOT do

- It does not judge whether continuing is a good idea. The completeness gate
  (#164) still runs at the end, so a continuation that stops short blocks
  exactly as a first run would.
- It does not skip the gates. Scope, ex-post and verification apply to the
  final state, not to how many dispatches produced it.
- It does not resurrect an approval. A continuation produces new commits,
  which move the SHA and void any prior approval — the existing rule.

### 4.4 Evidence for an interrupted run — captured at recovery, after classification

A hard kill skips finalization, so #164's archive is never written (§2.3).
Recovery therefore captures it — but **not before it has classified what it is
looking at** (owner correction, revision 4).

The naive placement (capture first, then reconcile) is wrong for a specific
reason: a **live orphan is still writing** its state database and its logs. An
archive taken mid-write is not evidence, it is a torn snapshot that looks like
evidence. Such a workstream must go back to monitoring, and its ordinary
finalization will capture a consistent archive at the proper moment.

So recovery runs in phases, and the existing four-way classification is left
exactly as it is:

```
probe / classify (isolation-aware, unchanged)
        │
        ├─ live orphan / possibly-live handle → back to monitoring, NO capture
        │                                        (finalization captures later)
        │
        └─ provably dead / stranded → capture checkpoint ──→ the branch's
                                       (single, shared)      original action
                                                             (cleanup, rework,
                                                              dispatch)
```

Four properties of the checkpoint, each with a reason:

- **One checkpoint, not four in-branch calls.** Capture is inserted between
  classification and action rather than inside each branch, so the branches
  keep their current logic and cannot drift apart in whether they capture.
- **Nothing destructive runs before it succeeds.** Cleanup, rework and dispatch
  are all downstream of the checkpoint, because each of them destroys or
  overwrites what is being captured.
- **An expected capture failure preserves the worktree** and hands the operator
  `RESUME_RECAPTURE` (#164), rather than silently proceeding.
- **A branch with no execution at all may record `state_missing`** — but only
  once the classifier has established that, never as a default assumption.

Otherwise unchanged from revision 2: capture goes through the existing
`_capture_evidence` core (a third caller after finalization and
`workstream-recapture`), is idempotent, and records `captured_by: recovery` so
an operator can tell interrupted evidence from evidence taken at an orderly
finalization.

### 4.5 Counting continuations without capping them

Every continuation is an explicit, audited operator action, not an automatic
loop, so there is **no numeric ceiling** (owner decision, §8.4). Forbidding the
N+1th without new knowledge would only move the operator to a workaround.

Continuations are counted and surfaced: the count appears in
`maestro workstreams`, and `workstream-continue` warns when it is already
high. Automatic retries remain governed by the existing retry budget and the
#165 classifier — this family is a different mechanism and does not borrow
their limits.

### 4.6 What `maestro stop` must do instead of killing

The global stop keeps its meaning — stop the orchestrator — but must stop
destroying healthy work (§2.1). Two acceptable shapes, in preference order:

1. **Drain (the required default).** Freeze new dispatch, **terminate
   nothing**, let in-flight executions finish, then exit. This is the same
   principle as quarantine: stopping the orchestrator is not a licence to
   destroy work in progress.
2. **Preserve the path**: if an execution must be ended before it finishes,
   the workstream is left able to continue — `resume_reason =
   RESUME_CONTINUE_TASKS` rather than the plain READY that today's `_cleanup`
   writes, which means regeneration.

Today's `_cleanup` does neither: it terminates every handle and writes plain
READY, so a routine `maestro stop` discards partial work exactly as the
watchdog's SIGKILL did. Whichever shape is implemented, the shutdown path must
not leave a workstream whose only way forward is a full regeneration.

## 5. State and storage

Revision 2 claimed no new table was needed; revision 3 reinstates a migration
(§3.2) because leaving the process alive means the status column cannot carry
the quarantine.

- **Quarantine**: `workstreams.quarantined_at` + `quarantine_reason`
  (additive, nullable) written under a CAS that refuses a row which has begun
  delivering (§3.3), plus an audit row (actor, reason, lifted_at, lifted_by)
  mirroring `workstream_reworks` (#124). The status column is untouched, so
  every existing CAS keeps meaning what it meant.
- **Dispatch suppression**: the READY dispatcher skips a row with
  `quarantined_at IS NOT NULL`. Durable by construction — a restart re-reads
  the column and does not silently resume.
- **Delivery suppression**: the MERGING CAS carries
  `AND quarantined_at IS NULL`.
- **Continuation** (half B): `resume_reason = RESUME_CONTINUE_TASKS`, set by
  `maestro workstream-continue <id>` in one CAS transaction on NEEDS_REVIEW,
  mirroring `requeue_for_recapture` (#164) so a crash cannot leave a READY
  workstream that falls through to regeneration.
- **Continuation count**: a counter on the workstream, for the warning in
  §4.5. Not a limit (§8.4).
- **Recovery capture**: reuses `postmortem_archives` (#164, migration 23);
  `captured_by` is a manifest field, not a schema change.

Migration: one additive column pair plus the audit table for half A; the
continuation counter for half B. Numbers to be re-checked at implementation —
a parallel session may claim one.

## 6. Test matrix

| # | Scenario | Expected |
|---|---|---|
| 1 | Quarantine does not terminate | The quarantined workstream's live handle keeps running; `terminate` is never called on it |
| 1b | Neighbours unaffected | The other two executions keep running and reach DONE |
| 1c | Finished quarantined workstream | Goes to NEEDS_REVIEW with the reason; **base branch unchanged**, no PR, no merge |
| 2 | Quarantined workstream's worktree | Preserved, with the reason recorded |
| 3 | No new dispatch | The READY dispatcher skips a quarantined workstream, including immediately after the write |
| 3b | Race: quarantine wins | Quarantine lands while RUNNING; the MERGING CAS then fails and completion routes to NEEDS_REVIEW — the branch never merges |
| 3c | Race: delivery wins | A row already in MERGING/PR_CREATED/DONE refuses the quarantine with "delivery already started"; no partial state |
| 3d | Status column untouched | A quarantined RUNNING row is still RUNNING, so `_handle_completion`'s CAS does not raise |
| 4 | Quarantine survives restart | Still quarantined and undispatched after a restart |
| 4a | Lifting is audited | `workstream-unquarantine` records actor and reason; dispatch resumes only after it |
| 4b | Dependents stay held | Dependents of a quarantined workstream do not start — it is not DONE — without any explicit subtree freeze |
| 4c | `maestro stop` drains | No new dispatch, and **no live handle is terminated**; in-flight executions finish |
| 4c2 | `maestro stop` preserves the path | If an execution must still be ended, the workstream can continue; it is not left needing regeneration |
| 4d | Recovery capture is idempotent | Running recovery twice yields one archive, marked `captured_by: recovery` |
| 4e | Recovery capture failure | Worktree preserved; operator gets the `RESUME_RECAPTURE` path; no cleanup, no rework, no dispatch |
| 5 | Continue after a graceful stop | spec-runner re-dispatched against the existing tasks.md; decomposer/spec-gen **never invoked** |
| 6 | Continue after a hard kill | Same, via the recovery path |
| 7 | Continue with a live orphan | Refused → NEEDS_REVIEW with the ambiguity marker; no dispatch |
| 8 | Continue with a missing worktree | Refused with a distinct reason; no silent regeneration |
| 9 | Continue with dangling deps in tasks.md | Refused by the #165 validator before the spawn |
| 10 | Continue with no executor state DB | Refused — "continue" needs something to continue from |
| 11 | Continuation that stops short | Completeness gate blocks it exactly like a first run |
| 12 | Unknown resume_reason | Still fail-closed to NEEDS_REVIEW (dispatch totality) |
| 13 | Graceful stop no longer kills healthy work | With freeze+quarantine available, `_cleanup` is reached only on a real shutdown; the shutdown path's own behaviour is asserted unchanged |
| 14 | Recovery capture ordering | A stranded RUNNING with a dead process produces a committed archive BEFORE cleanup, rework or dispatch |
| 15 | Continuation counter | Counted and surfaced; the N+1th is warned about, never refused |

## 7. Non-goals

- **No change to what the watchdog does.** External tooling may still kill the
  process; this makes that unnecessary, not impossible.
- **No implicit forced termination.** Neither quarantine nor `maestro stop`
  kills a running execution. Forcing one to stop stays a separate, explicit
  operation, so that "isolate this" and "destroy this work" can never be the
  same keystroke.
- **No automatic continuation.** Every continuation is an operator decision,
  like every other resume in this family. An orchestrator that resumes
  interrupted work by itself would need a policy for "how many times", which
  is the retry question #165 just answered for a different case.
- **No new scheduling mode.** Quarantine removes one workstream from dispatch
  by putting it in a status the dispatcher never picks up; it does not
  introduce partial-DAG scheduling, priorities or a subtree freeze. Dependents
  are held by the DAG invariant that already exists, not by new machinery.

## 8. Resolved (owner decisions, 2026-08-11)

### 8.1 Capture at recovery — yes

Before cleanup, rework or a new dispatch; through the existing post-mortem
core; idempotent; marked `captured_by: recovery`. An expected capture failure
preserves the worktree and routes the operator to `RESUME_RECAPTURE` rather
than silently continuing a destructive path (§4.4).

### 8.2 Freeze is per-workstream — corrects revision 1

The dependent subtree is already held by DAG invariants, so automatically
freezing descendants would add durable state and a harder unfreeze for no
gain. Healthy independent workstreams keep running (§3.2). Revision 1's
process-wide proposal rested on a rationale that #164 had already removed.

### 8.3 Quarantine always implies freeze, atomically

A state where the quarantine is recorded but the dispatcher may still start
the workstream is inadmissible. Lifting the quarantine and resuming afterwards
may be separate commands; establishing it may not be split (§3.2).

### 8.4 No numeric cap on continuations

`RESUME_CONTINUE_TASKS` is an explicit audited operator action, not an
automatic loop. Count and warn; do not forbid the N+1th without new knowledge.
Automatic retries stay under the existing budget (§4.5).

### 8.5 Quarantine does not terminate a live handle — reverses part of rev 2

Confirmed literally by the owner: quarantine forbids the result from
progressing, it does not destroy the current work. The consequence is a
durable `quarantined_at` column rather than an overloaded NEEDS_REVIEW,
because leaving the process alive means the status must stay RUNNING and the
existing `expected_status=RUNNING` CAS must keep working (§3.2). The migration
is cheaper than mixing process state with an operator prohibition. The
quarantine/completion race is resolved by one CAS at the MERGING edge (§3.3).

## 9. The five distinctions this design must preserve

Stated separately because each is a place where an implementation could
plausibly drift into the wrong behaviour:

1. **Quarantining one workstream stops nothing that is running — neither its
   neighbours nor itself.** Independent executions keep running because they
   were never touched; the quarantined one keeps running because quarantine
   blocks the progression of its *result*, not its process (§3.1). What
   changes for it is that no new dispatch occurs and its completion routes to
   NEEDS_REVIEW instead of the delivery tail.
2. **A global `maestro stop` freezes new dispatch and either lets in-flight
   workstreams finish or preserves `RESUME_CONTINUE_TASKS` for them.** It
   never leaves a workstream whose only way forward is regeneration.
3. **Continuation uses the existing spec, worktree and execution state**,
   passes `probe()` and the #165 validator, and **does not call
   `generate_spec`**. "Continue" that regenerates is not continue.
4. **If those preconditions are not proven, fail closed to NEEDS_REVIEW** with
   a distinct reason — never a hidden fallback to a full regeneration. A
   silent regeneration is the failure mode this whole document exists to
   remove, and it must not reappear as an error path.
5. **Nothing here terminates a running execution.** Quarantine forbids
   progression; `maestro stop` drains. Forced termination is a separate
   explicit operation. If an implementation ever finds itself calling
   `terminate()` from either path, it has reintroduced the defect.
