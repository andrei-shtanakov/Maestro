# Mode-1 run-level branch isolation (`git.run_branch`) — design

**Status:** revision 9 — phase B shipped; §6/§8 revised to the
as-implemented state (ruling R14; logger-based events)
**Date:** 2026-08-24
**Revision 9 (phase B implementation, 2026-08-25):** (1) §6 — the head
record is maintained by ATTRIBUTION, not observation (ruling R14, PR
#222): `on_auto_commit` hands over the sha of a commit the run itself
made; the earlier "refreshed on graceful suspension/stop" is dropped —
an observational refresh would normalize a foreign commit into the
run's record and pass the next continuation green over it, the exact
hole the stale check exists to catch. Priced cost, found twice (codex
round 4, PR #222): a run whose agent commits by itself (auto_commit
off) leaves the record behind the branch and stale-refuses on the next
continuation, where `--accept-branch-tip` is the audited way through.
(2) §7 — as-built decisions recorded in place: a fifth seam (`collect`),
sticky suspension, drain semantics, VERIFYING preservation, and the
undisturbed suspend marker. (3) §8 — the event surface is implemented
as structured obs records (`Attributes.event` + kwargs) through a
per-call logger, not as `EventType` members; reasons gain
`live_branch_mismatch` / `live_stale_checkout`.
**Revision 8 (codex-review round 6: two high, one medium; owner
decision on the process):** (1) §6 — continuation deliberately does
not require a clean tree: both alternatives (fail-closed; acceptance
flag) are weighed and refused *in the file*, the accepted consequence
is stated, and the gate owes visibility — a structured warning naming
the dirty paths — not a block. (2) §6 — `--db --clean` is a fresh
start, not a continuation: it discards the state there is to continue,
so the §4 start gate applies and the documented escape hatch survives.
(3) §6 — legacy adoption also persists `run_branch_head`, so the
adopted row can pass its next stale-check. Core observation for the
record: the spec's base (key, start gate, lock order, durable record,
verification-before-recovery) is unchanged since revision 3 — rounds
4–6 found issues only in the remedies of earlier rounds.
**Revision 7 (codex-review round 5, three majors, each confirmed):**
(1) §6 — continuation selectors also take the recovery path: phase A
re-keys the Mode-1 recovery guard from the `--resume` flag to "an
existing run was selected", because a gated continuation that skips
recovery stalls on crash-stranded tasks — a gate that blesses a
stalling invocation is worse than none. (2) §7 — the live tripwire
compares branch name AND tip against `run_branch_head`: a foreign
commit on the same branch moves the state as surely as a flip.
(3) §6 — legacy adoption verifies against the config-declared name
before adopting; a checkout that disagrees with declared intent
refuses instead of durably binding the run to an accident.
**Revision 6 (codex-review round 4: one major, one medium):** (1) §6 —
the supersession check is re-founded on *state* instead of proxies:
the run row records `run_branch_head` (tip sha, kept current by the
run), and continuation refuses when the branch tip moved
(`resume_stale_checkout`) — round 4 rightly showed the round-2
name-counting rule false-blocks, but its DAG-keying remedy is refused
with rationale (a different DAG that advanced the shared branch still
moved the state; the invariant is "the state did not move", not "no
newer run exists"); the crash window refuses over the run's own commit
and the escape is an explicit audited `--accept-branch-tip`, not
silent adoption. (2) §6 — plain `--db` naming a branch-bound database
is a continuation and walks the same verification, `--resume` or not.
**Revision 5 (codex-review round 3, three majors, each confirmed):**
(1) §7 — the completion-path tripwire fires before any terminal
transition, and the success tail reorders to tripwire → auto-commit →
`DONE` on gated runs: `DONE` is terminal, so a gate between `DONE` and
the commit would strand a terminal-yet-uncommitted task that resume
never revisits; the residual one-git-invocation window is stated, not
hidden. (2) §7 — verifier preflight added to the tripwire inventory
(`_run_verifier` reads the worktree), and the inventory itself becomes
test-asserted: a new checkout-using seam must claim a tripwire.
(3) §2/§6 — the gate covers *continuation of an existing run* as
`bootstrap_run` defines it (any `run_id_override`), so `--run <id>`
without `--resume` cannot slip past §6.
**Revision 4 (codex-review round 2 on PR #221, three majors, each
confirmed against the code):** (1) §6 — a superseded run refuses to
resume (`resume_superseded`): name equality is not state identity, so
resume checks the registry for a newer run bound to the same branch;
(2) §7 — the phase-B tripwire fires before *every* checkout use, the
completion path included (validation launch, `_auto_commit_task`), not
only before spawns; (3) §6 — `--db` with `run_branch` configured and no
run row now refuses (`record_missing`) instead of proceeding off the
config-derived name: unknown provenance is not green.
**Revision 3 (codex-review on PR #221, two majors, both confirmed
against the code):** (1) §5 — the checkout is mutated only under the
Mode-1 singleton fence: `RunIsLive` sees only the Mode-2 stage locks
(`orchestrate`/`review`), so the global PID lock — the real Mode-1
serializer, today acquired late — moves ahead of the gate; without
this a losing second invocation could flip the shared checkout under
the live scheduler and only then die on the lock. (2) §6 — `NULL`
cannot mean both "pre-migration row" and "opted out", so the run row
carries `run_branch_declared` alongside `run_branch` (#198's
`workstreams_declared` lesson at the same table); opt-out runs resume
genuinely byte-identically, and warn-plus-adopt applies only to true
pre-migration rows.
**Revision 2 (consumer review):** §4 asymmetry made explicit (switching
to an existing run branch is deliberate iteration, not state capture);
§6 records why this fail-open deliberately diverges from dispatcher's
fail-closed at the analogous seam, and adopts the consumer's
close-the-hole-earlier proposal (NULL record → adopt the current branch
on first resume); §3 states per-DAG-not-per-run explicitly; new §10
rollout note (pilot's temporary precondition task is *replaced*, not
kept); the three open questions are resolved (clean tree stays strict —
consumer's own #217 burn as evidence; phase B suspends, never kills;
`{run_id}` templating rejected, not deferred).
**Issue:** #216 part 2 (`slug: mode1-branch-isolation`), TODO wave 9
**Consumer input incorporated:** dispatcher's five points (2026-08-24,
relayed by the owner): (1) verification alone is not enough — Maestro
must be able to *create* the branch, or pass 1 is not UI-driven;
(2) dirty tree → refusal only, never stash-and-switch (the stash is
shared across all worktrees of the workspace); (3) the moment of the
check decides the *shape* of the refusal — dispatcher's `accepted:
true` is bound to the atomic rename of its run catalog (their §5.3), so
a pre-publication refusal is a clean `accepted: false` while a
post-publication one is "✓ started" plus a dead run (their #176 was
exactly this, for `ATP_CATALOG`); (4) `--resume` must read the branch
from the run record and re-verify it, never re-derive it (their #174);
(5) refusals must arrive as plain text on the child's stderr — the only
channel dispatcher relays to the operator.

## 1. Problem

Mode 1 (`maestro run`) executes on one shared checkout, on whatever
branch happens to be checked out. With `auto_commit: true` the run
writes task commits to that branch — in the Dark Factory pilot, straight
to the target repo's protected `master`, silently (issue #216). Part 1
(PR #218) closed the *false promise*: `git.branch_prefix` is now
rejected instead of silently ignored. Nothing yet provides the isolation
itself.

Per-task branches are semantically impossible here (one checkout,
concurrent tasks). The unit that *can* be isolated is the run: one
checkout, one branch for the whole run. The pilot currently emulates
this manually — the operator creates and switches to `pilot/<slug>`, and
the first DAG task asserts the precondition and fails before any change.
That scheme is explicitly temporary: branch creation by the operator
makes pass 1 not UI-driven (consumer point 1), and a precondition owned
by a DAG task is an agent action that can fail or lie, discovered only
after the checkout was already accepted.

## 2. Scope and non-goals

**In scope:** one opt-in config key, `git.run_branch`, enforced by the
Maestro runtime on exactly two seams — fresh run start (before the run
is published) and **continuation of an existing run**, whatever flags
select it (before recovery). "Continuation" is defined by
`bootstrap_run`'s own behavior, not by the `--resume` flag: the
existing-run path is taken whenever `run_id_override` is set,
`--resume` or not, so `--run <id>` without `--resume` walks the same
§6 verification (codex-review round 3, major 3 — the earlier "fresh
start and resume" wording left that invocation outside the gate, able
to continue a run bound to `feature/x` while standing on `main`). Plus
a durable record of the branch in the run row and a per-dispatch
tripwire (phase B).

**Non-goals:**

- **Mode 2 untouched.** Worktree-per-workstream already isolates
  branches there.
- **No stash, ever** (consumer point 2). Every path either proceeds on
  a clean, verified state or refuses with instructions. Maestro never
  runs `git stash`.
- **No push/PR semantics change.** `auto_push` keeps pushing the current
  branch — which under this gate is the run branch, exactly the
  protection wanted. No PR is created; that remains Mode 2's job.
- **No branch cleanup.** The run branch outlives the run; deleting or
  merging it is the operator's (or the caller's) decision.
- **No fix for `maestro validate`** being Mode-2-only. As with part 1,
  the gate lives on the `load_config` / `maestro run` path.

## 3. Config surface

```yaml
git:
  base_branch: master
  auto_commit: true
  run_branch: pilot/entrypoint-token-boundary-match
