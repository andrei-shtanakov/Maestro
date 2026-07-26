# Strict Docker verifier sandbox — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the Mode-1 verifier diff-judge inside a hardened, mount-less, digest-pinned Docker container (`verifier.backend: docker`), giving the judge real OS isolation while keeping `verifier.backend: local` byte-identical.

**Architecture:** A dedicated `VerifierDockerIsolator` builds a hardened launch policy (no project mount, `-i` stdin, `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, tmpfs `/scratch`, non-root numeric user, mem/cpu/pid limits, one `ANTHROPIC_API_KEY` env-file) but reuses the existing `DockerTaskHandle`/`DockerCli`/`docker_recovery`/`probe` lifecycle verbatim. A single `build_verifier_backend` factory serves both dispatch and recovery. An additive `Isolator.after_spawn` hook owns the eager env-file unlink. Eager global fail-loud preflight validates the image + CLI under the identical security profile. Recovery splits `open_handles` once by `execution_phase`, with a new all-status `_reconcile_verification_handles` owning verification handles (incl. credential-artifact crash cleanup).

**Tech Stack:** Python 3.12+, pydantic v2, asyncio subprocess, Docker CLI, SQLite (aiosqlite), pytest + anyio, uv.

**Spec:** `docs/superpowers/specs/2026-07-26-verifier-docker-sandbox-design.md` (authoritative; every task cites its sections).

## Global Constraints

- **No new architectural decisions.** Implement the ratified contract exactly; if the spec seems to leave a choice, it does not — re-read it. Escalate genuine gaps, never invent.
- **`verifier.backend: local` and any config without a `verifier:` block stay byte-identical** to post-#108 behavior. No new phase, no Docker, no preflight on those paths.
- **Hardening flags are not configurable** — `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, the tmpfs `nosuid,nodev,noexec`, `-i`, `--workdir /scratch`, and the absence of a project mount are baked into `VerifierDockerIsolator`, never fields.
- **`verifier-docker` is never registered in `ExecutionConfig.normalized()` / the general `BackendResolver`.** It is reachable only via `build_verifier_backend`.
- **Resource bounds are contract (spec §4.2):** image `^[^@\s]+@sha256:[0-9a-f]{64}$`; user `^\d+:\d+$` with uid≠0 and gid≠0; memory `128m..8g`; cpus finite `0.1..8`; pids_limit `16..4096`; tmpfs_size `16m..1g`. Docker sizes normalized to bytes before range-checking; decimals finite-only (no exponent/NaN/Infinity). `ANTHROPIC_API_KEY` must be present, non-empty, no NUL/CR/LF.
- **`DockerTaskHandle`, `DockerCli`, `docker_recovery`, `finalize_handle`, `ClaudeDiffJudge`, and the verdict/handshake contract are reused unchanged.** Stage B `maestro/domain/` is frozen (additive-only) — this slice does not edit it.
- **pytest runs FOREGROUND ONLY** (a workspace watchdog kills backgrounded pytest). Every test that opens a `Database` must close it (`try/finally: await db.close()` or a fixture that does).
- **`uv` only, never pip.** After each task: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`, all clean.
- **Line length 88; type hints on all new code; docstrings on public APIs.**
- Docker-dependent tests are gated behind a skip-if-no-docker marker so the default suite (no Docker) stays green.

---

## File Structure

**New files:**
- `maestro/verifier/docker_config.py` — `VerifierDockerConfig` + bounded validators + a `_parse_docker_size_bytes` helper.
- `maestro/verifier/docker_backend.py` — `VerifierDockerIsolator` (argv, `materialize`, `after_spawn`) + `build_verifier_backend` factory + the deterministic `verifier_exec_dir(root, execution_id)` path helper.
- `maestro/verifier/preflight.py` — `run_verifier_docker_preflight()` (halt matrix) + probe container runner.
- `tests/verifier/test_docker_config.py`, `test_docker_isolator.py`, `test_after_spawn.py`, `test_backend_factory.py`, `test_preflight.py`, `test_recovery_split.py`, `test_credential_cleanup.py`
- `tests/integration/test_verifier_docker_integration.py` (docker-gated), `test_verifier_docker_smoke.py` (opt-in authenticated)
- `examples/with-verifier-docker.yaml`

**Modified files:**
- `maestro/models.py` — `VerifierConfig.backend: Literal["local","docker"]` + `docker: VerifierDockerConfig | None` + cross-field validator.
- `maestro/execution/isolators.py` — add `after_spawn` to the `Isolator` Protocol + no-op impls on `BareIsolator`/`DockerIsolator`.
- `maestro/execution/local.py` — `LocalBackend.run()` awaits `after_spawn` before returning; fail-closed on hook error.
- `maestro/scheduler.py` — `_run_verifier` uses the factory; `_check_verifier_model` runs the docker preflight; scheduler holds a `DockerCli` for the verifier path.
- `maestro/database.py` — new `get_open_verification_handles()`.
- `maestro/recovery.py` — one-time `open_handles` split by phase; new `_reconcile_verification_handles`; `_recover_verifying_tasks` trimmed to FSM routing; GC receives only `general_handles`; `StateRecovery.__init__` takes `verifier` + shared `DockerCli`.
- `maestro/cli.py` — pass `verifier=config.verifier` into `StateRecovery`.
- `CLAUDE.md` — document `verifier.backend: docker`.

---

## Task 1: `VerifierDockerConfig` + bounded validation

**Files:**
- Create: `maestro/verifier/docker_config.py`
- Create: `tests/verifier/test_docker_config.py`
- Modify: `maestro/models.py` (extend `VerifierConfig`)

**Interfaces:**
- Produces: `VerifierDockerConfig(image, user, memory="512m", cpus="1", pids_limit=128, tmpfs_size="64m")`; `_parse_docker_size_bytes(s: str) -> int`; `VerifierConfig.backend: Literal["local","docker"]`, `VerifierConfig.docker: VerifierDockerConfig | None`.

- [ ] **Step 1: Write the failing test** — `tests/verifier/test_docker_config.py`

```python
import pytest
from pydantic import ValidationError

from maestro.models import VerifierConfig
from maestro.verifier.docker_config import (
    VerifierDockerConfig,
    _parse_docker_size_bytes,
)

_DIGEST = "example.com/img@sha256:" + "a" * 64


def _cfg(**over):
    base = {"image": _DIGEST, "user": "1000:1000"}
    base.update(over)
    return VerifierDockerConfig(**base)


def test_defaults_are_bounded_and_valid():
    c = _cfg()
    assert c.memory == "512m" and c.cpus == "1"
    assert c.pids_limit == 128 and c.tmpfs_size == "64m"


def test_parse_docker_size_bytes():
    assert _parse_docker_size_bytes("128m") == 128 * 1024 * 1024
    assert _parse_docker_size_bytes("8g") == 8 * 1024**3
    assert _parse_docker_size_bytes("512") == 512
    with pytest.raises(ValueError):
        _parse_docker_size_bytes("1e6")
    with pytest.raises(ValueError):
        _parse_docker_size_bytes("")


@pytest.mark.parametrize(
    "over",
    [
        {"image": "example.com/img:latest"},   # bare tag, no digest
        {"image": "img@sha256:short"},          # bad digest
        {"user": "root"},                        # symbolic
        {"user": "0:0"},                          # uid 0
        {"user": "1000:0"},                       # gid 0
        {"user": "1000"},                         # not uid:gid
        {"memory": "0"},                          # zero
        {"memory": "16g"},                        # over 8g
        {"memory": "64k"},                        # under 128m
        {"cpus": "0"},                            # zero
        {"cpus": "9"},                            # over 8
        {"cpus": "1e1"},                          # exponent
        {"cpus": "inf"},                          # non-finite
        {"pids_limit": 0},                        # zero
        {"pids_limit": -1},                       # docker "unlimited"
        {"pids_limit": 5000},                     # over 4096
        {"tmpfs_size": "8m"},                     # under 16m
        {"tmpfs_size": "2g"},                     # over 1g
    ],
)
def test_rejects_out_of_contract(over):
    with pytest.raises(ValidationError):
        _cfg(**over)


def test_verifier_config_backend_docker_requires_block():
    with pytest.raises(ValidationError):
        VerifierConfig(backend="docker", model="m")  # no docker block


def test_verifier_config_local_with_docker_block_rejected():
    with pytest.raises(ValidationError):
        VerifierConfig(backend="local", model="m", docker=_cfg())


def test_verifier_config_docker_ok():
    c = VerifierConfig(backend="docker", model="m", docker=_cfg())
    assert c.backend == "docker" and c.docker is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run (foreground): `uv run pytest tests/verifier/test_docker_config.py -v`
Expected: FAIL with `ModuleNotFoundError: maestro.verifier.docker_config`.

- [ ] **Step 3: Write `maestro/verifier/docker_config.py`**

```python
"""Bounded config for the strict Docker verifier sandbox (spec §4.2).

Tuning knobs have hard secure defaults and are range-checked so they can
never express "no limit"; hardening flags are NOT here — they are baked into
`VerifierDockerIsolator`.
"""

from __future__ import annotations

import math
import re

from pydantic import BaseModel, ConfigDict, field_validator

_IMAGE_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
_USER_RE = re.compile(r"^\d+:\d+$")
_SIZE_RE = re.compile(r"^(\d+)([bkmg]?)$", re.IGNORECASE)
_DECIMAL_RE = re.compile(r"^\d+(\.\d+)?$")

_UNIT = {"b": 1, "": 1, "k": 1024, "m": 1024**2, "g": 1024**3}

_MEM_MIN, _MEM_MAX = 128 * 1024**2, 8 * 1024**3
_TMPFS_MIN, _TMPFS_MAX = 16 * 1024**2, 1 * 1024**3
_CPUS_MIN, _CPUS_MAX = 0.1, 8.0
_PIDS_MIN, _PIDS_MAX = 16, 4096


def _parse_docker_size_bytes(value: str) -> int:
    """Parse a Docker size string (`128m`, `8g`, `512`) to bytes.

    Raises ValueError for an unparseable/exponent/empty value.
    """
    match = _SIZE_RE.match(value.strip())
    if match is None:
        raise ValueError(f"not a Docker size: {value!r}")
    return int(match.group(1)) * _UNIT[match.group(2).lower()]


class VerifierDockerConfig(BaseModel):
    """Tuning for the hardened verifier container (bounds are contract)."""

    model_config = ConfigDict(extra="forbid")

    image: str
    user: str
    memory: str = "512m"
    cpus: str = "1"
    pids_limit: int = 128
    tmpfs_size: str = "64m"

    @field_validator("image")
    @classmethod
    def _digest_pinned(cls, value: str) -> str:
        if _IMAGE_RE.match(value) is None:
            raise ValueError(f"image must be digest-pinned image@sha256:<64hex>: {value!r}")
        return value

    @field_validator("user")
    @classmethod
    def _numeric_nonroot(cls, value: str) -> str:
        if _USER_RE.match(value) is None:
            raise ValueError(f"user must be numeric 'uid:gid': {value!r}")
        uid, gid = (int(p) for p in value.split(":"))
        if uid == 0 or gid == 0:
            raise ValueError(f"user must be non-root (uid!=0, gid!=0): {value!r}")
        return value

    @field_validator("memory")
    @classmethod
    def _memory_bounds(cls, value: str) -> str:
        if not _MEM_MIN <= _parse_docker_size_bytes(value) <= _MEM_MAX:
            raise ValueError(f"memory must be within 128m..8g: {value!r}")
        return value

    @field_validator("tmpfs_size")
    @classmethod
    def _tmpfs_bounds(cls, value: str) -> str:
        if not _TMPFS_MIN <= _parse_docker_size_bytes(value) <= _TMPFS_MAX:
            raise ValueError(f"tmpfs_size must be within 16m..1g: {value!r}")
        return value

    @field_validator("cpus")
    @classmethod
    def _cpus_bounds(cls, value: str) -> str:
        if _DECIMAL_RE.match(value.strip()) is None:
            raise ValueError(f"cpus must be a finite decimal: {value!r}")
        parsed = float(value)
        if not math.isfinite(parsed) or not _CPUS_MIN <= parsed <= _CPUS_MAX:
            raise ValueError(f"cpus must be within 0.1..8: {value!r}")
        return value

    @field_validator("pids_limit")
    @classmethod
    def _pids_bounds(cls, value: int) -> int:
        if not _PIDS_MIN <= value <= _PIDS_MAX:
            raise ValueError(f"pids_limit must be within 16..4096: {value!r}")
        return value
```

- [ ] **Step 4: Extend `VerifierConfig` in `maestro/models.py`**

Add the import near the other verifier imports (top of the module, keeping import order — ruff `I001` will sort):
```python
from maestro.verifier.docker_config import VerifierDockerConfig
```
Change the `backend` field and add `docker` + a cross-field validator inside `class VerifierConfig`:
```python
    backend: Literal["local", "docker"] = "local"
    docker: VerifierDockerConfig | None = None

    @model_validator(mode="after")
    def _backend_docker_coherent(self) -> "VerifierConfig":
        if self.backend == "docker" and self.docker is None:
            raise ValueError("verifier.backend='docker' requires a verifier.docker block")
        if self.backend == "local" and self.docker is not None:
            raise ValueError("verifier.docker is set but backend is not 'docker'")
        return self
```
Ensure `model_validator` is imported from pydantic in `models.py` (it is already used elsewhere; if not, add it). Watch for an import cycle: `maestro.verifier.docker_config` imports only pydantic/stdlib, so importing it into `models.py` is safe.

- [ ] **Step 5: Run tests + quality gates**

Run: `uv run pytest tests/verifier/test_docker_config.py -v`
Expected: PASS.
Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add maestro/verifier/docker_config.py maestro/models.py tests/verifier/test_docker_config.py
git commit -m "feat(verifier): VerifierDockerConfig with contract-bounded validation"
```

---

## Task 2: `after_spawn` protocol hook + no-op impls + `LocalBackend.run` ordering

**Files:**
- Modify: `maestro/execution/isolators.py` (add `after_spawn` to `Isolator` Protocol; no-op on `BareIsolator`, `DockerIsolator`)
- Modify: `maestro/execution/local.py` (`LocalBackend.run` awaits the hook, fail-closed)
- Test: `tests/verifier/test_after_spawn.py`

**Interfaces:**
- Produces: `Isolator.after_spawn(self, prepared: PreparedRun, proc: asyncio.subprocess.Process) -> Awaitable[None]`; a `VerifierLaunchError` exception class in `maestro/execution/local.py` for a fail-closed post-spawn failure.
- Consumes: `LocalBackend.run` from Task-1's config (none); `PreparedRun` from `maestro/execution/models.py`.

- [ ] **Step 1: Write the failing test** — `tests/verifier/test_after_spawn.py`

```python
import asyncio

import pytest

from maestro.execution.isolators import BareIsolator, DockerIsolator
from maestro.execution.exec_config import DockerConfig
from maestro.execution.models import PreparedRun, PreparedRunPlan

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _fake_proc():
    # A trivial real subprocess we can pass as the `proc` argument.
    return await asyncio.create_subprocess_exec("true")


async def test_bare_after_spawn_is_noop():
    proc = await _fake_proc()
    prepared = PreparedRun(plan=PreparedRunPlan(argv=["true"]))
    await BareIsolator().after_spawn(prepared, proc)  # must not raise
    await proc.wait()


async def test_docker_after_spawn_is_noop():
    proc = await _fake_proc()
    iso = DockerIsolator(DockerConfig(image="img"))
    prepared = PreparedRun(plan=PreparedRunPlan(argv=["true"]))
    await iso.after_spawn(prepared, proc)  # must not raise
    await proc.wait()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/verifier/test_after_spawn.py -v`
Expected: FAIL with `AttributeError: 'BareIsolator' object has no attribute 'after_spawn'`.

- [ ] **Step 3: Add `after_spawn` to the protocol + no-op impls** in `maestro/execution/isolators.py`

Add `import asyncio` at the top. In the `Isolator` Protocol add:
```python
    async def after_spawn(
        self, prepared: PreparedRun, proc: asyncio.subprocess.Process
    ) -> None: ...
```
Add to `BareIsolator` and `DockerIsolator` a no-op (behavior byte-unchanged — their env-file, if any, is still cleaned at end by `DockerTaskHandle.cleanup`):
```python
    async def after_spawn(
        self, prepared: PreparedRun, proc: asyncio.subprocess.Process
    ) -> None:
        """No-op: this isolator has no post-spawn credential handoff."""
        return None
```

- [ ] **Step 4: Wire `after_spawn` into `LocalBackend.run`** in `maestro/execution/local.py`

Add a `VerifierLaunchError(RuntimeError)` at module scope (used by Task 3's isolator to signal a fail-closed launch). In `run()`, after the stdin write block and BEFORE building `ref`, insert:
```python
        try:
            await self._isolator.after_spawn(prepared, proc)
        except BaseException:
            # Fail-closed: a post-spawn hook failure (e.g. the verifier
            # credential handoff never confirmed) must not hand back a live
            # handle. Kill the process and clean the isolator's artifacts.
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            _cleanup_prepared(prepared)
            raise
```
No-op isolators make this a behavior-preserving addition for bare/general-docker/local.

- [ ] **Step 5: Run tests + quality gates**

Run: `uv run pytest tests/verifier/test_after_spawn.py -v`
Expected: PASS.
Run the broad local/isolator suites to prove no regression:
`uv run pytest tests/ -k "local or isolator or docker" -q`
Expected: PASS (no behavior change on existing backends).
Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`

- [ ] **Step 6: Commit**

```bash
git add maestro/execution/isolators.py maestro/execution/local.py tests/verifier/test_after_spawn.py
git commit -m "feat(execution): additive Isolator.after_spawn hook (no-op on bare/docker)"
```

---

## Task 3: `VerifierDockerIsolator` — hardened argv, materialize, eager env-file unlink

**Files:**
- Create: `maestro/verifier/docker_backend.py` (isolator + `verifier_exec_dir` helper; factory added in Task 4)
- Test: `tests/verifier/test_docker_isolator.py`

**Interfaces:**
- Consumes: `VerifierDockerConfig` (Task 1); `Isolator`/`DockerTaskHandle`/`DockerCli`/`write_env_file` patterns from `maestro/execution/isolators.py` + `secret_file.py`; `VerifierLaunchError` (Task 2).
- Produces: `VerifierDockerIsolator(cfg: VerifierDockerConfig, *, exec_root: Path, docker: DockerCli | None = None)`; `verifier_exec_dir(exec_root: Path, execution_id: str) -> Path`; `_security_flags(cfg: VerifierDockerConfig, uid: str, gid: str) -> list[str]` (reads `cfg.tmpfs_size`/limits internally; shared verbatim by production argv and the Task-6 probe so the security profile is identical); constant `ANTHROPIC_ENV_KEY = "ANTHROPIC_API_KEY"`.

**Design anchors (spec §3.2, §5, §3.5, §7.3):**
- Production argv exactly as spec §5. No `-v` mount. `-i`. Synthetic env HOME/TMPDIR/XDG_* → `/scratch/...`. Only `ANTHROPIC_API_KEY` via `--env-file`. Labels + cleanup_paths mirror `DockerIsolator` so `docker_recovery.labels_match`/`DockerTaskHandle` are reused.
- Deterministic temp-dir `verifier_exec_dir(exec_root, execution_id)` = `exec_root / f"maestro-verify-{execution_id}"` (stable root from the caller, NOT ambient TMPDIR).
- `after_spawn`: bounded-wait for the cidfile, then `unlink` the env-file; on timeout/early-exit before cidfile → raise `VerifierLaunchError`.

- [ ] **Step 1: Write the failing test** — `tests/verifier/test_docker_isolator.py`

```python
import asyncio
from pathlib import Path

import pytest

from maestro.execution.models import ExecutionRequest, CollectPolicy
from maestro.verifier.docker_backend import (
    ANTHROPIC_ENV_KEY,
    VerifierDockerIsolator,
    verifier_exec_dir,
)
from maestro.verifier.docker_config import VerifierDockerConfig

pytestmark = pytest.mark.anyio

_DIGEST = "example.com/img@sha256:" + "a" * 64


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _req(execution_id="e1", workdir=Path("/tmp/scratch-x")):
    return ExecutionRequest(
        run_id="verify-t1-1",
        argv=["claude", "-p", "PROMPT", "--output-format", "json", "--model", "m"],
        workdir=workdir,
        log_path=workdir / "judge.log",
        stdin="ENVELOPE",
        collect=CollectPolicy(mode="none"),
        execution_id=execution_id,
        entity_kind="task",
        attempt=1,
        backend_id="verifier-docker",
    )


def _iso(tmp_path):
    cfg = VerifierDockerConfig(image=_DIGEST, user="1000:1000")
    return VerifierDockerIsolator(cfg, exec_root=tmp_path)


def test_production_argv_has_full_hardening(tmp_path):
    iso = _iso(tmp_path)
    plan = iso.prepare(_req(), trace_env={}, host_env={ANTHROPIC_ENV_KEY: "sk-x"})
    a = plan.argv
    assert a[0:2] == ["docker", "run"]
    assert "-i" in a
    assert "--read-only" in a
    assert "--cap-drop=ALL" in a
    assert "--security-opt=no-new-privileges" in a
    assert "--user" in a and "1000:1000" in a
    assert "--workdir" in a and "/scratch" in a
    assert "--network" in a and "bridge" in a
    assert "--pids-limit" in a
    # tmpfs owned by the effective user, noexec/nosuid/nodev
    tmpfs = a[a.index("--tmpfs") + 1]
    assert tmpfs.startswith("/scratch:rw,nosuid,nodev,noexec,")
    assert "mode=0700" in tmpfs and "uid=1000" in tmpfs and "gid=1000" in tmpfs
    # NO project bind mount
    assert not any(tok == "-v" for tok in a)
    # digest image present, judge command tail present
    assert _DIGEST in a
    assert a[-6:] == ["claude", "-p", "PROMPT", "--output-format", "json", "--model", "m"]


def test_synthetic_env_no_host_passthrough(tmp_path):
    iso = _iso(tmp_path)
    plan = iso.prepare(
        _req(),
        trace_env={},
        host_env={ANTHROPIC_ENV_KEY: "sk-x", "HOME": "/Users/real", "GH_TOKEN": "t"},
    )
    joined = " ".join(plan.argv)
    assert "HOME=/scratch" in joined
    assert "XDG_CONFIG_HOME=/scratch/.config" in joined
    assert "/Users/real" not in joined          # host HOME never leaks
    assert "GH_TOKEN" not in joined              # denylisted, never present
    # secret goes via --env-file, value never in argv
    assert "--env-file" in plan.argv
    assert "sk-x" not in joined


def test_deterministic_exec_dir(tmp_path):
    assert verifier_exec_dir(tmp_path, "e1") == tmp_path / "maestro-verify-e1"


async def test_after_spawn_unlinks_env_file_once_cidfile_appears(tmp_path, monkeypatch):
    iso = _iso(tmp_path)
    plan = iso.prepare(_req(), trace_env={}, host_env={ANTHROPIC_ENV_KEY: "sk-x"})
    monkeypatch.setenv(ANTHROPIC_ENV_KEY, "sk-x")
    prepared = iso.materialize(plan)
    assert prepared.env_file is not None and prepared.env_file.exists()
    cidfile = plan.cidfile_path
    proc = await asyncio.create_subprocess_exec("sleep", "5")

    async def _touch_cid():
        await asyncio.sleep(0.2)
        cidfile.write_text("deadbeef")

    task = asyncio.create_task(_touch_cid())
    await iso.after_spawn(prepared, proc)
    await task
    assert not prepared.env_file.exists()   # eagerly unlinked
    proc.kill()
    await proc.wait()


async def test_after_spawn_fails_closed_when_cidfile_never_appears(tmp_path, monkeypatch):
    iso = _iso(tmp_path)
    # short bound for the test
    monkeypatch.setattr("maestro.verifier.docker_backend._CIDFILE_WAIT_SECONDS", 0.3)
    plan = iso.prepare(_req(), trace_env={}, host_env={ANTHROPIC_ENV_KEY: "sk-x"})
    monkeypatch.setenv(ANTHROPIC_ENV_KEY, "sk-x")
    prepared = iso.materialize(plan)
    proc = await asyncio.create_subprocess_exec("sleep", "5")
    from maestro.execution.local import VerifierLaunchError

    with pytest.raises(VerifierLaunchError):
        await iso.after_spawn(prepared, proc)
    proc.kill()
    await proc.wait()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/verifier/test_docker_isolator.py -v`
Expected: FAIL (`ModuleNotFoundError: maestro.verifier.docker_backend`).

- [ ] **Step 3: Write `maestro/verifier/docker_backend.py` (isolator portion)**

```python
"""VerifierDockerIsolator: hardened, mount-less launch policy for the judge.

Reuses DockerTaskHandle/DockerCli/docker_recovery lifecycle verbatim (via
LocalBackend.wrap); only the argv/mount/stdin/env construction differs from
the general DockerIsolator (spec §3.2, §5). Owns the eager env-file unlink
(spec §3.5) and the deterministic credential temp-dir (spec §7.3).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from maestro.execution.backend import TaskHandle
from maestro.execution.docker_cli import DockerCli
from maestro.execution.local import LocalTaskHandle, VerifierLaunchError
from maestro.execution.models import (
    ExecutionHandleRef,
    ExecutionRequest,
    PreparedRun,
    PreparedRunPlan,
)
from maestro.execution.secret_file import write_env_file
from maestro.verifier.docker_config import VerifierDockerConfig

if TYPE_CHECKING:
    pass

ANTHROPIC_ENV_KEY = "ANTHROPIC_API_KEY"
_CIDFILE_WAIT_SECONDS = 30.0
_CIDFILE_POLL_SECONDS = 0.1


def verifier_exec_dir(exec_root: Path, execution_id: str) -> Path:
    """Deterministic per-execution temp-dir under the dedicated verifier root."""
    return exec_root / f"maestro-verify-{execution_id}"


def _security_flags(cfg: VerifierDockerConfig, uid: str, gid: str) -> list[str]:
    """The hardening flag list shared by production launch and preflight probe.

    Baked, non-configurable (spec §3.2): read-only root, cap-drop, no-new-
    privileges, nosuid/nodev/noexec user-owned tmpfs, resource limits,
    bridge network, non-root user, /scratch workdir.
    """
    tmpfs = (
        f"/scratch:rw,nosuid,nodev,noexec,size={cfg.tmpfs_size},"
        f"mode=0700,uid={uid},gid={gid}"
    )
    return [
        "--read-only",
        "--tmpfs", tmpfs,
        "--workdir", "/scratch",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user", cfg.user,
        "--memory", cfg.memory,
        "--cpus", cfg.cpus,
        "--pids-limit", str(cfg.pids_limit),
        "--network", "bridge",
    ]


def _synthetic_env() -> dict[str, str]:
    """Maestro-authored container env (NOT host passthrough, spec §2.2)."""
    return {
        "HOME": "/scratch",
        "TMPDIR": "/scratch",
        "XDG_CONFIG_HOME": "/scratch/.config",
        "XDG_CACHE_HOME": "/scratch/.cache",
    }


class VerifierDockerIsolator:
    """Hardened isolator for the verifier judge. `id = "docker"` so
    DockerTaskHandle/docker_recovery treat it exactly like the general
    docker path; the *backend id* (`verifier-docker`) provides identity
    separation (set by the factory, Task 4)."""

    id = "docker"

    def __init__(
        self,
        cfg: VerifierDockerConfig,
        *,
        exec_root: Path,
        docker: DockerCli | None = None,
    ) -> None:
        self._cfg = cfg
        self._exec_root = exec_root
        self._docker = docker or DockerCli()

    def prepare(
        self,
        req: ExecutionRequest,
        *,
        trace_env: Mapping[str, str],
        host_env: Mapping[str, str],
    ) -> PreparedRunPlan:
        if req.execution_id is None:
            raise ValueError("VerifierDockerIsolator requires req.execution_id")
        uid, gid = self._cfg.user.split(":")
        name = f"maestro-{req.execution_id}"
        tmp_dir = verifier_exec_dir(self._exec_root, req.execution_id)
        cidfile = tmp_dir / "cid"
        env_file = tmp_dir / "env"

        # Exactly one secret key, only if present on the host.
        secret_keys = [ANTHROPIC_ENV_KEY] if host_env.get(ANTHROPIC_ENV_KEY) else []
        labels = {
            "maestro.execution_id": req.execution_id,
            "maestro.entity_kind": req.entity_kind or "task",
            "maestro.entity_id": req.run_id,
            "maestro.attempt": str(req.attempt),
            "maestro.backend_id": "verifier-docker",
        }
        argv: list[str] = [
            "docker", "run", "-i",
            "--name", name,
            "--cidfile", str(cidfile),
            *_security_flags(self._cfg, uid, gid),
        ]
        if secret_keys:
            argv += ["--env-file", str(env_file)]
        for key, value in {**_synthetic_env(), **dict(trace_env)}.items():
            argv += ["-e", f"{key}={value}"]
        for key, value in labels.items():
            argv += ["--label", f"{key}={value}"]
        argv.append(self._cfg.image)
        argv += list(req.argv)

        return PreparedRunPlan(
            argv=argv,
            env=dict(host_env),  # docker CLI subprocess env (PATH/DOCKER_HOST/...)
            container_name=name,
            labels=labels,
            env_file_keys=secret_keys,
            cidfile_path=cidfile,
            tmp_dir=tmp_dir,
        )

    def materialize(self, plan: PreparedRunPlan) -> PreparedRun:
        """Create the 0700 tmp-dir and, if a secret is planned, the 0600 env-file."""
        assert plan.tmp_dir is not None
        env_file: Path | None = None
        try:
            plan.tmp_dir.mkdir(parents=True, exist_ok=True)
            plan.tmp_dir.chmod(0o700)
            if plan.env_file_keys:
                env_file = plan.tmp_dir / "env"
                write_env_file(env_file, plan.env_file_keys, os.environ)
        except Exception:
            shutil.rmtree(plan.tmp_dir, ignore_errors=True)
            if env_file is not None:
                env_file.unlink(missing_ok=True)
            raise
        cleanup = [plan.tmp_dir] + ([env_file] if env_file is not None else [])
        return PreparedRun(plan=plan, env_file=env_file, cleanup_paths=cleanup)

    def transport_ref(self, prepared: PreparedRun, pid: int) -> str:  # noqa: ARG002
        return f"docker:{prepared.plan.container_name}"

    async def after_spawn(
        self, prepared: PreparedRun, proc: asyncio.subprocess.Process
    ) -> None:
        """Bounded-wait for the cidfile, then eagerly unlink the env-file.

        The credential is consumed by `docker run --env-file` at container
        creation (cidfile appears). Deleting it then shrinks the on-disk
        window to the spawn window only (spec §3.5/§7.3). If the cidfile
        never appears within the bound (timeout / early process exit), raise
        VerifierLaunchError so LocalBackend.run fails closed.
        """
        cidfile = prepared.plan.cidfile_path
        if cidfile is None:  # not a verifier run; nothing to do
            return
        deadline = time.monotonic() + _CIDFILE_WAIT_SECONDS
        while time.monotonic() < deadline:
            if cidfile.exists():
                if prepared.env_file is not None:
                    prepared.env_file.unlink(missing_ok=True)
                return
            if proc.returncode is not None:  # exited before creating the container
                break
            await asyncio.sleep(_CIDFILE_POLL_SECONDS)
        # Fail-closed: never hand back a handle while the credential handoff
        # is unconfirmed. The env-file is removed by LocalBackend.run's
        # _cleanup_prepared on the raised path.
        raise VerifierLaunchError(
            f"verifier container never reported a cidfile for {prepared.plan.container_name}"
        )

    def wrap(
        self,
        local: LocalTaskHandle,
        prepared: PreparedRun,
        ref: ExecutionHandleRef,
    ) -> TaskHandle:
        """Reuse the general DockerTaskHandle verbatim (shared lifecycle)."""
        from maestro.execution.docker_handle import DockerTaskHandle

        return DockerTaskHandle(
            local=local,
            container_name=prepared.plan.container_name or "",
            expected_labels=prepared.plan.labels,
            cleanup_paths=prepared.cleanup_paths,
            docker=self._docker,
            ref=ref,
        )
```

Note for the implementer: `prepare` reads only its arguments (deterministic); secret VALUES are read in `materialize` via `write_env_file(..., os.environ)`, mirroring `DockerIsolator.materialize`. The `time`-based wait is fine (no `Date.now`/random constraints apply to product code).

- [ ] **Step 4: Run tests + quality gates**

Run: `uv run pytest tests/verifier/test_docker_isolator.py -v`
Expected: PASS.
Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`

- [ ] **Step 5: Commit**

```bash
git add maestro/verifier/docker_backend.py tests/verifier/test_docker_isolator.py
git commit -m "feat(verifier): VerifierDockerIsolator hardened argv + eager env-file unlink"
```

---

## Task 4: `build_verifier_backend` factory

**Files:**
- Modify: `maestro/verifier/docker_backend.py` (add the factory)
- Test: `tests/verifier/test_backend_factory.py`

**Interfaces:**
- Produces: `build_verifier_backend(verifier_cfg: VerifierConfig, *, local_backend: ExecutionBackend, exec_root: Path, docker_cli: DockerCli | None = None) -> ExecutionBackend`.
- Consumes: `VerifierDockerIsolator` (Task 3), `LocalBackend` (`maestro/execution/local.py`), `VerifierConfig` (Task 1).

Note: `exec_root` is added to the signature (the spec's factory has `local_backend` + `docker_cli`; the deterministic verifier root is a required construction input the factory threads into the isolator — dispatch and recovery pass the same root computed from the db-dir).

- [ ] **Step 1: Write the failing test** — `tests/verifier/test_backend_factory.py`

```python
from pathlib import Path

import pytest

from maestro.execution.local import LocalBackend
from maestro.models import VerifierConfig
from maestro.verifier.docker_backend import build_verifier_backend
from maestro.verifier.docker_config import VerifierDockerConfig

_DIGEST = "example.com/img@sha256:" + "a" * 64


class _Sentinel:
    id = "local"


def test_local_returns_passed_backend(tmp_path):
    passed = _Sentinel()
    cfg = VerifierConfig(backend="local", model="m")
    out = build_verifier_backend(cfg, local_backend=passed, exec_root=tmp_path)
    assert out is passed  # never a fresh LocalBackend


def test_docker_builds_verifier_docker(tmp_path):
    cfg = VerifierConfig(
        backend="docker", model="m",
        docker=VerifierDockerConfig(image=_DIGEST, user="1000:1000"),
    )
    fake_cli = object()
    out = build_verifier_backend(
        cfg, local_backend=_Sentinel(), exec_root=tmp_path, docker_cli=fake_cli
    )
    assert isinstance(out, LocalBackend)
    assert out.id == "verifier-docker"


def test_docker_requires_docker_cli(tmp_path):
    cfg = VerifierConfig(
        backend="docker", model="m",
        docker=VerifierDockerConfig(image=_DIGEST, user="1000:1000"),
    )
    with pytest.raises(ValueError):
        build_verifier_backend(
            cfg, local_backend=_Sentinel(), exec_root=tmp_path, docker_cli=None
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/verifier/test_backend_factory.py -v`
Expected: FAIL (`ImportError: cannot import name 'build_verifier_backend'`).

- [ ] **Step 3: Add the factory to `maestro/verifier/docker_backend.py`**

```python
def build_verifier_backend(
    verifier_cfg: "VerifierConfig",
    *,
    local_backend: "ExecutionBackend",
    exec_root: Path,
    docker_cli: DockerCli | None = None,
) -> "ExecutionBackend":
    """Build the verifier backend for BOTH dispatch and recovery (spec §3.4).

    `local` returns the passed backend verbatim (behavior-identical seam,
    honors injected/fake backends). `docker` builds a LocalBackend wrapping
    VerifierDockerIsolator with backend_id "verifier-docker"; `docker_cli` is
    required there (None → ValueError, never a hidden client).
    """
    if verifier_cfg.backend == "local":
        return local_backend
    if verifier_cfg.docker is None:  # defense-in-depth; config validator also guards
        raise ValueError("verifier.backend='docker' requires a verifier.docker block")
    if docker_cli is None:
        raise ValueError("verifier docker path requires an explicit DockerCli")
    from maestro.execution.local import LocalBackend

    isolator = VerifierDockerIsolator(
        verifier_cfg.docker, exec_root=exec_root, docker=docker_cli
    )
    return LocalBackend(isolator, backend_id="verifier-docker", docker=docker_cli)
```
Add the `TYPE_CHECKING` imports at the top: `from maestro.execution.backend import ExecutionBackend` and `from maestro.models import VerifierConfig` (both under `if TYPE_CHECKING:` to avoid an import cycle — `models` imports `docker_config`, not `docker_backend`).

- [ ] **Step 4: Run tests + quality gates**

Run: `uv run pytest tests/verifier/test_backend_factory.py -v`
Expected: PASS.
Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`

- [ ] **Step 5: Commit**

```bash
git add maestro/verifier/docker_backend.py tests/verifier/test_backend_factory.py
git commit -m "feat(verifier): build_verifier_backend factory (local passthrough, docker requires cli)"
```

---

## Task 5: Dispatch wiring — scheduler uses the factory

**Files:**
- Modify: `maestro/scheduler.py` (`_run_verifier` resolves via factory; hold a verifier `DockerCli` + `exec_root`; mint the handle with the right `backend_id`)
- Test: `tests/verifier/test_scheduler_verifier_backend.py`

**Interfaces:**
- Consumes: `build_verifier_backend` (Task 4); existing `self._backends`, `self._verifier`, `self._db`.
- Produces: `Scheduler._verifier_backend()` helper returning the resolved verifier backend; `self._verifier_exec_root: Path`.

**Anchor (spec §3.4, §5, current `scheduler.py:2155-2199`):** today `backend = self._backends.resolve("local")` and the handle is minted with `backend_id=backend.id`, `transport_ref=f"{backend.id}:verify-{execution_id}"`. Both must now come from the factory-built backend so a docker run persists `backend_id="verifier-docker"`. For docker the transport_ref must be the docker form the handle actually uses; the `start_execution` placeholder transport_ref is later overwritten by `update_execution_handle_launch` (see `ClaudeDiffJudge.verify`), so the placeholder only needs to be identity-correct. Use `transport_ref=f"docker:maestro-{execution_id}"` for the docker backend and keep `f"local:verify-{execution_id}"` for local.

- [ ] **Step 1: Write the failing test** — `tests/verifier/test_scheduler_verifier_backend.py`

```python
from pathlib import Path

import pytest

from maestro.models import VerifierConfig
from maestro.verifier.docker_config import VerifierDockerConfig

_DIGEST = "example.com/img@sha256:" + "a" * 64


def test_verifier_backend_local(monkeypatch, tmp_path):
    from maestro.scheduler import Scheduler
    sch = Scheduler.__new__(Scheduler)  # bypass full init; unit-probe the helper
    sch._verifier = VerifierConfig(backend="local", model="m")
    sentinel = object()
    sch._backends = type("R", (), {"resolve": lambda self, n: sentinel})()
    sch._verifier_docker_cli = None
    sch._verifier_exec_root = tmp_path
    assert sch._verifier_backend() is sentinel


def test_verifier_backend_docker(tmp_path):
    from maestro.scheduler import Scheduler
    from maestro.execution.local import LocalBackend
    sch = Scheduler.__new__(Scheduler)
    sch._verifier = VerifierConfig(
        backend="docker", model="m",
        docker=VerifierDockerConfig(image=_DIGEST, user="1000:1000"),
    )
    sch._backends = type("R", (), {"resolve": lambda self, n: object()})()
    sch._verifier_docker_cli = object()
    sch._verifier_exec_root = tmp_path
    out = sch._verifier_backend()
    assert isinstance(out, LocalBackend) and out.id == "verifier-docker"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/verifier/test_scheduler_verifier_backend.py -v`
Expected: FAIL (`AttributeError: 'Scheduler' object has no attribute '_verifier_backend'`).

- [ ] **Step 3: Wire the scheduler**

In `Scheduler.__init__` (near `self._backends = BackendResolver(...)`, `scheduler.py:319`), add:
```python
        self._verifier_exec_root = self._db_dir / "verifier-exec"
        self._verifier_docker_cli = (
            DockerCli()
            if (verifier is not None and verifier.backend == "docker")
            else None
        )
```
(Use the scheduler's existing db-dir attribute — confirm its name; if the scheduler stores the db path, derive `self._db_dir = Path(<db_path>).parent`. Import `DockerCli` from `maestro.execution.docker_cli` and `build_verifier_backend` from `maestro.verifier.docker_backend`.)

Add the helper:
```python
    def _verifier_backend(self) -> "ExecutionBackend":
        """Resolve the verifier backend via the single factory (spec §3.4)."""
        assert self._verifier is not None
        return build_verifier_backend(
            self._verifier,
            local_backend=self._backends.resolve("local"),
            exec_root=self._verifier_exec_root,
            docker_cli=self._verifier_docker_cli,
        )
```

In `_run_verifier` replace `backend = self._backends.resolve("local")` (`scheduler.py:2157`) with:
```python
        backend = self._verifier_backend()
```
and change the placeholder `transport_ref` mint (`scheduler.py:2165`) to be identity-correct for docker:
```python
            transport_ref=(
                f"docker:maestro-{execution_id}"
                if backend.id == "verifier-docker"
                else f"{backend.id}:verify-{execution_id}"
            ),
```
The `ClaudeDiffJudge(model=..., backend=backend, ...)` construction (`scheduler.py:2194`) already takes `backend` — it now receives the verifier-docker backend unchanged.

- [ ] **Step 4: Run tests + quality gates**

Run: `uv run pytest tests/verifier/test_scheduler_verifier_backend.py -v`
Then the existing verifier-gate suite to prove the local path is unchanged:
`uv run pytest tests/ -k verifier -q`
Expected: PASS.
Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`

- [ ] **Step 5: Commit**

```bash
git add maestro/scheduler.py tests/verifier/test_scheduler_verifier_backend.py
git commit -m "feat(verifier): dispatch resolves judge backend via build_verifier_backend"
```

---

## Task 6: Eager global fail-loud preflight

**Files:**
- Create: `maestro/verifier/preflight.py`
- Modify: `maestro/scheduler.py` (`_check_verifier_model` runs the docker preflight)
- Test: `tests/verifier/test_preflight.py`

**Interfaces:**
- Produces: `async def run_verifier_docker_preflight(cfg: VerifierDockerConfig, *, docker: DockerCli, env: Mapping[str,str], timeout_s: float = 20.0) -> str` — returns the inspected image ID on success; raises `VerifierPreflightError` on any halt-matrix row. `class VerifierPreflightError(RuntimeError)`.
- Consumes: `DockerCli` (`version_ok`, `image_exists`), `_security_flags`/`ANTHROPIC_ENV_KEY` (Task 3).

**Anchor (spec §6):** halt matrix rows → each raises. Probe uses the identical security profile with `--rm` + `claude --version`, NO `-i`, NO `--env-file`, unique name/label, guaranteed cleanup on timeout, short dedicated timeout, no `ANTHROPIC_API_KEY`. Records the inspected image ID.

- [ ] **Step 1: Write the failing test** — `tests/verifier/test_preflight.py`

```python
import pytest

from maestro.verifier.docker_config import VerifierDockerConfig
from maestro.verifier.preflight import (
    VerifierPreflightError,
    run_verifier_docker_preflight,
)

pytestmark = pytest.mark.anyio
_DIGEST = "example.com/img@sha256:" + "a" * 64


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _cfg():
    return VerifierDockerConfig(image=_DIGEST, user="1000:1000")


class _FakeDocker:
    def __init__(self, *, version=True, image=True):
        self._v, self._i = version, image
    async def version_ok(self):
        return self._v
    async def image_exists(self, image):
        return self._i


async def test_missing_api_key_halts():
    with pytest.raises(VerifierPreflightError, match="ANTHROPIC_API_KEY"):
        await run_verifier_docker_preflight(_cfg(), docker=_FakeDocker(), env={})


async def test_blank_api_key_halts():
    with pytest.raises(VerifierPreflightError):
        await run_verifier_docker_preflight(
            _cfg(), docker=_FakeDocker(), env={"ANTHROPIC_API_KEY": "  "}
        )


async def test_api_key_with_newline_halts():
    with pytest.raises(VerifierPreflightError):
        await run_verifier_docker_preflight(
            _cfg(), docker=_FakeDocker(), env={"ANTHROPIC_API_KEY": "sk\nx"}
        )


async def test_docker_unreachable_halts():
    with pytest.raises(VerifierPreflightError, match="docker"):
        await run_verifier_docker_preflight(
            _cfg(), docker=_FakeDocker(version=False),
            env={"ANTHROPIC_API_KEY": "sk-x"},
        )


async def test_image_absent_halts_no_pull():
    with pytest.raises(VerifierPreflightError, match="image"):
        await run_verifier_docker_preflight(
            _cfg(), docker=_FakeDocker(image=False),
            env={"ANTHROPIC_API_KEY": "sk-x"},
        )
```

(The `claude --version` probe + image-ID inspect run only after those gates pass; they are exercised in the docker-gated integration test, Task 10 — here we assert the cheap, daemon-free halt rows.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/verifier/test_preflight.py -v`
Expected: FAIL (`ModuleNotFoundError: maestro.verifier.preflight`).

- [ ] **Step 3: Write `maestro/verifier/preflight.py`**

```python
"""Eager, global fail-loud preflight for verifier.backend=docker (spec §6)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping

from maestro.execution.docker_cli import DockerCli
from maestro.verifier.docker_backend import ANTHROPIC_ENV_KEY, _security_flags
from maestro.verifier.docker_config import VerifierDockerConfig

_FORBIDDEN = ("\x00", "\r", "\n")


class VerifierPreflightError(RuntimeError):
    """A verifier docker preflight halt (spec §6.1). Global fail-loud."""


def _check_api_key(env: Mapping[str, str]) -> None:
    value = env.get(ANTHROPIC_ENV_KEY)
    if value is None or not value.strip():
        raise VerifierPreflightError(
            f"{ANTHROPIC_ENV_KEY} is required and non-empty for verifier.backend=docker"
        )
    if any(c in value for c in _FORBIDDEN):
        raise VerifierPreflightError(
            f"{ANTHROPIC_ENV_KEY} must not contain NUL/CR/LF"
        )


async def run_verifier_docker_preflight(
    cfg: VerifierDockerConfig,
    *,
    docker: DockerCli,
    env: Mapping[str, str],
    timeout_s: float = 20.0,
) -> str:
    """Run every §6.1 halt check; return the inspected image ID on success.

    Raises VerifierPreflightError on any halt row (missing/blank/dirty key,
    docker unreachable, image absent, CLI missing/non-zero under the hardened
    profile). Never pulls; never passes the credential to the probe.
    """
    _check_api_key(env)
    if not await docker.version_ok():
        raise VerifierPreflightError("docker daemon unreachable")
    if not await docker.image_exists(cfg.image):
        raise VerifierPreflightError(f"image absent (no auto-pull): {cfg.image}")

    uid, gid = cfg.user.split(":")
    name = "maestro-verify-preflight"
    argv = [
        "docker", "run", "--rm", "--name", name,
        "--label", "maestro.verify_preflight=1",
        *_security_flags(cfg, uid, gid),
        cfg.image, "claude", "--version",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            with contextlib.suppress(Exception):
                await docker.kill(name)
            raise VerifierPreflightError(
                "claude --version probe timed out under the hardened profile"
            ) from exc
    except FileNotFoundError as exc:  # docker binary missing
        raise VerifierPreflightError(f"docker CLI not found: {exc}") from exc
    if proc.returncode != 0:
        raise VerifierPreflightError(
            "claude --version failed under the hardened profile "
            f"(exit={proc.returncode}); image/CLI/hardening incompatible"
        )
    image_id = await _inspect_image_id(cfg.image)
    return image_id


async def _inspect_image_id(image: str) -> str:
    """`docker inspect --format {{.Id}} <image>` (audit; best-effort)."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "image", "inspect", "--format", "{{.Id}}", image,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", "replace").strip() if proc.returncode == 0 else ""
```

- [ ] **Step 4: Call preflight from `_check_verifier_model`** in `maestro/scheduler.py`

`_check_verifier_model` is sync (`scheduler.py:841`). It runs at scheduler construction. Extend it so that when `self._verifier.backend == "docker"` it runs the async preflight to completion and raises `SchedulerError` on failure, recording the image ID in an event. Since it's a sync method, run the coroutine with `asyncio.run` ONLY if no loop is running; otherwise document that the caller invokes an async variant. Simplest robust approach: keep `_check_verifier_model` sync for the model check, and add the docker preflight as a small sync wrapper that uses `asyncio.run`:
```python
        if self._verifier.backend == "docker":
            assert self._verifier.docker is not None
            try:
                image_id = asyncio.run(
                    run_verifier_docker_preflight(
                        self._verifier.docker,
                        docker=self._verifier_docker_cli or DockerCli(),
                        env=os.environ,
                    )
                )
            except VerifierPreflightError as exc:
                raise SchedulerError(f"verifier docker preflight failed: {exc}") from exc
            self._emit_event(
                EventType.VERIFIER_STARTED,  # or a dedicated preflight event if present
                {"verifier_preflight": "ok", "image_id": image_id},
            )
```
Implementer note: confirm `_check_verifier_model` is not itself called from within a running event loop at construction time (Scheduler is built synchronously in `cli.py`/tests before the loop runs). If it can be, replace `asyncio.run` with the scheduler's existing loop-runner utility. Do NOT swallow — a halt must propagate as `SchedulerError` (global fail-loud). Add imports: `run_verifier_docker_preflight`, `VerifierPreflightError`, `os` (if not present).

- [ ] **Step 5: Add a scheduler-preflight test** — append to `tests/verifier/test_preflight.py`

```python
def test_scheduler_preflight_halts_on_missing_key(monkeypatch):
    from maestro.scheduler import Scheduler, SchedulerError
    from maestro.models import VerifierConfig
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sch = Scheduler.__new__(Scheduler)
    sch._verifier = VerifierConfig(
        backend="docker", model="m", docker=_cfg(),
    )
    sch._verifier_docker_cli = _FakeDocker()
    # stub the model resolution half so only the docker preflight is exercised
    monkeypatch.setattr(sch, "_emit_event", lambda *a, **k: None, raising=False)
    import maestro.scheduler as S
    monkeypatch.setattr(S, "load_catalog", lambda: None, raising=True)
    monkeypatch.setattr(S, "resolve_verifier_model", lambda cfg, cat: "m", raising=True)
    with pytest.raises(SchedulerError, match="preflight"):
        sch._check_verifier_model()
```
(Adjust the stubs to your `_check_verifier_model` structure; the load-bearing assertion is: docker backend + missing key → `SchedulerError`.)

- [ ] **Step 6: Run tests + quality gates**

Run: `uv run pytest tests/verifier/test_preflight.py -v`
Expected: PASS.
Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`

- [ ] **Step 7: Commit**

```bash
git add maestro/verifier/preflight.py maestro/scheduler.py tests/verifier/test_preflight.py
git commit -m "feat(verifier): eager global fail-loud docker preflight (halt matrix)"
```

---

## Task 7: `get_open_verification_handles()` DB query

**Files:**
- Modify: `maestro/database.py` (new method)
- Test: `tests/verifier/test_verification_handles_query.py`

**Interfaces:**
- Produces: `Database.get_open_verification_handles() -> list[dict[str, Any]]` — all rows with `state IN ('prepared','running','terminal','collected')` AND `execution_phase = 'verification'`, **regardless of backend_id** (unlike `get_open_execution_handles`, which filters `backend_id != 'local'`).

- [ ] **Step 1: Write the failing test** — `tests/verifier/test_verification_handles_query.py`

```python
import pytest

from maestro.database import Database

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _seed(db, *, execution_id, backend_id, phase, state):
    # Mint via start_execution then move to `state`. Use the existing durable
    # handle API the verifier gate uses (start_execution + mark_execution_state).
    ...  # implementer: mirror an existing execution-handle test's seeding helper


async def test_returns_local_and_docker_verification_handles(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    try:
        # seed: local verification handle + verifier-docker verification handle
        # + a non-verification task handle (must NOT be returned)
        # (implementer: reuse the seeding helper from an existing
        # tests/**/test_*execution_handle*.py file)
        rows = await db.get_open_verification_handles()
        phases = {r["execution_phase"] for r in rows}
        backends = {r["backend_id"] for r in rows}
        assert phases == {"verification"}
        assert "local" in backends and "verifier-docker" in backends
    finally:
        await db.close()
```
Implementer: locate the existing execution-handle test helper (search `tests/` for `start_execution(` usage) and reuse its seeding pattern rather than hand-rolling SQL. The assertion that matters: local verification handles ARE returned (the general query filters them out) and non-verification handles are NOT.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/verifier/test_verification_handles_query.py -v`
Expected: FAIL (`AttributeError: ... get_open_verification_handles`).

- [ ] **Step 3: Add the method** in `maestro/database.py` (next to `get_open_execution_handles`, ~line 1996)

```python
    async def get_open_verification_handles(self) -> list[dict[str, Any]]:
        """Return all open verification-phase handles (any backend, any task
        status) — the phase-specific recovery owner's input (spec §7).

        Unlike `get_open_execution_handles` (which filters `backend_id !=
        'local'`), this returns `execution_phase = 'verification'` rows for
        every backend, so a `local` verifier handle is never dropped.
        """
        if self._connection is None:
            raise DatabaseError("Database not connected")
        cursor = await self._connection.execute(
            """
            SELECT execution_id, entity_kind, entity_id, attempt, backend_id,
                   transport_ref, state, created_at, finished_at,
                   remote_host, remote_dir, status_marker, collected_at,
                   execution_phase
            FROM execution_handles
            WHERE state IN ('prepared', 'running', 'terminal', 'collected')
              AND execution_phase = 'verification'
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
```

- [ ] **Step 4: Run tests + quality gates**

Run: `uv run pytest tests/verifier/test_verification_handles_query.py -v`
Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`

- [ ] **Step 5: Commit**

```bash
git add maestro/database.py tests/verifier/test_verification_handles_query.py
git commit -m "feat(db): get_open_verification_handles (all-backend, phase-scoped)"
```

---

## Task 8: Recovery split — phase-specific ownership + GC exclusion

**Files:**
- Modify: `maestro/recovery.py` (`recover()` split; new `_reconcile_verification_handles`; trim `_recover_verifying_tasks`; GC gets `general_handles`; `__init__` takes `verifier` + shared `DockerCli` + `db_dir`)
- Modify: `maestro/cli.py` (pass `verifier=config.verifier`)
- Test: `tests/verifier/test_recovery_split.py`

**Interfaces:**
- Consumes: `get_open_verification_handles` (Task 7); `build_verifier_backend` (Task 4); `VerifierConfig` (Task 1); existing `handle_ref_from_row`, `_close_handle`, `mark_execution_state`, `gc_terminal_handle`.
- Produces: `StateRecovery.__init__(db, docker=None, execution=None, verifier: VerifierConfig | None = None, db_dir: Path | None = None)`; `StateRecovery._reconcile_verification_handles() -> None`; `StateRecovery._verifier_backend_for(row) -> ExecutionBackend | None`.

**Anchor (spec §7):** the guard test asserts every open verification handle is processed exactly once by phase-specific recovery and never by the general loop nor `_gc_terminal_handles`.

- [ ] **Step 1: Write the failing test** — `tests/verifier/test_recovery_split.py`

```python
import pytest

from maestro.recovery import StateRecovery

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_verification_handles_excluded_from_general_gc(tmp_path, monkeypatch):
    """A verifier-docker terminal verification handle must reach neither the
    general open-handle loop nor _gc_terminal_handles (spec §7.1)."""
    db = _make_db(tmp_path)  # implementer: reuse existing recovery-test db fixture
    await db.connect()
    try:
        rec = StateRecovery(db, execution=None, verifier=_docker_verifier_cfg())
        seen_general = []
        # spy: general GC must never see a verification row
        real_gc = rec._gc_terminal_handles
        async def _spy(handles):
            seen_general.extend(handles)
            return await real_gc(handles)
        monkeypatch.setattr(rec, "_gc_terminal_handles", _spy)
        # seed a verifier-docker verification handle in 'terminal'
        ...  # implementer: seed via start_execution + mark_execution_state
        await rec.recover()
        assert all(h["execution_phase"] != "verification" for h in seen_general)
    finally:
        await db.close()


async def test_settled_task_verification_handle_reconciled(tmp_path):
    """A terminal verification handle whose task is already DONE is still
    marked cleaned by _reconcile_verification_handles (spec §7.2)."""
    ...  # implementer: seed DONE task + terminal verification handle;
        # assert handle state becomes 'cleaned' after recover()
```
Implementer: reuse the existing recovery test scaffolding (search `tests/` for `StateRecovery(` and the db seeding helpers). The two load-bearing assertions: (a) no verification row reaches `_gc_terminal_handles`; (b) a settled-task terminal verification handle is reconciled to `cleaned`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/verifier/test_recovery_split.py -v`
Expected: FAIL (either `TypeError: __init__ got unexpected keyword 'verifier'`, or the assertions fail).

- [ ] **Step 3: Extend `StateRecovery.__init__`** in `maestro/recovery.py`

```python
    def __init__(
        self,
        db: Database,
        docker: DockerProbe | None = None,
        execution: ExecutionConfig | None = None,
        verifier: "VerifierConfig | None" = None,
        db_dir: "Path | None" = None,
    ) -> None:
        ...  # existing body
        self._verifier = verifier
        self._verifier_exec_root = (db_dir or Path(".")) / "verifier-exec"
```
Add imports: `from pathlib import Path`, `from maestro.models import VerifierConfig` (or under TYPE_CHECKING for the annotation), `from maestro.verifier.docker_backend import build_verifier_backend`.

- [ ] **Step 4: Split `open_handles` in `recover()`** (spec §7.1)

Right after `open_handles = await self._db.get_open_execution_handles()` (`recovery.py:161`), keep the general set free of verification rows:
```python
        general_handles = [
            h for h in open_handles if h.get("execution_phase", "task") != "verification"
        ]
```
Change `_by_phase` to iterate `general_handles` instead of `open_handles`, and change the GC call (`recovery.py:224`) to:
```python
        await self._gc_terminal_handles(general_handles)
```
Then replace the `verifying_recovered = await self._recover_verifying_tasks()` line with the two operations:
```python
        # Phase-specific owner of ALL open verification handles (any status/backend).
        await self._reconcile_verification_handles()
        # FSM routing for tasks still stuck in VERIFYING.
        verifying_recovered = await self._recover_verifying_tasks()
```

- [ ] **Step 5: Trim `_recover_verifying_tasks`** to FSM routing only

Remove the per-task `get_execution_handle` lookup + `_reconcile_verifying_handle` call (that ownership moves to `_reconcile_verification_handles`). Keep the `get_tasks_by_status(VERIFYING)` loop that routes each to NEEDS_REVIEW and returns the count. Delete the now-unused `_reconcile_verifying_handle` method (superseded by `_reconcile_verification_handles`).

- [ ] **Step 6: Add `_reconcile_verification_handles`** (spec §7.2 state matrix)

```python
    async def _reconcile_verification_handles(self) -> None:
        """Own ALL open verification handles regardless of task status/backend.

        prepared/running -> accepts_ref then probe (preserve on live/error).
        terminal/collected, local -> mark cleaned directly (no Docker GC).
        terminal/collected, verifier-docker -> ownership-checked container GC
          + credential-artifact cleanup (§7.3) -> cleaned.
        unknown/mismatch/error -> preserve open (fail-closed).
        """
        rows = await self._db.get_open_verification_handles()
        for row in rows:
            await self._reconcile_one_verification_handle(row)

    async def _reconcile_one_verification_handle(self, row: dict[str, Any]) -> None:
        state = row["state"]
        backend_id = row["backend_id"]
        if state in ("prepared", "running"):
            backend = self._verifier_backend_for(row)
            if backend is None:
                return  # unknown/mismatch/no-config -> preserve
            ref = handle_ref_from_row(row)
            if not backend.accepts_ref(ref):
                return
            try:
                result = await backend.probe(ref)
            except Exception:
                return
            if not result.needs_review:
                await self._close_handle(row["execution_id"])
            return
        if state in ("terminal", "collected"):
            if backend_id == "verifier-docker":
                try:
                    outcome = await gc_terminal_handle(row, self._docker)
                except Exception:
                    return
                if outcome not in GC_CLEAN_OUTCOMES:
                    return  # preserve for next sweep
                self._cleanup_credential_artifacts(row["execution_id"])
            await self._db.mark_execution_state(
                row["execution_id"], "cleaned", allowed_from=[state]
            )

    def _verifier_backend_for(self, row: dict[str, Any]):
        """Build the verifier backend for a persisted row via the factory,
        matched to the current VerifierConfig. None if unresolvable/mismatched
        (fail-closed -> caller preserves)."""
        if self._verifier is None:
            return None
        backend_id = row["backend_id"]
        if backend_id == "local" and self._verifier.backend != "local":
            return None
        if backend_id == "verifier-docker" and self._verifier.backend != "docker":
            return None
        try:
            return build_verifier_backend(
                self._verifier,
                local_backend=self._backends.resolve("local"),
                exec_root=self._verifier_exec_root,
                docker_cli=cast("DockerCli", self._docker),
            )
        except Exception:
            return None
```

- [ ] **Step 7: Credential-artifact cleanup with path-safety** (spec §7.3)

```python
    def _cleanup_credential_artifacts(self, execution_id: str) -> None:
        """Delete the deterministic verifier temp-dir (env-file/cidfile/dir).

        Path-safety (spec §7.3): validate execution_id is a UUID and the
        canonical temp-dir is inside the dedicated verifier root before any
        destructive op — never delete outside the root.
        """
        import uuid

        try:
            uuid.UUID(execution_id)
        except ValueError:
            return
        root = self._verifier_exec_root.resolve()
        target = (root / f"maestro-verify-{execution_id}").resolve()
        if root not in target.parents and target != root:
            return
        shutil.rmtree(target, ignore_errors=True)
```
Add `import shutil` at the top of `recovery.py` if absent.

- [ ] **Step 8: Wire `cli.py`** — `maestro/cli.py:507`

```python
                recovery = StateRecovery(
                    db,
                    execution=config.execution,
                    verifier=config.verifier,
                    db_dir=Path(db_path).parent,
                )
```
(Use the db path variable already in scope; import `Path` if needed. Confirm `config.verifier` is the attribute name on the loaded config; align with how the scheduler reads it.)

- [ ] **Step 9: Run tests + quality gates**

Run: `uv run pytest tests/verifier/test_recovery_split.py -v`
Then the full recovery suite to prove no regression on the RUNNING/VALIDATING/local-verifying paths:
`uv run pytest tests/ -k recovery -q`
Expected: PASS.
Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`

- [ ] **Step 10: Commit**

```bash
git add maestro/recovery.py maestro/cli.py tests/verifier/test_recovery_split.py
git commit -m "feat(recovery): phase-split verification handle ownership + credential GC"
```

---

## Task 9: Credential-crash regression + path-safety tests

**Files:**
- Test: `tests/verifier/test_credential_cleanup.py`

**Interfaces:**
- Consumes: `_cleanup_credential_artifacts` (Task 8); `VerifierDockerIsolator.materialize`/`after_spawn` (Task 3).

- [ ] **Step 1: Write the tests** — `tests/verifier/test_credential_cleanup.py`

```python
import uuid
from pathlib import Path

import pytest

from maestro.recovery import StateRecovery

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _rec(tmp_path):
    rec = StateRecovery.__new__(StateRecovery)
    rec._verifier_exec_root = tmp_path / "verifier-exec"
    return rec


def test_cleanup_removes_deterministic_dir(tmp_path):
    rec = _rec(tmp_path)
    eid = str(uuid.uuid4())
    d = rec._verifier_exec_root / f"maestro-verify-{eid}"
    d.mkdir(parents=True)
    (d / "env").write_text("ANTHROPIC_API_KEY=sk-x")
    rec._cleanup_credential_artifacts(eid)
    assert not d.exists()


def test_cleanup_rejects_non_uuid(tmp_path):
    rec = _rec(tmp_path)
    outside = tmp_path / "evil"
    outside.mkdir()
    rec._cleanup_credential_artifacts("../evil")  # not a UUID -> no-op
    assert outside.exists()


def test_cleanup_never_escapes_root(tmp_path, monkeypatch):
    rec = _rec(tmp_path)
    # A UUID whose computed path resolves outside the root cannot occur by
    # construction, but assert the containment guard holds if the root moves.
    eid = str(uuid.uuid4())
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    # point root elsewhere, target stays under the (new) root only if contained
    rec._verifier_exec_root = tmp_path / "verifier-exec"
    rec._cleanup_credential_artifacts(eid)  # dir doesn't exist -> safe no-op
    assert sibling.exists()
```

- [ ] **Step 2: Run + quality gates**

Run: `uv run pytest tests/verifier/test_credential_cleanup.py -v`
Expected: PASS.
Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`

- [ ] **Step 3: Commit**

```bash
git add tests/verifier/test_credential_cleanup.py
git commit -m "test(verifier): credential-artifact cleanup path-safety + crash regression"
```

---

## Task 10: Docker-gated integration test

**Files:**
- Create: `tests/integration/test_verifier_docker_integration.py`

**Interfaces:** Consumes the whole stack (isolator + factory + preflight) against a real Docker daemon and a real hardened container running a trivial command (NOT `claude` — no auth). Gated behind a skip-if-no-docker marker.

- [ ] **Step 1: Add a docker-availability skip helper + tests**

```python
import shutil
import subprocess

import pytest

from maestro.verifier.docker_backend import VerifierDockerIsolator, verifier_exec_dir
from maestro.verifier.docker_config import VerifierDockerConfig


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10
        ).returncode == 0
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_ok(), reason="docker unavailable"),
    pytest.mark.anyio,
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _digest_for(image_tag: str) -> str:
    # resolve a locally-present image (e.g. alpine) to a digest-pinned ref
    out = subprocess.run(
        ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", image_tag],
        capture_output=True, text=True,
    )
    ref = out.stdout.strip()
    if "@sha256:" not in ref:  # fall back to Id-based digest is not valid form;
        pytest.skip("no repo digest for test image; pull one first")
    return ref


async def test_read_only_root_blocks_write(tmp_path):
    """--read-only means writing outside /scratch fails; /scratch is writable."""
    subprocess.run(["docker", "pull", "alpine"], check=True, capture_output=True)
    digest = _digest_for("alpine")
    cfg = VerifierDockerConfig(image=digest, user="1000:1000")
    iso = VerifierDockerIsolator(cfg, exec_root=tmp_path)
    # write to /etc must fail (read-only), write to /scratch must succeed,
    # id -u must be 1000 (non-root), and no project mount is present.
    ...  # implementer: build a request whose argv is a `sh -c` asserting:
        #   'id -u' == 1000; 'touch /scratch/x' ok; 'touch /root/x' fails;
        # run via LocalBackend(iso, backend_id="verifier-docker", docker=DockerCli())
        # and assert exit code + that env-file is gone after the run (after_spawn).
```

Implementer: build a real `ExecutionRequest` with `argv=["sh","-c", <assertion script>]`, run it through `LocalBackend(iso, backend_id="verifier-docker", docker=DockerCli())`, `await handle.wait()`, and assert:
- `id -u` inside prints `1000` (non-root enforced),
- writing to `/root` fails but `/scratch` succeeds (read-only root + tmpfs),
- `sh` runs under `noexec` `/scratch` (proves the runtime lives in the image, not unpacked to scratch),
- the deterministic env-file is gone after the run (after_spawn eager unlink),
- container is cleaned (`docker ps -a` has no `maestro-<execution_id>`).

- [ ] **Step 2: Run only if docker present**

Run: `uv run pytest tests/integration/test_verifier_docker_integration.py -v -m integration`
Expected: PASS (or SKIPPED where docker is absent). Confirm the default suite still skips these.
Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_verifier_docker_integration.py
git commit -m "test(verifier): docker-gated hardened-container integration"
```

---

## Task 11: Opt-in authenticated smoke (default-off)

**Files:**
- Create: `tests/integration/test_verifier_docker_smoke.py`

**Interfaces:** A real end-to-end judge run inside the sandbox proving the authenticated code path works under `noexec` (the full spec §6.3 proof). Default-off: gated behind BOTH docker availability AND an explicit env flag (e.g. `MAESTRO_VERIFIER_SMOKE=1`) so CI never spends tokens.

- [ ] **Step 1: Write the smoke test**

```python
import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        os.environ.get("MAESTRO_VERIFIER_SMOKE") != "1",
        reason="opt-in authenticated smoke; set MAESTRO_VERIFIER_SMOKE=1",
    ),
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker unavailable"),
    pytest.mark.anyio,
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_real_judge_under_hardened_sandbox():
    """End-to-end: a real `claude` judge PASS/FAIL under noexec/read-only,
    with ANTHROPIC_API_KEY via env-file. If this FAILS under noexec, do NOT
    silently drop noexec — escalate a threat-model decision (spec §6.3)."""
    ...  # implementer: build a minimal TaskVerificationContext with a tiny
        # real diff envelope, run ClaudeDiffJudge(model=<cheap>, backend=
        # verifier-docker) and assert a well-formed TaskHandshakeResult
        # (PASS or FAIL, not ERROR). Requires a verifier image with the
        # claude CLI installed and ANTHROPIC_API_KEY exported.
```

Register the `smoke` and `integration` markers in `pyproject.toml`/`pytest.ini` if not already present, so `-m` selection and default-skip both work.

- [ ] **Step 2: Confirm default-skip**

Run: `uv run pytest tests/integration/test_verifier_docker_smoke.py -v`
Expected: SKIPPED (no `MAESTRO_VERIFIER_SMOKE`).
Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_verifier_docker_smoke.py pyproject.toml
git commit -m "test(verifier): opt-in authenticated sandbox smoke (default-off)"
```

---

## Task 12: Docs — CLAUDE.md + example config

**Files:**
- Modify: `CLAUDE.md` (document `verifier.backend: docker`)
- Create: `examples/with-verifier-docker.yaml`

- [ ] **Step 1: Add `examples/with-verifier-docker.yaml`**

A minimal Mode-1 config with a verifier docker block, honestly commented:
```yaml
# Strict Docker verifier sandbox (filesystem/process isolation, NOT network
# isolation — the container keeps unrestricted bridge networking so the judge
# can reach the model). Requires ANTHROPIC_API_KEY in the environment and a
# digest-pinned image whose `claude` CLI is installed.
project: demo
repo: .
verifier:
  runner: claude
  model: claude-haiku-4-5-20251001
  backend: docker
  docker:
    image: "your.registry/verifier@sha256:0000000000000000000000000000000000000000000000000000000000000000"
    user: "1000:1000"        # mandatory numeric non-root uid:gid
    memory: "512m"           # 128m..8g
    cpus: "1"                # 0.1..8
    pids_limit: 128          # 16..4096
    tmpfs_size: "64m"        # 16m..1g
tasks:
  - id: demo-1
    title: Example
    prompt: Make the change.
    scope: ["src/**"]
    validation_cmd: "pytest -q"
```

- [ ] **Step 2: Update `CLAUDE.md`**

Under the "Verifier gate (Mode-1, opt-in)" bullet, append a sentence describing `backend: docker`: the hardened, mount-less, digest-pinned sandbox (read-only root, cap-drop=ALL, no-new-privileges, non-root, tmpfs `/scratch`, one `ANTHROPIC_API_KEY` env-file), that it is **filesystem/process isolation, not network isolation** (bridge = unrestricted container network), the eager global fail-loud preflight, and that `backend: local` stays byte-identical. Point to `examples/with-verifier-docker.yaml` and the spec.

- [ ] **Step 3: Full suite + quality gates**

Run: `uv run pytest -q` (foreground)
Expected: all green (docker/smoke tests SKIPPED without a daemon/flag).
Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md examples/with-verifier-docker.yaml
git commit -m "docs(verifier): document verifier.backend=docker + example config"
```

---

## Final verification (before PR)

- [ ] `uv run pytest -q` — full suite green (docker/smoke SKIPPED).
- [ ] `uv run ruff format . && uv run ruff check . && uv run pyrefly check` — clean.
- [ ] Grep-audit the invariants: `verifier-docker` never appears in `ExecutionConfig.normalized()`/`BackendResolver`; no `-v ` project mount in the verifier argv; hardening flags present and not config-gated; `backend: local` path untouched (diff `_run_verifier`'s local branch, recovery's RUNNING/VALIDATING paths).
- [ ] Confirm a config with no `verifier:` block and one with `backend: local` produce **zero** Docker calls and **zero** preflight (byte-identical behavior).
