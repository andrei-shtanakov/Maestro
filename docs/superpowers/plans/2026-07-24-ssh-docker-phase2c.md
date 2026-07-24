# SSH + Docker isolation (Phase 2c) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run a harness inside a Docker container on a remote host over SSH (`transport: ssh` + `isolation: {type: docker}`), by rewriting the launch argv into a `docker run` on the center while the remote supervisor stays byte-identical, and driving container lifecycle/recovery through the existing `DockerCli` run over SSH.

**Architecture:** SSH+Docker stays an `SshBackend` (inherits every ssh-branch in the orchestrator: persist / collect-before-gates / WAL mirror / GC). The center rewrites `descriptor["argv"]` into `docker run -v <remote_repo>:/work … <image> <inner_argv>` (pure builder); the supervisor runs it unchanged and its child's exit code is the container's. Container stop/kill/cleanup/probe reuse `DockerCli` by injecting an SSH-tunneling `run_cmd`. Recovery probes **both** the remote process-group and the container, fail-closed.

**Tech Stack:** Python 3.12+, uv, pydantic, aiosqlite, asyncio subprocess, OpenSSH (`ssh`/`rsync`), the `docker` CLI, pytest + anyio.

## Global Constraints

- Package manager: **uv only** (`uv add`, `uv run`); never pip. Line length **88**. Type hints on all code; `uv run pyrefly check` clean. `uv run ruff format .` + `uv run ruff check .` clean. Public APIs get docstrings. f-strings for formatting.
- **No `execution` config, or `isolation: bare` → behavior-compatible with today** (not byte-identical: the shared `DockerCli` hardening + full-label tightening below deliberately touch the local docker path — those are fail-closed changes covered by regression tests).
- **The remote supervisor source (`maestro/execution/resources/maestro_supervisor.py`) stays byte-identical** — no edit, no protocol bump.
- **A remote executor never receives GitHub credentials.** `GH_TOKEN` / `GITHUB_TOKEN` / `GH_*` denylist is already enforced in `exec_config`; do not weaken it.
- **Secrets reach the container only via `docker run --env-file`**, never in argv, never in the docker-run process env (descriptor `env_file` points at a non-existent `<root>/noenv`), never logged. Maestro does not guarantee absence of same-named values in the remote user's ambient env.
- **Root only via explicit `user: "0:0"`.** ssh+docker with unset `user` resolves the remote SSH user's `uid:gid`; an explicit `user` wins verbatim; local+docker user semantics are unchanged.
- **Verification discipline (operational learning):** verify with **targeted foreground** runs (single files / `-k` halves) + `pyrefly check` + `ruff`. **Never** offload the whole suite to a background wait — a workspace watchdog kills long background `pytest`. Rely on PR CI for the full suite.
- Spec of record: `docs/superpowers/specs/2026-07-24-maestro-ssh-docker-phase2c-design.md`. Where this plan and the spec disagree, stop and reconcile before coding.

## File Structure

**New files:**
- `maestro/execution/ssh_docker.py` — pure builders: `build_docker_run_argv` (remote `docker run` argv + expected labels) and `resolve_effective_user` (probe remote uid:gid over ssh). No container lifecycle here.
- `maestro/execution/ssh_docker_probe.py` — `ssh_docker_run_cmd(ssh)` adapter (SshCli → `DockerCli.RunCmd`, timeout-enforcing) and `ContainerOps` (ownership-verified stop/remove over the ssh-backed `DockerCli`).
- `tests/test_ssh_docker.py`, `tests/test_ssh_docker_probe.py` — unit tests for the above.

**Modified files:**
- `maestro/execution/docker_cli.py` — `stop`/`kill`/`rm` check rc and raise on non-zero (shared hardening).
- `maestro/execution/docker_recovery.py` — full-label verification via a shared `labels_match` helper (shared tightening).
- `maestro/execution/docker_handle.py` — `cleanup` uses `labels_match` (full-set) instead of the single-label check.
- `maestro/execution/ssh_launch.py` — `transport_ref` v2: carry `isolation` + `expected_labels`; `decode` maps missing/`v:1` → bare.
- `maestro/execution/ssh_handle.py` — `SshTaskHandle` accepts `container_ops`; docker path stops the container on terminate/kill and rm's it before `rm -rf` root.
- `maestro/execution/ssh_backend.py` — accept a docker `isolation`; `run()` rewrites argv + points env_file at `<root>/noenv` + encodes v2 ref; `can_run()` docker branch (daemon/image/in-image tools/scratch preflight); builds the over-ssh `DockerCli` and `ContainerOps`.
- `maestro/execution/resolver.py` — `_build_ssh` wires `DockerIsolation`.
- `maestro/orchestrator.py` — recovery probes both entities; GC removes the container before the remote root.
- `maestro/CLAUDE.md` — architecture note for SSH+Docker.
- `examples/with-ssh-docker.yaml` — a Mode-2 project.yaml using ssh+docker.

**Dependency order:** T1 → T2 → {T3, T4} → T5 → {T6, T7} ; T8 → T9 ; T10 → T11 → T12. T9 depends on T4+T8; T10 depends on T5.

---

### Task 1: Harden `DockerCli.stop`/`kill`/`rm` to raise on non-zero rc

**Files:**
- Modify: `maestro/execution/docker_cli.py:108-119`
- Test: `tests/test_docker_cli.py`

**Interfaces:**
- Produces: `DockerCli.stop(name, timeout)`, `.kill(name)`, `.rm(name)` now raise `RuntimeError` on a non-zero docker return code (unchanged signatures, unchanged happy path).

- [ ] **Step 1: Write failing tests**

Add to `tests/test_docker_cli.py`:

```python
import pytest
from maestro.execution.docker_cli import DockerCli


def _cli(rc: int, err: str = "boom"):
    async def run_cmd(argv, timeout):
        return rc, "", err
    return DockerCli(run_cmd=run_cmd)


@pytest.mark.anyio
async def test_rm_raises_on_nonzero_rc():
    with pytest.raises(RuntimeError, match="docker rm"):
        await _cli(1).rm("maestro-abc")


@pytest.mark.anyio
async def test_stop_raises_on_nonzero_rc():
    with pytest.raises(RuntimeError, match="docker stop"):
        await _cli(1).stop("maestro-abc", 10.0)


@pytest.mark.anyio
async def test_kill_raises_on_nonzero_rc():
    with pytest.raises(RuntimeError, match="docker kill"):
        await _cli(1).kill("maestro-abc")


@pytest.mark.anyio
async def test_rm_ok_on_zero_rc():
    await _cli(0).rm("maestro-abc")  # no raise
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_docker_cli.py -k "raises_on_nonzero or rm_ok" -v`
Expected: FAIL (no exception raised today).

- [ ] **Step 3: Implement**

Replace the three methods in `maestro/execution/docker_cli.py`:

```python
    async def stop(self, name: str, timeout: float) -> None:  # noqa: ASYNC109
        """Stop a container by name with a timeout. Raises on non-zero rc."""
        secs = max(1, int(timeout))
        rc, out, err = await self._run(
            [self._binary, "stop", "-t", str(secs), name], secs + 10.0
        )
        if rc != 0:
            raise RuntimeError(f"docker stop {name} failed: {err.strip() or out.strip()}")

    async def kill(self, name: str) -> None:
        """Kill a container by name. Raises on non-zero rc."""
        rc, out, err = await self._run([self._binary, "kill", name], self._op_timeout)
        if rc != 0:
            raise RuntimeError(f"docker kill {name} failed: {err.strip() or out.strip()}")

    async def rm(self, name: str) -> None:
        """Remove a container by name with -f. Raises on non-zero rc."""
        rc, out, err = await self._run([self._binary, "rm", "-f", name], self._op_timeout)
        if rc != 0:
            raise RuntimeError(f"docker rm {name} failed: {err.strip() or out.strip()}")
```

- [ ] **Step 4: Run to verify pass + regression**

