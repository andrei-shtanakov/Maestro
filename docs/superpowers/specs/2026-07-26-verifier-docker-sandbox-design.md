# Strict Docker verifier sandbox — design

**Date:** 2026-07-26
**Status:** approved (brainstorm), pending Codex review → plan
**Extends:** Mode-1 adversarial verifier gate (PR #107 / `540b090`, fix #108 / `c05b250`)
**Slice:** give the Mode-1 verifier judge real OS isolation (filesystem + process), closing
the one consciously-left limitation of the verifier gate.

---

## 1. Goal & scope

The verifier gate MVP shipped **policy isolation**, not OS isolation: the `claude`
diff-judge runs on the `local` backend in an empty scratch cwd, with `collect=none`,
the project path never passed, and `inherit_env=False` — but nothing stops the judge
process from reading the filesystem. `VerifierConfig.backend` is `Literal["local"]`
today, with the config seam reserved for exactly this slice.

This slice adds `verifier.backend: docker`: the judge runs in a **hardened, mount-less,
digest-pinned Docker container**. The judge physically cannot see the repository.
`verifier.backend: local` (the default, and any config that omits the block) stays
**byte-identical** to today.

**Honest naming.** This is **filesystem/process isolation, NOT network isolation.**
The container keeps `--network bridge` (the judge must reach the model). `bridge` grants
**unrestricted container network** — not merely outbound internet, but potentially the
host gateway and the local network. We name it that way in docs and config comments;
network confinement is explicitly out of scope for this slice.

**Out of scope:** SSH+Docker verifier (the isolator is local-transport only this slice);
a second credential mechanism (mounted key files / `~/.claude`); configurable network;
Mode-2 domain-verification hardening (Stage B is untouched).

---

## 2. Security contract (the threat model, ratified)

The container:

- does **NOT** bind-mount the project worktree (no `-v workdir:/work`);
- receives the stdin envelope via `docker run -i`;
- has a `--read-only` root filesystem;
- gets a writable scratch **tmpfs** at `/scratch`, owned by the effective user, with
  `--workdir /scratch`;
- runs as a **mandatory non-root numeric user** (`uid:gid`), never the image `USER`,
  never a symbolic name; UID 0 forbidden, GID 0 forbidden;
- runs with `--cap-drop=ALL`, `--security-opt=no-new-privileges`, and
  memory / CPU / PID limits;
- receives exactly **one** secret — `ANTHROPIC_API_KEY` — via a 0600 env-file inside a
  0700 host temp-dir;
- is pinned to an **immutable image digest** (`image@sha256:…`); no auto-pull.

Durable lifecycle / recovery / probe stay the **existing** mechanism — no second one.

### 2.1 Credential contract (ratified)

- **Only `ANTHROPIC_API_KEY`.** The name is **not configurable** in this slice — a
  config-named var would silently become arbitrary-secret passthrough.
- **Mandatory when `verifier.backend: docker`:** it must be **present and non-empty, with
  no NUL / `\r` / `\n`** (those would corrupt the `KEY=value` env-file or smuggle extra
  lines). Absent / empty / control-char-bearing → preflight config error, the scheduler
  does not start (before the first task).
- **Stripped from the container:** host `HOME`, `USER`, all `CLAUDE_*`, all `GH_*`
  (already denylisted), all host env, all credential/config directories.
- **No credential file and no `~/.claude` mounted.**
- Env-file is 0600 inside a 0700 host temp-dir; the **path** may appear in argv, the
  **value** never does.
- Env-file cleanup is **guaranteed** on success, failure, timeout, cancellation, and
  spawn-failure.
- Preflight verifies the CLI **without an authenticated model call** (no spend, no
  credential leak). An authenticated end-to-end smoke is a **separate, opt-in,
  default-off** test.
- A future credential-provider mechanism is a separate slice; API-key incompatibility,
  if it ever surfaces, is a fact for that slice — never a reason to pre-allow mounts here.

### 2.2 Synthetic container environment (ratified refinement)

"No host passthrough" does **not** mean the CLI runs without a home. Maestro supplies
**safe, synthetic** values (not host values), all pointing inside the writable tmpfs:

```
HOME=/scratch
TMPDIR=/scratch
XDG_CONFIG_HOME=/scratch/.config
XDG_CACHE_HOME=/scratch/.cache
```

These are Maestro-authored constants, not host passthrough. `ANTHROPIC_API_KEY` remains
the only credential. Any `ENV` **baked into the digest-pinned image** is considered part
of the **trusted image artifact** (the digest is the trust anchor).

---

## 3. Architecture (approach 1 — dedicated hardened builder, shared lifecycle)

The judge already runs through the transport-agnostic execution layer
(`ClaudeDiffJudge` → `ExecutionRequest` → backend), so `ClaudeDiffJudge` needs **no
change**. Only the *argv/mount/stdin construction* genuinely differs from the general
`DockerIsolator`; the *durable machinery* is reused verbatim.

### 3.1 Reused verbatim (no fork)

- `DockerTaskHandle` — container lifecycle (stop/kill/rm, ownership-checked cleanup);
- `DockerCli` — the docker CLI wrapper;
- `docker_recovery` (`labels_match`, container probe);
- the durable execution handle + `finalize_handle` + cleanup path;
- `ClaudeDiffJudge` — already backend-agnostic (`capture_output=False` full-stdout read
  from `output_log_path`, CLI-envelope unwrap, transport/semantic split).

For recovery to genuinely stay shared, the `VerifierDockerIsolator` hands
`DockerTaskHandle` the **same canonical label set and cleanup paths** the general
`DockerIsolator` does.

### 3.2 Verifier-only launch policy (`VerifierDockerIsolator`, new)

A new isolator that builds a hardened, mount-less, stdin-attached argv but wraps the
result in the **same** `DockerTaskHandle`. Differences from `DockerIsolator`:

| aspect | general `DockerIsolator` | `VerifierDockerIsolator` |
|---|---|---|
| workspace | `-v workdir:/work`, `-w /work` | **no mount**, `--workdir /scratch` |
| scratch | (workdir mount) | `--tmpfs /scratch:rw,nosuid,nodev,noexec,size=<n>,mode=0700,uid=<uid>,gid=<gid>` |
| stdin | none | `-i` (envelope on stdin) |
| root fs | writable | `--read-only` |
| caps | default | `--cap-drop=ALL` |
| privileges | default | `--security-opt=no-new-privileges` |
| user | optional | **mandatory** numeric `--user uid:gid` |
| PID limit | none | `--pids-limit <n>` |
| env | req.env + trace | synthetic HOME/TMPDIR/XDG_* + trace |
| secret | secret_env list | exactly `ANTHROPIC_API_KEY` via `--env-file` |
| network | config | fixed `--network bridge` |
| image | tag or digest | **digest-only** (`image@sha256:…`) |

The hardening flags are **not config fields** — they cannot be turned off.

### 3.3 Identity separation

- External config says `backend: docker`, but the **internal backend ID is
  `verifier-docker`**, so the persisted handle can never collide with the general
  `docker` backend's identity/recovery. The persisted handle is self-sufficient.
- **`verifier-docker` is NOT registered in `ExecutionConfig.normalized()` or the general
  `BackendResolver`** — it is never task-selectable. A coding task can never accidentally
  run mount-less. It is reachable **only** via `VerifierConfig`, through the single
  factory below.

### 3.4 The single factory

```python
def build_verifier_backend(
    verifier_cfg: VerifierConfig,
    *,
    local_backend: ExecutionBackend,
    docker_cli: DockerCli | None = None,
) -> ExecutionBackend
```

Used by **both** dispatch (`scheduler._run_verifier`) and recovery
(`_recover_verifying_tasks`), guaranteeing they construct an identical backend (same
`backend_id`, same isolator, same `accepts_ref` identity):

- `verifier_cfg.backend == "local"` → returns the **passed** `local_backend` verbatim
  (production supplies `self._backends.resolve("local")`), so today's `local` seam is
  **behavior-identical** and any injected/fake local backend is honored — the factory
  never builds its own `LocalBackend`;
- `verifier_cfg.backend == "docker"` → `LocalBackend(VerifierDockerIsolator(...),
  backend_id="verifier-docker", docker=<docker_cli>)`, sharing the passed `DockerCli`,
  its `probe()` and `accepts_ref()`.

The factory **never silently creates either a hidden local backend or a hidden
`DockerCli`** — in tests dispatch and recovery must inject one shared fake, or
recovery/local fakes would be bypassed. `docker_cli` is **required on the docker path**
(`None` there is a programming error → raise `ValueError`); the `local` path ignores it.
Production composition constructs the real `DockerCli` **outside** and passes it in — so
there is no "None becomes real" branch to reason about.

### 3.5 Post-spawn credential handoff (who deletes the env-file)

The §7.3 invariant "delete the env-file as soon as the cidfile appears" needs a concrete
lifecycle owner, and today there is **none**: `materialize()` runs *before* spawn,
`LocalBackend.run()` spawns the process, and `DockerTaskHandle` is constructed only *after*
spawn — no component has a post-spawn hook. writing-plans must **not** invent a new
lifecycle architecture; the seam is fixed here as **one additive hook** on the `Isolator`
protocol:

```python
async def after_spawn(self, prepared: PreparedRun, proc: LocalProcess) -> None
```

- `BareIsolator` and the general `DockerIsolator`: **no-op** — behavior byte-unchanged
  (their env-file, if any, keeps its existing end-of-run `DockerTaskHandle.cleanup`).
- `VerifierDockerIsolator`: **bounded-wait** for the cidfile to appear, then **immediately
  `unlink` the env-file** (`docker run --env-file` has consumed the credential by the time
  the container is created).
- cidfile never appears within the bound (timeout / early process exit before creation) →
  **terminate/kill** the addressed container/process, run cleanup, and surface a **launch
  error** — fail-closed, no handle handed back while a secret is still on disk.
- a **crash during the wait** is closed by recovery via the deterministic §7.3 path.
- `LocalBackend.run()` does **not return the handle** until `after_spawn` has completed, so
  by the time any caller holds a verifier-docker handle the env-file is already gone (or the
  run has failed closed).

`DockerTaskHandle` therefore stays unchanged — the credential handoff lives entirely in the
isolator hook plus the `run()` ordering. (The verifier env-file remains in `cleanup_paths`
too, so the end-of-run/recovery `unlink(missing_ok=True)` is a harmless idempotent no-op
after the eager delete.)

---

## 4. Config surface

### 4.1 `VerifierConfig` (`maestro/models.py`, extended)

```python
class VerifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runner: Literal["claude"] = "claude"
    model: str | None = None
    timeout_seconds: int = Field(default=120, ge=1)
    max_diff_bytes: int = Field(default=100_000, ge=1)
    backend: Literal["local", "docker"] = "local"
    docker: VerifierDockerConfig | None = None
```

Cross-field validation (`model_validator`):
- `backend == "docker"` **requires** a `docker` block → else config error;
- `backend == "local"` with a `docker` block → config error (no dead config that
  silently does nothing).

### 4.2 `VerifierDockerConfig` (`maestro/verifier/docker_config.py`, new)

```python
class VerifierDockerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: str                       # MANDATORY, must be image@sha256:<64-hex>
    user: str                        # MANDATORY, numeric "uid:gid"; uid != 0, gid != 0
    memory: str = "512m"             # secure default, bounded
    cpus: str = "1"                  # secure default, bounded
    pids_limit: int = 128            # secure default, bounded
    tmpfs_size: str = "64m"          # secure default, bounded
    # network is NOT a field — fixed to "bridge" this slice.
    # hardening flags are NOT fields — baked, not disableable.
```

**Validation (tuning must never disable isolation — ratified refinement).** These bounds
are part of the **security contract**, not an implementation detail: no value may express
"no limit." A Docker size string is **normalized to bytes before the range check**; a
decimal accepts only a **finite** number (no exponent, `NaN`, or `Infinity`).

- `image`: must match `^[^@\s]+@sha256:[0-9a-f]{64}$` (digest-pinned; a bare tag is
  rejected).
- `user`: must match `^\d+:\d+$`; UID `!= 0` **and** GID `!= 0` (strict non-root).
- `memory`: a positive Docker size, normalized to bytes, in **`128m .. 8g`** inclusive;
  zero / empty / negative / unparseable rejected.
- `cpus`: a finite positive decimal in **`0.1 .. 8`** inclusive; `0` / negative /
  non-finite / exponent rejected.
- `pids_limit`: an int in **`16 .. 4096`** inclusive; `0` and `-1` (docker's "unlimited")
  rejected.
- `tmpfs_size`: a positive Docker size, normalized to bytes, in **`16m .. 1g`** inclusive
  (`16m` floor is the CLI minimum).

---

## 5. Production launch argv

Built by `VerifierDockerIsolator.prepare` (pure, no I/O), materialized (tmp-dir 0700 +
env-file 0600) exactly as `DockerIsolator` does:

```
docker run -i
  --name maestro-<execution_id>
  --cidfile <tmp>/cid
  --read-only
  --tmpfs /scratch:rw,nosuid,nodev,noexec,size=<tmpfs_size>,mode=0700,uid=<uid>,gid=<gid>
  --workdir /scratch
  --cap-drop=ALL
  --security-opt=no-new-privileges
  --user <uid>:<gid>
  --memory <memory> --cpus <cpus> --pids-limit <pids_limit>
  --network bridge
  --env-file <tmp>/env                # contains only ANTHROPIC_API_KEY
  -e HOME=/scratch -e TMPDIR=/scratch
  -e XDG_CONFIG_HOME=/scratch/.config -e XDG_CACHE_HOME=/scratch/.cache
  -e <trace-env...>
  --label maestro.execution_id=<id>
  --label maestro.entity_kind=task --label maestro.entity_id=<run_id>
  --label maestro.attempt=<n> --label maestro.backend_id=verifier-docker
  <image@sha256:...>
  claude -p <JUDGE_PROMPT> --output-format json --model <resolved_model>
```

Notes:
- **No `-v` mount.** The judge sees only the tmpfs; the diff arrives inside the stdin
  envelope (already frozen upstream by `build_envelope`, Task 6 of the gate).
- `capture_output=False` (set by `ClaudeDiffJudge._build_request`) is preserved: the
  attached `docker run` process's stdout is the container's stdout, written in full to
  `execution.output_log_path` — the full-stdout read + CLI-envelope unwrap keep working.
- `entity_kind`/`attempt`/labels/cleanup-paths mirror the general `DockerIsolator` so
  `docker_recovery.labels_match` and `DockerTaskHandle.cleanup` are reused unchanged.

---

## 6. Preflight (approach 2 — eager, global fail-loud halt)

Runs at scheduler start, extending `Scheduler._check_verifier_model`, **only when a
`verifier:` block is present with `backend == "docker"`**. `backend: local` or an absent
block → **no Docker preflight at all** (today's cheap in-process model check unchanged).

### 6.1 Halt matrix (every row = hard global fail-loud; scheduler does not start)

| condition | outcome |
|---|---|
| `ANTHROPIC_API_KEY` absent / empty / contains NUL·CR·LF | config/preflight error, no start |
| Docker unreachable | halt |
| image digest absent locally | halt (**no auto-pull**) |
| `claude` missing / `claude --version` non-zero | halt |
| hardened flags incompatible with image/runtime | halt |
| verifier block absent, or `backend: local` | Docker preflight not run |

Silent gate-disable is forbidden: the user explicitly requested the sandbox; continuing
without it changes run semantics and creates a false sense of protection.

### 6.2 Probe shape

- **Identical security profile, NOT byte-identical argv.** The probe reuses the same
  `VerifierDockerIsolator`/security-profile builder (`--read-only`, tmpfs+`noexec`,
  non-root user, cap-drop, no-new-privileges, limits), but:
  - probe: `--rm` + command `claude --version`;
  - production: durable `--name`/`--cidfile`/labels, `-i`, `--env-file`, judge command.
- The probe runs **without `ANTHROPIC_API_KEY` and without the env-file** — a version
  check must never receive a credential.
- Probe container gets a **unique name/label** and **guaranteed cleanup even on timeout**.
- A **short, dedicated preflight timeout** (separate from `verifier.timeout_seconds`).
- The **inspected image ID** (resolved from the digest) is recorded in the launch/start
  event for audit.

### 6.3 noexec proof boundary

`claude --version` proves only that the CLI **starts** under `--read-only` + `noexec`
tmpfs + non-root. It does **not** prove the authenticated code path never lazily unpacks
an executable into `/scratch`. Full `noexec` proof stays with the **opt-in authenticated
smoke** (§8). If that smoke ever fails under `noexec`, `noexec` is **not** silently
dropped — it forces an explicit threat-model decision.

---

## 7. Recovery contract

The general/phase-specific split, the two-operation ownership, and the credential crash
contract change; the shared `backend.probe()` boundary and fail-closed semantics are
preserved.

### 7.1 One-time split of `open_handles` (single owner for the whole lifecycle)

The general open-handle path is kept off verification handles today only because it
filters `backend_id == "local"` (`recovery.py:218-221`) — a historical heuristic.
**Replace it outright** with the persistent semantic discriminator
`execution_phase == "verification"`. (A union of the two would leave competing
classifications and could again wrongly exclude an ordinary `local` task handle.)

The flip must be applied **once, immediately after `open_handles` is read**, and cover
**every** general consumer — not only task/validation recovery but also the terminal-
handle GC. Today `recover()` passes the original `open_handles` to
`_gc_terminal_handles()`; without the split, a `verifier-docker` `terminal`/`collected`
row would be (1) reconciled by the phase-specific owner, then (2) handed to the general
GC, which (3) cannot resolve `verifier-docker` through the general `BackendResolver` —
breaking both the single-owner claim and the "exactly once" guard test.

```python
verification_handles = [h for h in open_handles if h["execution_phase"] == "verification"]
general_handles      = [h for h in open_handles if h["execution_phase"] != "verification"]
```

- task/validation recovery **and** `_gc_terminal_handles()` receive **only**
  `general_handles`;
- `_reconcile_verification_handles()` (§7.2) **owns** `verification_handles` for the whole
  lifecycle — including their `terminal`/`collected` GC — resolving the backend through the
  verifier factory, never the general resolver;
- `local` verification handles are **not** surfaced by `get_open_execution_handles()`
  (it filters `backend_id == "local"`), so a dedicated **`get_open_verification_handles()`**
  query (§7.2) returns **all** open `phase=verification` rows regardless of backend and
  task status — the split above removes the `verifier-docker` rows from every general
  consumer, and the new query feeds the phase-specific owner below.

### 7.2 Two separate operations: handle reconciliation vs. FSM routing

Handle reconciliation and task-status routing are **decoupled** — a verification handle
can be `terminal`/`collected` while its task has already settled to `DONE` or
`NEEDS_REVIEW` (the gate reconciles the handle only once the judge is provably dead,
which can lag the task transition). If reconciliation were scoped to
`get_tasks_by_status(VERIFYING)` (as the gate's `_recover_verifying_tasks` is today), then
after we exclude all verification handles from general GC these would orphan forever:

- a verification handle in `terminal`/`collected` whose task is already `DONE`;
- a handle whose task already moved to `NEEDS_REVIEW`;
- a `local` verification handle of such a settled task — not even surfaced by
  `get_open_execution_handles()` (the `backend_id != "local"` filter).

So verification recovery is **two** operations:

**(a) `_reconcile_verification_handles()` — owns ALL open `phase=verification` rows**,
regardless of task status or backend, driven by `get_open_verification_handles()` (§7.1).
State-specific reconciliation, using `build_verifier_backend(cfg, local_backend=…,
docker_cli=…)` (never the general resolver):

- `prepared`/`running` → `accepts_ref()` **then** `probe()`; a live result, an
  `accepts_ref` reject, or any resolve/probe error → **preserve the handle open**;
- `terminal`/`collected`, **`local`** backend → mark `cleaned` **directly, no Docker GC**
  (there is no container and no credential artifact to remove);
- `terminal`/`collected`, **`verifier-docker`** → **ownership-checked** container GC (the
  shared `DockerTaskHandle`/`docker_recovery.labels_match` path) **+ credential-artifact
  cleanup (§7.3)**, then mark `cleaned`;
- unknown backend id / config mismatch / missing `VerifierConfig` / probe or GC error →
  **preserve open** (fail-closed);
- **task status never affects ownership cleanup** — a handle is reconciled on its own
  monotonic state, not the task's.

**(b) `_recover_verifying_tasks()` — ONLY the FSM routing** `VERIFYING → NEEDS_REVIEW`
for tasks still in `VERIFYING`. Unchanged from the gate: a mid-`VERIFYING` crash is always
fail-closed to NEEDS_REVIEW (never auto-re-run). It no longer owns handle GC — (a) does —
so a `proven cleaned` reconciliation in (a) is what opens the guarded `maestro retry`
requeue fence per the existing contract.

Dispatch and recovery share one **injectable** `DockerCli`; the factory never creates a
hidden client that would bypass recovery fakes in tests.

### 7.3 Credential-artifact crash contract

Normal cleanup (`DockerTaskHandle.cleanup` → `cleanup_paths`) covers controlled success /
failure / timeout / cancellation. But a **center crash after `materialize`** leaves the
0600 env-file holding `ANTHROPIC_API_KEY` on disk, and recovery does not know
`cleanup_paths` — they live only in the in-memory `PreparedRun`, never in the persisted
handle. For a security slice this needs an explicit crash contract:

- **Deterministic path.** The verifier temp-dir is `<verifier_exec_root>/maestro-verify-
  <execution_id>` under a **stable, dedicated root** (derived from the Maestro db-dir, not
  ambient `TMPDIR`), with the env-file and cidfile inside it. Recovery recomputes this
  path from the **trusted `execution_id` alone** — never from any persisted, attacker- or
  drift-influenced path string.
- **Path-safety precondition for destructive cleanup.** Before any `unlink`/`rmtree`,
  recovery validates that `execution_id` is a **well-formed UUID** and that the recomputed
  **canonical (`realpath`)** temp-dir is **contained within** the dedicated
  `verifier_exec_root`. A malformed id, or a resolved path outside the root, **aborts the
  delete** (fail-closed) — destructive cleanup can never escape the verifier root.
- **Shrink the live window (primary).** During the live run the env-file is deleted **as
  soon as container creation is confirmed** (the cidfile appears — proof `docker run`
  already parsed `--env-file`). The credential is therefore on disk only during the narrow
  spawn window (materialize → cidfile), not for the judge's whole runtime.
- **Recovery closes the spawn window (fail-safe).** `_reconcile_verification_handles`
  recomputes the temp-dir and, after ownership-checked container reconciliation (or proof
  the spawn aborted — no container and no cidfile), deletes env-file + cidfile + tmp-dir.
  If a container is **live but not yet proven to have read** the env-file, the secret file
  is **preserved** (deleting it could race a still-starting judge); it is removed once the
  container is proven to exist (read complete) or the spawn is proven aborted. Because the
  path is derived from our own trusted `execution_id`, deletion never targets a foreign
  file.
- **Regression test (required):** simulate a crash after `materialize`/launch that leaves
  the env-file; assert recovery's container GC removes **both** the container **and** the
  credential artifacts (env-file/cidfile/tmp-dir).

---

## 8. Testing

- **Unit — argv builder:** exact-flag assertions for the **production** argv (§5) and the
  **probe** argv (§6.2), including the tmpfs `mode/uid/gid`, synthetic env, digest image,
  labels, and cleanup paths. Assert **no `-v` mount** and **no host HOME/USER/CLAUDE_*/GH_*.**
- **Unit — config validation:** digest form; numeric `uid:gid` with uid≠0/gid≠0;
  memory/cpus/pids_limit/tmpfs bounds reject "unlimited" and out-of-range; backend/docker
  cross-field rules.
- **Unit — factory:** `local` → returns the passed `local_backend`; `docker` → id
  `verifier-docker` with the injected `DockerCli`, and `docker` with `docker_cli=None`
  raises; verifier-docker absent from the general resolver registry.
- **Unit — `after_spawn` hook:** `BareIsolator`/general `DockerIsolator` → no-op (env-file,
  if any, untouched at spawn); `VerifierDockerIsolator` → unlinks the env-file once the
  cidfile appears; cidfile absent within the bound → terminate/kill + launch error (handle
  not returned); `LocalBackend.run()` awaits the hook before returning the handle.
- **Recovery guard test (required):** every open verification handle is processed
  **exactly once** by phase-specific recovery and **never** by the general open-handle
  loop **nor by `_gc_terminal_handles()`** (assert the split — a `verifier-docker`
  `terminal`/`collected` row reaches neither general consumer) — parametrized over
  `local`, `verifier-docker`, and an **unknown/future** backend id (which must route
  NEEDS_REVIEW with the handle preserved).
- **Settled-task reconciliation (required):** a `terminal`/`collected` verification handle
  whose **task is already `DONE`/`NEEDS_REVIEW`** is still reconciled to `cleaned` by
  `_reconcile_verification_handles` (owns all open `phase=verification` rows regardless of
  task status), for both `local` and `verifier-docker` — proving the handle never orphans
  after exclusion from general GC.
- **Credential-crash regression (required):** a crash after `materialize`/launch leaves
  the 0600 env-file at the deterministic path; recovery recomputes it from `execution_id`
  and removes **both** the container **and** the credential artifacts; plus the live-path
  assertion that the env-file is gone once the cidfile appears.
- **Preflight matrix:** each §6.1 halt row (missing key, docker down, image absent, CLI
  missing/non-zero, hardening incompatible) → scheduler start raises/halts; `local`/absent
  → preflight not invoked.
- **Integration (docker-gated, skip-if-no-docker):** read-only root actually blocks a
  write; non-root uid enforced (`id -u` ≠ 0); stdin envelope delivered through `-i`;
  `claude --version` runs under `noexec`; env-file + container cleaned on success and on
  timeout; probe container cleaned even on timeout.
- **Opt-in authenticated smoke (default-off, marker-gated):** a real judge run end-to-end
  proving the authenticated code path works under `noexec` (the full §6.3 proof).

All docker/integration tests are gated behind a skip-if-no-docker marker; the default
suite (no Docker) stays green. Per house rule: pytest runs **foreground only**, and any
test opening a DB closes its connection.

---

## 9. Non-goals / invariants preserved

- `verifier.backend: local` (default) and any config without a `verifier:` block are
  **byte-identical** to post-#108 behavior — no new phase, no Docker, no preflight.
- Stage B (Mode-2 domain verification, `maestro/domain/`) is **untouched** (frozen;
  additive-only rule holds).
- `ClaudeDiffJudge`, the verdict/provider-binding contract, and the `VERIFYING` FSM are
  unchanged — this slice only swaps the backend the judge runs on and adds preflight +
  recovery resolution for the new backend id.
- The single isolation-aware `backend.probe()` boundary is preserved; no direct
  `DockerCli` probing outside it.
