# Validation through the execution layer — PR1 slice (local transports, durable)

**Date:** 2026-07-25
**Parent design:** `2026-07-21-maestro-distributed-execution-design.md` §9 (validation
through the execution layer), §5 (collect lifecycle), §12 (remote recovery).
**Scope:** scheduler mode (Mode 1). `validation_cmd` exists only on `Task` /
`TaskConfig`; workstreams delegate validation to spec-runner, so this slice does
not touch orchestrator mode.

## Problem

Post-task validation runs today as a **local** subprocess in `Validator.validate`
(`maestro/validator.py:186`): `shlex.split(validation_cmd)` →
`asyncio.create_subprocess_exec` in `task.workdir` with the full `os.environ`.
It bypasses the execution layer entirely. If a task went containerized because it
needed that environment, local validation can miss the task's dependencies (§9).

§9's target: validation becomes a **second `ExecutionRequest`**
(`capture_output=True`, `collect=none`) on a chosen backend, selected by a new
`validation_backend: same | local | <name>` field.

## Why the naive framing is wrong (and what replaces it)

An earlier framing assumed `same` on a remote backend requires preserving the
task's first remote workdir and inserting validation **inside** `finalize_handle`
(between collect and cleanup). That is unnecessary. Validation is a *separate*
`ExecutionRequest`, and `SshBackend.run()` already materializes a **fresh** remote
layout from the local worktree on every execution (`ssh_backend.py:70-78`). So the
correct lifecycle is two fully independent executions, no `finalize_handle`
reorder:

```
task exec  → wait → collect(local) → cleanup task exec
validation → wait → collect(none)  → cleanup validation exec   (fresh, on selected backend)
```

`same` therefore means *the same backend*, validating the **collected** state, with
one contract shared by local / docker / (later) SSH.

## Staging decision

Durable execution is **not** SSH-specific. A crash of the center during the second
`docker run` orphans the validation container exactly as an SSH supervisor would —
reopening the orphan/recovery defect class that Docker-isolation Phase 1 closed.
Therefore the durable validation lifecycle begins in **PR1**, and PR2 adds only the
SSH-specific materialization and recovery.

### PR1 (this slice) — general durable validation lifecycle + local transports

