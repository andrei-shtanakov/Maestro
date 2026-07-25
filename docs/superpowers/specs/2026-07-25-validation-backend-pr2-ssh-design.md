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
the gate is lifted and three gaps are closed:

1. **`SshTaskHandle.wait()` captures no output** — it returns `exit_code` +
   `output_log_path` but leaves `stdout_tail`/`stderr_tail` empty
   (`ssh_handle.py:200`). The validation adapter folds those tails into the retry
   context (`execution_result_to_validation`), so an SSH-validated task would lose
   all validation feedback. (See §1.)
2. `SshTaskHandle.collect()` unconditionally rsyncs + applies the remote worktree
   even for `CollectPolicy(mode="none")` (`ssh_handle.py:228`), so a validation run
   (which must apply *nothing*) would wrongly pull the remote tree back. (See §2.)
3. **Mode-1 SSH recovery does not exist.** `StateRecovery` only docker-probes
   (`probe_execution`); an SSH handle has no container, so the probe returns "no
   container found" and the task is **silently re-READYed** — the exact
   silent-restart hazard §12 forbids. This pre-existing Phase-2b gap becomes more
   dangerous once a *second* SSH lifecycle (validation) exists, so PR2 closes it
   for **both** the task and validation phases via a single backend-probe boundary.
   (See §3–§4.)

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
  handle; PR2 adds the backend-probe boundary and the SSH branch behind it (§4).
- **Mode-2 precedent.** The orchestrator already implements the exact dual-probe,
  fail-closed, row→ref recovery this design ports to Mode-1
  (`orchestrator.py:174` `_handle_ref_from_row`, `:651-676` dual probe,
  `:1258-1271` remote-coord persistence via `update_execution_handle_launch`).

## Design

### 1. SSH capture output → `ExecutionResult` tails

`SshTaskHandle.wait()` must populate `stdout_tail`/`stderr_tail` when the request
set `capture_output=True`, or SSH validation loses its retry feedback. Two
problems: the tails are never read, and the monitor can miss bytes written between
its last tail and the terminal marker.

- After the terminal marker is observed (in `wait()`, before returning), do a
  **final `_tail_log()`** so no trailing bytes are lost between the last monitor
  tail and completion.
- Read a **bounded** tail from the local mirrored log (reuse the existing
  `_TAIL_LIMIT`/`_decode_tail` convention from `local.py`) into `stdout_tail`.
- **Channel semantics (documented, honest minimum):** the remote supervisor merges
  the run's stdout and stderr into a single log, so PR2 sets
  `stdout_tail = combined remote-log tail` and `stderr_tail = ""`. This mirrors the
  local capture's log (which also merges). Splitting stderr would require a
  supervisor/descriptor protocol change + a version bump and is **out of scope**;
  the combined tail preserves validation retry context (which reads the merged
  `ValidationResult.output`).
- Only populate tails when `capture_output` is set (plain runs still stream to
  `log_path` only), matching the local backend.

### 2. `CollectPolicy(mode="none")` is a true no-op

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

### 3. Persist real remote coordinates for Mode-1 SSH handles

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

### 4. Backend-based classification — `ExecutionBackend.probe()` is the single boundary

The naïve `remote_host IS NULL → docker` discriminator is **wrong**: PR1 makes any
`backend.id != "local"` durable, including a **named-local bare** backend, whose
handle has `remote_host = NULL` but is a live **local PID**, not a container. A
docker probe of it returns "no container found" and it would be silently re-READYed
over a possibly-live process.

Recovery must classify by the **persisted `backend_id`** resolved through a
`BackendResolver`, and dispatch to that backend's own `probe()` — never by a
nullable coordinate and never by hand-assembling transport×isolation in
`recovery.py`. `ExecutionBackend.probe(ref) -> ProbeResult` becomes the single
recovery boundary; each backend encodes its own transport-correct, fail-closed
policy:

- resolved **LocalBackend bare** → PID probe (`os.kill(pid, 0)`); alive → review,
  dead → safe to reclaim.
- resolved **LocalBackend + Docker** → container probe (`probe_execution`); found /
  ambiguous → review, "no container" → safe to reclaim (bind-mount, nothing
  unsynced). *(`LocalBackend.probe` becomes isolation-aware — today it only handles
  the `local_pid:` ref, `local.py:254`.)*
- resolved **SshBackend bare** → `probe_ssh`; **always review** for an open handle
  (a remote worktree may hold uncollected changes even when the pgid is provably
  dead — `probe_ssh` returns `needs_review=True` in every branch,
  `ssh_recovery.py:22`).
