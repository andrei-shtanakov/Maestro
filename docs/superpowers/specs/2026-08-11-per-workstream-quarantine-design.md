# Per-workstream quarantine + resume without regeneration (#166) — design

**Status:** proposed (revision 1, 2026-08-11). Boundary with #164 was fixed
when that shipped: #164 gives approve (accept an incomplete result, execute
nothing) and rework (ordinary re-decomposition); **catching up the remaining
work is this document's subject**, and #164 deliberately left the naming free.
Builds on the whole of wave 4: `stop_reason` is guaranteed by the 2.24.0 pin
(#169b), the post-mortem archive already preserves an interrupted run's
evidence (#164), and the retry classifier already distinguishes failures worth
re-running (#165).

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
  "Always regenerate" (`orchestrator.py:1606`) — a fresh spec, a fresh LLM
  lottery, fresh money, and the partial work discarded.

A is worth fixing because it removes the need to kill. B is worth fixing
because kills still happen — SIGKILL, OOM, a watchdog with no patience — and
B is what makes the acceptance criterion's second branch true.

## 2. What is actually true today (verified, not assumed)

| Path | What happens | Evidence |
|---|---|---|
| Graceful stop (SIGTERM/SIGINT) | `_cleanup` terminates **every** running handle, then walks each RUNNING → FAILED → READY | `orchestrator.py::_cleanup` |
| Hard kill (SIGKILL, watchdog) | Nothing runs; rows stay RUNNING until the next start | — |
| Next start after a hard kill | `_recover_stranded_workstreams` reconciles: a dead RUNNING → READY; a live orphan or possibly-live handle → NEEDS_REVIEW with an ambiguity marker | `orchestrator.py:541` |
| Either way | READY → `_spawn_workstream` → DECOMPOSING → **regenerate** | `orchestrator.py:1606` |
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

### 3.2 Freeze dispatch

Quarantine alone is not enough: the reason to kill the process was usually
"stop *everything* before dependents start from a bad base". So a second,
process-level verb that is **not** a kill:

`maestro orchestrate --freeze` / `maestro freeze <project.yaml>`: no new
workstream is dispatched; in-flight executions run to completion. The DAG
stops advancing while the operator investigates. Unfreezing resumes dispatch.

Freeze is durable (a row, not a memory flag) so a restart does not silently
resume dispatching into an unresolved incident.

### 3.3 Why this removes most kills

The pilot's sequence becomes: freeze dispatch (dependents cannot start) →
quarantine `w-adapters` (the false-DONE one stops) → `w-events` and
`w-verifier` finish normally. No healthy execution is interrupted, and the
watchdog never needs the process-level hammer.

## 4. Half B — resume an interrupted workstream without regeneration

### 4.1 The second meaning of READY, stated once

Today READY has exactly one meaning at dispatch: *(re)generate a spec and
spawn the author*. `resume_reason` already carves out five exceptions
(`verification_rework`, `verification_reverify`, `operator_rework`,
`completeness_accept_partial`, `postmortem_recapture`), and the dispatch is
exhaustive over that set — an unknown value is routed fail-closed, never
treated as a plain resume (`orchestrator.py:1533`).

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

### 4.4 Evidence for an interrupted run

A hard kill skips finalization, so no archive is written (§2.3). Two options,
and the spec must pick one:

- **(a) Capture at recovery.** When `_recover_stranded_workstreams` finds a
  stranded RUNNING with a dead process, run the #164 capture before doing
  anything else. The worktree is still there and the state DB is current, so
  the evidence is real; the cost is one capture per stranded workstream at
  startup.
- **(b) Leave it.** The worktree is preserved anyway, so the data is not lost
  — only unarchived.

Recommend **(a)**: the operator's complaint was that a kill left them doing
manual worktree forensics, and an archive is exactly the artifact that removes
that. It also makes "interrupted" and "finished" produce the same shape of
evidence, which matters for #164's gate reading a manifest rather than a
worktree. Open question 1 asks the owner to confirm.

## 5. State and storage

- **Freeze**: one row per project (`orchestration_freeze`: project, frozen_at,
  reason, actor) — durable, so a restart honours it.
- **Quarantine**: no new storage. NEEDS_REVIEW + reason in `error_message` is
  the existing vocabulary, and the operator's exits are the existing verbs
  (approve / rework / recapture / — new — continue).
- **Continuation**: `resume_reason = RESUME_CONTINUE_TASKS`, set by a new
  `maestro workstream-continue <id>` in one guarded transaction, mirroring
  `requeue_for_recapture` (#164): a CAS on NEEDS_REVIEW, and the reason
  written in the same statement so a crash cannot leave a READY workstream
  that falls through to regeneration.

Migration: one additive table (freeze). Next free number to be re-checked at
implementation — a parallel session may claim one.

## 6. Test matrix

| # | Scenario | Expected |
|---|---|---|
| 1 | Quarantine one of three running | Only that execution stops; the other two keep running and reach DONE |
| 2 | Quarantined workstream's worktree | Preserved, with the reason recorded |
| 3 | Freeze | No new dispatch; in-flight executions finish |
| 4 | Freeze survives restart | Still frozen after the orchestrator restarts |
| 5 | Continue after a graceful stop | spec-runner re-dispatched against the existing tasks.md; decomposer/spec-gen **never invoked** |
| 6 | Continue after a hard kill | Same, via the recovery path |
| 7 | Continue with a live orphan | Refused → NEEDS_REVIEW with the ambiguity marker; no dispatch |
| 8 | Continue with a missing worktree | Refused with a distinct reason; no silent regeneration |
| 9 | Continue with dangling deps in tasks.md | Refused by the #165 validator before the spawn |
| 10 | Continue with no executor state DB | Refused — "continue" needs something to continue from |
| 11 | Continuation that stops short | Completeness gate blocks it exactly like a first run |
| 12 | Unknown resume_reason | Still fail-closed to NEEDS_REVIEW (dispatch totality) |
| 13 | Graceful stop no longer kills healthy work | With freeze+quarantine available, `_cleanup` is reached only on a real shutdown; the shutdown path's own behaviour is asserted unchanged |
| 14 | Recovery capture (if §4.4a) | A stranded RUNNING with a dead process produces a committed archive before reconciliation |

## 7. Non-goals

- **No change to what the watchdog does.** External tooling may still kill the
  process; this makes that unnecessary, not impossible.
- **No automatic continuation.** Every continuation is an operator decision,
  like every other resume in this family. An orchestrator that resumes
  interrupted work by itself would need a policy for "how many times", which
  is the retry question #165 just answered for a different case.
- **No partial-DAG scheduling changes.** Freeze is a stop-the-world switch for
  *dispatch*, not a new scheduling mode.

## 8. Open questions for the owner

1. **§4.4** — capture the post-mortem archive at recovery for stranded
   workstreams (recommended), or leave interrupted runs unarchived?
2. **Freeze granularity** — process-wide (proposed) or per-workstream-subtree?
   Subtree freezing sounds more precise but needs a reachability rule over the
   DAG, and the incident only ever needed "stop everything new".
3. **Should quarantine imply freeze?** The pilot wanted both together every
   time. Coupling them is convenient and slightly surprising; recommend
   keeping them separate verbs with the sequence documented.
4. **Continuation budget** — spec-runner enforces its own budget per run.
   Should a continuation carry a Maestro-side ceiling on how many times one
   workstream may be continued, or is that the operator's business (each
   continuation being an explicit decision)? Recommend the latter for v1.
