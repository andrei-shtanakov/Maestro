# Per-workstream quarantine + resume without regeneration (#166) — design

**Status:** approved (revision 2, 2026-08-11 — all four §8 questions answered
by the owner and folded in; one of them corrected the design: freeze is
per-workstream, not process-wide). Boundary with #164 was fixed
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

### 3.1 The operator verb

`maestro workstream-quarantine <id> --reason "<why>"`: stop **that**
workstream's execution, leave every other execution running, and leave its
worktree intact for inspection. The workstream lands in NEEDS_REVIEW carrying
the reason, exactly like every other block, so no new status is needed.

Deliberately a separate verb from `maestro stop`, which keeps meaning "stop
the orchestrator". Overloading it with a scope argument would make the
dangerous form the default one.

### 3.2 Quarantine atomically freezes that workstream

Quarantine and "stop dispatching it" are **one transaction, never two**. A
state where the quarantine is recorded but the dispatcher may still start the
workstream is exactly the race the feature exists to remove, and a two-step
version would leave a window for it on every crash.

This needs no new storage, which is the pleasant consequence of an existing
invariant: **the dispatcher only ever picks up READY**, so a single guarded
transition into NEEDS_REVIEW *is* the freeze. The quarantine reason rides in
`error_message` like every other block, and the operator's exits are the verbs
that already exist (rework, continue — §4, recapture).

**Freeze is per-workstream, not process-wide** (owner decision, §8.2). The
process-wide switch proposed in revision 1 was unnecessary: a dependent cannot
start while its dependency is not DONE, so the DAG already holds the subtree,
and an automatic freeze of descendants would add durable state whose unfreezing
is its own problem. Healthy independent workstreams keep running, which is the
whole point.

Revision 1 justified a global freeze with "stop everything before dependents
start from a bad base". That rationale died with #164: dependents only started
in the incident because the workstream reached a **false** DONE, and a DONE
that lies is now blocked by the completeness gate. What is left is genuinely
per-workstream.

### 3.3 Why this removes the reason to kill

The pilot's sequence becomes: quarantine `w-adapters` (its execution stops,
its dispatch is frozen by the same write) → `w-events` and `w-verifier` finish
normally → dependents of `w-adapters` stay blocked because it is not DONE. No
healthy execution is interrupted, and the watchdog never needs the
process-level hammer.

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

### 4.4 Evidence for an interrupted run — captured at recovery

A hard kill skips finalization, so #164's archive is never written (§2.3).
Recovery therefore captures it (owner decision, §8.1), and the ordering is the
load-bearing part: **capture runs before cleanup, before any rework, and
before a new dispatch** — those are precisely the operations that destroy or
overwrite what is being captured.

Four properties, each with a reason:

- **Through the existing post-mortem core**, not a second implementation.
  `_capture_evidence` already takes an explicit evidence key and a workspace
  (it was factored that way for `workstream-recapture`), so recovery is a
  third caller rather than a parallel path that could drift.
- **Idempotent.** Recovery can run repeatedly — a restart loop, an operator
  re-running `orchestrate --resume` — and must not multiply archives or fail
  on the second pass. The archive is keyed by execution and the row upserts,
  so a repeat reconciles.
- **Marked with its source.** The manifest records `captured_by: recovery`, so
  an operator reading the evidence knows it was taken after an interruption
  rather than at an orderly finalization; the two say different things about
  how complete the executor state is.
- **An expected capture failure preserves the worktree and hands the operator
  `RESUME_RECAPTURE`** — the #164 path — instead of silently proceeding down a
  destructive route. This is the same rule finalization already follows: if
  the evidence cannot be saved, nothing that would destroy it may run.

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

1. **Drain**: freeze new dispatch, let in-flight executions finish, then exit.
2. **Preserve the path**: if an execution must be ended before it finishes,
   the workstream is left able to continue — `resume_reason =
   RESUME_CONTINUE_TASKS` rather than the plain READY that today's `_cleanup`
   writes, which means regeneration.

Today's `_cleanup` does neither: it terminates every handle and writes plain
READY, so a routine `maestro stop` discards partial work exactly as the
watchdog's SIGKILL did. Whichever shape is implemented, the shutdown path must
not leave a workstream whose only way forward is a full regeneration.

## 5. State and storage

**No new table.** Revision 1 proposed an `orchestration_freeze` row; the
per-workstream decision removes the need, because NEEDS_REVIEW already stops
dispatch and is already durable, CAS-guarded and audited.

- **Quarantine**: one guarded transition (RUNNING → NEEDS_REVIEW) carrying the
  reason, plus termination of that workstream's execution. Atomic by
  construction — the dispatcher cannot pick up a non-READY row.
- **Continuation**: `resume_reason = RESUME_CONTINUE_TASKS`, set by
  `maestro workstream-continue <id>` in one CAS transaction on NEEDS_REVIEW,
  mirroring `requeue_for_recapture` (#164) so a crash cannot leave a READY
  workstream that falls through to regeneration.
- **Continuation count**: a counter on the workstream, for the warning in
  §4.5. Not a limit (§8.4).
- **Recovery capture**: reuses `postmortem_archives` (#164, migration 23);
  `captured_by` is a manifest field, not a schema change.

Migration: only the continuation counter is additive schema. Number to be
re-checked at implementation — a parallel session may claim one.

## 6. Test matrix

| # | Scenario | Expected |
|---|---|---|
| 1 | Quarantine one of three running | Only that execution stops; the other two keep running and reach DONE |
| 2 | Quarantined workstream's worktree | Preserved, with the reason recorded |
| 3 | Quarantine freezes atomically | The dispatcher never picks up the quarantined workstream, including immediately after the write |
| 4 | Quarantine survives restart | Still quarantined and undispatched after a restart |
| 4b | Dependents stay held | Dependents of a quarantined workstream do not start — it is not DONE — without any explicit subtree freeze |
| 4c | `maestro stop` preserves the path | After a global stop, an interrupted workstream can continue; it is not left needing regeneration |
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

## 9. The four distinctions this design must preserve

Stated separately because each is a place where an implementation could
plausibly drift into the wrong behaviour:

1. **Quarantining one workstream does not stop its neighbours.** Independent
   executions keep running; only the quarantined one is terminated.
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
