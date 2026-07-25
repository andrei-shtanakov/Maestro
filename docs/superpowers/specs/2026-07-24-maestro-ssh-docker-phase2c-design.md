# SSH + Docker isolation — Distributed Execution Phase 2c

## Context

Phases 2a/2b landed the SSH transport (Mode 2 remote workstreams, then Mode 1
remote). Docker isolation (Phase 1) landed for the **local** transport only.
Phase 2c composes the two orthogonal axes that the master design
(`2026-07-21-maestro-distributed-execution-design.md` §2, §2c) always intended:
run a harness **inside a Docker container on a remote host reached over SSH**.

Current state (grounded in the live tree):

- `SshBackend` (`maestro/execution/ssh_backend.py`) is **bare-isolation only** —
  its remote supervisor runs `descriptor["argv"]` directly in `layout.repo`.
- `resolver._build_ssh` (`maestro/execution/resolver.py:75`) **ignores
  `spec.isolation`** and always builds a bare `SshBackend`.
- `DockerIsolator` (`maestro/execution/isolators.py:86`) is **local-only**: it
  builds `docker run -v {req.workdir}:/work …` against **local** paths and a
  local `DockerCli`, and composes with `LocalBackend`.
- The remote supervisor (`maestro/execution/resources/maestro_supervisor.py`) is
  a versioned, stdlib-only source that runs whatever argv the descriptor carries
  and writes an atomic `.status` marker. Its child's exit code is the workload's
  exit code.
- Recovery already branches per backend: `backend_id == "docker"` →
  `docker_recovery.probe_execution`; `isinstance(backend, SshBackend)` →
  `ssh_recovery.probe_ssh` (`maestro/orchestrator.py:667-689`,
  `maestro/recovery.py`).
- The orchestrator branches on `isinstance(backend, SshBackend)` in six places
  (persist transport_ref, collect-before-gates, mirror, recovery, GC).

## Goal

Enable `transport: ssh` + `isolation: {type: docker}` end to end, by
**composition, not a new backend class**, so that:

- The SSH+Docker backend **remains an `SshBackend`** — every existing ssh-branch
  in the orchestrator (persist / collect-before-gates / WAL mirror / GC) is
  inherited unchanged.
- The remote **supervisor source stays byte-identical** — no remote-protocol
  version bump. All docker-wrapping happens in pure builders on the center; the
  supervisor still just runs `descriptor["argv"]`.
- Container lifecycle (stop/kill/cleanup/probe) reuses the existing `DockerCli`
  and `docker_recovery` logic by running them **over SSH** instead of locally.

Approach **A** from brainstorming (rewrite argv on the center; supervisor
untouched), confirmed. A separate composable `RemoteIsolator` protocol is
deliberately **not** introduced (premature for a single 2-way composition).

## Design

### 1. The central composition trick: `DockerCli` over an SSH `run_cmd`

`DockerCli` already shells every op (`version_ok`, `image_exists`, `inspect`,
`ps_ids_by_label`, `stop`, `kill`, `rm`) through an injected
`RunCmd = Callable[[list[str], float|None], Awaitable[tuple[int,str,str]]]`
(`maestro/execution/docker_cli.py:14`). Phase 2c injects a `RunCmd` that tunnels
each `docker …` argv through the guarded `SshCli`:

```python
def ssh_docker_run_cmd(ssh: SshCli) -> RunCmd:
    async def run_cmd(argv, timeout):
        res = await ssh.run(argv)          # ssh host 'docker …' (shlex-joined)
        return res.returncode, res.stdout, res.stderr
    return run_cmd
```

`DockerCli(run_cmd=ssh_docker_run_cmd(ssh))` then drives the **remote** daemon
with all of `DockerCli`'s existing structured-inspect / absent-vs-error logic
reused verbatim. This is what makes container stop/kill/cleanup/probe on the
remote host cost almost nothing new. (The `op_timeout` for remote ops should be
widened from the 30 s local default to absorb SSH round-trips — made
configurable, defaulting higher for the ssh runner.)

**Shared hardening (blocking prerequisite).** The reuse claim above is only true
after `DockerCli` is hardened — today `stop`/`kill`/`rm` discard the `_run(...)`
tuple and treat **any** return code as success (`docker_cli.py:108-119`), and
the sketched `ssh_docker_run_cmd` above ignores its `timeout` argument
(`SshCli.run` takes no timeout). Phase 2c must, as a **shared** change (i.e. it
also affects the **local** docker path):

- `stop`/`kill`/`rm` check the return code and **raise** on non-zero (so an
  ownership-verified cleanup that silently fails can never be mistaken for a
  successful removal — the recovery/GC logic in §6/§7 depends on this).