- `validation_backend: local | same | <name>`, **default `local`** (preserves
  today's observable behavior).
- `same` / `<name>` are honored **only** when the resolved backend's
  `transport.type == local` (this includes local Docker isolation).
- SSH target (whether via `same` on an ssh-backed task or an explicit ssh
  `<name>`) → **fail-loud at preflight**. No silent `same → local` substitution —
  the operator chose an execution environment; the system must not quietly run a
  different one.
- Separate `execution_id` + `execution_handles` row for validation, minted
  atomically.
- Persisted `execution_phase: task | validation` discriminator so recovery never
  guesses which of a task's open handles is which.
- VALIDATING-state recovery for local Docker via the **existing** Docker probe.
- One `ExecutionResult → ValidationResult` adapter; real `capture_output`.
- Validation handle runs `wait → collect(none) → cleanup` through `finalize_handle`.

### PR2 (follow-up) — SSH extension

- Fix the real `CollectPolicy(mode="none")` no-op in `SshTaskHandle` (see below).
- Fresh remote layout for the validation execution.
- SSH validation persistence / probe / recovery + crash-window & orphan-cleanup
  tests.
- **Planned default change `local → same`** with a release note (for Docker tasks
  the validation environment genuinely changes).

## Contract

### Config field

`Task` and `TaskConfig` gain:

```python
validation_backend: str = Field(
    default="local",
    description=(
        "Backend for the post-task validation run: 'local' | 'same' "
        "(the task's backend) | a named backend. Non-local targets must be "
        "transport.type == local in this release; SSH targets fail preflight."
    ),
)
```

Resolution:

| value      | resolves to                                     |
|------------|-------------------------------------------------|
| `local`    | `LocalBackend` (bare), regardless of task backend |
| `same`     | the task's resolved backend (`task.backend` → `default_backend`) |
| `<name>`   | the named backend                               |

### Persistence (must survive resume)

The scheduler re-reads `Task` from SQLite after a resume, so the field cannot live
only on the Pydantic models — the `tasks` table currently persists `validation_cmd`
but has no `validation_backend` column (`database.py:59`). PR1 adds:

- `tasks.validation_backend TEXT NOT NULL DEFAULT 'local'`;
- a `_migrate_*` migration (`ALTER TABLE tasks ADD COLUMN … DEFAULT 'local'`);
- create / update / row→model wiring in `database.py` (alongside `validation_cmd`);
- a resume test proving a `same` / named value round-trips through SQLite.

### Preflight (fail-loud, PR1)

A dedicated check rejects a config where `validation_backend` resolves to a
backend whose `transport.type != "local"`. The **scheduler-start gate is the only
Mode-1 preflight** for this check: it runs at scheduler start alongside the
existing `validate_ssh_scopes(...)` call (`scheduler.py:742`).

`maestro validate` is **not** a Mode-1 gate for this: that command takes a Mode-2
`project.yaml` and calls `load_orchestrator_config()` (`cli.py:966`); it never
loads a Mode-1 tasks config. A standalone `maestro validate-tasks` would expand
scope and is out of scope for PR1.

The gate message names the task id, the offending `validation_backend` value, and
the resolved backend/transport, and states that SSH validation is a PR2 follow-up.

## Lifecycle details (normative)

These five points are load-bearing; the implementation MUST honor them.

### 1. Validation finalize updates the durable row between phases

The validation handle goes through `finalize_handle` with **both** callbacks, exactly
like the primary execution (`finalize.py:33`):

```
wait
→ mark validation handle 'terminal'      (on_terminal)
→ collect(none)
→ mark 'collected'                        (on_collected)
→ cleanup
→ mark 'cleaned'  iff cleanup succeeded
```

Without the callbacks the validation handle is stranded in `prepared`/`running`
and recovery/GC misbehave. The scheduler reuses its existing `_finalize_running`
shape (`scheduler.py:1372`) with validation-phase `mark_execution_state` closures.

### 2. Committed-transition dispatch after the atomic RUNNING→VALIDATING mint

**Prerequisite — wire the event.** `TASK_EFFECTS[VALIDATING]` is currently empty
(`StatusEffect()`, `transitions.py:38`): `EventType.VALIDATION_STARTED` exists in
the enum but is attached to no transition, so today the VALIDATING transition fires
nothing. PR1 wires `VALIDATION_STARTED` into `TASK_EFFECTS[VALIDATING]` — a new,
intentional observable behavior. It applies to **both** the plain and durable paths,
so they stay symmetric in their potential effects (that symmetry is the point — a
dead enum and path-divergent effects are the failure mode being closed).

For a durable (non-local) validation backend, the `RUNNING → VALIDATING`
transition is folded into `start_execution` (atomic CAS + handle insert). Because
that write is atomic it cannot go through `_transition`; it MUST be followed by
`_dispatch_committed_transition(task, frm=RUNNING)` (`scheduler.py:362`) so the
now-wired `VALIDATION_STARTED` effect actually fires. Skipping it reproduces the
status-committed-but-effect-not-dispatched (anti-desync) defect class. The local
validation path keeps the plain `_transition(RUNNING→VALIDATING)`, which fires the
same effect (no handle).

### 3. Launch-failure taxonomy after persistence

`start_execution` transitions the entity (`RUNNING → VALIDATING`) and creates the
`prepared` row **before** `backend.run()` (`database.py:1332`). After that
persistence, a failed validation launch is classified (mirroring the primary
dispatch, `scheduler.py:1254-1264`) with exact machine states:

- **`LaunchNotStarted`** — the validator provably never launched. Nothing runs
  remotely, so:
  - handle: `prepared → cleaned` (deterministic close, nothing to reap);
  - reservation: **released** (proven-not-started — see detail 6);
  - task: `VALIDATING → FAILED → NEEDS_REVIEW`, message = validation-infrastructure
    failure. **This does not consume a task retry.**
- **Unknown launch result** (e.g. handshake lost) — a container may be live against
  the bind-mounted workdir. The scheduler does **not** wait for a crash+recovery; it
  routes immediately:
  - handle: **preserved** in `prepared` (fail-closed — recovery/cleanup must probe
    the possibly-live container);
  - reservation: **HELD** (detail 6);
  - task: `VALIDATING → FAILED → NEEDS_REVIEW`.
- Never delete the row or bounce the task back to `RUNNING`/`READY` on either.

**Retry-accounting rationale / limitation.** The task retry path re-runs the whole
task (authoring agent **and** validation), not validation alone. A proven-not-started
validation is not an authoring-agent attempt; counting it as one would force a
re-run of already-successful coding work. Because the state machine has no
validation-only re-run, PR1 does **not** silently retry: both infra outcomes are
**fail-closed to `NEEDS_REVIEW`** for a human decision. Documenting the absence of a
validation-only retry as an explicit PR1 limitation is deliberate.

### 4. `execution_phase` participates in every query and recovery selection

`execution_phase` is not merely a dict-construction tiebreaker. Invariant:

```
RUNNING    handle → execution_phase = 'task'
VALIDATING handle → execution_phase = 'validation'
```

Recovery selects the open handle **by phase**: `_recover_running_tasks` uses the
`task` handle, `_recover_validating_tasks` uses the `validation` handle
(`recovery.py:161,190`). The `task_handles` map (`recovery.py:127`) is split by
phase so a stale, still-open task-phase handle cannot shadow a live validation
handle for the same `entity_id`.

### 5. `non-local` criterion stays literal: `backend.id != "local"`

The existing durable criterion is `backend.id != "local"` (`scheduler.py:1155,1220`),
so a **named local bare** backend also gets a durable handle. That is safe and is
adopted **literally** for validation. PR1 does **not** introduce a new
`requires_durable_handle` capability abstraction.

### 6. The task reservation covers the validation execution too

The `(workdir, scope)` reservation is released today purely on the **primary
task's** finalization (`scheduler.py:1438` — release iff `exec_id is None or
fin.collect_succeeded`). But a durable validation runs a **second** execution
against the **same** bind-mounted workdir, so releasing on primary finalize alone
would free a `(workdir, scope)` that a live (or uncertain) validation container is
still using. The reservation lifetime MUST extend to cover validation:

- **Release** only once the workdir is durably free: validation `LaunchNotStarted`
  (proven not started) **or** the validation handle reached `collected`/`cleaned`.
- **HOLD** while the validation handle is `prepared`/`running`/`terminal`
  (uncertain or in-flight) — mirrors the primary path's HOLD on a collect conflict.
- **Recovery** re-holds the reservation for any open validation handle it finds
  (same reconstruction the primary path uses for a `terminal`/open task handle).
- The **only** subsequent release paths are validation cleanup succeeding or an
  explicit operator waive — never a guess made at scheduling time.

Local validation (no handle, `backend.id == "local"`) is unaffected: it holds no
separate reservation and the primary-task release rule stands.

## Components touched

- `maestro/models.py` — `validation_backend` on `Task` + `TaskConfig` (+ passthrough
  in `Task.from_config`).
- `maestro/validator.py` — new backend-routed path building the validation
  `ExecutionRequest` (`argv = shlex.split(cmd)`, `workdir = task.workdir`,
  `capture_output=True`, `collect=CollectPolicy(mode="none")`,
  `timeout_seconds = Validator.DEFAULT_TIMEOUT` (300s) — behavior-preserving:
  `_run_validation` passes no timeout override today, so the request must not
  substitute `task.timeout_minutes`) and the
  `ExecutionResult → ValidationResult` adapter. The current local subprocess
  becomes the `LocalBackend` path (behavior-preserving).
- `maestro/scheduler.py` — `_run_validation` resolves the validation backend and
  drives run→finalize; durable RUNNING→VALIDATING mint + committed dispatch;
  launch-failure taxonomy (detail 3); reservation held across validation (detail 6);
  the scheduler-start SSH-validation fail-loud gate (`scheduler.py:742`, the only
  Mode-1 gate).
- `maestro/transitions.py` — wire `EventType.VALIDATION_STARTED` into
  `TASK_EFFECTS[VALIDATING]` (detail 2).
- `maestro/database.py` — `start_execution(..., execution_phase='task')` param;
  `execution_handles.execution_phase` column + `_migrate_*` (ADD COLUMN … DEFAULT
  'task'); phase-aware `get_open_execution_handles` consumers; **plus**
  `tasks.validation_backend` column + `_migrate_*` (DEFAULT 'local') + create/update/
  row→model wiring (persistence).
- `maestro/recovery.py` — phase-split `task_handles`; VALIDATING recovery probes
  the validation-phase handle; re-hold the reservation for an open validation handle.
- `maestro/preflight.py` — the reusable resolve-and-check helper the scheduler-start
  gate calls (`maestro validate` is Mode-2 only and is **not** wired to this check).

## Argv / shell parity

Validation commands are parsed with `shlex.split` today (exec-style, no shell
operators), and `ExecutionRequest.argv` is a list — so parity is preserved by
`shlex.split(validation_cmd)`. No shell (`sh -c`) is introduced; a command relying
on `&&`/pipes is as unsupported after this slice as before it.

## Testing

- **Adapter parity:** local `validation_backend=local` (and default) produces the
  same `ValidationResult` (success/exit_code/stdout/stderr/timeout/`output`) as the
  pre-slice `Validator` for pass, fail, timeout, missing-cmd, bad-workdir cases.
- **Retry context:** a failing validation on a docker backend surfaces
  `stdout_tail`/`stderr_tail` into `format_for_retry` / the scheduler retry error.
- **Durable mint:** non-local validation creates an `execution_handles` row with
  `execution_phase='validation'`; local validation creates none.
- **Finalize row progression:** terminal → collected → cleaned marks fire in order;
  a cleanup failure leaves `collected` (not `cleaned`).
- **Recovery selection (point 4):** a task with a stale open **task**-phase handle
  AND an active **validation**-phase handle → recovery selects the *validation*
  handle for VALIDATING and probes it; routes fail-closed to `NEEDS_REVIEW`.
- **Launch taxonomy (detail 3):** `LaunchNotStarted` → handle `prepared→cleaned`,
  reservation released, task `VALIDATING→FAILED→NEEDS_REVIEW`, **no retry consumed**;
  unknown launch → `prepared` handle preserved, reservation HELD, task
  `VALIDATING→FAILED→NEEDS_REVIEW` immediately (no wait for recovery).
- **Reservation hold (detail 6):** an uncertain/in-flight validation handle HOLDS
  the `(workdir, scope)` reservation; it is released only on proven-not-started or
  `collected`/`cleaned`; recovery re-holds it for an open validation handle.
- **Preflight fail-loud (Mode-1 gate):** `validation_backend` resolving to an SSH
  backend fails the **scheduler-start** gate with a task-named message. (No
  `maestro validate` assertion — that command is Mode-2 only.)
- **Persistence / resume (point 2 of review):** a `same` / named `validation_backend`
  round-trips through SQLite — a task re-read after resume keeps the value, not
  `'local'`.
- **Migrations:** `execution_handles.execution_phase` DEFAULT `'task'` and
  `tasks.validation_backend` DEFAULT `'local'` are no-ops on an existing DB; existing
  rows read back the defaults.
- **Committed dispatch + event wiring (detail 2):** `TASK_EFFECTS[VALIDATING]` now
  carries `VALIDATION_STARTED`; both the durable mint (committed dispatch) and the
  local plain `_transition` fire it.

## Non-goals (PR1)

- SSH validation execution (fresh remote layout, persistence, probe, recovery) — PR2.
- Real `CollectPolicy(mode="none")` no-op in `SshTaskHandle` — PR2 (only needed once
  validation runs on SSH; local backends already no-op collect).
- Changing the default to `same` — PR2, with a release note.
- Any orchestrator-mode / workstream validation change.
