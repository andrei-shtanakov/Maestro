# validation_backend PR2 — SSH validation + Mode-1 SSH recovery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support post-task validation on an SSH backend (a fresh remote `ExecutionRequest`), and close the pre-existing Mode-1 SSH recovery gap for **both** the task and validation phases by making `ExecutionBackend.probe()` the single, isolation-aware, fail-closed recovery boundary.

**Architecture:** Recovery today hand-composes `probe_ssh`/`probe_execution` (returning `RecoveryVerdict`) at two sites — Mode-2 `orchestrator._probe_open_handle` and Mode-1 `StateRecovery`, the latter docker-only. PR2 moves that dual-probe logic *into* each backend's `probe(ref) -> ProbeResult` (isolation-aware), gives `ProbeResult` an honest `needs_review` field and `ExecutionHandleRef` an `execution_id`, then routes **both** recovery sites through `backend.probe()`. SSH validation itself is already driven by the transport-agnostic `_run_durable_validation` (PR1) once the preflight gate is lifted, `CollectPolicy(mode="none")` is a true no-op on the SSH handle, and SSH capture-output is populated.

**Tech Stack:** Python 3.12+, uv, pytest (anyio), Pydantic, aiosqlite, Typer. Scheduler mode (Mode 1) is the target; Mode-2 orchestrator recovery is migrated to the shared boundary (regression-covered).

## Global Constraints

- Package manager is **uv** only. Tests: `uv run pytest`. Types: `uv run pyrefly check` (repo gate: **0 errors**). Format/lint: `uv run ruff format . && uv run ruff check .`.
- Type hints on all code; line length 88; docstrings on public APIs; f-strings.
- Branch: `feat/validation-backend-ssh` (already checked out, spec committed). No direct commits to `master`. One PR at the end.
- **Any test that builds a `Database` MUST close it** via a fixture `yield d; await d.close()` — an unclosed aiosqlite connection is a `ResourceWarning`-as-error and its lingering thread stalls teardown (~120s "hang"). Real scheduler ctor: `Scheduler(db, DAG([]), spawners={}, config=SchedulerConfig(workdir=tmp_path), execution=<ExecutionConfig|None>)`. Recovery class: `StateRecovery(db, docker=..., execution=...)`.
- **Verify tests FOREGROUND only**, on the named file(s) — a workspace watchdog kills long background pytest. Never background the suite; rely on PR CI for the full run. Use `-o faulthandler_timeout=<n>` to surface a hang as a stack dump.
- **Fail-closed dominates** (spec): SSH open handle → always `needs_review`; unresolvable backend / identity conflict / placeholder row / null docker `execution_id` → `needs_review`. Recovery **never deletes** remote state; GC is a separate ownership-checked step.
- **`default = local` stays** (the `local → same` flip is PR3, release-noted). Do **not** change the default in this PR.
- Behavior-compatible, not byte-identical: local/local-docker recovery outcomes are preserved; SSH is newly correct.

---

## File Structure

- `maestro/execution/models.py` — `ProbeResult.needs_review`; `ExecutionHandleRef.execution_id`.
- `maestro/execution/handle_ref.py` (new) — shared `handle_ref_from_row(row) -> ExecutionHandleRef`.
- `maestro/execution/local.py` — fill `ref.execution_id` at mint; isolation-aware `LocalBackend.probe`.
- `maestro/execution/ssh_handle.py` — capture `stdout_tail`; `CollectSpec.mode` + `collect()` none no-op; fill `ref.execution_id`.
- `maestro/execution/ssh_backend.py` — `CollectSpec.mode` from `req.collect.mode`; isolation-aware dual-probe `SshBackend.probe`.
- `maestro/execution/docker_handle.py` — fill `ref.execution_id` at mint (docker isolator wrap).
- `maestro/orchestrator.py` — use shared `handle_ref_from_row`; route `_probe_open_handle` through `backend.probe()`.
- `maestro/recovery.py` — `StateRecovery` gains a `BackendResolver`; phase+state selection (incl. `collected`); classify by `backend_id` → `backend.probe()`; transport-aware GC.
- `maestro/scheduler.py` — persist remote coords after task- and validation-phase SSH `backend.run`.
- `maestro/preflight.py` — drop the SSH rejection, keep the unknown-name check.
- `maestro/cli.py` — pass `execution` config into `StateRecovery`.
- Tests under `tests/`.

---

## Task 1: `ProbeResult.needs_review` + `ExecutionHandleRef.execution_id`

**Files:**
- Modify: `maestro/execution/models.py` — `ProbeResult` (`:118`), `ExecutionHandleRef` (`:90`).
- Test: `tests/test_probe_ref_contract.py`