Run: `uv run pytest tests/test_docker_cli.py tests/test_docker_handle.py tests/test_docker_recovery.py -v`
Expected: PASS. (Note: `DockerTaskHandle._stop_container` and `.kill` already wrap in `contextlib.suppress(Exception)`, so raising is absorbed there; `cleanup()`'s `rm` is intentionally now raising on failure — the regression suite confirms existing cleanup tests still pass because they use rc=0 fakes.)

- [ ] **Step 5: Commit**

```bash
git add maestro/execution/docker_cli.py tests/test_docker_cli.py
git commit -m "fix(docker): stop/kill/rm raise on non-zero rc (shared hardening)"
```

---

### Task 2: `ssh_docker_run_cmd` — drive `DockerCli` over SSH with enforced timeout

**Files:**
- Create: `maestro/execution/ssh_docker_probe.py`
- Test: `tests/test_ssh_docker_probe.py`

**Interfaces:**
- Consumes: `SshCli.run(argv) -> RunResult` (`ssh_cli.py`), `DockerCli.RunCmd = Callable[[list[str], float|None], Awaitable[tuple[int,str,str]]]`.
- Produces: `ssh_docker_run_cmd(ssh: SshCli) -> RunCmd` — each `docker …` argv runs via `ssh.run`; a `timeout` (seconds) is enforced with `asyncio.wait_for` and expiry raises `TimeoutError` (matching the local `_default_run_cmd` contract, so `DockerCli.inspect`/`version_ok` fail closed on a hung daemon).

- [ ] **Step 1: Write failing tests**

Create `tests/test_ssh_docker_probe.py`:

```python
import asyncio

import pytest

from maestro.execution.exec_config import SshTransport
from maestro.execution.ssh_cli import RunResult, SshCli
from maestro.execution.ssh_docker_probe import ssh_docker_run_cmd


def _ssh(runner):
    return SshCli(SshTransport(type="ssh", host="h", workdir_root="/r"), runner=runner)


@pytest.mark.anyio
async def test_run_cmd_tunnels_argv_and_returns_tuple():
    seen = {}

    async def runner(argv, stdin):
        seen["argv"] = argv
        return RunResult(0, "out", "err")

    run_cmd = ssh_docker_run_cmd(_ssh(runner))
    rc, out, err = await run_cmd(["docker", "version"], 30.0)
    assert (rc, out, err) == (0, "out", "err")
    # argv is shlex-joined into the ssh command tail
    assert seen["argv"][0] == "ssh"
    assert "docker version" in seen["argv"][-1]


@pytest.mark.anyio
async def test_run_cmd_timeout_raises():
    async def slow_runner(argv, stdin):
        await asyncio.sleep(1.0)
        return RunResult(0, "", "")

    run_cmd = ssh_docker_run_cmd(_ssh(slow_runner))
    with pytest.raises(TimeoutError):
        await run_cmd(["docker", "inspect", "x"], 0.01)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ssh_docker_probe.py -v`
Expected: FAIL with `ModuleNotFoundError: maestro.execution.ssh_docker_probe`.

- [ ] **Step 3: Implement the adapter**

Create `maestro/execution/ssh_docker_probe.py`:

```python
"""Drive `DockerCli` against a remote daemon over SSH, plus ownership-verified
container lifecycle for the SSH+Docker path (Phase 2c).

`ssh_docker_run_cmd` adapts `SshCli` to `DockerCli.RunCmd`, enforcing the op
timeout locally (SshCli.run has none) and raising TimeoutError on expiry so
DockerCli's probe/inspect paths fail closed on a hung remote daemon.
"""

import asyncio

from maestro.execution.docker_cli import DockerCli, RunCmd
from maestro.execution.ssh_cli import SshCli


def ssh_docker_run_cmd(ssh: SshCli) -> RunCmd:
    """Adapt an `SshCli` into a `DockerCli` run_cmd that tunnels over SSH."""

    async def run_cmd(
        argv: list[str],
        timeout: float | None,  # noqa: ASYNC109
    ) -> tuple[int, str, str]:
        async with asyncio.timeout(timeout):
            res = await ssh.run(argv)
        return res.returncode, res.stdout, res.stderr

    return run_cmd
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_ssh_docker_probe.py -v && uv run pyrefly check`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add maestro/execution/ssh_docker_probe.py tests/test_ssh_docker_probe.py
git commit -m "feat(ssh-docker): DockerCli-over-SSH run_cmd with enforced timeout"
```

---

### Task 3: Pure remote `docker run` argv builder + effective-user resolver

**Files:**
- Create: `maestro/execution/ssh_docker.py`
- Test: `tests/test_ssh_docker.py`

**Interfaces:**
- Consumes: `SshCli` (for the uid:gid probe).
- Produces:
  - `build_docker_run_argv(*, execution_id, entity_kind, entity_id, attempt, backend_id, image, remote_repo, remote_root, remote_env_file, effective_user, network, memory, cpus, inline_env, has_secret_env_file, inner_argv) -> tuple[list[str], dict[str, str]]` — returns `(docker_run_argv, expected_labels)`. No `--rm`. Container name `maestro-<execution_id>`; cidfile `<remote_root>/cid`.
  - `async resolve_effective_user(ssh: SshCli, configured_user: str | None) -> str` — returns `configured_user` if set, else `"<uid>:<gid>"` from `ssh id -u`/`id -g`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_ssh_docker.py`:

```python
import pytest

from maestro.execution.exec_config import SshTransport
from maestro.execution.ssh_cli import RunResult, SshCli
from maestro.execution.ssh_docker import build_docker_run_argv, resolve_effective_user


def _build(**over):
    kw = dict(
        execution_id="exec1",
        entity_kind="workstream",
        entity_id="api",
        attempt=1,
        backend_id="remote-sandbox",
        image="img:tag",
        remote_repo="/var/tmp/maestro/maestro-exec-exec1/repo",
        remote_root="/var/tmp/maestro/maestro-exec-exec1",
        remote_env_file="/var/tmp/maestro/maestro-exec-exec1/env",
        effective_user="1000:1000",
        network="none",
        memory="8g",
        cpus=None,
        inline_env={"TRACEPARENT": "00-abc-def-01"},
        has_secret_env_file=True,
        inner_argv=["spec-runner", "run", "--all"],
    )
    kw.update(over)
    return build_docker_run_argv(**kw)


def test_argv_shape_and_no_rm():
    argv, labels = _build()
    assert argv[0:2] == ["docker", "run"]
    assert "--rm" not in argv
    assert "--name" in argv and "maestro-exec1" in argv
    assert "-v" in argv and "/var/tmp/maestro/maestro-exec-exec1/repo:/work" in argv
    assert "-w" in argv and "/work" in argv
    assert "--user" in argv and "1000:1000" in argv
    assert "--network" in argv and "none" in argv
    assert "--memory" in argv and "8g" in argv
    assert "--cpus" not in argv  # None omitted
    # image then inner argv are the tail
    assert argv[-4:] == ["img:tag", "spec-runner", "run", "--all"]


def test_secret_via_env_file_and_trace_inlined():
    argv, _ = _build()
    assert "--env-file" in argv
    assert "/var/tmp/maestro/maestro-exec-exec1/env" in argv
    # trace env inlined as -e KEY=VALUE, not in a file
    joined = " ".join(argv)
    assert "-e TRACEPARENT=00-abc-def-01" in joined


def test_no_env_file_when_no_secrets():
    argv, _ = _build(has_secret_env_file=False)
    assert "--env-file" not in argv


def test_labels_full_set():
    _, labels = _build()
    assert labels == {
        "maestro.execution_id": "exec1",
        "maestro.entity_kind": "workstream",
        "maestro.entity_id": "api",
        "maestro.attempt": "1",
        "maestro.backend_id": "remote-sandbox",
    }
    argv, _ = _build()
    assert "--label" in argv and "maestro.execution_id=exec1" in argv


def test_root_opt_in_user_honored():
    argv, _ = _build(effective_user="0:0")
    assert "0:0" in argv


def _ssh(runner):
    return SshCli(SshTransport(type="ssh", host="h", workdir_root="/r"), runner=runner)


@pytest.mark.anyio
async def test_resolve_effective_user_explicit_wins():
    async def runner(argv, stdin):  # should never be called
        raise AssertionError("no probe when user is explicit")

    assert await resolve_effective_user(_ssh(runner), "1000:1000") == "1000:1000"


@pytest.mark.anyio
async def test_resolve_effective_user_probes_remote():
    calls = []

    async def runner(argv, stdin):
        calls.append(argv[-1])
        val = "1000" if "id -u" in argv[-1] else "2000"
        return RunResult(0, val + "\n", "")

    assert await resolve_effective_user(_ssh(runner), None) == "1000:2000"


@pytest.mark.anyio
async def test_resolve_effective_user_probe_failure_raises():
    async def runner(argv, stdin):
        return RunResult(1, "", "nope")

    with pytest.raises(RuntimeError, match="resolve remote uid"):
        await resolve_effective_user(_ssh(runner), None)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ssh_docker.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the builders**

Create `maestro/execution/ssh_docker.py`:

```python
"""Pure builders for the SSH+Docker path (Phase 2c): the remote `docker run`
argv (with remote paths) and the effective-user resolution. No container
lifecycle here — that lives in `ssh_docker_probe.ContainerOps`.
"""

from collections.abc import Mapping

from maestro.execution.ssh_cli import SshCli


def build_docker_run_argv(
    *,
    execution_id: str,
    entity_kind: str,
    entity_id: str,
    attempt: int,
    backend_id: str,
    image: str,
    remote_repo: str,
    remote_root: str,
    remote_env_file: str,
    effective_user: str,
    network: str,
    memory: str | None,
    cpus: str | None,
    inline_env: Mapping[str, str],
    has_secret_env_file: bool,
    inner_argv: list[str],
) -> tuple[list[str], dict[str, str]]:
    """Build the remote `docker run` argv + its expected label set.

    Deliberately omits `--rm` so a killed/crashed container stays inspectable
    as recovery evidence. Secrets are delivered only via `--env-file`
    (when present); non-secret `inline_env` (trace + explicit env) is inlined
    as `-e KEY=VALUE`. The label set mirrors the local `DockerIsolator`.
    """
    name = f"maestro-{execution_id}"
    labels = {
        "maestro.execution_id": execution_id,
        "maestro.entity_kind": entity_kind,
        "maestro.entity_id": entity_id,
        "maestro.attempt": str(attempt),
        "maestro.backend_id": backend_id,
    }
    argv: list[str] = [
        "docker",
        "run",
        "--name",
        name,
        "--cidfile",
        f"{remote_root}/cid",
        "-v",
        f"{remote_repo}:/work",
        "-w",
        "/work",
        "--network",
        network,
        "--user",
        effective_user,
    ]
    if memory:
        argv += ["--memory", memory]
    if cpus:
        argv += ["--cpus", cpus]
    if has_secret_env_file:
        argv += ["--env-file", remote_env_file]
    for key, value in inline_env.items():
        argv += ["-e", f"{key}={value}"]
    for key, value in labels.items():
        argv += ["--label", f"{key}={value}"]
    argv.append(image)
    argv += list(inner_argv)
    return argv, labels


async def resolve_effective_user(ssh: SshCli, configured_user: str | None) -> str:
    """Return `configured_user` if set, else the remote SSH user's uid:gid.

    Raises RuntimeError if the remote id probe fails (fail-fast: we must know
    the ownership the container will write with before it runs).
    """
    if configured_user:
        return configured_user
    uid = await ssh.run(["id", "-u"])
    gid = await ssh.run(["id", "-g"])
    if uid.returncode != 0 or gid.returncode != 0:
        raise RuntimeError(
            f"could not resolve remote uid:gid (id -u rc={uid.returncode}, "
            f"id -g rc={gid.returncode})"
        )
    return f"{uid.stdout.strip()}:{gid.stdout.strip()}"
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_ssh_docker.py -v && uv run pyrefly check`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add maestro/execution/ssh_docker.py tests/test_ssh_docker.py
git commit -m "feat(ssh-docker): pure docker-run argv builder + effective-user resolver"
```

---

### Task 4: `transport_ref` v2 — carry isolation + expected_labels

**Files:**
- Modify: `maestro/execution/ssh_launch.py:77-100`
- Test: `tests/test_ssh_launch.py`

**Interfaces:**
- Produces: `encode_transport_ref(host, port, remote_dir, status_marker, *, isolation="bare", expected_labels=None) -> str` (v2 JSON). `decode_transport_ref(s)` returns a dict where a missing `isolation` (legacy `v:1`) reads as `"bare"` and missing `expected_labels` as `{}`.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_ssh_launch.py`:

```python
from maestro.execution.ssh_launch import decode_transport_ref, encode_transport_ref


def test_transport_ref_v2_docker_roundtrip():
    labels = {"maestro.execution_id": "e1", "maestro.backend_id": "rs"}
    s = encode_transport_ref(
        "h", 22, "/r/maestro-exec-e1", "/r/maestro-exec-e1/e1.status",
        isolation="docker", expected_labels=labels,
    )
    d = decode_transport_ref(s)
    assert d["v"] == 2
    assert d["isolation"] == "docker"
    assert d["expected_labels"] == labels


def test_transport_ref_default_is_bare():
    s = encode_transport_ref("h", None, "/r/x", "/r/x/x.status")
    d = decode_transport_ref(s)
    assert d["isolation"] == "bare"
    assert d["expected_labels"] == {}


def test_legacy_v1_decodes_as_bare():
    import json
    legacy = json.dumps({
        "v": 1, "transport": "ssh", "host": "h", "port": None,
        "remote_dir": "/r/x", "status_marker": "/r/x/x.status",
    })
    d = decode_transport_ref(legacy)
    assert d["isolation"] == "bare"
    assert d["expected_labels"] == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ssh_launch.py -k "transport_ref or legacy_v1" -v`
Expected: FAIL (v is 1, no `isolation` key).

- [ ] **Step 3: Implement**

Replace `encode_transport_ref`/`decode_transport_ref` in `maestro/execution/ssh_launch.py`:

```python
def encode_transport_ref(
    host: str,
    port: int | None,
    remote_dir: str,
    status_marker: str,
    *,
    isolation: str = "bare",
    expected_labels: dict[str, str] | None = None,
) -> str:
    """Encode an opaque, versioned (v2) transport_ref for an SSH execution.

    `isolation` (`"bare"|"docker"`) and `expected_labels` are the recovery
    SSOT for a run's isolation identity — persisted so a config edit after
    launch cannot change how the run is probed/GC'd.
    """
    return json.dumps(
        {
            "v": 2,
            "transport": "ssh",
            "host": host,
            "port": port,
            "remote_dir": remote_dir,
            "status_marker": status_marker,
            "isolation": isolation,
            "expected_labels": expected_labels or {},
        }
    )


def decode_transport_ref(s: str) -> dict:
    """Decode a `transport_ref`. A legacy `v:1` ref (no `isolation`) reads as
    a `bare` run with an empty expected-label set."""
    data = json.loads(s)
    data.setdefault("isolation", "bare")
    data.setdefault("expected_labels", {})
    return data
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_ssh_launch.py -v && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/execution/ssh_launch.py tests/test_ssh_launch.py
git commit -m "feat(ssh-docker): transport_ref v2 carries isolation + expected_labels"
```

---

### Task 5: `SshBackend` docker isolation — construct + `run()` argv rewrite

**Files:**
- Modify: `maestro/execution/ssh_backend.py`
- Test: `tests/test_ssh_backend.py`

**Interfaces:**
- Consumes: `build_docker_run_argv`, `resolve_effective_user` (T3); `ssh_docker_run_cmd` (T2); `encode_transport_ref(..., isolation=, expected_labels=)` (T4); `DockerIsolation` (`exec_config.py:69`).
- Produces: `SshBackend(name, transport, *, secret_env, isolation: DockerIsolation | None = None, runner=None, local_staging_root=None)`. New properties: `isolation_kind -> "bare"|"docker"`, `docker -> DockerCli | None`. When docker, `run()` sets `descriptor["argv"]` to the `docker run` argv, points the descriptor's `env_file` at `<root>/noenv`, and encodes a v2 docker transport_ref with the expected labels.

- [ ] **Step 1: Write failing test**

Add to `tests/test_ssh_backend.py` (a fake runner that records the descriptor written via `tee` and returns the handshake):

```python
import json

import pytest

from maestro.execution.exec_config import DockerIsolation, SshTransport
from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.execution.ssh_backend import SshBackend
from maestro.execution.ssh_cli import RunResult
from maestro.execution.ssh_launch import decode_transport_ref


class _FakeRunner:
    """Records tee'd descriptor; answers id/uid and the supervisor handshake."""

    def __init__(self):
        self.descriptor = None

    async def __call__(self, argv, stdin):
        tail = argv[-1]
        if "tee" in tail and stdin and stdin.lstrip().startswith("{"):
            self.descriptor = json.loads(stdin)
        if tail.endswith("id -u"):
            return RunResult(0, "1000\n", "")
        if tail.endswith("id -g"):
            return RunResult(0, "1000\n", "")
        if "maestro_supervisor.py" in tail:
            return RunResult(0, "MAESTRO-SUPERVISOR-READY exec1\n", "")
        return RunResult(0, "", "")


def _req(tmp_path):
    wd = tmp_path / "wt"
    wd.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=wd, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=wd, check=True)
    return ExecutionRequest(
        run_id="api",
        execution_id="exec1",
        entity_kind="workstream",
        argv=["spec-runner", "run", "--all"],
        workdir=wd,
        log_path=tmp_path / "log",
        collect=CollectPolicy(mode="scope_paths", include=["src/**"]),
    )