```

- `run_branch: <literal branch name>`, optional. Absent → today's
  behavior, byte-identical (opt-in, like every recent gate).
- **The branch is per-DAG, not per-run.** The key lives in the DAG's
  config, so two runs of one DAG share the branch. What is isolated is
  the run *from `base`*, never runs *from each other* — re-running a
  DAG continues on its own branch (see §4). The feature name says
  "run-level" because the *enforcement unit* is the run (one gate per
  run start, one record per run row), not because each run gets a fresh
  branch.
- **One key, not two.** A separate `require_non_base_branch: true`
  (check-only) is not offered: a check-only mode cannot make pass 1
  UI-driven (consumer point 1), and the check is implied — `run_branch`
  equal to `base_branch` is rejected at config validation, in the same
  validator style as part 1's `branch_prefix` rejection.
- **Literal name only — `{run_id}` templating rejected, not deferred**
  (consumer answer 3): the point of the pilot branch is that a human can
  find it and open a PR from it. A branch per run means a branch per
  attempt — the pilot had three in one day, two of them dead — which is
  branch-list noise plus a standing "which one do I PR from?" question.
  A future consumer would have to overturn this with a use case, not
  merely claim it.

## 4. Start gate (fresh run)

Let `B = git.run_branch`, `base = git.base_branch`, `cur` = current
branch of the `repo:` checkout.

| Checkout state | Action |
|---|---|
| `cur == B`, tree clean | proceed |
| `cur == B`, tree dirty | **refuse** (`dirty_tree`) |
| `B` exists, `cur != B`, tree clean | `git switch B`, proceed |
| `B` exists, `cur != B`, tree dirty | **refuse** (`dirty_tree`) — switching under uncommitted work is the data-loss case |
| `B` missing, `cur == base`, tree clean | `git switch -c B`, proceed |
| `B` missing, `cur == base`, tree dirty | **refuse** (`dirty_tree`) |
| `B` missing, `cur != base` | **refuse** (`wrong_start_point`) — creating `B` from an arbitrary branch would silently capture that branch's state |

Decisions behind the table:

- **Creation only from `base`.** The one creation rule is "Maestro cuts
  the run branch from the declared base". Anything cleverer (create from
  an explicit ref, fast-forward first) is deferred until a consumer
  needs it.
- **The switch/create asymmetry is deliberate, not an oversight.**
  Switching to an *existing* `B` accepts whatever state `B` carries —
  including commits from a previous run — while creating `B` from a
  non-`base` branch refuses as `wrong_start_point`. The difference is
  ownership: `B` is *the run branch this config declares*, so continuing
  on it is normal iteration over the DAG's own prior work (the pilot ran
  three attempts exactly this way); an arbitrary `cur` is somebody
  else's state, and cutting `B` from it would capture that state
  silently.
- **Clean tree required on every fresh-start path, including "already on
  `B`" — strict, confirmed by the consumer** (answer 1). A dirty tree
  plus `auto_commit: true` means pre-existing content gets swept into an
  agent's commit. Not hypothetical: #217's run logs sat un-gitignored in
  deployer's tree and would have ridden into the pilot's commit exactly
  this way. The pilot's own temporary precondition already required a
  clean tree. Refusal names the dirty paths (bounded list).
- **Detached HEAD refuses** (`wrong_start_point`): there is no `cur` to
  reason about.

## 5. Timing: before publication, structurally

The gate runs inside `bootstrap_run`, **after** identity resolution and
the `RunIsLive` check, **before** `create_run` builds the staging
directory. Consequences, in the shape the consumer cares about
(point 3):

- A refusal exits before the run directory's atomic rename — no run row
  exists, `resolve_runs` never sees it, dispatcher observes a clean
  `accepted: false`. There is nothing to garbage-collect.
- The `RunIsLive` check stays first: while another run is live, Maestro
  must not touch the checkout at all — not even to switch to a branch
  the live run may itself be standing on.
- **The checkout is mutated only under the Mode-1 singleton fence**
  (codex-review major 1, revision 3). `RunIsLive` alone is NOT that
  fence: `resolve_runs` derives liveness from the scoped stage locks,
  whose stages are only `orchestrate` and `review` — a live Mode-1
  scheduler is invisible to it. The lock that actually serializes
  `maestro run` is the global PID lock, today acquired late in
  `_run_scheduler` (after bootstrap, DB open and scheduler
  construction). Left there, a losing second invocation would pass the
  gate, `git switch` the shared checkout under the live scheduler, and
  only then die on the lock — moving the branch the live run's
  `auto_commit` writes to. Phase A therefore moves PID-lock acquisition
  **ahead of the gate**: acquire lock → identity + `RunIsLive` → gate →
  publish; any refusal on that path releases the lock and leaves no
  run. The lock's scope is unchanged (one Mode-1 scheduler per Maestro
  home, exactly what it enforces today) — only the acquisition point
  moves earlier, which narrows nobody.
- The gate is also what may *create and switch* the branch, and that
  side effect lands only on the success path toward publication. A
  crash between the switch and the rename leaves the checkout on `B`
  with no run — visible, clean-tree, and harmless (re-running the same
  config hits the `cur == B`, clean row of the table).

Mechanically: `bootstrap_run` gains a seam between liveness resolution
and `create_run` (an optional pre-publish hook or an explicit split into
resolve + publish — implementation's choice; the contract is only the
ordering above, PID lock included). `--db <path>` skips `bootstrap_run`
entirely; there the gate runs at the equivalent point in
`_run_scheduler` — before the database is opened, and equally after the
PID lock.

## 6. Durable record and `--resume`

`create_run_row` gains **two** columns, written in the same transaction
as the rest of the row — inside staging, so the record is atomic with
publication (no published run without its record): `run_branch` (TEXT,
the bound branch or NULL) and `run_branch_declared` (0/1). The second
column exists because `NULL` alone cannot carry two meanings
(codex-review major 2, revision 3) — this is #198's
`workstreams_declared` lesson applied at the same table: a run created
*after* this feature with `run_branch` **omitted** writes
`(NULL, declared=0)`, and a pre-migration row reads
`(NULL, declared=NULL)`. Without the bit, every new opt-out run would
hit the legacy warn-and-adopt path on its first resume — a warning plus
a persisted branch binding on a run that opted out, breaking the §3
byte-identical promise, and adopting whatever branch the operator
happened to resume from. A schema migration adds both columns to
existing databases; the number is claimed at implementation time
(parallel actors also claim migrations).

On continuation of an existing run — `--resume`, `--run <id>`, or both,
**and plain `--db <path>` naming a database whose run row is
branch-bound** (`declared=1`): that invocation already continues prior
task state today (`--db` skips the resolver, opens the named database,
and the scheduler preserves its existing tasks), so it walks the same
verification whether or not `--resume` was typed (codex-review round 4,
medium — the selector list alone left it undefined). The one exception
is `--db ... --clean` (round 6, major 2): `--clean` discards the named
database's state, so there is nothing left to continue — that
invocation is a **fresh start** and takes the §4 start gate instead.
Routing it through continuation verification would refuse
(`resume_stale_checkout`) an invocation whose entire point is
abandoning that state, breaking a documented escape hatch. The flagless
definition is deliberate so no selector of an existing run bypasses
this section (consumer point 4).

**Continuation selectors also take the recovery path** (round 5,
major 1): today Mode-1 recovery is guarded by `if resume:` alone, so a
`--run <id>` or plain-`--db` continuation would pass the branch gate,
open the database — and then stall: crash-stranded `RUNNING`/
`VALIDATING` rows are never reconciled, never re-spawned, and never
complete. A gate that blesses a stalling invocation is worse than
none. Phase A re-keys the recovery guard to the same definition this
section uses — "an existing run was selected" — so every continuation
reconciles before scheduling, `--resume` flag or not.

The verification rules:

- The branch is **read from the run row and verified against the
  checkout** — never re-derived from the config. The config may have
  been edited since (that is #198's territory); the record is the run's
  own truth.
- `cur != recorded` → **refuse** (`resume_branch_mismatch`), naming both
  branches and the exact `git switch <recorded>` the operator can run.
  No auto-switch, even on a clean tree: resume can run unattended
  (`maestro service`), and a checkout silently moved under a human at
  the terminal is the same class of surprise this issue exists to
  remove. Acting nowhere beats acting in the wrong place.
- **Continuation does not require a clean tree — a priced hole, not an
  oversight** (round 6, major 1; owner decision). Both alternatives
  were weighed and refused, and the argument is recorded here so the
  next reader — human or gate — does not re-litigate it from scratch:
  *fail-closed on any dirt* refuses exactly the resume the feature
  exists to serve, because a crashed run legitimately leaves
  uncommitted task work in the tree and no cheap check can attribute
  dirt to the run versus to a foreign edit made after the crash; *an
  explicit acceptance flag* puts every crash-resume behind a flag,
  which operators learn to type reflexively — the same hole with extra
  ceremony. Accepted consequence: a foreign uncommitted edit made
  between crash and resume is indistinguishable from the run's own
  residue, and the next auto-commit may sweep it. What the gate owes
  the operator here is **visibility, not a block**: continuation emits
  a structured warning naming the dirty paths (bounded list), so what
  will ride along is seen before it does. Same shape as §7's TOCTOU
  window — stated, priced, bounded.
- **A run whose branch moved underneath it refuses to resume** —
  checked against *state*, not proxies (revised twice: round 2 major 1
  established the hazard — an older run resuming on top of a newer
  run's commits while a name-only check smiles; round 4 major 1 showed
  the round-2 remedy, "any newer run recording the same branch name",
  is itself a proxy with false positives — a newer run that bound the
  branch but never advanced it would block a perfectly resumable run.
  Keying supersession to DAG identity, as round 4 suggested, would be
  the wrong fix: a different DAG that *did* advance the shared branch
  still moved the state under this run, and DAG-keying would wave that
  resume through on top of foreign commits. The invariant was never
  "no newer run exists"; it is "the state did not move"). So the run
  row records **`run_branch_head`** — the branch tip sha — written at
  publication, and updated by attribution only — after each commit the
  run itself makes (ruling R14; revision 9 dropped the graceful-stop
  observational refresh, which would normalize foreign commits into
  the record). On continuation: tip of
  the recorded branch equals the recorded head → proceed; anything
  else → refuse (`resume_stale_checkout`), naming both shas — this
  catches a newer run of the same DAG, a run of a different DAG, and a
  human commit alike, and never blocks when nothing actually moved.
  Runs without a branch record (`declared=0`/pre-migration) do not
  participate.
  **The crash window is stated, not hidden:** a crash between a task's
  git commit and the bookkeeping update leaves the tip one commit
  ahead of the record, so the next resume refuses over the run's *own*
  work. The escape is explicit and audited, not automatic:
  `--accept-branch-tip` re-records the observed tip as a deliberate
  operator statement ("I inspected the delta and it is this run's
  own"), emitting a structured event — the same
  verify-then-state-it-explicitly shape as
  `workstream-resolve-ambiguity`. Without the flag the refusal stands;
  silent adoption here would reopen the hole the check exists to
  close.
- The record is read as a three-state matrix on the two columns:
  - `declared=1` → `run_branch` is set; verify as above.
  - `declared=0` → the run opted out at creation; the gate does
    nothing on resume — genuinely byte-identical, no warning, no
    adoption. Adding `run_branch` to the config later takes effect on
    the next *fresh* run, never mid-run (a run must not change its own
    rules mid-flight — #198's own principle).
  - `declared=NULL` → a true pre-migration row. A row whose config
    does *not* declare `run_branch` is recorded as `declared=0` and
    stays silent. When the config *does* declare one, the record is
    absent so the config-declared name is the only intent available —
    and **adoption verifies against it first** (round 5, major 3):
    observed branch == configured `run_branch` → warn (legacy binding,
    #198's fail-open precedent) and adopt, one UPDATE setting both
    columns **plus `run_branch_head` = the observed tip** (round 6,
    medium — without the head, the next continuation's stale-check
    would have nothing to compare against and would falsely refuse),
    announced by a structured event — the hole closes on
    first use, not by legacy runs dying out. Observed branch !=
    configured → **refuse** (`resume_branch_mismatch`, verified
    against the config-declared name since no record exists). The
    earlier rule adopted whatever branch happened to be checked out,
    which would let one accidental `--resume` on `main` durably bind
    the run to `main` and send recovery — which mutates the working
    tree — straight into it, making the mistake permanent. Adoption
    records observed state only when that state matches declared
    intent; it never manufactures intent from an accident.
  **This deliberately diverges from dispatcher's fail-closed at their
  analogous seam** ("predates checkout binding, re-submit"), and the
  divergence is priced, not accidental: a dispatcher request is cheap to
  recreate, while a Maestro run *exists* — it has state, and a refused
  resume would strand it forever. Both decisions are correct where they
  stand; anyone later "harmonizing" them into one rule would be turning
  one of them into a bug.
- With `--db` there may be no run row at all (legacy/manual databases).
  **When `run_branch` is configured, that resume refuses**
  (`record_missing`) — revised from warn-and-fall-back-to-config in
  round 2 (codex-review major 3): a database with no run row has
  unknown provenance, and passing the gate because the checkout happens
  to match a *config-derived* name is "unknown as green" at the exact
  seam the gate protects — the same anonymous-state hazard the frozen
  legacy default was split away to remove. The refusal names the two
  exits: resume through the resolver path (whose databases carry run
  rows), or drop `run_branch` from the config, which makes the run an
  ordinary ungated `--db` run — an explicit operator choice, not a
  silent fallback. A `--db` pointing at a resolver-created database
  *has* a run row and verifies normally.

**Ordering: verification precedes recovery.** This deliberately diverges
from #198, which halts *after* the liveness/reconciliation pass. #198's
recovery is checkout-neutral; Mode-1 recovery is not — finalizing open
SSH handles *collects task results into the working tree*. Collecting
onto the wrong branch is precisely "acting in the wrong place", so on a
branch mismatch nothing runs, recovery included. The refusal leaves all
state untouched and the run resumable.

## 7. Phase B: per-dispatch tripwire

The start gate makes the guarantee hold "before the first task". The
shared checkout stays shared afterwards: the single-run invariant is a
working agreement, not a mechanism (dispatcher's §5.4.1 — their durable
lock holds off a second *controller* run, not a human at the terminal),
and that human can switch branches mid-run. Phase B closes that window at the same point the codebase
already trusts (#166: the late check is the guarantee):

- The tripwire fires at **every point where the scheduler is about to
  use the checkout**, not only before spawns (codex-review round 2,
  major 2). The current inventory of those seams, each guarded:
  immediately before each task spawn; before launching a task's
  validation; before verifier preflight (round 3, major 2 —
  `_run_verifier` builds the scope patch and reads `HEAD` from the
  worktree, so a flip between validation and the judge would have the
  judge inspect, and rule on, unrelated tree state); and before the
  auto-commit-plus-DONE finalization block. The inventory is a claim
  about the scheduler, so the implementation derives it from "every
  checkout read/write on the run path" and a test asserts the list —
  a new checkout-using seam must claim a tripwire, the way a new
  status must claim a transitions-table entry. **Each check compares
  both the branch name and the branch tip** against the run's recorded
  `run_branch_head` (round 5, major 2): §6's invariant is state
  immobility, not name stability, and a foreign commit landed on the
  *same* branch mid-run moves the state just as surely as a branch
  flip — the name-only check would smile through it and auto-commit on
  top of moved state. The run's own commits update the recorded head
  (§6), so only foreign movement trips. Two `git rev-parse`
  invocations per seam — still negligible.
- **The completion-path check runs before any terminal transition**
  (round 3, major 1). Today the scheduler marks a task `DONE` and only
  then calls `_auto_commit_task`; `DONE` is terminal, so a gate firing
  between the two would strand a task that is terminal yet
  uncommitted — "preserved for resume" would be a lie, because resume
  never revisits `DONE`. Phase B therefore reorders the success tail
  on gated runs: tripwire → auto-commit → `DONE`. A mismatch suspends
  with the task still in its pre-terminal status, which resume
  re-enters. The residual window between a passed check and the git
  operation it guards cannot be closed without git-level locking and
  is stated, not hidden — the tripwire narrows every window to the
  width of one git invocation; the start-gate and continuation checks
  remain the outer barriers (the same honesty as the approver spec's
  "H-6's SHA check is the final barrier").
- Mismatch at any tripwire point → the pending mutation does not
  happen (no spawn / no validation launch / no verifier / no commit,
  no terminal transition), the task's pre-terminal state is preserved
  for resume, and the run
  **suspends** (`suspended_at` +
  `suspend_reason`, spec §B.1.1 — resumable, not an outcome), and the
  refusal text goes to stderr. Tasks already running are not killed —
  the same drain philosophy as #166's first-signal behavior. Confirmed
  by the consumer (answer 2): a kill loses the task's result and leaves
  partial edits in the tree — the very mess the gate protects against —
  and their console renders `suspended` as resumable, where a killed run
  would read as breakage.
- Resume then passes through §6's verification as usual.
- **As implemented (revision 9):** the inventory gained a fifth seam,
  `collect` — finalizing a remote handle applies results into the
  working tree, which §6 itself names as checkout-mutating; the
  inventory test (`tests/test_run_branch_tripwire.py`) asserts all
  five. The trip is **sticky**: once the run is suspending, every later
  seam refuses without re-reading git — a branch restored mid-drain
  must not let half the completions finalize under a run already
  recorded suspended. The drain never finalizes: exited processes are
  dropped from tracking with their RUNNING rows and open execution
  handles intact — the crash shape resume recovery already reconciles
  after §6 re-verification (the task's own timeout keeps its
  pre-existing terminate; it is the task's policy, not the gate's). A
  task tripped in VERIFYING stays VERIFYING and resolves through the
  verifier's fail-closed crash recovery (NEEDS_REVIEW, never
  auto-re-run) — preserved, not softened. The suspend marker is not
  cleared on resume: `classify_run` ranks observed liveness above it,
  a completed resume's outcome wins over it, and `maestro service`
  reading "suspended = human required" is the safe direction for a
  checkout only a human can fix.

Phase B ships as a separate PR on the same spec. Phase A alone closes
the consumer's blocker (UI-driven pass 1); Phase B is the hardening that
makes "runtime guarantee" literally true for the whole run, not its
first instant.

## 8. Refusal surface

- One typed error family (`RunBranchGateError` with a machine-readable
  `reason`: `branch_equals_base`, `dirty_tree`, `wrong_start_point`,
  `resume_branch_mismatch`, `resume_stale_checkout`, `record_missing`,
  `live_branch_mismatch`, `live_stale_checkout`).
  The one fail-open case (§6: a true pre-migration record) is a
  warning, not a member of this family.
- Rendered as plain text on **stderr** (`err_console`, matching every
  existing refusal in `cli.py`), exit code 1 (consumer point 5: stderr
  is the only channel dispatcher relays to the operator). Each message
  carries the observed state and the one command that fixes it.
- Structured event (`run_branch_gate.refused` / `.created` /
  `.verified`) through the obs pipeline for post-mortem debugging —
  best-effort, the stderr text is the contract. As implemented, the
  structured events are obs records emitted through a per-call logger
  (`run_branch_gate.py::_emit`) — the event name lands in
  `Attributes.event` — rather than `EventType` members; telemetry
  only, the stderr text remains the contract.

## 9. Testing sketch

- Table-driven unit tests over §4's matrix against real temp git repos
  (the existing `test_git*` fixtures pattern).
- Publication-ordering test: a refusing gate leaves `runs/` empty and
  `resolve_runs` blind (no staging residue).
- Resume tests over the three-state record matrix: `declared=1` record
  honored over config, mismatch refuses before recovery (recovery mock
  asserts zero calls); `declared=0` resume is silent — no warning, no
  adoption, no gate; `declared=NULL` + config-declared warns, adopts
  the observed branch, and a second resume then verifies against the
  adopted record; `--db` + configured `run_branch` + no run row
  refuses (`record_missing`), never proceeds off the config-derived
  name.
- Stale-checkout tests (round 2 major 1 + round 4 major 1): run A
  suspended on `B`, a fresh run advances `B` → `--resume --run <A>`
  refuses (`resume_stale_checkout`) with both shas; a newer run that
  recorded `B` but never committed → resume of A **proceeds** (the
  round-4 false positive); a human commit on `B` between suspension
  and resume → refuses; `--accept-branch-tip` re-records the tip with
  an audited event and the next resume proceeds; crash between a
  task's commit and the head-record update → refusal over the run's
  own commit, then the flag path.
- Plain `--db` continuation test (round 4, medium): `--db` naming a
  database whose run row is branch-bound, no `--resume`, wrong
  checkout branch → same refusal as the `--resume` form.
- Continuation-recovery test (round 5, major 1): `--run <id>` and
  plain `--db` continuations with a crash-stranded `RUNNING` task →
  recovery reconciles it exactly as under `--resume`; no stalled run.
- Legacy-adoption verification test (round 5, major 3):
  `declared=NULL` + config declares `pilot/x` + checkout on `main` →
  refuses (`resume_branch_mismatch`), nothing adopted; on `pilot/x` →
  warns, adopts, proceeds.
- Live same-branch movement test (round 5, major 2): foreign commit on
  the run branch while a task runs → next tripwire seam refuses, no
  auto-commit on top of the moved state, run suspended.
- Dirty-continuation visibility test (round 6, major 1): continuation
  with uncommitted paths in the tree → proceeds, structured warning
  names the dirty paths.
- `--clean` fresh-start test (round 6, major 2): `--db state.db
  --clean` on a branch-bound database with a moved tip → no
  continuation refusal; the §4 start gate applies and the state is
  discarded as documented.
- Lock-ordering test (round 1, major 1): with the PID lock already
  held, a second `maestro run` with `run_branch` configured refuses
  **without touching the checkout** — current branch asserted
  unchanged.
- Phase B: mid-run branch flip → no further spawns, run suspended,
  running task untouched; the completion path — branch flipped between
  task exit and finalization → no validation launch, no auto-commit,
  **no terminal transition** (task asserted pre-terminal and re-entered
  on resume — round 3, major 1); a verifier-enabled task with a flip
  between validation and verifier preflight → judge never invoked, run
  suspended (round 3, major 2); and the seam-inventory test — every
  checkout-using point on the run path claims a tripwire.
- Continuation-selector test (round 3, major 3): `--run <id>` without
  `--resume` on a branch-bound run passes through the same §6
  verification — wrong checkout branch refuses identically to the
  `--resume` form.
- The opt-out path (no `run_branch`) byte-identical: existing suite
  green without modification is the evidence, as with every recent gate.

## 10. Rollout and review resolutions

**Rollout (coordination with the pilot, consumer remark 4):** when
phase A lands, the pilot's temporary scheme is **replaced, not kept**.
deployer#35 put a first DAG task that asserts "on `pilot/<slug>`, tree
clean"; once the runtime gate fires earlier, that task checks the
already-guaranteed and becomes a relic that drifts from reality — the
next DAG reader cannot tell which of the two checks is the real one.
Removing it is a dispatcher/deployer-side step and belongs in their
adoption of phase A; recorded here so neither side treats coexistence
as the plan.

**Review resolutions (revision 2 — dispatcher, issue #216):** the three
questions §10 used to hold are closed, with the reasoning folded into
their sections:

1. Fresh-start cleanliness stays **strict** on every path (§4).
2. Phase B **suspends**, never kills (§7).
3. `{run_id}` templating **rejected** (§3).