- The remote `run_cmd` **actually enforces** `timeout` — since `SshCli.run` has
  no timeout parameter, the run_cmd wraps it (`asyncio.wait_for` / `asyncio.
  timeout`) and maps expiry to a non-zero/`TimeoutError` result, mirroring the
  local `_default_run_cmd` kill-on-timeout contract.
- Tests cover the non-zero-rc and timeout branches for both the local and the
  over-SSH runner, **including regression coverage** for the local docker path
  that this hardening touches.

### 2. Pure remote-docker argv builder (new `ssh_docker.py`)

By analogy to `ssh_launch.py`'s pure builders, a new module builds the remote
`docker run` argv with **remote** paths — no I/O, trivially unit-testable:

```python
def build_docker_run_argv(
    *, execution_id, image, remote_repo, remote_env_file,
    effective_user, network, memory, cpus,
    inline_env: Mapping[str,str],      # trace_env + req.env (non-secret)
    has_secret_env_file: bool,
    inner_argv: list[str],
) -> tuple[list[str], dict[str,str]]:   # (argv, expected_labels)
```

Produces:

```
docker run
  --name maestro-<execution_id>
  --cidfile <remote_root>/cid
  -v <remote_repo>:/work  -w /work
  --network <network>
  --user <effective_user>              # see §3
  [--memory <m>] [--cpus <c>]
  [--env-file <remote_env_file>]       # secrets, iff any (§4)
  -e KEY=VALUE …                       # trace_env + req.env, inlined
  --label maestro.execution_id=<id>
  --label maestro.entity_kind=<k> --label maestro.entity_id=<run_id>
  --label maestro.attempt=<n> --label maestro.backend_id=<name>
  <image>
  <inner_argv…>
```

**No `--rm`** — a killed/crashed container must remain inspectable as recovery
evidence (§6). The label set mirrors the local `DockerIsolator` exactly so the
`docker_recovery` label checks apply unchanged.

`SshBackend.run()` (docker path) calls this builder and sets
`descriptor["argv"] = docker_run_argv` **before** `build_descriptor`. The
supervisor runs it; `docker run` (attached, no `-d`) blocks until the container
exits and propagates the container's exit code as its own — so the existing
`.status` marker mechanism records the correct workload exit code with zero
supervisor changes.

### 3. Effective-user (UID) resolution — exact semantics

| Case | `isolation.user` | Effective `--user` |
|------|------------------|--------------------|
| ssh + docker | unset | resolve **remote** SSH user's `uid:gid` (`ssh host id -u` / `id -g`) |
| ssh + docker | set (e.g. `1000:1000`) | honored verbatim (takes priority) |
| ssh + docker | `"0:0"` | root **only** as this deliberate explicit opt-in |
| local + docker | unset / set | **unchanged** from Phase 1 (no new default) |

Rationale: the container writes into a bind-mount owned by the remote SSH user.
A root container writes root-owned files there that the SSH user then cannot
read (collect rsync) or delete (`rm -rf` cleanup). Defaulting `--user` to the
remote uid:gid keeps every produced file owned by the account that later
collects and cleans up. Remote uid:gid is probed once during `run()` (and during
`can_run`, §5) and cached on the backend for the transport's lifetime.

An image that cannot run under the remote UID must **fail fast** at `can_run`
(§5), not mid-task.

### 4. Secret / env delivery

`SshBackend` already writes the secret allowlist to a remote `0600` env-file at
`layout.env_file` (`ssh_backend.py:230-240`). For the docker path that file is
passed to the container via `--env-file <layout.env_file>`; non-secret
`trace_env` (`TRACEPARENT`) + `req.env` are inlined as `-e KEY=VALUE` (matching
the local `DockerIsolator`). To avoid the secrets also landing in the
docker-*client* process env on the remote host, the docker-path descriptor
points `env_file` at a **non-existent path under `layout.root`** (e.g.
`<root>/noenv`): the supervisor's `_validate` still accepts it (it is under
root), and `_load_env` finds `Path(env_file).exists()` false and adds nothing —
so the container receives secrets solely through the `docker run --env-file`
flag, which references the real `layout.env_file`. GH credential denylisting is
already enforced upstream in `exec_config`.

Precise promise: **Maestro does not add allowlisted secrets to the `docker run`
process env; it delivers them exclusively via `--env-file`.** Maestro cannot
guarantee the absence of a same-named value already present in the remote SSH
user's *ambient* environment — that is the operator's responsibility, not a
guarantee this seam can make.

### 5. `can_run` for SSH+Docker (replaces bare-PATH probe)