@pytest.mark.anyio
async def test_docker_run_rewrites_argv_and_ref(tmp_path):
    runner = _FakeRunner()
    backend = SshBackend(
        "remote-sandbox",
        SshTransport(type="ssh", host="h", workdir_root="/var/tmp/maestro"),
        secret_env=["ANTHROPIC_API_KEY"],
        isolation=DockerIsolation(type="docker", image="img:tag", network="none"),
        runner=runner,
    )
    handle = await backend.run(_req(tmp_path))
    desc = runner.descriptor
    assert desc["argv"][0:2] == ["docker", "run"]
    assert desc["argv"][-3:] == ["spec-runner", "run", "--all"]
    assert desc["env_file"].endswith("/noenv")   # secrets NOT sourced by supervisor
    assert "--env-file" in desc["argv"]           # secrets via docker instead
    ref = decode_transport_ref(handle.ref.transport_ref)
    assert ref["isolation"] == "docker"
    assert ref["expected_labels"]["maestro.execution_id"] == "exec1"
    await handle.cleanup()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ssh_backend.py -k docker_run_rewrites -v`
Expected: FAIL (`SshBackend.__init__` has no `isolation` kwarg).

- [ ] **Step 3: Implement**

In `maestro/execution/ssh_backend.py`:

Add imports:

```python
from maestro.execution.exec_config import DockerIsolation, SshTransport
from maestro.execution.ssh_docker import build_docker_run_argv, resolve_effective_user
from maestro.execution.ssh_docker_probe import ssh_docker_run_cmd
from maestro.execution.docker_cli import DockerCli
```

Extend `__init__` to accept `isolation` and build the over-ssh DockerCli:

```python
    def __init__(
        self,
        name: str,
        transport: SshTransport,
        *,
        secret_env: list[str],
        isolation: DockerIsolation | None = None,
        runner: Runner | None = None,
        local_staging_root: Path | None = None,
    ) -> None:
        """Build the backend for a named ssh transport (bare or docker)."""
        self._name = name
        self._t = transport
        self._secret_env = secret_env
        self._isolation = isolation
        self._ssh = SshCli(transport, runner=runner)
        self._docker = (
            DockerCli(run_cmd=ssh_docker_run_cmd(self._ssh), op_timeout=60.0)
            if isolation is not None
            else None
        )
        self._staging_root = local_staging_root or Path(
            os.environ.get("TMPDIR", "/tmp")
        )

    @property
    def isolation_kind(self) -> str:
        """`"docker"` when a container isolation is configured, else `"bare"`."""
        return "docker" if self._isolation is not None else "bare"

    @property
    def docker(self) -> DockerCli | None:
        """The over-SSH DockerCli for the docker path (None for bare)."""
        return self._docker