**Interfaces:**
- Produces: `ProbeResult(needs_review: bool, alive: bool | None = None, exit_code: int | None = None, detail: str = "")`; `ExecutionHandleRef.execution_id: str | None = None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_probe_ref_contract.py
from datetime import UTC, datetime

from maestro.execution.models import ExecutionHandleRef, ProbeResult


def test_probe_result_needs_review_is_primary():
    r = ProbeResult(needs_review=True, alive=False, detail="dead but uncollected")
    assert r.needs_review is True
    assert r.alive is False  # diagnostic only


def test_probe_result_alive_optional():
    r = ProbeResult(needs_review=False)
    assert r.alive is None


def test_handle_ref_carries_execution_id():
    ref = ExecutionHandleRef(
        backend_id="sandbox",
        run_id="t1",
        transport_ref="sandbox:maestro-e1",
        execution_id="e1",
        started_at=datetime.now(UTC),
    )
    assert ref.execution_id == "e1"
    # Back-compat: field is optional.
    ref2 = ExecutionHandleRef(
        backend_id="local", run_id="t1", transport_ref="local_pid:5",
        started_at=datetime.now(UTC),
    )
    assert ref2.execution_id is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_probe_ref_contract.py -v`
Expected: FAIL — `ProbeResult` has no `needs_review`; `ExecutionHandleRef` has no `execution_id`.

- [ ] **Step 3: Change the models**

In `models.py`, replace the `ProbeResult` class:

```python
class ProbeResult(BaseModel):
    needs_review: bool
    alive: bool | None = None
    exit_code: int | None = None
    detail: str = ""
```

In `ExecutionHandleRef`, add after `run_id`:

```python
    execution_id: str | None = None
```

- [ ] **Step 4: Fix the two existing `probe()` implementations to the new field**

`local.py` `LocalBackend.probe` (`:254`) — translate to `needs_review` (a live/uncertain local PID needs review; a dead one is reclaimable):

```python
    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        if not ref.transport_ref.startswith("local_pid:"):
            return ProbeResult(needs_review=True, detail="not a local ref")
        pid = int(ref.transport_ref.split(":", 1)[1])
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return ProbeResult(needs_review=False, alive=False)
        except PermissionError:
            return ProbeResult(needs_review=True, alive=True, detail="exists (EPERM)")
        return ProbeResult(needs_review=True, alive=True)
```

`ssh_backend.py` `SshBackend.probe` (`:408` area) — set `needs_review` from the verdict:

```python
        verdict = await probe_ssh(self._ssh, ref)
        return ProbeResult(needs_review=verdict.needs_review, alive=None, detail=verdict.reason)
```

(Task 6 makes this dual-probe; this step only fixes the field.)

- [ ] **Step 5: Run tests + types**

Run: `uv run pytest tests/test_probe_ref_contract.py tests/test_local_backend.py tests/test_ssh_backend.py -v && uv run pyrefly check`
Expected: PASS; `pyrefly` 0 errors. (Update any existing assertion that read `ProbeResult(...).alive` as the decision signal to `.needs_review`.)

- [ ] **Step 6: Commit**

```bash
git add maestro/execution/models.py maestro/execution/local.py maestro/execution/ssh_backend.py tests/test_probe_ref_contract.py
git commit -m "feat(execution): ProbeResult.needs_review + ExecutionHandleRef.execution_id"
```

---

## Task 2: Fill `ref.execution_id` at mint in every handle

**Files:**
- Modify: `maestro/execution/local.py` (`LocalBackend.run` ref build, `:245`); `maestro/execution/ssh_backend.py` (`run` ref build, `:292`); `maestro/execution/docker_handle.py` (ref build in isolator `wrap`, if it constructs its own ref).
- Test: `tests/test_handle_execution_id.py`

**Interfaces:**
- Consumes: `ExecutionRequest.execution_id` (`models.py:53`, already present).
- Produces: every runtime handle's `ref.execution_id == req.execution_id`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_handle_execution_id.py
import pytest
from pathlib import Path

from maestro.execution.local import LocalBackend
from maestro.execution.models import CollectPolicy, ExecutionRequest

pytestmark = pytest.mark.anyio


async def test_local_handle_ref_carries_execution_id(tmp_path):
    req = ExecutionRequest(
        run_id="t1", argv=["true"], workdir=tmp_path,
        log_path=tmp_path / "l.log", collect=CollectPolicy(mode="none"),
        backend_id="local", execution_id="exec-123",
    )
    handle = await LocalBackend().run(req)
    try:
        assert handle.ref.execution_id == "exec-123"
    finally:
        await handle.wait()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_handle_execution_id.py -v -o faulthandler_timeout=30`
Expected: FAIL — `ref.execution_id is None`.

- [ ] **Step 3: Set `execution_id` in each ref build**

`local.py:245`:

```python
        ref = ExecutionHandleRef(
            backend_id=req.backend_id,
            run_id=req.run_id,
            execution_id=req.execution_id,
            transport_ref=self._isolator.transport_ref(prepared, proc.pid),
            started_at=datetime.now(UTC),
        )
```

`ssh_backend.py:292`:

```python
        ref = ExecutionHandleRef(
            backend_id=req.backend_id,
            run_id=req.run_id,
            execution_id=req.execution_id,
            transport_ref=encode_transport_ref(...),   # unchanged args
            status_marker=layout.status,
            started_at=datetime.now(UTC),
        )
