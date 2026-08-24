# Mode-1 run-level branch isolation (`git.run_branch`) — design

**Status:** revision 3 — codex-review findings incorporated; awaiting
owner approval
**Date:** 2026-08-24
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
Maestro runtime on exactly two seams — run start (before the run is
published) and resume (before recovery). Plus a durable record of the
branch in the run row and a per-dispatch tripwire (phase B).

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

On `--resume` (consumer point 4):

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
- No cleanliness check on resume: a crashed run legitimately leaves
  uncommitted task work in the tree.
- The record is read as a three-state matrix on the two columns:
  - `declared=1` → `run_branch` is set; verify as above.
  - `declared=0` → the run opted out at creation; the gate does
    nothing on resume — genuinely byte-identical, no warning, no
    adoption. Adding `run_branch` to the config later takes effect on
    the next *fresh* run, never mid-run (a run must not change its own
    rules mid-flight — #198's own principle).
  - `declared=NULL` → a true pre-migration row. **Fails open** with a
    structured warning, following #198's `workstreams_declared`
    precedent — and, when the config declares `run_branch`, **adopts
    the observed branch into the record** (consumer proposal, one
    UPDATE setting both columns): the first resume writes the current
    branch, so every later resume of that run is protected. The hole
    closes on first use, not by legacy runs dying out. The adoption is
    announced (structured event + the same warning), because it
    records observed state, not verified intent. A pre-migration row
    whose config does *not* declare `run_branch` is recorded as
    `declared=0` and stays silent.
  **This deliberately diverges from dispatcher's fail-closed at their
  analogous seam** ("predates checkout binding, re-submit"), and the
  divergence is priced, not accidental: a dispatcher request is cheap to
  recreate, while a Maestro run *exists* — it has state, and a refused
  resume would strand it forever. Both decisions are correct where they
  stand; anyone later "harmonizing" them into one rule would be turning
  one of them into a bug.
- With `--db` there may be no run row at all (legacy/manual databases).
  Then resume verification falls back to the config-derived name with an
  explicit warning that the record is absent. Documented limitation of
  the `--db` escape hatch.

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

- Immediately before each task spawn, the scheduler compares the current
  branch against the recorded `run_branch` (one `git rev-parse
  --abbrev-ref HEAD`, negligible cost).
- Mismatch → no spawn; the run **suspends** (`suspended_at` +
  `suspend_reason`, spec §B.1.1 — resumable, not an outcome), and the
  refusal text goes to stderr. Tasks already running are not killed —
  the same drain philosophy as #166's first-signal behavior. Confirmed
  by the consumer (answer 2): a kill loses the task's result and leaves
  partial edits in the tree — the very mess the gate protects against —
  and their console renders `suspended` as resumable, where a killed run
  would read as breakage.
- Resume then passes through §6's verification as usual.

Phase B ships as a separate PR on the same spec. Phase A alone closes
the consumer's blocker (UI-driven pass 1); Phase B is the hardening that
makes "runtime guarantee" literally true for the whole run, not its
first instant.

## 8. Refusal surface

- One typed error family (`RunBranchGateError` with a machine-readable
  `reason`: `branch_equals_base`, `dirty_tree`, `wrong_start_point`,
  `resume_branch_mismatch`). The two fail-open cases (§6: NULL record,
  `--db` without a run row) are warnings, not members of this family.
- Rendered as plain text on **stderr** (`err_console`, matching every
  existing refusal in `cli.py`), exit code 1 (consumer point 5: stderr
  is the only channel dispatcher relays to the operator). Each message
  carries the observed state and the one command that fixes it.
- Structured event (`run_branch_gate.refused` / `.created` /
  `.verified`) through the obs pipeline for post-mortem debugging —
  best-effort, the stderr text is the contract.

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
  adopted record; `--db` fallback warns.
- Lock-ordering test (major 1): with the PID lock already held, a
  second `maestro run` with `run_branch` configured refuses **without
  touching the checkout** — current branch asserted unchanged.
- Phase B: mid-run branch flip → no further spawns, run suspended,
  running task untouched.
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