```

In `run()`, after `descriptor = build_descriptor(...)` and BEFORE `_launch_supervisor`, rewrite for the docker path and thread the isolation into the ref. Replace the block from `descriptor = build_descriptor(...)` through the `ref = ExecutionHandleRef(...)` assignment with:

```python
        descriptor = build_descriptor(
            req.execution_id, layout, list(req.argv), self._t.workdir_root
        )
        isolation = "bare"
        expected_labels: dict[str, str] = {}
        if self._isolation is not None:
            effective_user = await resolve_effective_user(
                self._ssh, self._isolation.user
            )
            trace_env = child_env()  # non-secret trace propagation
            inline_env = {**req.env, **trace_env}
            docker_argv, expected_labels = build_docker_run_argv(
                execution_id=req.execution_id,
                entity_kind=req.entity_kind or "workstream",
                entity_id=req.run_id,
                attempt=req.attempt,
                backend_id=self._name,
                image=self._isolation.image,
                remote_repo=layout.repo,
                remote_root=layout.root,
                remote_env_file=layout.env_file,
                effective_user=effective_user,
                network=self._isolation.network,
                memory=self._isolation.memory,
                cpus=self._isolation.cpus,
                inline_env=inline_env,
                has_secret_env_file=bool(self._secret_env),
                inner_argv=list(req.argv),
            )
            descriptor["argv"] = docker_argv
            # secrets reach the container via docker --env-file; keep them OUT of
            # the docker-run process env by pointing the supervisor's env_file at
            # a non-existent path under root (passes _validate, _load_env skips it).
            descriptor["env_file"] = f"{layout.root}/noenv"
            isolation = "docker"

        result = await self._launch_supervisor(layout, descriptor)
        if _HANDSHAKE not in result.stdout:
            raise RuntimeError(f"supervisor handshake missing: {result.stderr[:400]}")

        ref = ExecutionHandleRef(
            backend_id=self._name,
            run_id=req.run_id,
            transport_ref=encode_transport_ref(
                self._t.host,
                self._t.port,
                layout.root,
                layout.status,
                isolation=isolation,
                expected_labels=expected_labels,
            ),
            status_marker=layout.status,
            started_at=datetime.now(UTC),
        )
```

Add the `child_env` import at the top:

```python
from maestro.execution.env import child_env
```

(Confirm the symbol: `grep -n "def child_env" maestro/execution/env.py`; if it lives elsewhere, import from there. This is the same trace-env source the bare path should use.)

Then pass `container_ops` into the `SshTaskHandle(...)` construction (implemented in Task 7 — for now add the kwarg wired to `None` for bare and a `ContainerOps` for docker):

```python
        handle = SshTaskHandle(
            self._ssh,
            layout,
            ref,
            log_path=req.log_path,
            timeout_seconds=req.timeout_seconds,
            collect_spec=CollectSpec(
                req.workdir, staging, journal, baseline,
                scope=_collect_scope(req.collect),
            ),
            expected_owner=req.execution_id,
            mirror_spec=self._build_mirror_spec(req, layout),
            container_ops=self._make_container_ops(expected_labels)
            if self._isolation is not None
            else None,
        )
```

Add the helper (its `ContainerOps` type lands in Task 7; import it now):

```python
    def _make_container_ops(self, expected_labels: dict[str, str]):
        from maestro.execution.ssh_docker_probe import ContainerOps

        assert self._docker is not None
        return ContainerOps(
            docker=self._docker,
            container_name=f"maestro-{expected_labels['maestro.execution_id']}",
            expected_labels=expected_labels,
        )