Today `SshBackend.can_run` probes `req.required_tools` on the **bare remote
PATH** (`ssh_backend.py:105-108`) — wrong for docker, where the harness lives
**in the image**, not on the host. The docker path instead checks, via the
over-SSH `DockerCli` (§1):

1. **Daemon reachable** — `docker version` (over ssh) succeeds.
2. **Image present** — `docker image inspect <image>` succeeds (no implicit
   pull in MVP; a missing image is a fail-fast, not a silent `docker run` pull).
3. **Tools in the image under the effective user** — for each required tool,
   `docker run --rm --user <effective_user> <image> command -v <tool>`.
4. **Bind-mount scratch preflight** — under the effective user, write → read →
   delete a probe file in a `/work`-mounted scratch dir, to catch a UID/image
   incompatibility (read-only rootfs, wrong ownership) **before** the real task.

Any failure returns `CapabilityResult(ok=False, …)` with a specific reason. The
bare-PATH probe is retained for the bare-isolation ssh path.

### 6. Container lifecycle in the handle (stop/kill/cleanup)

`SshTaskHandle` becomes docker-aware (a small injected `container_ops` object,
None for the bare path — keeps the bare handle untouched). All container ops go
through the over-SSH `DockerCli` and are **ownership-verified before acting**:

- **Ownership check** = the exact cleanup semantics you specified: find the
  container by exact `maestro.execution_id` label, require **exactly one**
  result, verify the **full** expected label set on it, then act on that exact
  name/id. (This tightens `docker_recovery`'s current single-label check to the
  full label set — applied here, and the tightening is shared with the local
  docker path.)
- **terminate/kill** — a process-group signal does **not** stop the container (a
  SIGKILL of the `docker run` client detaches it). So the docker handle
  additionally issues an ownership-verified remote `docker stop -t <grace>` /
  `docker kill` on the exact container. The pgid signal is still sent (reaps the
  supervisor/client), but the container stop is the authoritative one.
- **cleanup** — ownership-verified remote `docker rm -f <exact id>` **first**
  (so no container is orphaned even though we skip `--rm`), then the existing
  ownership-checked `rm -rf` of the remote root and local staging/journal
  removal. A non-unique / label-mismatch result skips the `rm` and is surfaced,
  never force-removed.

Collect is unchanged: the container's writes land in the bind-mounted
`layout.repo`, which the existing `SshTaskHandle.collect()` rsyncs back exactly
as for bare ssh. (Ownership from §3 is what makes that rsync + `rm -rf`
succeed.)

### 7. Recovery — probe BOTH entities, fail closed

An SSH+Docker crash can strand **two** resources: the remote supervisor/process
group **and** the container. Recovery for such a handle must check both:

**`transport_ref` as the isolation SSOT (bump `v` 1 → 2).** The persisted
`transport_ref` — not the current config — is the source of truth for a run's
isolation type and expected identity; the resolved backend config is used
**only** to connect (host, user, `ssh_opts`, connection policy) and to
cross-check. Recovery is therefore *not* fully config-independent (SSH still
needs live connection settings), only **isolation-identity**-independent.
Concretely:

- bump `transport_ref.v` from 1 to 2;
- store `isolation: "bare" | "docker"`;
- for docker, store the **full `expected_labels`** (for the ownership probe);
- do **not** store `image`/`user` — the ownership probe does not need them;
- treat a legacy `v:1` ref as `bare`;
- a mismatch between persisted isolation and the resolved backend →
  `NEEDS_REVIEW`, with **no** attempt to guess.

Recovery flow: resolve the backend; if the persisted ref says `docker` and the
resolved `SshBackend` agrees (cross-check):
  - Run `probe_ssh` (existing) **and** `docker_recovery.probe_execution` driven
    by the over-SSH `DockerCli`.
  - `NEEDS_REVIEW` if **either** says review is needed — a live/leftover
    container, a daemon unreachable/error, a label mismatch, an ambiguous match,
    an alive process group, or a terminal-marker-without-confirmed-collect.
  - **probe deletes nothing.**
- GC (`terminal`/`collected` → `cleaned`) proceeds only when the ssh side is
  `collected` **and** the container is confirmed gone. **Ordering is
  load-bearing — the container is GC'd first:**

  ```
  docker gc_terminal_handle  →  only on a clean outcome:
    gc_ssh_terminal (remote-root rm -rf)  →  mark handle cleaned
  ```

  If `gc_ssh_terminal` ran first it could delete the remote worktree **and the
  secret evidence** and then fail to remove the container — leaving an orphaned
  container that can no longer be correlated to its run. So a non-clean docker GC
  outcome short-circuits: the remote root is **not** removed, and the row is left
  `collected` for the next sweep / a human.

