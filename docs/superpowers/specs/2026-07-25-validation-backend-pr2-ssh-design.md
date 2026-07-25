# validation_backend PR2 — SSH validation + Mode-1 SSH recovery

**Date:** 2026-07-25
**Follows:** `2026-07-25-validation-backend-slice-design.md` (PR1, merged as #102).
**Parent:** `2026-07-21-maestro-distributed-execution-design.md` §9 (validation
through the execution layer), §5 (collect lifecycle), §12 (remote recovery).
**Scope:** scheduler mode (Mode 1) — `validation_cmd` is a `Task` concept.

## Problem

PR1 shipped durable validation for **local transports** (bare + Docker) and
**fails loud at scheduler start** for any SSH validation target
(`check_validation_backends`). PR2 lifts that gate: validation may run on an SSH
backend as a second, fresh remote `ExecutionRequest`.

The durable validation machinery is transport-agnostic and already drives SSH once
the gate is lifted and two gaps are closed:

1. `SshTaskHandle.collect()` unconditionally rsyncs + applies the remote worktree
   even for `CollectPolicy(mode="none")` (`ssh_handle.py:228`), so a validation run
   (which must apply *nothing*) would wrongly pull the remote tree back.
2. **Mode-1 SSH recovery does not exist.** `StateRecovery` only docker-probes
   (`probe_execution`); an SSH handle has no container, so the probe returns "no
   container found" and the task is **silently re-READYed** — the exact
   silent-restart hazard §12 forbids. This pre-existing Phase-2b gap becomes more
   dangerous once a *second* SSH lifecycle (validation) exists, so PR2 closes it
   for **both** the task and validation phases.

## What is already free (no new code)

- **Fresh remote layout.** `SshBackend.run()` materializes a fresh remote worktree
  from the local (post-task-collect) worktree on every call
  (`ssh_backend.py:70-78`). The existing `_run_durable_validation` →
  `backend.run` → `finalize` path therefore ships the collected worktree, runs the
  validation command remotely, and finalizes — no validation-specific launch code.
- **Cleanup.** `finalize_handle` → `handle.cleanup()` already removes the remote
  tmp (rm -rf) and, for SSH+Docker, the container.
- **secret_env.** The resolver injects `effective_secret_env(name)` into
  `SshBackend` (`resolver.py:75`), which materializes it into the remote env-file in
  `run()` (`ssh_backend.py:397`). The validation `ExecutionRequest` carries **no**
  `secret_env` and needs none — the backend is the SSOT. (`inherit_env=True` is
  honored only by a bare LocalBackend; SSH ignores it.)
- **Recovery phase-split** (PR1) already routes VALIDATING to the validation-phase
  handle; PR2 only adds the SSH branch behind it.
- **Mode-2 precedent.** The orchestrator already implements the exact dual-probe,
  fail-closed, row→ref recovery this design ports to Mode-1
  (`orchestrator.py:174` `_handle_ref_from_row`, `:651-676` dual probe,
  `:1258-1271` remote-coord persistence via `update_execution_handle_launch`).

## Design

### 1. `CollectPolicy(mode="none")` is a true no-op

`CollectSpec` (`ssh_handle.py:37`) carries only `scope: list | None` and loses the
policy `mode`. Thread the **`CollectPolicy.mode`** through (domain-faithful — it
lets `collect()` honestly distinguish `none` / `whole_worktree` / `scope_paths`,
not a boolean `no_collect`):

- `CollectSpec` gains `mode: Literal["none","whole_worktree","scope_paths"]`.
- `SshTaskHandle.collect()` short-circuits at the top:
  `if self._collect.mode == "none": return CollectResult(applied=False)` — no
  rsync, no `plan_collect`, no `apply_collect`, remote tmp untouched (cleanup still
  runs afterward via `finalize_handle`).
- `SshBackend.run` sets `CollectSpec.mode` from `req.collect.mode`. The existing
  `_collect_scope(policy)` (scope only for `scope_paths`) is unchanged.

Local backends already no-op collect, so this is SSH-only.

### 2. Persist real remote coordinates for Mode-1 SSH handles

`start_execution` seeds only a placeholder `transport_ref`; the real coordinates
(`remote_host`, `remote_dir`, `status_marker`, JSON `transport_ref` with isolation)
are minted by `SshBackend.run()` and live on the returned `handle.ref`. Recovery
cannot probe without them. Mirror the orchestrator (`orchestrator.py:1258-1271`):
after `backend.run` returns, when `isinstance(backend, SshBackend)`, call

```python
await self._db.update_execution_handle_launch(
    execution_id,
    transport_ref=handle.ref.transport_ref,
    remote_host=info.get("host"),          # info = decode_transport_ref(handle.ref.transport_ref)
    remote_dir=info.get("remote_dir"),
    status_marker=handle.ref.status_marker,
)
```

This is added in **two** Mode-1 places:

- **Task dispatch** (`_dispatch_task`, after the task-phase `backend.run`) — closes
  the Phase-2b gap so a RUNNING SSH task is recoverable.
- **Validation** (`_run_durable_validation`, after the validation-phase
  `backend.run`) — so a VALIDATING SSH task is recoverable.

### 3. Phase-aware, fail-closed Mode-1 SSH/SSH-Docker recovery

Extend `StateRecovery` to probe SSH handles, mirroring the orchestrator's contract.
Classification per open handle row:

- **remote_host is NULL → local path (unchanged):** `probe_execution` (docker).
  "no container found" → safe re-READY (a local-docker workdir is bind-mounted, so
  a dead container leaves no unsynced state).
- **remote_host is set → SSH path (new):** build a ref from the row (a Mode-1
  `_handle_ref_from_row` equivalent) and probe. **The verdict is always
  NEEDS_REVIEW for an open (prepared/running/terminal) SSH handle** — `probe_ssh`
  already returns `needs_review=True` in every branch (`ssh_recovery.py:22`),
  because a remote worktree may hold **uncollected** changes even when the process
  group is provably dead. The probe is **diagnostic** (its reason feeds the
  NEEDS_REVIEW message); it is never a licence to re-READY.
  - **SSH + Docker (transport_ref.isolation == "docker") → dual probe:** the remote
    **process group** (`probe_ssh`) AND the remote **container**
    (`probe_execution` over the SSH-tunneled `DockerCli`, with the persisted full
    `expected_labels`). Either signalling review, or any ambiguity on either
    entity, → NEEDS_REVIEW. `SshBackend.probe()` today only calls `probe_ssh`
    (`ssh_backend.py:408`); either extend it to dual-probe by persisted isolation,
    or drive the dual probe from a shared phase-aware recovery helper.

Phase routing (extends PR1's phase-split):

```
RUNNING    task  → probe the execution_phase='task'       SSH handle
VALIDATING task  → probe the execution_phase='validation' SSH handle
```

State contract for an SSH handle:

- `prepared` / `running` / `terminal` → probe (diagnostic) → **NEEDS_REVIEW**
  (collect unconfirmed). The recovery must consider `terminal` too, unlike the
  docker re-READY path — a terminal marker means the remote ran but collect/cleanup
  may not have completed.
- `collected` → does **not** block the task (scope already released); remains a
  candidate for **guarded GC** (`gc_ssh_terminal`, ownership-checked) only because
  the DB row says `collected`, never on a probe guess.
- `cleaned` → safely absent from the open set.
- Automatic re-READY is permitted **only** on a durable proof that nothing runs and
  nothing is unsynced — a `LaunchNotStarted` outcome or an already-closed handle —
  **never** on a `kill -0` "process dead" result.
- Recovery **probes and classifies only**; it never deletes remote state
  (GC of `collected`/`terminal` handles stays in the existing ownership-checked
  sweep).

VALIDATING → NEEDS_REVIEW uses the `VALIDATING → FAILED → NEEDS_REVIEW` edge (no
direct edge), matching PR1's `_route_validation_infra_review` and the existing
`_route_docker_task_to_review`.

### 4. Lift the preflight gate

`check_validation_backends` rejected any SSH validation target (PR1). PR2 removes
that rejection — SSH validation is now supported. The scheduler-start gate and the
`ValidationBackendError` are removed (or the function reduced to a no-op / unknown-
name check that the resolver already covers). `test_validation_backend_preflight`'s
"SSH fails" assertions are updated: an SSH validation target now **passes**
preflight and runs remotely.

### 5. Non-goals (deferred to PR3)

- **Default flip `local → same`.** PR2 keeps `default = local`: it must first prove
  SSH validation's happy path, crash recovery, and GC without simultaneously
  changing default behavior. PR3 flips the default with a release note (Docker/SSH
  tasks' validation environment changes).
- Patch-collect for validation (validation is `collect=none`, N/A).
- Mode-2 workstream validation (spec-runner owns it).

## Components touched

- `maestro/execution/ssh_handle.py` — `CollectSpec.mode`; `collect()` `none`
  short-circuit.
- `maestro/execution/ssh_backend.py` — set `CollectSpec.mode` from
  `req.collect.mode`; dual-probe in `probe()` by persisted isolation (or via the
  shared recovery helper).
- `maestro/scheduler.py` — persist remote coords after both task-phase and
  validation-phase SSH `backend.run` (`update_execution_handle_launch`); remove the
  SSH validation preflight gate wiring.
- `maestro/recovery.py` — SSH classification + phase-aware dual probe in
  `StateRecovery`; a Mode-1 `_handle_ref_from_row` equivalent; fail-closed
  NEEDS_REVIEW for open SSH handles (both phases); local-docker path unchanged.
- `maestro/preflight.py` — remove/neutralize the SSH `ValidationBackendError`
  rejection.
- `maestro/database.py` — reuse `update_execution_handle_launch` (exists); no new
  columns (migration-9 `remote_*` columns already present).

## Testing

- **collect-none unit:** `SshTaskHandle.collect()` with `mode="none"` performs no
  rsync/plan/apply and returns `applied=False` (assert the ssh client is never
  called); `whole_worktree`/`scope_paths` still collect.
- **secret_env SSOT:** an SSH validation run materializes the backend-config
  allowlist into the remote env-file; the validation `ExecutionRequest` carries no
  `secret_env`. (Assert via the env-file write path with a fake runner.)
- **remote-coord persistence:** after a task-phase and a validation-phase SSH
  `backend.run`, the handle row has `remote_host`/`remote_dir`/`status_marker`
  populated (not the placeholder).
- **Recovery — bare SSH, both phases:** a RUNNING task with an open task-phase SSH
  handle → NEEDS_REVIEW (never re-READY), for each of: terminal-marker-present,
  pgid-alive, pgid-dead. A VALIDATING task with an open validation-phase SSH handle
  → NEEDS_REVIEW. Assert the probe ran (diagnostic reason present) and no re-READY.
- **Recovery — SSH+Docker dual probe:** an open handle whose process group is dead
  but whose container is present (or ambiguous) → NEEDS_REVIEW; probe deletes
  nothing.
- **Recovery — local-docker unchanged:** "no container found" still re-READYs
  (regression guard).
- **`collected`/`cleaned`:** a `collected` SSH handle does not block the task and is
  a GC candidate; a `cleaned` handle is absent from the open set.
- **Preflight lift:** an SSH `validation_backend` now passes the scheduler-start
  gate (update the PR1 fail-loud tests).
- **Opt-in localhost-ssh validation e2e:** a task run locally with
  `validation_backend: <localhost-ssh>` runs the validation command on the SSH
  backend, applies no collect, cleans up the remote tmp, and reports the captured
  result (gated like the existing SSH e2e).

## Open implementation choices (resolve in the plan)

- **Dual-probe location:** extend `SshBackend.probe()` to be isolation-aware, vs a
  standalone phase-aware recovery helper in `recovery.py` that composes `probe_ssh`
  + `probe_execution`. Prefer the shared helper if it keeps `recovery.py` from
  importing SSH-launch internals (mirror how `docker_recovery` avoids the ssh
  module).
- **Row→ref translation in Mode-1:** a private `_handle_ref_from_row` in
  `recovery.py` (copy of the orchestrator's), or a shared helper. Keep it minimal —
  only the fields `probe_ssh`/`probe_execution` read (`transport_ref`,
  `status_marker`, `backend_id`).