```

> **Note for the implementer:** Task 7 defines `ContainerOps` and `SshTaskHandle`'s `container_ops` kwarg. If you implement T5 before T7, add a minimal `ContainerOps` stub + kwarg first, then flesh out T7. Recommended: implement T7 immediately after T5 (they share the `container_ops` seam) and run both test files together.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_ssh_backend.py -k "docker_run_rewrites" -v && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/execution/ssh_backend.py tests/test_ssh_backend.py
git commit -m "feat(ssh-docker): SshBackend rewrites argv to docker run + v2 docker ref"
```

---

### Task 6: `can_run` docker branch — daemon + image + in-image tools + scratch preflight

**Files:**
- Modify: `maestro/execution/ssh_backend.py:105-108` (`can_run`)
- Test: `tests/test_ssh_backend.py`

**Interfaces:**
- Consumes: `self._docker` (over-ssh DockerCli, T5), `self._isolation`, `SshCli`.
- Produces: `SshBackend.can_run(req)` — bare path unchanged (remote-PATH probe); docker path returns `CapabilityResult(ok=False, missing_tools=[reason])` on: daemon unreachable, image absent, an in-image tool missing, or a scratch write/read/delete failure under the effective user.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_ssh_backend.py`:

```python
from maestro.execution.models import ExecutionRequest as _ER


def _docker_backend(runner):
    return SshBackend(
        "rs",
        SshTransport(type="ssh", host="h", workdir_root="/r"),
        secret_env=[],
        isolation=DockerIsolation(type="docker", image="img:tag", network="none"),
        runner=runner,
    )


def _can_run_req():
    return _ER(run_id="w", execution_id="e", argv=["spec-runner"],
               workdir=Path("/tmp"), log_path=Path("/tmp/l"),
               collect=CollectPolicy(mode="none"), required_tools=["spec-runner"])


@pytest.mark.anyio
async def test_can_run_docker_ok(tmp_path):
    async def runner(argv, stdin):
        return RunResult(0, "ok", "")  # every docker/id/scratch check succeeds
    res = await _docker_backend(runner).can_run(_can_run_req())
    assert res.ok


@pytest.mark.anyio
async def test_can_run_docker_image_absent():
    async def runner(argv, stdin):
        tail = argv[-1]
        if "image inspect" in tail:
            return RunResult(1, "", "No such image")
        return RunResult(0, "ok", "")
    res = await _docker_backend(runner).can_run(_can_run_req())
    assert not res.ok
    assert any("image" in m for m in res.missing_tools)


@pytest.mark.anyio
async def test_can_run_docker_tool_missing():
    async def runner(argv, stdin):
        tail = argv[-1]
        if "command -v" in tail:
            return RunResult(1, "", "")
        return RunResult(0, "ok", "")
    res = await _docker_backend(runner).can_run(_can_run_req())
    assert not res.ok
    assert any("spec-runner" in m for m in res.missing_tools)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ssh_backend.py -k can_run_docker -v`
Expected: FAIL (current `can_run` probes bare PATH → `command -v spec-runner` returns 0 → wrongly ok).

- [ ] **Step 3: Implement**

Replace `can_run` in `ssh_backend.py`:

```python
    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        """Probe capability on the executor.

        Bare: each `required_tool` on the remote PATH. Docker: the remote
        daemon is reachable, the image exists, each required tool resolves
        INSIDE the image under the effective user, and a scratch file can be
        written/read/deleted in a `/work`-style bind mount (catches a
        UID/rootfs incompatibility before the real task).
        """
        if self._isolation is None:
            missing = [
                t for t in req.required_tools if not await self._ssh.probe_tool(t)
            ]
            return CapabilityResult(ok=not missing, missing_tools=missing)

        assert self._docker is not None
        problems: list[str] = []
        if not await self._docker.version_ok():
            return CapabilityResult(ok=False, missing_tools=["docker daemon unreachable"])
        image = self._isolation.image
        if not await self._docker.image_exists(image):
            return CapabilityResult(ok=False, missing_tools=[f"image absent: {image}"])
        user = await resolve_effective_user(self._ssh, self._isolation.user)
        for tool in req.required_tools:
            probe = await self._ssh.run(
                ["docker", "run", "--rm", "--user", user, image,
                 "sh", "-c", f"command -v {tool}"]
            )
            if probe.returncode != 0:
                problems.append(f"tool missing in image: {tool}")
        scratch = await self._ssh.run(
            ["docker", "run", "--rm", "--user", user, image,
             "sh", "-c", "t=$(mktemp) && echo ok > \"$t\" && cat \"$t\" && rm \"$t\""]
        )
        if scratch.returncode != 0:
            problems.append(f"scratch write/read/delete failed under user {user}")
        return CapabilityResult(ok=not problems, missing_tools=problems)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_ssh_backend.py -k "can_run_docker or can_run" -v && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/execution/ssh_backend.py tests/test_ssh_backend.py
git commit -m "feat(ssh-docker): can_run probes daemon/image/in-image tools/scratch"
```

---

### Task 7: Docker-aware `SshTaskHandle` lifecycle + `ContainerOps`

**Files:**
- Modify: `maestro/execution/ssh_docker_probe.py` (add `ContainerOps`)
- Modify: `maestro/execution/ssh_handle.py` (accept `container_ops`; stop container on terminate/kill; rm before rm -rf)
- Test: `tests/test_ssh_handle.py`, `tests/test_ssh_docker_probe.py`

**Interfaces:**
- Produces:
  - `ContainerOps(docker: DockerCli, container_name: str, expected_labels: dict[str,str])` with `async stop(grace: float) -> None` (ownership-verified best-effort `docker stop`+`kill`) and `async remove() -> None` (ownership-verified `docker rm -f`, raising on label mismatch).
  - `labels_match(actual: Mapping[str,str], expected: Mapping[str,str]) -> bool` — every expected key present with an equal value, and no expected value is None.
  - `SshTaskHandle(..., container_ops: ContainerOps | None = None)` — docker path stops the container in `terminate`/`kill` and `remove()`s it at the start of `cleanup` (before `rm -rf` root).

- [ ] **Step 1: Write failing tests**

Add to `tests/test_ssh_docker_probe.py`:

```python
from maestro.execution.ssh_docker_probe import ContainerOps, labels_match


class _FakeDocker:
    def __init__(self, labels):
        self._labels = labels
        self.removed = []
        self.stopped = []

    async def inspect(self, name):
        return {"Config": {"Labels": self._labels}} if self._labels else None

    async def stop(self, name, timeout):
        self.stopped.append(name)

    async def kill(self, name):
        self.stopped.append(("kill", name))

    async def rm(self, name):
        self.removed.append(name)


def test_labels_match_full_set():
    assert labels_match({"a": "1", "b": "2"}, {"a": "1", "b": "2"})
    assert not labels_match({"a": "1"}, {"a": "1", "b": "2"})   # missing key
    assert not labels_match({"a": "9"}, {"a": "1"})              # value mismatch
    assert not labels_match({"a": None}, {"a": None})            # None never matches


@pytest.mark.anyio
async def test_container_ops_remove_ownership_verified():
    exp = {"maestro.execution_id": "e1", "maestro.backend_id": "rs"}
    dk = _FakeDocker(exp)
    ops = ContainerOps(docker=dk, container_name="maestro-e1", expected_labels=exp)
    await ops.remove()
    assert dk.removed == ["maestro-e1"]


@pytest.mark.anyio
async def test_container_ops_remove_refuses_on_mismatch():
    exp = {"maestro.execution_id": "e1", "maestro.backend_id": "rs"}
    dk = _FakeDocker({"maestro.execution_id": "OTHER"})
    ops = ContainerOps(docker=dk, container_name="maestro-e1", expected_labels=exp)
    with pytest.raises(RuntimeError, match="label mismatch"):
        await ops.remove()
    assert dk.removed == []
```

Add to `tests/test_ssh_handle.py` a test that a docker handle stops+removes the container (use the existing ssh handle fixtures + a fake ContainerOps recording calls); assert `cleanup()` calls `remove()` before the remote `rm -rf` and `terminate()` calls `stop()`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ssh_docker_probe.py -k "labels_match or container_ops" -v`
Expected: FAIL (`ContainerOps`/`labels_match` undefined).