```

If `docker_handle.py`'s isolator builds/copies a ref, thread `execution_id` there too (grep `ExecutionHandleRef(` under `maestro/execution/`).

- [ ] **Step 4: Run tests + types**

Run: `uv run pytest tests/test_handle_execution_id.py tests/test_ssh_backend.py tests/test_docker_handle.py -v -o faulthandler_timeout=30 && uv run pyrefly check`
Expected: PASS; 0 type errors.

- [ ] **Step 5: Commit**

```bash
git add maestro/execution/local.py maestro/execution/ssh_backend.py maestro/execution/docker_handle.py tests/test_handle_execution_id.py
git commit -m "feat(execution): carry execution_id on every handle ref"
```

---

## Task 3: Shared `handle_ref_from_row` helper

**Files:**
- Create: `maestro/execution/handle_ref.py`
- Modify: `maestro/orchestrator.py` — delete the private `_handle_ref_from_row` (`:174`), import the shared one; read `execution_id` from the row.
- Test: `tests/test_handle_ref_from_row.py`

**Interfaces:**
- Produces: `handle_ref_from_row(row: dict) -> ExecutionHandleRef` (reads `backend_id`, `entity_id` as `run_id`, `transport_ref`, `status_marker`, `execution_id`, `created_at`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_handle_ref_from_row.py
from maestro.execution.handle_ref import handle_ref_from_row


def test_builds_ref_with_execution_id():
    row = {
        "backend_id": "gpu", "entity_id": "t1", "transport_ref": "gpu:e1",
        "status_marker": "/t/e1.status", "execution_id": "e1",
        "created_at": "2026-07-25T00:00:00+00:00",
    }
    ref = handle_ref_from_row(row)
    assert ref.backend_id == "gpu"
    assert ref.run_id == "t1"
    assert ref.transport_ref == "gpu:e1"
    assert ref.status_marker == "/t/e1.status"
    assert ref.execution_id == "e1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_handle_ref_from_row.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Create the helper**

```python
# maestro/execution/handle_ref.py
"""Rebuild an ExecutionHandleRef from an execution_handles DB row.

Shared by Mode-1 StateRecovery and Mode-2 orchestrator recovery so the row->ref
translation has one definition.
"""

from datetime import datetime
from typing import Any

from maestro.execution.models import ExecutionHandleRef


def handle_ref_from_row(row: dict[str, Any]) -> ExecutionHandleRef:
    """Reconstruct a persisted execution ref from an `execution_handles` row."""
    return ExecutionHandleRef(
        backend_id=row["backend_id"],
        run_id=row["entity_id"],
        execution_id=row.get("execution_id"),
        transport_ref=row["transport_ref"],
        status_marker=row.get("status_marker"),
        started_at=datetime.fromisoformat(row["created_at"]),
        workdir_mirror_path=None,
        state_mirror_path=None,
    )
```

- [ ] **Step 4: Migrate the orchestrator to the shared helper**

In `orchestrator.py`, delete the private `_handle_ref_from_row` (`:174`) and `from maestro.execution.handle_ref import handle_ref_from_row`; replace call sites `_handle_ref_from_row(row)` → `handle_ref_from_row(row)`.

- [ ] **Step 5: Run tests + types (Mode-2 regression)**

Run: `uv run pytest tests/test_handle_ref_from_row.py tests/test_orchestrator_ssh_wiring.py -v -o faulthandler_timeout=60 && uv run pyrefly check`
Expected: PASS — orchestrator SSH recovery unchanged in behavior (the reviewer-required Mode-2 regression).

- [ ] **Step 6: Commit**

```bash
git add maestro/execution/handle_ref.py maestro/orchestrator.py tests/test_handle_ref_from_row.py
git commit -m "refactor(execution): shared handle_ref_from_row (Mode-1/Mode-2)"
```

---

## Task 4: SSH capture output → `stdout_tail`

**Files:**
- Modify: `maestro/execution/ssh_handle.py` — `wait()` (`:200`); reuse `_tail_log()`/`_log_path` and a bounded read (`_TAIL_LIMIT`-style, mirror `local._decode_tail`).
- Test: `tests/test_ssh_capture_output.py`

**Interfaces:**
- Consumes: `req.capture_output`, `self._log_path`, `self._tail_log()`.
- Produces: `wait()` returns `stdout_tail` = bounded combined tail (capture only), `stderr_tail == ""`.

- [ ] **Step 1: Write the failing test** (fake ssh runner; assert a final tail runs and the tail is returned)

```python
# tests/test_ssh_capture_output.py
# Construct an SshTaskHandle with capture_output=True over a fake runner whose
# log file already holds "validation output\n"; drive it to terminal and assert
# wait().stdout_tail == "validation output\n" and stderr_tail == "".
# Mirror the fixture style in tests/test_ssh_handle.py (fake Runner, _terminal set).
```

Write the concrete body by copying the `SshTaskHandle` construction from `tests/test_ssh_handle.py`, seeding `handle._log_path.write_text("validation output\n")`, setting the terminal event, and asserting the tails. Add a non-capture case asserting `stdout_tail == ""`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ssh_capture_output.py -v -o faulthandler_timeout=30`
Expected: FAIL — `stdout_tail == ""` even with capture.

- [ ] **Step 3: Implement capture in `wait()`**

```python
    async def wait(self) -> ExecutionResult:
        """Await terminal completion and return the cached result.

        On capture_output, do a final tail (bytes may have landed between the
        monitor's last tail and completion) and return a bounded combined tail.
        The remote supervisor merges stdout+stderr into one log, so stderr_tail
        stays "" (separate stderr needs a supervisor/descriptor version bump).
        """
        await self._terminal.wait()
        stdout_tail = ""
        if self._req.capture_output:
            with contextlib.suppress(Exception):
                await self._tail_log()
            stdout_tail = _tail_text(self._log_path)
        return ExecutionResult(
            exit_code=self._exit_code,
            stdout_tail=stdout_tail,
            stderr_tail="",
            output_log_path=self._log_path,
            timed_out=self._timed_out,
        )
```

Add a small bounded reader near the top of the module (mirror `local._TAIL_LIMIT = 4000` / `_decode_tail`):

```python
_TAIL_LIMIT = 4000


def _tail_text(path: Path) -> str:
    try:
        data = path.read_bytes()[-_TAIL_LIMIT:]
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")
```

Confirm `SshTaskHandle` holds `self._req` (the `ExecutionRequest`); if not, thread `capture_output` in at construction.

- [ ] **Step 4: Run tests + types**

Run: `uv run pytest tests/test_ssh_capture_output.py tests/test_ssh_handle.py -v -o faulthandler_timeout=40 && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/execution/ssh_handle.py tests/test_ssh_capture_output.py
git commit -m "feat(ssh): capture combined stdout_tail on capture_output runs"
```

---

## Task 5: `CollectPolicy(mode="none")` true no-op on the SSH handle

**Files:**
- Modify: `maestro/execution/ssh_handle.py` — `CollectSpec` (`:37`) gains `mode`; `collect()` (`:228`) short-circuits.
- Modify: `maestro/execution/ssh_backend.py` — build `CollectSpec(mode=req.collect.mode, ...)` (`:314`).
- Test: `tests/test_ssh_collect_none.py`

**Interfaces:**
- Produces: `CollectSpec.mode: Literal["none","whole_worktree","scope_paths"]`; `collect()` returns `CollectResult(applied=False)` without touching ssh when `mode == "none"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ssh_collect_none.py
# Build an SshTaskHandle with collect_spec.mode="none" over a fake ssh whose
# .rsync/.run raise if called; assert collect() returns CollectResult(applied=False)
# and the fake ssh was never invoked. A "whole_worktree"/"scope_paths" spec still
# calls rsync (guard). Mirror tests/test_ssh_handle.py collect fixtures.
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ssh_collect_none.py -v -o faulthandler_timeout=30`
Expected: FAIL — `collect()` rsyncs regardless of mode.

- [ ] **Step 3: Add `mode` to `CollectSpec` and short-circuit**

`CollectSpec`:

```python
@dataclass
class CollectSpec:
    worktree: Path
    staging_dir: Path
    journal_dir: Path
    baseline: dict[str, str]
    mode: str = "whole_worktree"
    scope: list[str] | None = None
```

Top of `collect()`:

```python
    async def collect(self) -> CollectResult:
        if self._collect.mode == "none":
            return CollectResult(applied=False, detail="collect=none: no-op")
        ...  # existing rsync + plan_collect + apply_collect
```

`ssh_backend.py:314` — pass the mode:

```python
            collect_spec=CollectSpec(
                worktree=...,
                staging_dir=...,
                journal_dir=...,
                baseline=...,
                mode=req.collect.mode,
                scope=_collect_scope(req.collect),
            ),
```

- [ ] **Step 4: Run tests + types**

Run: `uv run pytest tests/test_ssh_collect_none.py tests/test_ssh_handle.py tests/test_ssh_collect.py -v -o faulthandler_timeout=40 && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/execution/ssh_handle.py maestro/execution/ssh_backend.py tests/test_ssh_collect_none.py
git commit -m "feat(ssh): CollectPolicy(mode=none) is a true no-op in SshTaskHandle"
```

---

## Task 6: Isolation-aware `SshBackend.probe` (dual probe) + `LocalBackend.probe`

**Files:**
- Modify: `maestro/execution/ssh_backend.py` — `probe()` dual-probe by persisted isolation (move the orchestrator's `_probe_open_handle` SSH logic here).
- Modify: `maestro/execution/local.py` — `LocalBackend.probe` docker branch (`probe_execution(ref.execution_id)`), fail-closed on null.
- Test: `tests/test_backend_probe_isolation.py`

**Interfaces:**
- Consumes: `decode_transport_ref` (isolation), `probe_ssh`, `probe_execution`, `ref.execution_id`, `self._docker`.
- Produces: `SshBackend.probe(ref) -> ProbeResult(needs_review=...)` — bare → probe_ssh; docker → dual; any ambiguity → `needs_review=True`. `LocalBackend.probe` — bare → PID; docker → container.

- [ ] **Step 1: Write the failing test** (fake ssh + fake docker; cases: bare-ssh always review; ssh-docker pgid-dead+container-present → review; ssh-docker both clean → still review [collect unconfirmed]; local-docker no-container → not review; local-docker null execution_id → review)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_backend_probe_isolation.py -v`
Expected: FAIL — `SshBackend.probe` bare-only; `LocalBackend.probe` docker returns "not a local ref".

- [ ] **Step 3: Implement `SshBackend.probe` dual-probe** (port from `orchestrator._probe_open_handle`, `:672-712`)

```python
    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        from maestro.execution.ssh_recovery import probe_ssh

        decoded = decode_transport_ref(ref.transport_ref)
        ssh_verdict = await probe_ssh(self._ssh, ref)
        needs = ssh_verdict.needs_review
        detail = ssh_verdict.reason
        if decoded.get("isolation") == "docker":
            if self.isolation_kind != "docker" or self.docker is None:
                return ProbeResult(
                    needs_review=True,
                    detail="persisted docker isolation but backend is bare",
                )
            if ref.execution_id is None:
                return ProbeResult(needs_review=True, detail="docker probe: no execution_id")
            cont = await probe_execution(
                ref.execution_id, self.docker,
                expected_labels=decoded.get("expected_labels"),
            )
            needs = needs or cont.needs_review
            detail = f"{detail}; container: {cont.reason}"
        return ProbeResult(needs_review=needs, detail=detail)
```

- [ ] **Step 4: Implement `LocalBackend.probe` docker branch**

```python
    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        if ref.transport_ref.startswith("local_pid:"):
            pid = int(ref.transport_ref.split(":", 1)[1])
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return ProbeResult(needs_review=False, alive=False)
            except PermissionError:
                return ProbeResult(needs_review=True, alive=True, detail="exists (EPERM)")
            return ProbeResult(needs_review=True, alive=True)
        # Docker isolation: probe the container by execution_id (fail-closed on null).
        if self._docker is None or ref.execution_id is None:
            return ProbeResult(needs_review=True, detail="docker probe: no execution_id/daemon")
        verdict = await probe_execution(ref.execution_id, self._docker)
        return ProbeResult(needs_review=verdict.needs_review, detail=verdict.reason)
```

(`LocalBackend` holds `self._docker` when the resolver paired it with a `DockerIsolator` — see `local.py` `__init__`.)

- [ ] **Step 5: Run tests + types**

Run: `uv run pytest tests/test_backend_probe_isolation.py tests/test_local_backend.py tests/test_ssh_backend.py -v -o faulthandler_timeout=40 && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add maestro/execution/ssh_backend.py maestro/execution/local.py tests/test_backend_probe_isolation.py
git commit -m "feat(execution): isolation-aware backend.probe (dual for ssh+docker)"
```

---

## Task 7: Route Mode-2 orchestrator recovery through `backend.probe()`

**Files:**
- Modify: `maestro/orchestrator.py` — `_probe_open_handle` (`:655-712`) becomes a thin resolve → `backend.probe(ref)` → `needs_review`.
- Test: `tests/test_orchestrator_ssh_wiring.py` (existing — must stay green), + a focused assertion.

**Interfaces:**
- Consumes: `self._backends.resolve(backend_id)`, `handle_ref_from_row`, `backend.probe`.

- [ ] **Step 1: Adjust/confirm the regression test**

The existing `tests/test_orchestrator_ssh_wiring.py` already asserts the dual-probe outcomes (leftover container, isolation mismatch, ssh clean). Keep them; they now flow through `backend.probe`. Add one assertion that a bare-ssh open handle → NEEDS_REVIEW via `backend.probe` (not the old inline path).

- [ ] **Step 2: Run to see current behavior**

Run: `uv run pytest tests/test_orchestrator_ssh_wiring.py -v -o faulthandler_timeout=60`
Expected: PASS pre-change (baseline).

- [ ] **Step 3: Simplify `_probe_open_handle`**

```python
        row = workstream_handles.get(workstream_id)
        if row is None:
            return False
        backend = self._backends.resolve(row["backend_id"])
        result = await backend.probe(handle_ref_from_row(row))
        if not result.needs_review:
            # confirmed reclaimable → close the open handle so it can't shadow
            # the next attempt (unchanged terminal->cleaned bookkeeping).
            await self._db.mark_execution_state(
                row["execution_id"], "terminal", allowed_from=["prepared", "running"]
            )
            await self._db.mark_execution_state(
                row["execution_id"], "cleaned", allowed_from=["terminal"]
            )
            return False
        # ... existing NEEDS_REVIEW transition, using result.detail as the reason
        return True
```

Preserve the exact status-transition/bookkeeping that follows today; only the probe *composition* moves into `backend.probe`. Unresolvable `backend_id` → treat as `needs_review` (wrap `resolve` in try/except `ExecutionConfigError`).

- [ ] **Step 4: Run tests + types**

Run: `uv run pytest tests/test_orchestrator_ssh_wiring.py -v -o faulthandler_timeout=60 && uv run pyrefly check`
Expected: PASS — Mode-2 behavior identical, now via the single boundary.

- [ ] **Step 5: Commit**

```bash
git add maestro/orchestrator.py tests/test_orchestrator_ssh_wiring.py
git commit -m "refactor(orchestrator): route recovery probe through backend.probe()"
```

---

## Task 8: Persist real remote coords after Mode-1 SSH `backend.run`

**Files:**
- Modify: `maestro/scheduler.py` — after the task-phase `backend.run` (`_dispatch_task`, ~`:1253`) and the validation-phase `backend.run` (`_run_durable_validation`), persist coords when `isinstance(backend, SshBackend)`.
- Test: `tests/test_scheduler_ssh_coord_persist.py`

**Interfaces:**
- Consumes: `Database.update_execution_handle_launch` (`database.py:1537`), `decode_transport_ref`, `handle.ref`.

- [ ] **Step 1: Write the failing test** (fake SshBackend whose `run` returns a handle with a JSON `transport_ref` + `status_marker`; assert the row gains `remote_host`/`remote_dir`/`status_marker` after dispatch/validation)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scheduler_ssh_coord_persist.py -v -o faulthandler_timeout=40`
Expected: FAIL — coords stay NULL (placeholder).

- [ ] **Step 3: Add a shared persist helper + call it at both sites**

```python
    async def _persist_ssh_launch(self, execution_id: str, handle) -> None:
        """Persist the real remote coordinates SshBackend.run() minted, so
        recovery can build a ref and probe (mirrors orchestrator.py:1258)."""
        info = decode_transport_ref(handle.ref.transport_ref)
        await self._db.update_execution_handle_launch(
            execution_id,
            transport_ref=handle.ref.transport_ref,
            remote_host=info.get("host"),
            remote_dir=info.get("remote_dir"),
            status_marker=handle.ref.status_marker,
        )
```

Call after `handle = await backend.run(request)` in both the task dispatch (non-local branch) and `_run_durable_validation`, guarded by `isinstance(backend, SshBackend)`. Import `SshBackend`, `decode_transport_ref`.

- [ ] **Step 4: Run tests + types**

Run: `uv run pytest tests/test_scheduler_ssh_coord_persist.py tests/test_scheduler.py -v -o faulthandler_timeout=60 && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/scheduler.py tests/test_scheduler_ssh_coord_persist.py
git commit -m "feat(scheduler): persist remote coords for Mode-1 SSH handles (task+validation)"
```

---

## Task 9: `StateRecovery` — backend-based, phase+state-aware classification

**Files:**
- Modify: `maestro/recovery.py` — `StateRecovery.__init__` gains `execution` → `BackendResolver`; `recover()` phase maps include `collected`; replace `_route_docker_task_to_review` with a backend-probe router honoring the §4c matrix.
- Modify: `maestro/cli.py` (`:506`) — pass the execution config into `StateRecovery`.
- Test: `tests/test_recovery_backend_classify.py`

**Interfaces:**
- Consumes: `handle_ref_from_row`, `backend.probe`, `BackendResolver`.
- Produces: `StateRecovery(db, docker=..., execution=...)`; RUNNING/VALIDATING recovery routes per the matrix.

- [ ] **Step 1: Write the failing test** — the matrix, each row:
  - named-local **bare** open handle, PID alive → NEEDS_REVIEW; PID dead → re-READY (the mis-classification guard).
  - local-docker no container → re-READY (regression).
  - SSH bare open handle (any pgid state) → NEEDS_REVIEW.
  - `collected` handle on a RUNNING task → NEEDS_REVIEW.
  - unresolvable `backend_id` / placeholder SSH row (no coords) → NEEDS_REVIEW.
  Use fakes: monkeypatch the resolved backend's `probe` to return the intended `ProbeResult`; seed handles with `start_execution` + `update_execution_handle_launch`. Close the db via fixture.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_recovery_backend_classify.py -v -o faulthandler_timeout=60`
Expected: FAIL — docker-only probe; named-local-bare mis-READYed; `collected` not selected.

- [ ] **Step 3: Constructor + resolver**

```python
    def __init__(self, db, docker=None, execution=None):
        self._db = db
        self._docker = docker or DockerCli()
        self._backends = BackendResolver(execution, mode="scheduler")
```

- [ ] **Step 4: Phase+state selection includes `collected`**

In `recover()` `_by_phase`, widen the state filter:

```python
        def _by_phase(phase: str) -> dict[str, dict[str, Any]]:
            return {
                h["entity_id"]: h
                for h in open_handles
                if h["entity_kind"] == "task"
                and h["state"] in ("prepared", "running", "terminal", "collected")
                and h.get("execution_phase", "task") == phase
            }
```

(Keep the PR1 validation-preferred merge for VALIDATING.)

- [ ] **Step 5: Backend-probe router (replaces docker-only `_route_docker_task_to_review`)**

```python
    async def _route_open_handle_to_review(self, task, handles) -> bool:
        """Probe a task's open handle via its resolved backend; NEEDS_REVIEW
        unless the backend proves it reclaimable. §4c matrix; fail-closed."""
        row = handles.get(task.id)
        if row is None:
            return False
        try:
            backend = self._backends.resolve(row["backend_id"])
        except ExecutionConfigError:
            await self._route_to_review(task, f"unresolvable backend {row['backend_id']}")
            return True
        result = await backend.probe(handle_ref_from_row(row))
        if not result.needs_review:
            await self._close_handle(row["execution_id"])   # terminal->cleaned bookkeeping
            return False
        await self._route_to_review(task, result.detail)
        return True
```

`_route_to_review` does RUNNING→NEEDS_REVIEW or VALIDATING→FAILED→NEEDS_REVIEW (reuse the existing transitions). Wire it into both `_recover_running_tasks` and `_recover_validating_tasks` in place of the docker-only call.

- [ ] **Step 6: CLI passes execution config**

`cli.py:506` — `StateRecovery(db, execution=<loaded execution config>)`. Load the same execution block the scheduler uses (from the run's config); `None` keeps the zero-config local path.

- [ ] **Step 7: Run tests + types**

Run: `uv run pytest tests/test_recovery_backend_classify.py tests/test_recovery.py tests/test_recovery_validation_phase.py -v -o faulthandler_timeout=60 && uv run pyrefly check`
Expected: PASS (existing docker + validation-phase recovery regressions included).

- [ ] **Step 8: Commit**

```bash
git add maestro/recovery.py maestro/cli.py tests/test_recovery_backend_classify.py
git commit -m "feat(recovery): backend-probe, phase+state-aware Mode-1 SSH recovery"
```

---

## Task 10: Transport-aware GC in `StateRecovery`

**Files:**
- Modify: `maestro/recovery.py` — `_gc_terminal_handles` (`:279-317`) sweeps by resolved backend/transport per §5.
- Test: `tests/test_recovery_gc_transport.py`

**Interfaces:**
- Consumes: `gc_terminal_handle` (docker), `gc_ssh_terminal` (ssh), resolved backend/isolation.

- [ ] **Step 1: Write the failing test**
  - SSH `terminal` handle → **not** marked `cleaned` (must never docker-GC it).
  - SSH `collected` bare → remote-root GC → `cleaned`.
  - SSH `collected` docker → container GC → then remote-root GC → `cleaned`.
  - local-docker `terminal` → docker GC (unchanged).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_recovery_gc_transport.py -v -o faulthandler_timeout=60`
Expected: FAIL — SSH terminal wrongly `cleaned`.

- [ ] **Step 3: Rewrite the sweep by transport**

Classify each open handle via the resolver; branch:
- resolved LocalBackend (docker) → existing `gc_terminal_handle` on `terminal`.
- resolved SshBackend:
  - `terminal` → skip (never GC; collect unconfirmed).
  - `collected` bare → `gc_ssh_terminal(ssh, ref)` → mark `cleaned` on clean outcome.
  - `collected` docker → container GC first; **only** on a clean outcome → `gc_ssh_terminal` → mark `cleaned`.
- Mark `cleaned` only after all applicable cleanups succeed; any ambiguity leaves the row.

Write the concrete code following the existing `_gc_terminal_handles` structure (iterate rows, resolve, branch, `mark_execution_state(..., "cleaned", allowed_from=["terminal","collected"])`). Use `GC_CLEAN_OUTCOMES` for docker outcomes.

- [ ] **Step 4: Run tests + types**

Run: `uv run pytest tests/test_recovery_gc_transport.py tests/test_recovery.py -v -o faulthandler_timeout=60 && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/recovery.py tests/test_recovery_gc_transport.py
git commit -m "feat(recovery): transport-aware GC (never docker-GC an SSH terminal handle)"
```

---

## Task 11: Lift the SSH preflight gate (keep the unknown-name check)

**Files:**
- Modify: `maestro/preflight.py` — `check_validation_backends` drops the SSH-transport rejection but **keeps** a startup check that a non-`local`/`same` named `validation_backend` resolves to a known backend (fail-fast beats a late error inside a validation run).
- Test: `tests/test_validation_backend_preflight.py` (update PR1 assertions).

**Interfaces:**
- Produces: SSH `validation_backend` **passes**; an unknown named backend still **raises** `ValidationBackendError`.

- [ ] **Step 1: Update the tests**
  - `test_explicit_ssh_validation_backend_fails` / `test_same_on_ssh_task_fails` → renamed to assert SSH now **passes**.
  - Keep `test_same_with_no_backend_resolves_to_ssh_default_and_fails` → now **passes** (SSH allowed).
  - **New:** `validation_backend: "does-not-exist"` → raises `ValidationBackendError` (unknown-name fast-fail retained).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_validation_backend_preflight.py -v`
Expected: FAIL — current code rejects SSH.

- [ ] **Step 3: Narrow the check**

```python
    resolved = (task.backend or default_backend) if name == "same" else name
    spec = registry.get(resolved) if resolved is not None else None
    # SSH is now supported (PR2) — no transport rejection. Keep the fast-fail on
    # an explicitly named validation_backend that resolves to nothing: a late
    # ExecutionConfigError inside the validation run is worse than preflight.
    if name not in ("local", "same") and resolved is not None and spec is None:
        raise ValidationBackendError(
            f"task '{task.id}': validation_backend '{name}' is not a known backend"
        )
```

(Remove the `isinstance(transport, SshTransport)` branch. Keep `ValidationBackendError` and the scheduler-start gate wiring.)

- [ ] **Step 4: Run tests + types**

Run: `uv run pytest tests/test_validation_backend_preflight.py -v && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/preflight.py tests/test_validation_backend_preflight.py
git commit -m "feat(preflight): allow SSH validation_backend; keep unknown-name fail-fast"
```

---

## Task 12: Full suite, docs, opt-in e2e, PR

**Files:**
- Modify: `maestro/CLAUDE.md` — update the validation_backend note (SSH now supported; default still `local`, PR3 flips).
- Create: `tests/e2e/test_ssh_validation_e2e.py` — opt-in localhost-ssh validation (gated like the existing SSH e2e).
- Test: whole suite.

- [ ] **Step 1: Opt-in e2e** — a task run locally with `validation_backend: <localhost-ssh>` runs the validation command on the SSH backend, applies no collect, cleans the remote tmp, reports the captured result. Gate the entire body behind the existing `MAESTRO_SSH_E2E` env check (zero subprocess at collection).

- [ ] **Step 2: Full suite + types + lint** (foreground)

Run: `uv run pytest -q -p no:cacheprovider -o faulthandler_timeout=110 --ignore=tests/e2e && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: all green (fix formatting first, then types, then lint on any regression).

- [ ] **Step 3: CLAUDE.md note**

Update the PR1 validation_backend bullet: SSH validation is now supported (fresh remote layout, durable, dual-probe recovery, GC); `default` is still `local`; **PR3** flips the default to `same` with a release note.

- [ ] **Step 4: Commit + push + PR**

```bash
git add maestro/CLAUDE.md tests/e2e/test_ssh_validation_e2e.py
git commit -m "docs+test: SSH validation note + opt-in localhost-ssh e2e"
git push -u origin feat/validation-backend-ssh
gh pr create --title "feat: validation_backend PR2 — SSH validation + Mode-1 SSH recovery" --body "$(cat <<'BODY'
Implements PR2 of the validation_backend slice (spec:
docs/superpowers/specs/2026-07-25-validation-backend-pr2-ssh-design.md).

- SSH validation runs as a fresh remote ExecutionRequest (durable, capture_output).
- CollectPolicy(mode=none) is a true no-op on the SSH handle.
- ExecutionBackend.probe() is the single, isolation-aware, fail-closed recovery
  boundary (ProbeResult.needs_review; ExecutionHandleRef.execution_id); both Mode-1
  StateRecovery and Mode-2 orchestrator route through it.
- Mode-1 SSH recovery closed for BOTH task and validation phases: an open
  (prepared/running/terminal/collected) SSH handle is always NEEDS_REVIEW (collect
  unconfirmed); named-local-bare is PID-probed, not docker; transport-aware GC never
  docker-GCs an SSH terminal handle.
- SSH validation_backend now passes preflight (unknown-name fail-fast retained).

default stays `local`; the `local -> same` flip is PR3 (release-noted).

Full suite green, pyrefly 0, ruff clean.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```

- [ ] **Step 5: Read Copilot review, iterate** — fix valid comments with new commits; rebut invalid; do not merge (the user merges).

---

## Self-Review

**Spec coverage:**
- §1 SSH capture → Task 4. ✓
- §2 CollectPolicy(none) no-op → Task 5. ✓
- §3 persist remote coords → Task 8. ✓
- §4 backend-based classification / single boundary → Tasks 1, 6, 7, 9. ✓
- §4a ProbeResult.needs_review → Task 1. ✓
- §4b ExecutionHandleRef.execution_id → Tasks 1, 2, 3. ✓
- §4c state matrix (incl. collected) → Task 9. ✓
- §5 transport-aware GC → Task 10. ✓
- §6 lift preflight gate (keep unknown-name) → Task 11. ✓
- secret_env SSOT (no request change) → verified by Task 12 e2e; no code change (documented non-goal of new plumbing). ✓
- Shared handle_ref_from_row + Mode-2 regression → Tasks 3, 7. ✓
- Default stays local (PR3 flip) → Global Constraints + Task 12 doc. ✓

**Reviewer planning notes:** explicit `execution_id: str | None = None` + fail-closed on null docker key → Tasks 1, 6, 9. Keep `check_validation_backends` (SSH lifted, unknown-name retained) → Task 11. Mode-2 regression after `handle_ref_from_row` extraction → Tasks 3, 7.

**Placeholder scan:** Tasks 4/5/6/9/10 describe the test intent + fixtures to mirror rather than inlining every fake — the implementer copies the concrete fixture from the named existing test file (`tests/test_ssh_handle.py`, `tests/test_recovery.py`, `tests/test_orchestrator_ssh_wiring.py`). This is deliberate (the SSH/recovery fakes are large and already exist); each such step names the exact file to copy from and the exact assertions. No `TODO`/`TBD`.

**Type consistency:** `ProbeResult(needs_review=…)` and `ExecutionHandleRef(execution_id=…)` defined Task 1, used Tasks 2/6/7/9. `handle_ref_from_row` defined Task 3, used Tasks 7/9/10. `StateRecovery(execution=…)` defined Task 9, called Task 9 (cli).
