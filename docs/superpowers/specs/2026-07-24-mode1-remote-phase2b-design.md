# Mode-1 remote execution — Phase 2b (Safety-core)

Child of `docs/superpowers/specs/2026-07-21-maestro-distributed-execution-design.md`
(§7 shared-workdir hazard, §8 collect-apply, Phasing 2b) and sibling of
`docs/superpowers/specs/2026-07-24-maestro-ssh-backend-phase2a-design.md`
(the shipped SSH backend, PR #99 / `324e9aa`). This spec adapts the parent §7
onto the shipped Phase-2a SSH backend and folds in a round of design review
(2026-07-24). Where the parent design and this spec differ, **this spec wins**.

Scope of this phase is deliberately narrow — the **Safety-core**: lift the
Mode-1 SSH guard, and ship exactly the two mechanisms that make lifting it
safe. `validation_backend` (parent §9) and patch-based collect are explicitly
**out of scope** (see Known limitations).

## Context

The SSH backend (Phase 2a) is fully wired into **both** modes: Mode-1's
`Scheduler` already dispatches every task through
`BackendResolver → ExecutionRequest → backend.run() → handle.collect()/cleanup()`
(`maestro/scheduler.py:1058`, `:1071`, `:1159`). The only thing stopping a
Mode-1 task from running over SSH today is a single blanket guard in
`BackendResolver._build` (`maestro/execution/resolver.py:52-57`):

```python
if isinstance(transport, SshTransport):
    if self._mode == "scheduler":
        raise ExecutionConfigError(
            f"backend {name!r} uses ssh transport: SSH backends are "
            "Mode-2 (orchestrator) only until Phase 2b"
        )
```

The guard exists because Mode 1 runs agents directly in a **shared**
`task.workdir` (parent §7). Two remote Mode-1 tasks from the same workdir would
each snapshot a baseline, mutate remotely, and rsync back — the second
clobbering the first. Mode 2 is safe because each workstream owns a private git
worktree.

### What already exists and is reused

- `maestro/execution/ssh_collect.py` — full-worktree baseline (`capture_baseline`,
  a whole-tree `{relpath: sha256}` manifest), `plan_collect` (whole-tree diff
  with baseline-divergence detection, symlink/traversal rejection, structural
  conflict checks), `apply_collect`. Phase 2b **extends** this with a scope
  filter; it does not restrict the baseline.
- `execution/models.py:CollectPolicy` — already has `mode="scope_paths"` and an
  `include`/`exclude` glob shape (unused by scheduler spawners, which all emit
  `mode="none"`).
- Durable handle state machine `prepared → running → terminal → collected →
  cleaned` (`database.py`), atomic `start_execution` (READY→RUNNING CAS + handle
  insert in one txn, `database.py:1328`), monotonic `mark_execution_state`
  (`:1431`), and the **non-cleaned non-local handle** recovery query
  (`:1539`). This state machine is the anchor for reservation lifecycle (§6).
- `Task` already carries `scope: list[str]`, `workdir: str`, `backend: str |
  None` (`models.py:527`, `:537`, `:550`).
- `maestro/preflight.py` — two-tier scope-overlap (static heuristic + exact
  file-set intersection). Reusable **only** for the part that proves overlap in
  possible-path space; the exact-file-set tier is not sound for the lock (§3).

## Goal

Allow a Mode-1 (`maestro run`) task to execute on an SSH backend safely, under
one invariant:

> Remote (ssh) Mode-1 execution is permitted only with **scope-bounded collect**
> and **no overlap** between the active `(workdir, scope)` reservations.

Local (`bare`) and local-Docker Mode-1 execution are unchanged — they mutate the
shared workdir in place, with no snapshot→rsync round-trip, so the collect
clobber hazard does not apply to them and they are out of the reservation
protocol.

### Hard requirements

- **Pure-local workdirs: behavior-compatible with today.** The reservation
  protocol arms per-workdir and only when SSH execution targets that workdir
  (§1). "Behavior-compatible", not "byte-identical": internal structures (the
  arming pass, the reservation registry) exist regardless, but the *observable
  scheduling* of a workdir with no SSH task is unchanged — scopeless local tasks
  keep running concurrently.
- **A remote executor never receives GitHub credentials.** Inherited from
  Phase 2a (`GH_*` denylist); unchanged.
- **`spec-runner plan --full` stays local.** N/A to Mode 1 (no decomposition),
  but the "weak center" boundary is respected: git/validation stay on center.
- **Naive shared-workdir full rsync is never silently enabled.** A remote Mode-1
  task without a bounding scope fails fast (§2); it is never run with an
  unbounded whole-worktree collect.

## Non-goals

- **`validation_backend: same | local | <backend>` (parent §9).** Validation
  stays a local subprocess after a successful collect. Next slice.
- **Patch-based collect (`git diff --binary` on the remote).** `scope_paths`
  collect only. Patch is an alternative delivery strategy, not an MVP condition.
- **Local Docker changes.** In-place bind-mount; untouched by this phase.
- **SSH + Docker isolation (Phase 2c).**

## Design

### §1. Arming — static, per-workdir

At scheduler start, **after** full config load and validation and **before** the
first task is dispatched, compute the set of *armed* workdirs. The set is
**immutable for the whole run**.

- A task is **SSH** iff its *effective backend* —
  `task.backend or execution.default_backend` — resolves to a `BackendSpec`
  whose `transport.type == "ssh"`. This is the precise test: `backend != "local"`
  is wrong, because local Docker is non-local yet needs no scope-collect.
- Group tasks by **canonical workdir** (§4 path policy). A workdir is *armed* iff
  it hosts ≥ 1 SSH task.
- Backend and scope are static per task, so this is fully computable up front;
  no runtime state changes the arming set.

On a **non-armed** workdir the reservation protocol is inert — dispatch is
byte-for-byte the current path. On an **armed** workdir every task on it
(local *and* SSH) participates in the reservation protocol (§3).

### §2. Fail-fast — start-time

During the arming pass, any **SSH task with an empty / undeclared `scope`** makes
the run fail immediately with `ExecutionConfigError` (surfaced the same way a
preflight error is), before the first task starts. Rationale: an unbounded
remote Mode-1 collect is exactly the naive whole-worktree round-trip the parent
§7 forbids. Scope and backend are both static, so this is a start-time check, not
a mid-run surprise.

(A scopeless *local* task on an armed workdir is **not** an error — it is
admitted with a whole-workdir reservation, §3.)

### §3. Reservation registry — in-memory, `maestro/execution/reservations.py`

A new module owns the `(canonical_workdir, scope)` reservation set. In-memory
(`asyncio`), reconstructed on recovery from durable handle state (§6).

**Reservation.** For a task on an armed workdir:
- SSH task → reservation = its normalized scope globs (guaranteed non-empty, §2).
- local task with a scope → reservation = its normalized scope globs.
- local task without a scope → reservation = `**` (whole workdir).

**Overlap test — conservative in path-space, never fs-snapshot.** Exact
file-set intersection is **unsound** for a lock: two scopes can match no common
file *today* yet both permit creating the same future path. The lock must have
**no false negatives**. Algorithm:

1. For each glob, compute its **static anchor** — the longest leading path
   segment run containing no wildcard (`src/api/*.py` → `src/api`;
   `pkg/**` → `pkg`; `lib/**/x.py` → `lib`).
2. A glob with no safe anchor (leading wildcard, e.g. `**`, `*.py`, `**/x`)
   → anchor = the workdir root (reserves everything).
3. A reservation covers the **subtree** of each of its anchors.
4. Two reservations **overlap** iff any anchor subtree of one contains-or-equals
   an anchor subtree of the other (prefix relation on canonical segment paths).

This over-approximates (false positives — two disjoint globs under a shared
ancestor may serialize) but never under-approximates. Exact-path matching stays
where it is sound: in **collect** (§4), against actual changed paths. The
`preflight.py` two-tier helper is reused **only** if its logic proves overlap in
possible-path space; its exact-file-set tier is not reused for the lock.

**Acquire / hold / release — keyed to the execution handle, not task status.**

- **Acquire** happens immediately before the atomic `start_execution`
  (`database.py:1328`, the READY→RUNNING CAS + handle insert). If acquisition
  fails (an active reservation overlaps), the task is **not dispatched this
  tick**: it consumes no concurrency slot and is retried on a later tick when the
  conflicting reservation releases. One reservation per task (its whole scope
  set at once) ⇒ no multi-lock ordering, no deadlock.
- **Rollback.** If `start_execution` raises `ConcurrentModificationError`
  (lost CAS), or the spawn/`backend.run()` fails after the CAS, the reservation
  is released as part of the same failure unwind — a reservation is never left
  behind by a failed start.
- **Hold** spans the full handle lifecycle: run → wait → collect → cleanup.
- **Release** happens **after finalization and the durable state transition**
  (`mark_execution_state("collected"/"cleaned")`), not merely when a terminal
  *task* status is written. Release is driven by handle state (§6), so a
  terminal-but-not-yet-collected handle keeps its reservation.

### §4. Scope-aware collect — extend `ssh_collect.py`

For an SSH Mode-1 task the scheduler rewrites the built `CollectPolicy` to
`mode="scope_paths"`, `include=task.scope`, keeping the default `exclude`
(`.git/**`, `.maestro/**`, …). The **baseline stays full-worktree** — scope
bounds the *apply set* and the *reject filter*, never the baseline:

1. **Baseline (full worktree).** `capture_baseline` over the entire worktree
   before the run — the pre-run manifest. Required to tell a pre-existing
   out-of-scope file apart from one the remote task created/modified.
2. **Diff (full worktree).** `plan_collect` computes modified/deleted over the
   whole tree (existing behavior).
3. **Scope reject.** Any changed path (modify, delete, **or new file**) that
   does **not** match `task.scope` → `CollectConflict`. Plus the existing
   baseline-divergence-within-touched-paths, symlink, traversal, and structural
   checks.
4. **Apply (scope only).** Only in-scope changes are applied into the shared
   workdir.
5. **Preflight before mutation.** The full plan (steps 1–3) completes and passes
   before the first local mutation — apply is all-or-reject.

A conflict routes the task to `NEEDS_REVIEW` with the shared workdir left intact
(parity with Phase 2a collect-conflict handling).

### §5. Guard removal — `resolver.py`

Delete the `mode == "scheduler"` SSH block (`resolver.py:52-57`). The resolver
builds `SshBackend` uniformly for both modes. The Mode-1 safety contract lives
in the scheduler (§2 fail-fast + §3 reservations), not in the resolver — the
resolver's job is backend construction, not policy.

### §6. Recovery — reservation reconstruction from durable handle state

On scheduler restart, reservations are rebuilt from **durable handle state**,
not from task status. A restart is **not** evidence that no remote execution is
live; the existing fail-closed probe (`ssh_recovery` / `probe_execution`) runs
first.

Reconstruction matrix (per SSH `execution_handle`):

| Handle condition | Reservation |
|---|---|
| Any **non-cleaned** SSH handle (`database.py:1539` query), any task status | **reconstructed** |
| Probe: running / unreachable / ambiguous | **held** |
| Confirmed terminal, **collect not yet done**, subsequent manual/recovery collect permitted | **held** |
| `collected` / `cleaned` | **released** |
| Explicitly abandoned with proven no-process **and** collect waived | **released** |

Release is gated on the durable transition (`collected`/`cleaned`) plus
finalization — never on merely observing a terminal task row. A task that the
fail-closed probe has already sent to `NEEDS_REVIEW` can still have a live remote
process; its reservation is held until the handle is durably `collected`/
`cleaned` or explicitly abandoned.

## Behavior-compatibility statement

With no SSH task on a workdir, that workdir is not armed (§1), the reservation
registry is never consulted for it, and its observable scheduling — including
concurrent scopeless local tasks — is identical to pre-2b. The semantic change
(scopeless tasks on an armed workdir serialize under a whole-workdir
reservation) is a direct, explicit consequence of opting a workdir into SSH
execution.

## File change map

- `maestro/execution/resolver.py` — remove the Mode-2-only SSH guard (§5).
- `maestro/execution/reservations.py` — **new**: reservation registry, anchor
  overlap algorithm, acquire/release, recovery reconstruction (§3, §6).
- `maestro/scheduler.py` — arming pass + start-time fail-fast (§1, §2);
  acquire-before-`start_execution` with rollback (§3); `CollectPolicy` rewrite
  for SSH Mode-1 tasks (§4); release-on-finalization wiring; recovery
  reconstruction call (§6).
- `maestro/execution/ssh_collect.py` — scope reject filter + scope-bounded apply
  over the full-worktree baseline/diff (§4).
- `maestro/preflight.py` — extract/reuse the path-space overlap helper only if
  sound (§3); otherwise leave untouched.

## Testing

- **Overlap unit** (`reservations.py`): anchor extraction; prefix-subtree
  overlap; scopeless → `**`; leading-wildcard glob → whole workdir; the
  no-false-negative property (future-path overlap two globs share).
- **Arming**: SSH task arms its workdir; local-Docker task does **not**; pure
  local workdir stays inert (behavior-compat assertion).
- **Fail-fast**: SSH task with empty scope → start-time `ExecutionConfigError`.
- **Reservation contention**: two overlapping-scope tasks on an armed workdir
  serialize (second not dispatched until first releases); two disjoint-scope
  tasks run concurrently.
- **Acquire rollback**: lost CAS / spawn failure releases the reservation.
- **Scope collect**: out-of-scope modify/delete/new-file → `CollectConflict`;
  in-scope changes apply; baseline-divergence within scope → conflict.
- **Guard removal e2e**: localhost-SSH Mode-1 task runs end-to-end (arm →
  reserve → run → scope-collect → release).
- **Recovery reconstruction**: non-cleaned SSH handle rebuilds a held
  reservation across restart; `collected`/`cleaned` releases; NEEDS_REVIEW with
  a live handle stays held.

## Known limitations (ship with these documented)

- Validation still runs **locally** after a successful collect —
  `validation_backend` is the next slice, not this one.
- Collect is `scope_paths` only; patch-based collect is deferred.
- Local Docker Mode-1 is unchanged (in-place, no reservation).
- Overlap is conservative (may serialize disjoint-but-co-anchored scopes);
  precision is a later refinement, safety is the priority here.