- [ ] **Step 3: Implement `ContainerOps` + `labels_match`**

Append to `maestro/execution/ssh_docker_probe.py`:

```python
from collections.abc import Mapping


def labels_match(actual: Mapping[str, str], expected: Mapping[str, str]) -> bool:
    """True iff every expected label is present on `actual` with an equal,
    non-None value. Used for full-set ownership verification (Phase 2c)."""
    for key, value in expected.items():
        if value is None or actual.get(key) != value:
            return False
    return True


class ContainerOps:
    """Ownership-verified container lifecycle over an (ssh-backed) DockerCli."""

    def __init__(
        self,
        *,
        docker: DockerCli,
        container_name: str,
        expected_labels: dict[str, str],
    ) -> None:
        """Bind the ops to one container name + its full expected label set."""
        self._docker = docker
        self._name = container_name
        self._expected = expected_labels

    async def _verify(self) -> bool:
        """Inspect by name; True iff present AND full labels match. None → absent."""
        info = await self._docker.inspect(self._name)
        if info is None:
            return False
        labels = (info.get("Config") or {}).get("Labels") or {}
        if not labels_match(labels, self._expected):
            raise RuntimeError(
                f"refusing to act on {self._name}: label mismatch "
                f"(expected {self._expected}, got {labels})"
            )
        return True

    async def stop(self, grace: float) -> None:
        """Best-effort ownership-verified stop→kill (a channel/daemon blip
        must not wedge terminate/kill)."""
        import contextlib

        with contextlib.suppress(Exception):
            if await self._verify():
                with contextlib.suppress(Exception):
                    await self._docker.stop(self._name, grace)
                with contextlib.suppress(Exception):
                    await self._docker.kill(self._name)

    async def remove(self) -> None:
        """Ownership-verified `docker rm -f`. Raises on a label mismatch;
        a no-op when the container is already absent."""
        if await self._verify():
            await self._docker.rm(self._name)
```

Note the `DockerCli` import already exists at the top of the module (Task 2).

- [ ] **Step 4: Wire `container_ops` into `SshTaskHandle`**

In `maestro/execution/ssh_handle.py`, add `container_ops` to `__init__` (store `self._container_ops = container_ops`), then:

In `terminate`, after signaling the group, stop the container:

```python
    async def terminate(self, grace_seconds: float) -> None:
        """Send TERM to the process group; escalate to KILL after grace.

        For a docker run, also targeted-stop the container — killing the
        remote `docker run` client does not stop the container itself.
        """
        await self._signal_group("TERM")
        if self._container_ops is not None:
            await self._container_ops.stop(grace_seconds)
        await asyncio.sleep(grace_seconds)
        if not self._terminal.is_set():
            await self._signal_group("KILL")
```

In `kill`:

```python
    async def kill(self) -> None:
        """Force-kill the process group and, for docker, the container."""
        await self._signal_group("KILL")
        if self._container_ops is not None:
            await self._container_ops.stop(0.0)
```

In `cleanup`, remove the container BEFORE the remote `rm -rf`:

```python
    async def cleanup(self) -> None:
        """Ownership-checked container removal (docker), then remote `rm -rf`
        + local staging/journal removal."""
        await self._verify_ownership()
        if self._container_ops is not None:
            await self._container_ops.remove()
        await self._ssh.run(["rm", "-rf", self._layout.root])
        for p in (self._collect.staging_dir, self._collect.journal_dir):
            shutil.rmtree(p, ignore_errors=True)
        if self._monitor is not None:
            self._monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor
```

Add the `container_ops` param to the signature (keyword-only, default `None`) with a docstring line; import type under `TYPE_CHECKING` to avoid a cycle:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from maestro.execution.ssh_docker_probe import ContainerOps
```

- [ ] **Step 5: Run + commit**

Run: `uv run pytest tests/test_ssh_docker_probe.py tests/test_ssh_handle.py -v && uv run pyrefly check`
Expected: PASS.

```bash
git add maestro/execution/ssh_docker_probe.py maestro/execution/ssh_handle.py tests/test_ssh_docker_probe.py tests/test_ssh_handle.py
git commit -m "feat(ssh-docker): ownership-verified ContainerOps + docker-aware SshTaskHandle"
```

---

### Task 8: Full-label ownership tightening in `docker_recovery` + `docker_handle` (shared)

**Files:**
- Modify: `maestro/execution/docker_recovery.py` (probe/gc use `labels_match`)
- Modify: `maestro/execution/docker_handle.py:94-108` (cleanup uses `labels_match`)
- Test: `tests/test_docker_recovery.py`, `tests/test_docker_handle.py`

**Interfaces:**
- Consumes: `labels_match` (T7). To avoid a docker_recovery→ssh_docker_probe import direction, move `labels_match` into `docker_recovery.py` and re-export it from `ssh_docker_probe.py` (`from maestro.execution.docker_recovery import labels_match`). Update T7's import accordingly.
- Produces: `probe_execution`/`gc_terminal_handle`/`DockerTaskHandle.cleanup` verify the **full** expected label set, not just `maestro.execution_id`. `probe_execution`/`gc` currently only know the id (from the row) — they keep the single-id check (the row carries no full label set), while `DockerTaskHandle.cleanup` (which HAS `expected_labels`) is tightened to the full set.

> Scope clarification: the row-driven recovery functions only have `execution_id`, so their check stays id-based (unchanged behavior). The tightening applies where a full `expected_labels` is available: `DockerTaskHandle.cleanup` and the new `ContainerOps`. This keeps the "full-label verify" guarantee wherever the labels are known, without inventing a label set recovery doesn't have.

- [ ] **Step 1: Write failing test**

Add to `tests/test_docker_handle.py` a test that `cleanup` refuses when a non-id label diverges but the id matches:

```python
@pytest.mark.anyio
async def test_cleanup_refuses_on_full_label_mismatch():
    expected = {"maestro.execution_id": "e1", "maestro.backend_id": "rs"}
    # id matches but backend_id differs -> full-set check must refuse
    info = {"Config": {"Labels": {"maestro.execution_id": "e1",
                                  "maestro.backend_id": "OTHER"}}}
    docker = _FakeDocker(inspect_result=info)   # existing test helper
    handle = _make_handle(expected_labels=expected, docker=docker)
    with pytest.raises(RuntimeError, match="label mismatch"):
        await handle.cleanup()
    assert docker.removed == []
```

(Adapt `_FakeDocker`/`_make_handle` to the existing fixtures in the file.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_docker_handle.py -k full_label_mismatch -v`
Expected: FAIL (current cleanup only checks `maestro.execution_id`, so it would rm).

- [ ] **Step 3: Implement**

Move `labels_match` into `maestro/execution/docker_recovery.py` (top-level function, same body as T7). In `ssh_docker_probe.py` replace the local definition with `from maestro.execution.docker_recovery import labels_match`.

In `docker_handle.py` `cleanup`, replace the single-label check with:

```python
        info = await self._docker.inspect(self._name)
        if info is not None:
            labels = (info.get("Config") or {}).get("Labels") or {}
            from maestro.execution.docker_recovery import labels_match

            if not labels_match(labels, self._expected):
                raise RuntimeError(
                    f"refusing to rm {self._name}: label mismatch "
                    f"(expected {self._expected}, got {labels})"
                )
            await self._docker.rm(self._name)
```

- [ ] **Step 4: Run to verify pass + regression**

Run: `uv run pytest tests/test_docker_handle.py tests/test_docker_recovery.py tests/test_ssh_docker_probe.py -v && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/execution/docker_recovery.py maestro/execution/docker_handle.py maestro/execution/ssh_docker_probe.py tests/test_docker_handle.py
git commit -m "refactor(docker): full-label ownership verify via shared labels_match"
```

---

### Task 9: Dual-entity recovery — probe both process-group AND container; GC container first