- resolved **SshBackend + Docker** → **dual probe**: remote process group
  (`probe_ssh`) AND remote container (`probe_execution` over the SSH-tunneled
  `DockerCli`, with the persisted full `expected_labels`). Either signalling review,
  or any ambiguity on either entity → review. *(`SshBackend.probe` becomes
  isolation-aware — today it only calls `probe_ssh`, `ssh_backend.py:408`.)*
- **Unresolvable `backend_id`, or a persisted identity that conflicts with the
  current config, or a placeholder SSH row (crash before
  `update_execution_handle_launch`, no real remote coords) → NEEDS_REVIEW**
  (fail-closed; never fall through to the local-docker branch).

So `ProbeResult.needs_review` (transport-correct) drives the routing: `True` →
NEEDS_REVIEW; `False` → safe to reclaim/re-READY. Automatic re-READY is permitted
**only** on a durable proof (a backend `probe` that returns not-needs-review, a
`LaunchNotStarted` outcome, or an already-`cleaned` handle) — **never** on a raw
`kill -0` "process dead".

**Phase routing** (extends PR1's phase-split) — and the map must now include
`terminal`, which PR1 filtered out (`recovery.py:123`):

```
RUNNING    task → probe the execution_phase='task'       handle  (states prepared/running/terminal)
VALIDATING task → probe the execution_phase='validation' handle  (states prepared/running/terminal)
```

**Task-status drives the outcome, handle-state modulates safety.** A task left in
RUNNING/VALIDATING by a crash has an **unconfirmed execution outcome**. For any
non-`cleaned` handle on such a task, recovery routes to NEEDS_REVIEW (there is no
validation-only re-run in PR1/PR2 — a lost outcome cannot be safely reconstructed).
`collected` only means the **scope reservation was released** and the handle is
GC-eligible; it does **not** prove the task's outcome survived, so a
RUNNING/VALIDATING task with a `collected` handle is still NEEDS_REVIEW. (The
earlier "collected does not block the task" framing was too strong.)

VALIDATING → NEEDS_REVIEW uses the `VALIDATING → FAILED → NEEDS_REVIEW` edge,
matching PR1's `_route_validation_infra_review`.

### 5. State- and transport-aware GC

`_gc_terminal_handles` (`recovery.py:294`) currently hands **every** `terminal` row
to the docker GC (`gc_terminal_handle`), so an SSH `terminal` handle gets marked
`cleaned` after a "no container found" answer — destroying the record of an
**uncollected** remote run. Rewrite the sweep by transport/isolation, keyed off the
resolved backend:

- **SSH `terminal`** → **never GC** (collect unconfirmed; the remote tmp must be
  preserved for the operator). Left `terminal`.
- **SSH `collected`, bare** → guarded remote-root GC (`gc_ssh_terminal`,
  ownership-checked) → mark `cleaned`.
- **SSH `collected`, Docker** → remote **container** GC first → **only on a clean
  outcome** → remote-root GC → mark `cleaned` (container-first, mirroring Phase-2c
  ordering: never delete the remote root while a container may still reference it).
- **local Docker `terminal`/`collected`** → current docker GC (unchanged).
- Mark `cleaned` **only after all applicable cleanups succeed**; any ambiguity
  leaves the row for the next sweep or a human. GC never changes entity status.

### 6. Lift the preflight gate

`check_validation_backends` rejected any SSH validation target (PR1). PR2 removes
that rejection — SSH validation is now supported. The scheduler-start gate and the
`ValidationBackendError` are removed (or the function reduced to a no-op / unknown-
name check that the resolver already covers). `test_validation_backend_preflight`'s
"SSH fails" assertions are updated: an SSH validation target now **passes**
preflight and runs remotely.

### 7. Non-goals (deferred to PR3)

- **Default flip `local → same`.** PR2 keeps `default = local`: it must first prove
  SSH validation's happy path, crash recovery, and GC without simultaneously
  changing default behavior. PR3 flips the default with a release note (Docker/SSH
  tasks' validation environment changes).
- Patch-collect for validation (validation is `collect=none`, N/A).
- Mode-2 workstream validation (spec-runner owns it).

## Components touched

- `maestro/execution/ssh_handle.py` — populate `stdout_tail` (combined) on
  `capture_output` in `wait()` with a final `_tail_log()` (§1); `CollectSpec.mode`
  + `collect()` `none` short-circuit (§2).
- `maestro/execution/ssh_backend.py` — set `CollectSpec.mode` from
  `req.collect.mode`; make `probe()` isolation-aware (dual probe for persisted
  Docker isolation, §4).
- `maestro/execution/local.py` — make `LocalBackend.probe()` isolation-aware
  (bare → PID, Docker → `probe_execution`), so it is a self-sufficient recovery
  boundary (§4).
- `maestro/execution/` (new shared helper) — `handle_ref_from_row(row)` reused by
  both `StateRecovery` (Mode 1) and the orchestrator (Mode 2); replaces the private
  `orchestrator._handle_ref_from_row`.
- `maestro/scheduler.py` — persist remote coords after both task-phase and
  validation-phase SSH `backend.run` (`update_execution_handle_launch`); remove the
  SSH validation preflight gate wiring.
- `maestro/recovery.py` — `StateRecovery` gains a `BackendResolver`; classify open
  handles by persisted `backend_id` → resolve → `backend.probe(ref)` as the single
  boundary (§4); include `terminal` in the phase-aware selection; fail-closed for
  unresolvable/identity-conflict/placeholder rows; rewrite `_gc_terminal_handles`
  by transport/isolation (§5). `StateRecovery.__init__` takes the execution config
  (to build the resolver); the CLI (`cli.py:506`) passes it.
- `maestro/preflight.py` — remove/neutralize the SSH `ValidationBackendError`
  rejection.
- `maestro/database.py` — reuse `update_execution_handle_launch` (exists,
  `:1537`); no new columns (migration-9 `remote_*` columns already present).

## Testing

- **SSH capture unit:** an `SshTaskHandle` run with `capture_output=True` returns
  `stdout_tail` = the (bounded) combined remote-log tail and `stderr_tail == ""`;
  the final `_tail_log()` picks up bytes written after the last monitor tail; a
  non-capture run leaves the tails empty. End-to-end: an SSH validation *failure*
  surfaces the remote output into `ValidationResult.output` / the retry context.
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
- **Recovery — named-local BARE (the mis-classification bug):** a RUNNING task with
  an open handle on a `backend.id != "local"` **bare** backend (`remote_host=NULL`)
  is probed via its **PID** (not docker) — alive → NEEDS_REVIEW, dead → re-READY.
  Guards against the wrong `remote_host IS NULL → docker` discriminator.
- **Recovery — fail-closed rows:** an unresolvable `backend_id`, a persisted-vs-
  config identity conflict, and a placeholder SSH row (no persisted remote coords)
  each → NEEDS_REVIEW (never the local-docker branch).
- **Recovery — `collected` still NEEDS_REVIEW:** a RUNNING/VALIDATING task with a
  `collected` handle → NEEDS_REVIEW (outcome unconfirmed), and the scope reservation
  is released; a `cleaned` handle is absent from the open set.
- **GC by transport:** an SSH `terminal` handle is **never** marked `cleaned` (the
  bug: docker GC must not sweep it); an SSH `collected` bare handle → remote-root GC
  → `cleaned`; an SSH `collected` Docker handle → container GC → then remote-root GC
  → `cleaned`; a local-docker `terminal` handle → docker GC (unchanged).
- **Preflight lift:** an SSH `validation_backend` now passes the scheduler-start
  gate (update the PR1 fail-loud tests).
- **Opt-in localhost-ssh validation e2e:** a task run locally with
  `validation_backend: <localhost-ssh>` runs the validation command on the SSH
  backend, applies no collect, cleans up the remote tmp, and reports the captured
  result (gated like the existing SSH e2e).

## Architecture decisions (firm — not deferred to the plan)

These bound the recovery surface and are settled here so the plan does not have to
re-derive them:

1. **`ExecutionBackend.probe(ref) -> ProbeResult` is the single recovery boundary.**
   Recovery never hand-assembles transport×isolation combinations. Each backend's
   `probe()` owns its transport-correct, fail-closed policy.
2. **`StateRecovery` gets a `BackendResolver`** (built from the execution config the
   CLI already loads) and classifies every open handle by its **persisted
   `backend_id`**, then calls the resolved backend's `probe()`. Never by
   `remote_host`/nullable coordinates.
3. **`SshBackend.probe()` becomes isolation-aware:** a persisted Docker isolation
   (from `transport_ref`) triggers the **dual** probe (process group + remote
   container); bare stays `probe_ssh`-only. `LocalBackend.probe()` likewise becomes
   isolation-aware (bare PID vs container).
4. **The row→ref builder is a shared execution helper** (`handle_ref_from_row`),
   reused by Mode 1 (`StateRecovery`) and Mode 2 (orchestrator) — the private
   `orchestrator._handle_ref_from_row` is promoted, not copied.
5. **Fail-closed dominates:** an unresolvable backend, a persisted-vs-config
   identity conflict, or a placeholder row without real coordinates never falls
   through to a permissive branch — it is NEEDS_REVIEW.