### 8. Config & resolver wiring

Config shape is already expressible — `BackendSpec` accepts
`transport: ssh` + `isolation: {type: docker, image, network, memory, cpus,
user}` (`exec_config.py:80-97`). The only change is `resolver._build_ssh`:

```yaml
execution:
  default_backend: local
  secret_env_defaults: [ANTHROPIC_API_KEY]
  backends:
    remote-sandbox:
      transport: { type: ssh, host: gpu-box, workdir_root: /var/tmp/maestro }
      isolation:
        type: docker
        image: ghcr.io/andrei-shtanakov/maestro-runner:2026-07-21
        network: none
        memory: 8g
        # user omitted -> effective = remote uid:gid (§3)
      secret_env: [ANTHROPIC_API_KEY]
```

`_build_ssh` inspects `spec.isolation`: `BareIsolation` → today's bare
`SshBackend`; `DockerIsolation` → an `SshBackend` constructed with a docker
isolation config (image/network/limits/user) and an over-SSH `DockerCli`. Either
way the returned object is an `SshBackend` (§Goal).

### 9. Observability

Extend the existing `execution.*` spans: `execution.dispatch` records
`isolation=docker`, `image`, `effective_user`, `host`; `can_run` failures and
the scratch-preflight verdict are logged with specific reasons; the backend/host
each workstream ran on already extends `DOGFOOD_LOG`.

## Testing

- **Pure builders** (`ssh_docker.build_docker_run_argv`, effective-user
  resolution, label set): daemon-free, ssh-free unit tests — argv golden, no
  `--rm`, secret via `--env-file`, trace inlined, `"0:0"` opt-in.
- **Over-SSH `DockerCli`**: inject a fake `Runner` into `SshCli`; assert each
  `docker …` argv is correctly shlex-joined through `ssh_base()`; reuse the
  `docker_recovery` fakes for probe/gc against it.
- **`DockerCli` hardening (§1, shared)**: `stop`/`kill`/`rm` raise on non-zero
  rc; the remote run_cmd enforces `timeout` (expiry → non-zero/`TimeoutError`);
  **local-docker regression** — the existing local path still passes with the
  rc-checked ops.
- **`transport_ref` v2**: docker ref round-trips `isolation` + full
  `expected_labels`; a legacy `v:1` ref decodes as `bare`; persisted-vs-resolved
  isolation mismatch → `NEEDS_REVIEW`.
- **Recovery-GC ordering (§7)**: a non-clean docker GC outcome leaves the remote
  root intact (no `gc_ssh_terminal`) and the row `collected`.
- **`can_run`**: fake ssh runner returning scripted results for `docker
  version` / `image inspect` / in-image `command -v` / scratch write-read-delete;
  assert each failure maps to a specific `CapabilityResult` reason.
- **Handle lifecycle**: fake docker+ssh; terminate → ownership-verified
  `docker stop`; cleanup → `docker rm -f` exact id then `rm -rf`; non-unique /
  label-mismatch → skip + surface.
- **Recovery**: both-entity matrix — {ssh alive/dead} × {container
  present/absent/ambiguous/daemon-error} → assert `NEEDS_REVIEW` unless both
  clean; probe never deletes.
- **Opt-in e2e** (marker-gated, like Phase 1/2a): localhost-SSH + real Docker,
  one workstream, mirrored progress visible, collect applied, container removed,
  remote root gone.

## Non-goals (unchanged from master design)

- `DOCKER_HOST=ssh://` remote-socket mode (different mechanism; may return as an
  explicit backend later).
- Remote `spec-runner plan --full` generation (stays local).
- Implicit image pull / registry auth on the executor (image must pre-exist).
- Config-file/login-state agent auth on stateless executors.
- Elastic fleets, queues, VM provisioning (Phase 3).

## Rollout guarantee

The **remote supervisor source stays byte-identical** — no remote-protocol
version bump (`transport_ref.v` is a center-side persisted field, not the
supervisor wire format).

For the runtime paths the promise is **behavior-compatible**, not byte-identical,
because the shared `DockerCli` hardening (§1: rc-checked `stop`/`kill`/`rm`,
enforced timeout) and the tightened full-label ownership check (§6) deliberately
touch the **local** docker path too. Those are *fail-closed* changes — a
previously-silent failure now raises / routes to review rather than being
mistaken for success — and are covered by local-docker regression tests.

The SSH+Docker path is reached only by an explicit `isolation: {type: docker}`
under an `ssh` transport. No `execution` block, or `isolation: bare`, still
selects the bare paths — unchanged apart from the shared fail-closed hardening
noted above.