**Files:**
- Modify: `maestro/orchestrator.py` (the workstream recovery probe ~`:667-689` and `_gc_terminal_handles` ~`:691-769`)
- Test: `tests/test_orchestrator_ssh_wiring.py`

**Interfaces:**
- Consumes: `decode_transport_ref` (T4, `isolation`/`expected_labels`), `probe_ssh` (existing), `docker_recovery.probe_execution` / `gc_terminal_handle` driven by the backend's over-ssh DockerCli (`SshBackend.docker`, T5).
- Produces: for a persisted `isolation == "docker"` ssh handle, recovery routes to `NEEDS_REVIEW` if **either** `probe_ssh` OR the container probe says so, and never GCs the remote root unless the container is confirmed gone first. A mismatch between the persisted isolation and the resolved backend's `isolation_kind` → `NEEDS_REVIEW`.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_orchestrator_ssh_wiring.py`:

```python
# A docker ssh handle: probe_ssh clean, but a leftover container -> NEEDS_REVIEW.
@pytest.mark.anyio
async def test_recovery_docker_leftover_container_needs_review(...):
    # transport_ref isolation="docker"; fake SshBackend.docker.ps_ids_by_label
    # returns ["cid1"] with matching labels -> probe_execution says needs_review.
    # Assert the workstream is NOT silently re-READY'd.
    ...

# Persisted isolation="docker" but resolved backend is bare -> NEEDS_REVIEW.
@pytest.mark.anyio
async def test_recovery_isolation_mismatch_needs_review(...):
    ...

# GC ordering: docker gc returns a non-clean outcome -> remote root NOT rm'd.
@pytest.mark.anyio
async def test_gc_container_first_short_circuits(...):
    # Assert ssh.run(["rm","-rf",root]) was never issued when docker gc is dirty.
    ...
```

