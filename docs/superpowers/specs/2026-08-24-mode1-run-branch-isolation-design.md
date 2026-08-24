# Mode-1 run-level branch isolation (`git.run_branch`) — design

**Status:** draft for review (owner + dispatcher as consumer)
**Date:** 2026-08-24
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
- **One key, not two.** A separate `require_non_base_branch: true`
  (check-only) is not offered: a check-only mode cannot make pass 1
  UI-driven (consumer point 1), and the check is implied — `run_branch`
  equal to `base_branch` is rejected at config validation, in the same
  validator style as part 1's `branch_prefix` rejection.
- **Literal name only in this slice.** `{run_id}`-style templating is
  recorded as a possible extension, not designed: the pilot's usage is
  an operator-named per-DAG branch, and templating would reopen every
  "branch already exists" question for no present consumer.

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
- **Clean tree required on every fresh-start path, including "already on
  `B`".** A dirty tree plus `auto_commit: true` means pre-existing
  operator changes get swept into task commits — the same artifact-sweep
  hazard as #217, and the pilot's own temporary precondition already
  required a clean tree. Refusal names the dirty paths (bounded list).
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
- The gate is also what may *create and switch* the branch, and that
  side effect lands only on the success path toward publication. A
  crash between the switch and the rename leaves the checkout on `B`
  with no run — visible, clean-tree, and harmless (re-running the same
  config hits the `cur == B`, clean row of the table).

Mechanically: `bootstrap_run` gains a seam between liveness resolution
and `create_run` (an optional pre-publish hook or an explicit split into
resolve + publish — implementation's choice; the contract is only the
ordering above). `--db <path>` skips `bootstrap_run` entirely; there the
gate runs at the equivalent point in `_run_scheduler` — before the
database is opened.

## 6. Durable record and `--resume`

`create_run_row` gains a `run_branch` column, written in the same
transaction as the rest of the row — inside staging, so the record is
atomic with publication (no published run without its branch record).
A schema migration adds the column to existing databases; the number is
claimed at implementation time (parallel actors also claim migrations).

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
- `run_branch` recorded as NULL (a run started before this feature)
  **fails open** with a structured warning, following #198's
  `workstreams_declared` precedent: refusing every legacy resume would
  be worse than the hole, and the hole closes itself as old runs drain.
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
  the same drain philosophy as #166's first-signal behavior.
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
- Resume tests: record honored over config; mismatch refuses before
  recovery (recovery mock asserts zero calls); NULL record warns and
  proceeds; `--db` fallback warns.
- Phase B: mid-run branch flip → no further spawns, run suspended,
  running task untouched.
- The opt-out path (no `run_branch`) byte-identical: existing suite
  green without modification is the evidence, as with every recent gate.

## 10. Open questions for review

1. **Fresh-start cleanliness on "already on `B`"** (§4): strict refusal
   is specified; if the pilot needs "dirty but already on the run
   branch" to pass, that row can be relaxed to a warning without
   touching the rest.
2. **Phase B suspension vs. hard stop:** suspension keeps running tasks
   alive (drain); if the consumer would rather kill on tripwire, say so
   — the mechanism is the same, the policy differs.
3. **`{run_id}` templating:** deferred here; a consumer with a
   one-branch-per-run need should claim it with a use case.
