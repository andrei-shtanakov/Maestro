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

### Preflight (fail-loud, PR1)

A dedicated check rejects a config where `validation_backend` resolves to a
backend whose `transport.type != "local"`. It runs:

- standalone in `maestro validate` (Mode-1 config path), and
- as a fail-fast gate at scheduler start, alongside the existing
  `validate_ssh_scopes(...)` call (`scheduler.py:742`).

Message names the task id, the offending `validation_backend` value, and the
resolved backend/transport, and states that SSH validation is a PR2 follow-up.

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

For a durable (non-local) validation backend, the `RUNNING → VALIDATING`
transition is folded into `start_execution` (atomic CAS + handle insert). Because
that write is atomic it cannot go through `_transition`; it MUST be followed by
`_dispatch_committed_transition(task, frm=RUNNING)` (`scheduler.py:362`) so the
`VALIDATION_STARTED` event/notification actually fires. Skipping it reproduces the
status-committed-but-effect-not-dispatched (anti-desync) defect class. The local
validation path keeps the plain `_transition(RUNNING→VALIDATING)` (no handle).

### 3. Launch-failure taxonomy after persistence

`start_execution` transitions the entity and creates the `prepared` row **before**
`backend.run()` (`database.py:1332`). After that persistence, a failed launch is
classified (mirroring the primary dispatch, `scheduler.py:1254-1264`):

- `LaunchNotStarted` — the validator provably never launched → the handle is
  closed deterministically and the task takes a **validation-infrastructure-failure**
  outcome (not a validation *failure* of the task's code).
- **Unknown** launch result (e.g. handshake lost) — the `prepared` handle is
  **preserved**; the task stays **fail-closed** for recovery to probe → `NEEDS_REVIEW`.
- Never delete the row or bounce the task back to `RUNNING` on an uncertain launch.

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
  launch-failure taxonomy for the validation launch.
- `maestro/database.py` — `start_execution(..., execution_phase='task')` param;
  `execution_handles.execution_phase` column + `_migrate_*` (ADD COLUMN … DEFAULT
  'task'); phase-aware `get_open_execution_handles` consumers.
- `maestro/recovery.py` — phase-split `task_handles`; VALIDATING recovery probes
  the validation-phase handle.
- `maestro/preflight.py` (+ `scheduler.py:742` gate) — SSH-validation-target
  fail-loud check.

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
- **Launch taxonomy (point 3):** `LaunchNotStarted` → infra-failure outcome, handle
  closed; unknown → `prepared` handle preserved, task fail-closed.
- **Preflight fail-loud:** `validation_backend` resolving to an SSH backend fails
  `maestro validate` and the scheduler start gate with a task-named message.
- **Migration:** ADD COLUMN default `'task'` is a no-op on an existing DB; existing
  rows read back `execution_phase='task'`.
- **Committed dispatch (point 2):** the RUNNING→VALIDATING durable mint fires
  `VALIDATION_STARTED`.

## Non-goals (PR1)

- SSH validation execution (fresh remote layout, persistence, probe, recovery) — PR2.
- Real `CollectPolicy(mode="none")` no-op in `SshTaskHandle` — PR2 (only needed once
  validation runs on SSH; local backends already no-op collect).
- Changing the default to `same` — PR2, with a release note.
- Any orchestrator-mode / workstream validation change.