(Fill the `...` using the file's existing orchestrator + fake-backend fixtures; the fakes must expose `isolation_kind` and `docker`.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_orchestrator_ssh_wiring.py -k "docker_leftover or isolation_mismatch or container_first" -v`
Expected: FAIL (recovery ignores the container today).

- [ ] **Step 3: Implement**

In the workstream recovery probe (the `else` branch that resolves an `SshBackend` and calls `probe_ssh`), extend for docker:

```python
            backend = self._backends.resolve(backend_id)
            if isinstance(backend, SshBackend):
                ref = _handle_ref_from_row(row)
                decoded = decode_transport_ref(ref.transport_ref)
                ssh_verdict = await probe_ssh(backend._ssh, ref)
                if decoded["isolation"] == "docker":
                    if backend.isolation_kind != "docker" or backend.docker is None:
                        verdict = RecoveryVerdict(
                            True, "persisted docker isolation but backend is bare"
                        )
                    else:
                        cont = await probe_execution(
                            row["execution_id"], backend.docker
                        )
                        verdict = RecoveryVerdict(
                            ssh_verdict.needs_review or cont.needs_review,
                            f"ssh={ssh_verdict.reason}; container={cont.reason}",
                        )
                else:
                    verdict = ssh_verdict
            else:
                verdict = RecoveryVerdict(
                    True, f"unsupported backend {backend_id!r} for recovery probe"
                )
```

In `_gc_terminal_handles`, in the ssh branch (`state == "collected"`), remove the container FIRST when the persisted ref is docker:

```python
            try:
                backend = self._backends.resolve(backend_id)
                if not isinstance(backend, SshBackend):
                    continue
                ref = _handle_ref_from_row(row)
                decoded = decode_transport_ref(ref.transport_ref)
                if decoded["isolation"] == "docker":
                    if backend.docker is None:
                        continue  # config no longer docker; leave for a human
                    dk_outcome = await gc_terminal_handle(
                        {"execution_id": row["execution_id"]}, backend.docker
                    )
                    if dk_outcome not in GC_CLEAN_OUTCOMES:
                        self._logger.warning(
                            "recovery: container GC not clean for %s: %s — "
                            "leaving remote root intact",
                            row["execution_id"], dk_outcome,
                        )
                        continue
                outcome = await gc_ssh_terminal(backend._ssh, ref)
            except Exception as e:
                ...  # existing warning
                continue
```

Add imports at the top of `orchestrator.py` if missing: `decode_transport_ref` (already imported — it's used at :1215), `probe_execution`, `gc_terminal_handle`, `GC_CLEAN_OUTCOMES` (already imported for the docker branch), `RecoveryVerdict`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_orchestrator_ssh_wiring.py -v && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/orchestrator.py tests/test_orchestrator_ssh_wiring.py
git commit -m "feat(ssh-docker): dual-entity recovery + container-first GC ordering"
```

---

### Task 10: `resolver._build_ssh` wires `DockerIsolation`

**Files:**
- Modify: `maestro/execution/resolver.py:75-82`
- Test: `tests/test_backend_resolver.py`

**Interfaces:**
- Consumes: `SshBackend(..., isolation=DockerIsolation)` (T5), `DockerIsolation` (`exec_config.py`).
- Produces: a `backends.<name>` with `transport: ssh` + `isolation: {type: docker}` resolves to an `SshBackend` whose `isolation_kind == "docker"`; `isolation: bare` (or absent) → `isolation_kind == "bare"`. The returned object is an `SshBackend` in BOTH cases (so orchestrator ssh-branches still apply).

- [ ] **Step 1: Write failing test**

Add to `tests/test_backend_resolver.py`:

```python
from maestro.execution.exec_config import (
    BackendSpec, DockerIsolation, ExecutionConfig, SshTransport,
)
from maestro.execution.ssh_backend import SshBackend


def test_resolve_ssh_docker_backend():
    cfg = ExecutionConfig(
        default_backend="local",
        backends={
            "rs": BackendSpec(
                transport=SshTransport(type="ssh", host="h", workdir_root="/r"),
                isolation=DockerIsolation(type="docker", image="img:tag"),
                secret_env=["ANTHROPIC_API_KEY"],
            )
        },
    )
    backend = BackendResolver(cfg).resolve("rs")
    assert isinstance(backend, SshBackend)
    assert backend.isolation_kind == "docker"


def test_resolve_ssh_bare_backend():
    cfg = ExecutionConfig(
        default_backend="local",
        backends={
            "rb": BackendSpec(
                transport=SshTransport(type="ssh", host="h", workdir_root="/r"),
                isolation=BareIsolation(),
            )
        },
    )
    backend = BackendResolver(cfg).resolve("rb")
    assert isinstance(backend, SshBackend)
    assert backend.isolation_kind == "bare"
```

(Import `BareIsolation` and `BackendResolver` as the file already does.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_backend_resolver.py -k "ssh_docker or ssh_bare" -v`
Expected: FAIL (`isolation_kind` always bare — `_build_ssh` drops isolation).

- [ ] **Step 3: Implement**

Replace `_build_ssh` in `maestro/execution/resolver.py`:

```python
    def _build_ssh(
        self, name: str, spec: BackendSpec, transport: SshTransport
    ) -> ExecutionBackend:
        from maestro.execution.ssh_backend import SshBackend

        isolation = None
        if isinstance(spec.isolation, DockerIsolation):
            isolation = spec.isolation
        return SshBackend(
            name,
            transport,
            secret_env=self._execution.effective_secret_env(name),
            isolation=isolation,
        )
```

(The `_spec` param is now used — rename `_spec` → `spec`.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_backend_resolver.py tests/test_backend_resolver_registry.py -v && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/execution/resolver.py tests/test_backend_resolver.py
git commit -m "feat(ssh-docker): resolver wires DockerIsolation onto SshBackend"
```

---

### Task 11: Example config + CLAUDE.md note + observability field

**Files:**
- Create: `examples/with-ssh-docker.yaml`
- Modify: `maestro/CLAUDE.md` (Communication/Design-decisions bullet)
- Modify: `maestro/execution/ssh_backend.py` (add `isolation`/`image`/`host` to the `execution.dispatch` span if a span exists there; else skip — do not invent instrumentation)
- Test: `tests/test_examples_smoke.py` already parametrizes `examples/*.yaml` — the new file must load.

**Interfaces:**
- Consumes: `ExecutionConfig` schema (unchanged; the example only exercises existing fields).

- [ ] **Step 1: Write the example (self-checked by the existing smoke test)**

Create `examples/with-ssh-docker.yaml`:

```yaml
# Mode-2 orchestrator: run a workstream inside a Docker container on a remote
# host over SSH (Phase 2c). The harness lives in the image; the remote host
# only needs docker + rsync + git + python3.
name: ssh-docker-demo
repo_path: ~/code/demo
base_branch: main

execution:
  default_backend: local
  secret_env_defaults: [ANTHROPIC_API_KEY]
  backends:
    remote-sandbox:
      transport:
        type: ssh
        host: gpu-box                # ssh config alias or bare host (no user@ / :port)
        workdir_root: /var/tmp/maestro
        connect_timeout_s: 10
      isolation:
        type: docker
        image: ghcr.io/andrei-shtanakov/maestro-runner:2026-07-21
        network: none
        memory: 8g
        # user omitted -> container runs as the remote SSH user's uid:gid.
        # Set user: "0:0" only as a deliberate root opt-in.
      secret_env: [ANTHROPIC_API_KEY]
      inherit_secret_defaults: true

workstreams:
  - id: api
    backend: remote-sandbox
    description: Implement the API layer
    scope: ["src/api/**"]
```

- [ ] **Step 2: Run the smoke test to verify it loads**

Run: `uv run pytest tests/test_examples_smoke.py -k ssh_docker -v`
Expected: PASS (Mode-2 `load_orchestrator_config` + `validate_project(check_fs=False)`). If the smoke test needs an `observed-models.json` sibling or a dummy env var, mirror the pattern already used by `examples/with-ssh.yaml`.

- [ ] **Step 3: CLAUDE.md note**

In `maestro/CLAUDE.md`, under the distributed-execution bullet in "Key Design Decisions", append:

```
Phase 2c enables **SSH + Docker isolation** (`transport: ssh` + `isolation: {type: docker}`): the center rewrites the launch argv into a remote `docker run` (supervisor unchanged), drives container lifecycle/recovery via `DockerCli` run over SSH, defaults the container user to the remote uid:gid (root only via explicit `user: "0:0"`), and recovery probes BOTH the remote process-group and the container (fail-closed → NEEDS_REVIEW).
```

- [ ] **Step 4: Observability (only if a span already exists)**

Run: `grep -n "execution.dispatch\|obs.span" maestro/execution/ssh_backend.py maestro/orchestrator.py`
If an `execution.dispatch` span exists on the ssh dispatch path, add `isolation=self.isolation_kind`, `image`, `host` fields to it. If none exists, **skip** — do not add new instrumentation in this phase (YAGNI; the spec's observability is a "nice to have").

- [ ] **Step 5: Run + commit**

Run: `uv run pytest tests/test_examples_smoke.py -v`
Expected: PASS.

```bash
git add examples/with-ssh-docker.yaml maestro/CLAUDE.md maestro/execution/ssh_backend.py
git commit -m "docs(ssh-docker): example config + CLAUDE.md note"
```

---

### Task 12: Opt-in localhost-SSH + real-Docker e2e (marker-gated)

**Files:**
- Create: `tests/test_ssh_docker_integration.py`
- Reference: `tests/test_docker_integration.py` (marker/skip pattern), `tests/test_ssh_backend.py` (localhost-ssh pattern, if present).

**Interfaces:**
- Consumes: the full stack (T1–T10). Runs only when both a localhost sshd and a real docker daemon are available; otherwise skipped.

- [ ] **Step 1: Write the gated e2e**

Create `tests/test_ssh_docker_integration.py`:

```python
"""Opt-in e2e: SSH (localhost) + real Docker. Skipped unless
MAESTRO_SSH_DOCKER_E2E=1 and both `ssh localhost true` and `docker version`
succeed. Mirrors tests/test_docker_integration.py's gating.
"""

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MAESTRO_SSH_DOCKER_E2E") != "1",
    reason="set MAESTRO_SSH_DOCKER_E2E=1 to run the ssh+docker e2e",
)


def _preconditions() -> bool:
    if not shutil.which("ssh") or not shutil.which("docker"):
        return False
    ssh_ok = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "localhost", "true"], capture_output=True
    ).returncode == 0
    docker_ok = subprocess.run(
        ["docker", "version"], capture_output=True
    ).returncode == 0
    return ssh_ok and docker_ok


@pytest.mark.anyio
async def test_ssh_docker_end_to_end(tmp_path):
    if not _preconditions():
        pytest.skip("localhost sshd or docker daemon not available")
    # 1. init a git worktree with a trivial file under scope
    # 2. build SshBackend(host="localhost", isolation=docker image="alpine",
    #    inner argv=["sh","-c","echo hi > src/out.txt"])
    # 3. await run(); await handle.wait(); assert exit 0
    # 4. await handle.collect(); assert src/out.txt applied into the worktree
    # 5. await handle.cleanup(); assert `docker ps -aq --filter
    #    label=maestro.execution_id=<id>` is empty and the remote root is gone
    ...
```

Flesh out the `...` following `test_docker_integration.py`. Use `image: "alpine"` (no harness needed — the inner argv is a shell echo) so the test needs no custom image.

- [ ] **Step 2: Run (expect skip locally unless opted in)**

Run: `uv run pytest tests/test_ssh_docker_integration.py -v`
Expected: SKIPPED (unless `MAESTRO_SSH_DOCKER_E2E=1` + daemons present, then PASS).

- [ ] **Step 3: Commit**

```bash
git add tests/test_ssh_docker_integration.py
git commit -m "test(ssh-docker): opt-in localhost-ssh + real-docker e2e"
```

---

## Final verification (before opening the PR)

- [ ] Targeted foreground runs green: `uv run pytest tests/test_docker_cli.py tests/test_ssh_docker.py tests/test_ssh_docker_probe.py tests/test_ssh_launch.py tests/test_ssh_backend.py tests/test_ssh_handle.py tests/test_backend_resolver.py tests/test_docker_handle.py tests/test_docker_recovery.py tests/test_orchestrator_ssh_wiring.py tests/test_examples_smoke.py -v`
- [ ] `uv run pyrefly check` clean.
- [ ] `uv run ruff format . && uv run ruff check .` clean.
- [ ] `grep -rn "isolation: bare\|byte-identical" ` sanity: the bare ssh path and local docker path have no behavioral change beyond the documented fail-closed hardening.
- [ ] Remote supervisor source unchanged: `git diff --stat master -- maestro/execution/resources/maestro_supervisor.py` shows nothing.
- [ ] Push branch, `gh pr create`, address GitHub Copilot review, do NOT self-merge (project rule).

## Self-review notes (author)

- **Spec coverage:** §1 → T1,T2; §2 → T3,T5; §3 → T3,T6; §4 → T5; §5 → T6; §6 → T7,T8; §7 → T4,T9; §8 → T10; §9 → T11; Testing → each task + T12; Rollout → T1/T8 fail-closed notes + T11. All spec sections mapped.
- **Known interface seam:** T5 references `ContainerOps` before T7 defines it — flagged inline; implement T5→T7 back-to-back.
- **labels_match ownership:** lives in `docker_recovery.py` (T8), re-exported from `ssh_docker_probe.py` — one definition, no import cycle (recovery does not import the ssh module).
- **Honest placeholder disclosure (T9, T12):** the test bodies in Task 9 (orchestrator recovery) and Task 12 (opt-in e2e) are outlines with `...` because they must be built on the concrete fixtures already living in `tests/test_orchestrator_ssh_wiring.py` and `tests/test_docker_integration.py`, which this plan's author did not read line-by-line. **Before executing T9/T12, read those two files and concretize the fake-backend fixtures (must expose `isolation_kind` + `docker`) and the e2e steps.** Every other task (T1–T8, T10, T11) carries complete, runnable test code. If executing via subagents, give the T9/T12 implementer explicit permission to read the referenced fixtures first.
