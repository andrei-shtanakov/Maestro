# validation_backend PR1 slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route Maestro's post-task validation through the execution layer as a second `ExecutionRequest` on a selected backend (`validation_backend: local | same | <name>`, default `local`), with a durable validation lifecycle for non-local backends and a fail-loud gate for SSH targets.

**Architecture:** Validation becomes a separate `ExecutionRequest` (`capture_output=True`, `collect=none`). Local validation runs through `LocalBackend` behavior-preservingly (no handle). Non-local (docker/named-local) validation mints its own durable `execution_handles` row (`execution_phase='validation'`) via the atomic `RUNNING→VALIDATING` `start_execution`, runs through `finalize_handle`, and is recoverable by the existing Docker probe. SSH validation targets fail-loud at scheduler start. This is PR1; SSH validation execution + flipping the default to `same` is PR2.

**Tech Stack:** Python 3.12+, uv, pytest (anyio), Pydantic, aiosqlite, Typer. Scheduler mode (Mode 1) only.

## Global Constraints

- Package manager is **uv** only — never pip. Tests: `uv run pytest`. Types: `uv run pyrefly check`. Format/lint: `uv run ruff format . && uv run ruff check .`.
- Type hints on all code; line length 88; docstrings on public APIs; f-strings.
- Branch: `feat/validation-backend` (already checked out). No direct commits to `master`. One PR at the end.
- `validation_cmd` / `validation_backend` are **`Task`/`TaskConfig`** concepts (scheduler mode). Do **not** touch orchestrator/workstream validation.
- **Default `validation_backend = "local"`** (preserves today's observable behavior). Do NOT default to `same` — that is a PR2 change with a release note.
- **No silent substitution:** an SSH-resolving `validation_backend` fails loud; it never quietly runs local.
- Durable criterion is **literal `backend.id != "local"`** — do not introduce a new capability abstraction.
- Migrations: append at the tail of the `ordered` list in `Database._apply_migrations` (`database.py:417`); never reorder. Idempotent via `PRAGMA table_info`.
- Argv parity: validation commands are parsed with `shlex.split` (exec-style, no shell). Do not introduce `sh -c`.

---

## File Structure

- `maestro/models.py` — add `validation_backend` field to `Task` and `TaskConfig`; passthrough in `Task.from_config`.
- `maestro/database.py` — `execution_handles.execution_phase` column + migration; `tasks.validation_backend` column + migration; `start_execution(execution_phase=...)`; `get_open_execution_handles` returns the phase; `create_task`/`update_task`/`_row_to_task` wiring.
- `maestro/validator.py` — pure helpers: `build_validation_request(...)` and `execution_result_to_validation(...)`.
- `maestro/preflight.py` — `check_validation_backends(tasks, execution)` resolve-and-check helper (SSH → error).
- `maestro/transitions.py` — wire `VALIDATION_STARTED` into `TASK_EFFECTS[VALIDATING]`.
- `maestro/scheduler.py` — `_resolve_validation_backend`, rewritten `_run_validation`/`_validate` (local + durable paths, launch taxonomy), reservation hold across validation, the scheduler-start SSH gate.
- `maestro/recovery.py` — phase-split `task_handles`; VALIDATING recovery uses the validation-phase handle.
- Tests under `tests/`.

---

## Task 1: `execution_phase` column on `execution_handles`

**Files:**
- Modify: `maestro/database.py` — `SCHEMA_SQL` execution_handles block (`:145`), migrations list (`:417`), new `_migrate_execution_phase`, `start_execution` INSERT (`:1409`) + signature (`:1332`), `get_open_execution_handles` SELECT (`:1539`).
- Test: `tests/test_execution_phase_column.py`

**Interfaces:**
- Produces: `Database.start_execution(..., execution_phase: str = "task")`; `get_open_execution_handles()` rows now carry key `"execution_phase"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_execution_phase_column.py
import pytest
from maestro.database import Database
from maestro.models import Task, TaskStatus


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "m.db")
    await d.connect()
    await d.initialize_schema()
    yield d
    await d.close()


async def _seed_task(db: Database, tid: str) -> None:
    await db.create_task(
        Task(id=tid, title="t", prompt="p", workdir="/tmp", status=TaskStatus.READY)
    )


async def test_start_execution_persists_validation_phase(db):
    await _seed_task(db, "t1")
    await db.start_execution(
        entity_kind="task",
        entity_id="t1",
        expected_status="ready",
        running_status="validating",
        execution_id="e-val",
        backend_id="sandbox",
        transport_ref="sandbox:maestro-e-val",
        attempt=1,
        execution_phase="validation",
    )
    rows = await db.get_open_execution_handles()
    assert rows[0]["execution_id"] == "e-val"
    assert rows[0]["execution_phase"] == "validation"


async def test_start_execution_defaults_phase_task(db):
    await _seed_task(db, "t2")
    await db.start_execution(
        entity_kind="task",
        entity_id="t2",
        expected_status="ready",
        running_status="running",
        execution_id="e-task",
        backend_id="sandbox",
        transport_ref="sandbox:maestro-e-task",
        attempt=1,
    )
    rows = await db.get_open_execution_handles()
    assert rows[0]["execution_phase"] == "task"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_execution_phase_column.py -v`
Expected: FAIL — `start_execution() got an unexpected keyword argument 'execution_phase'` / KeyError `execution_phase`.

- [ ] **Step 3: Add the column to `SCHEMA_SQL`**

In `database.py` `execution_handles` CREATE (`:152` area), add after the `state` line:

```sql
    state          TEXT NOT NULL CHECK (state IN ('prepared','running','terminal','collected','cleaned')),
    execution_phase TEXT NOT NULL DEFAULT 'task' CHECK (execution_phase IN ('task','validation')),
```

- [ ] **Step 4: Register + write the migration**

Append to the `ordered` list in `_apply_migrations` (after `(9, "ssh_handle_columns", ...)`):

```python
            (10, "execution_phase", self._migrate_execution_phase),
```

Add the method (near `_migrate_entity_backend_columns`):

```python
    async def _migrate_execution_phase(self) -> None:
        """Migration 10: add `execution_phase` to `execution_handles`.

        Discriminates a task's primary execution from its validation
        execution so recovery selects the right open handle per phase.
        Idempotent via PRAGMA table_info; pre-existing rows default to
        'task'.
        """
        assert self._connection is not None
        cursor = await self._connection.execute(
            "PRAGMA table_info(execution_handles)"
        )
        columns = {row["name"] for row in await cursor.fetchall()}
        if "execution_phase" not in columns:
            await self._connection.execute(
                "ALTER TABLE execution_handles ADD COLUMN execution_phase "
                "TEXT NOT NULL DEFAULT 'task' "
                "CHECK (execution_phase IN ('task','validation'))"
            )
```

- [ ] **Step 5: Thread the param through `start_execution`**

Add `execution_phase: str = "task"` to the signature (after `status_marker`, `:1345`). Update the INSERT (`:1409`) to include the column and value:

```python
                """
                INSERT INTO execution_handles
                  (execution_id, entity_kind, entity_id, attempt, backend_id,
                   transport_ref, state, created_at, finished_at,
                   remote_host, remote_dir, status_marker, execution_phase)
                VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, NULL, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    entity_kind,
                    entity_id,
                    attempt,
                    backend_id,
                    transport_ref,
                    _format_datetime(datetime.now(UTC)),
                    remote_host,
                    remote_dir,
                    status_marker,
                    execution_phase,
                ),
```

- [ ] **Step 6: Return the phase from `get_open_execution_handles`**

In the SELECT (`:1539`), add `execution_phase` to the column list:

```python
            SELECT execution_id, entity_kind, entity_id, attempt, backend_id,
                   transport_ref, state, created_at, finished_at,
                   remote_host, remote_dir, status_marker, collected_at,
                   execution_phase
            FROM execution_handles
            WHERE state IN ('prepared', 'running', 'terminal', 'collected')
              AND backend_id != 'local'
```

- [ ] **Step 7: Run tests + types**

Run: `uv run pytest tests/test_execution_phase_column.py -v && uv run pyrefly check`
Expected: PASS; no new type errors.

- [ ] **Step 8: Commit**

```bash
git add maestro/database.py tests/test_execution_phase_column.py
git commit -m "feat(db): execution_phase discriminator on execution_handles"
```

---

## Task 2: Persist `validation_backend` on tasks

**Files:**
- Modify: `maestro/models.py` — `Task` (after `backend`, `:471`), `TaskConfig` (after `backend`, `:550`), `Task.from_config` (`:719`).
- Modify: `maestro/database.py` — `tasks` CREATE (`:74`), migration list (`:417`), new `_migrate_tasks_validation_backend`, `create_task` INSERT (`:820`), `update_task` UPDATE (`:987`), `_row_to_task` (`:293`).
- Test: `tests/test_validation_backend_persistence.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Task.validation_backend: str` (default `"local"`); persisted + resume-safe.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_validation_backend_persistence.py
import pytest
from maestro.database import Database
from maestro.models import Task, TaskConfig, TaskStatus


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "m.db")
    await d.connect()
    await d.initialize_schema()
    yield d
    await d.close()


async def test_validation_backend_round_trips(db):
    await db.create_task(
        Task(
            id="t1",
            title="t",
            prompt="p",
            workdir="/tmp",
            status=TaskStatus.READY,
            validation_backend="same",
        )
    )
    got = await db.get_task("t1")
    assert got.validation_backend == "same"


async def test_validation_backend_defaults_local(db):
    await db.create_task(
        Task(id="t2", title="t", prompt="p", workdir="/tmp", status=TaskStatus.READY)
    )
    got = await db.get_task("t2")
    assert got.validation_backend == "local"


def test_task_config_passthrough():
    cfg = TaskConfig(
        id="c1", title="t", prompt="p", validation_backend="sandbox"
    )
    task = Task.from_config(cfg, workdir="/tmp")
    assert task.validation_backend == "sandbox"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_validation_backend_persistence.py -v`
Expected: FAIL — `Task` has no field `validation_backend` (ValidationError).

- [ ] **Step 3: Add the model fields**

In `models.py`, add to `Task` (after the `backend` field, `:471`) and to `TaskConfig` (after its `backend`, `:550`):

```python
    validation_backend: str = Field(
        default="local",
        description=(
            "Backend for the post-task validation run: 'local' | 'same' "
            "(the task's backend) | a named backend. Non-local targets must "
            "resolve to transport.type == local; SSH targets fail preflight."
        ),
    )
```

In `Task.from_config` (`:719`), add after `backend=config.backend,`:

```python
            validation_backend=config.validation_backend,
```

- [ ] **Step 4: Add the column + migration**

In `tasks` CREATE (`database.py:74`), after `validation_cmd TEXT,`:

```sql
    validation_backend TEXT NOT NULL DEFAULT 'local',
```

Append to the `ordered` migrations list:

```python
            (11, "tasks_validation_backend", self._migrate_tasks_validation_backend),
```

Add the method:

```python
    async def _migrate_tasks_validation_backend(self) -> None:
        """Migration 11: add `validation_backend` to `tasks` (DEFAULT 'local').

        Pre-existing rows keep today's local-validation behavior. Idempotent
        via PRAGMA table_info, same shape as `_migrate_entity_backend_columns`.
        """
        assert self._connection is not None
        cursor = await self._connection.execute("PRAGMA table_info(tasks)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "validation_backend" not in columns:
            await self._connection.execute(
                "ALTER TABLE tasks ADD COLUMN validation_backend "
                "TEXT NOT NULL DEFAULT 'local'"
            )
```

- [ ] **Step 5: Wire create/update/read**

`create_task` INSERT (`:820`): add `validation_backend` to the column list and a matching `?`, with value `task.validation_backend` in the params tuple (place it next to `task.validation_cmd`).

`update_task` UPDATE (`:987`): add `validation_backend = ?` to the SET list and `task.validation_backend` to the params (next to `task.validation_cmd`).

`_row_to_task` (`:293`): add after `backend=row["backend"],`:

```python
        validation_backend=row["validation_backend"],
```

- [ ] **Step 6: Run tests + types**

Run: `uv run pytest tests/test_validation_backend_persistence.py -v && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add maestro/models.py maestro/database.py tests/test_validation_backend_persistence.py
git commit -m "feat: persist Task.validation_backend (default local)"
```

---

## Task 3: Validation request + result adapter (pure helpers)

**Files:**
- Modify: `maestro/validator.py` — add `build_validation_request` and `execution_result_to_validation`.
- Test: `tests/test_validation_adapter.py`

**Interfaces:**
- Consumes: `ExecutionRequest`, `CollectPolicy` (`maestro.execution.models`), `ExecutionResult`.
- Produces:
  - `build_validation_request(task: Task, *, backend_id: str, run_id: str, attempt: int) -> ExecutionRequest`
  - `execution_result_to_validation(res: ExecutionResult) -> ValidationResult`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_validation_adapter.py
from pathlib import Path

from maestro.execution.models import ExecutionResult
from maestro.models import Task, TaskStatus
from maestro.validator import (
    build_validation_request,
    execution_result_to_validation,
)


def _task() -> Task:
    return Task(
        id="t1",
        title="t",
        prompt="p",
        workdir="/work/dir",
        status=TaskStatus.VALIDATING,
        validation_cmd="pytest -q",
    )


def test_build_request_shape():
    req = build_validation_request(
        _task(), backend_id="local", run_id="val-t1-1", attempt=1
    )
    assert req.argv == ["pytest", "-q"]
    assert req.workdir == Path("/work/dir")
    assert req.capture_output is True
    assert req.collect.mode == "none"
    assert req.inherit_env is True
    assert req.backend_id == "local"
    assert req.timeout_seconds == 300


def test_map_success():
    res = ExecutionResult(
        exit_code=0, stdout_tail="ok", stderr_tail="", output_log_path=Path("/x")
    )
    vr = execution_result_to_validation(res)
    assert vr.success is True
    assert vr.exit_code == 0
    assert vr.output == "ok"


def test_map_failure_and_timeout():
    fail = execution_result_to_validation(
        ExecutionResult(
            exit_code=2, stdout_tail="", stderr_tail="boom", output_log_path=Path("/x")
        )
    )
    assert fail.success is False
    assert fail.error_message == "Exit code: 2"
    assert fail.output == "boom"

    to = execution_result_to_validation(
        ExecutionResult(
            exit_code=None, timed_out=True, output_log_path=Path("/x")
        )
    )
    assert to.success is False
    assert to.timed_out is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_validation_adapter.py -v`
Expected: FAIL — `cannot import name 'build_validation_request'`.

- [ ] **Step 3: Implement the helpers**

Add to `validator.py` (imports at top: `from pathlib import Path` already present; add `from maestro.execution.models import CollectPolicy, ExecutionRequest, ExecutionResult` and `from maestro.models import Task`):

```python
def build_validation_request(
    task: Task,
    *,
    backend_id: str,
    run_id: str,
    attempt: int,
) -> ExecutionRequest:
    """Build the validation ExecutionRequest for a task.

    `capture_output=True` (retry context is read from the tails),
    `collect=none` (validation applies no file changes), `inherit_env=True`
    so a bare LocalBackend reproduces today's `{**os.environ, **child_env()}`
    environment exactly. Timeout mirrors the pre-slice Validator default
    (no per-task override existed).

    Raises ValueError if `validation_cmd` is empty or unparseable.
    """
    if not task.validation_cmd:
        raise ValueError("no validation_cmd")
    argv = shlex.split(task.validation_cmd)
    if not argv:
        raise ValueError("empty validation command")
    return ExecutionRequest(
        run_id=run_id,
        argv=argv,
        workdir=Path(task.workdir),
        log_path=Path(task.workdir) / f".maestro-validation-{run_id}.log",
        capture_output=True,
        inherit_env=True,
        timeout_seconds=float(Validator.DEFAULT_TIMEOUT),
        collect=CollectPolicy(mode="none"),
        backend_id=backend_id,
        entity_kind="task",
        attempt=attempt,
    )


def execution_result_to_validation(res: ExecutionResult) -> ValidationResult:
    """Map a captured ExecutionResult onto a ValidationResult."""
    success = res.exit_code == 0 and not res.timed_out
    if res.timed_out:
        error_message: str | None = "Command timed out"
    elif not success:
        error_message = f"Exit code: {res.exit_code}"
    else:
        error_message = None
    return ValidationResult(
        success=success,
        exit_code=res.exit_code,
        stdout=res.stdout_tail,
        stderr=res.stderr_tail,
        timed_out=res.timed_out,
        error_message=error_message,
    )
```

- [ ] **Step 4: Run tests + types**

Run: `uv run pytest tests/test_validation_adapter.py -v && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/validator.py tests/test_validation_adapter.py
git commit -m "feat(validator): validation ExecutionRequest + result adapter"
```

---

## Task 4: Wire `VALIDATION_STARTED` into the VALIDATING transition

**Files:**
- Modify: `maestro/transitions.py` — `TASK_EFFECTS[VALIDATING]` (`:38`).
- Test: `tests/test_validating_effect.py`

**Interfaces:**
- Produces: the `VALIDATING` transition now declares `event=EventType.VALIDATION_STARTED`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_validating_effect.py
from maestro.event_log import EventType
from maestro.models import TaskStatus
from maestro.transitions import TASK_EFFECTS


def test_validating_declares_validation_started():
    assert TASK_EFFECTS[TaskStatus.VALIDATING].event == EventType.VALIDATION_STARTED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_validating_effect.py -v`
Expected: FAIL — effect is `None`.

- [ ] **Step 3: Wire the effect**

In `transitions.py`, replace `TaskStatus.VALIDATING: StatusEffect(),` with:

```python
    TaskStatus.VALIDATING: StatusEffect(event=EventType.VALIDATION_STARTED),
```

- [ ] **Step 4: Run tests + types**

Run: `uv run pytest tests/test_validating_effect.py -v && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/transitions.py tests/test_validating_effect.py
git commit -m "feat(transitions): fire VALIDATION_STARTED on VALIDATING"
```

---

## Task 5: Preflight — SSH validation target fails loud

**Files:**
- Modify: `maestro/preflight.py` — add `check_validation_backends`.
- Modify: `maestro/scheduler.py` — call it at start, next to `validate_ssh_scopes` (`:742`).
- Test: `tests/test_validation_backend_preflight.py`

**Interfaces:**
- Consumes: `BackendResolver` (`maestro.execution.resolver`), `SshTransport` / `ExecutionConfig` (`maestro.execution.exec_config`).
- Produces: `check_validation_backends(tasks: list[Task], execution: ExecutionConfig | None) -> None` (raises `ValidationBackendError`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_validation_backend_preflight.py
import pytest

from maestro.execution.exec_config import ExecutionConfig
from maestro.models import Task, TaskStatus
from maestro.preflight import ValidationBackendError, check_validation_backends


def _task(vb: str, backend: str | None = None) -> Task:
    return Task(
        id="t1", title="t", prompt="p", workdir="/tmp", status=TaskStatus.READY,
        validation_cmd="pytest", validation_backend=vb, backend=backend,
    )


SSH_CFG = ExecutionConfig.model_validate(
    {
        "default_backend": "local",
        "backends": {
            "local": {"transport": "local", "isolation": "bare"},
            "gpu": {
                "transport": {"type": "ssh", "host": "gpu", "workdir_root": "/t"},
                "isolation": "bare",
            },
        },
    }
)


def test_explicit_ssh_validation_backend_fails():
    with pytest.raises(ValidationBackendError, match="gpu"):
        check_validation_backends([_task("gpu")], SSH_CFG)


def test_same_on_ssh_task_fails():
    with pytest.raises(ValidationBackendError):
        check_validation_backends([_task("same", backend="gpu")], SSH_CFG)


def test_local_and_default_pass():
    check_validation_backends([_task("local", backend="gpu")], SSH_CFG)
    check_validation_backends([_task("same", backend="local")], SSH_CFG)


def test_no_validation_cmd_skipped():
    t = Task(id="t", title="t", prompt="p", workdir="/tmp",
             status=TaskStatus.READY, validation_backend="gpu")
    check_validation_backends([t], SSH_CFG)  # no cmd → no gate
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_validation_backend_preflight.py -v`
Expected: FAIL — `cannot import name 'ValidationBackendError'`.

- [ ] **Step 3: Implement the check**

Add to `preflight.py` (imports: `from maestro.execution.exec_config import ExecutionConfig, SshTransport`; `from maestro.execution.resolver import BackendResolver, ExecutionConfigError`; `from maestro.models import Task`):

```python
class ValidationBackendError(Exception):
    """A task's validation_backend resolves to an unsupported (SSH) backend."""


def check_validation_backends(
    tasks: list[Task], execution: ExecutionConfig | None
) -> None:
    """Fail loud if any task's validation runs on a non-local-transport backend.

    PR1 supports validation only on transport.type == local (bare or Docker).
    `same` resolves to the task's own backend; a named value resolves directly.
    An SSH target (or any non-local transport) raises — never a silent
    fallback to local. Tasks without a validation_cmd are skipped.
    """
    resolver = BackendResolver(execution, mode="scheduler")
    registry = (execution or ExecutionConfig()).normalized()
    for task in tasks:
        if not task.validation_cmd:
            continue
        name = task.validation_backend
        resolved = task.backend if name == "same" else (
            None if name == "local" else name
        )
        spec = registry.get(resolved) if resolved is not None else None
        transport = spec.transport if spec is not None else None
        if transport is not None and isinstance(transport, SshTransport):
            raise ValidationBackendError(
                f"task '{task.id}': validation_backend '{name}' resolves to "
                f"backend '{resolved}' (transport ssh:{transport.host}); SSH "
                f"validation is a PR2 follow-up. Use validation_backend: local."
            )
```

- [ ] **Step 4: Wire the scheduler-start gate**

In `scheduler.py`, find the `validate_ssh_scopes(tasks, self._execution)` call (`:742`) and add immediately after it:

```python
            check_validation_backends(tasks, self._execution)
```

Add the import near the other preflight/execution imports:

```python
from maestro.preflight import check_validation_backends
```

- [ ] **Step 5: Run tests + types**

Run: `uv run pytest tests/test_validation_backend_preflight.py -v && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add maestro/preflight.py maestro/scheduler.py tests/test_validation_backend_preflight.py
git commit -m "feat(preflight): fail-loud on SSH validation_backend (scheduler gate)"
```

---

## Task 6: Local validation through the execution layer (behavior-preserving)

**Files:**
- Modify: `maestro/scheduler.py` — add `_resolve_validation_backend`; rewrite `_run_validation` (`:1843`) to route through the resolved backend for the **local** case.
- Test: `tests/test_run_validation_local.py`

**Interfaces:**
- Consumes: `build_validation_request`, `execution_result_to_validation` (Task 3); `BackendResolver.resolve` (already on `self._backends`).
- Produces: `Scheduler._resolve_validation_backend(task) -> ExecutionBackend`; `_run_validation` returns a `ValidationResult` for local backends via `backend.run → handle.wait`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_validation_local.py
import pytest

from maestro.models import Task, TaskStatus
from maestro.scheduler import Scheduler


@pytest.fixture
async def scheduler(tmp_path):
    db_path = tmp_path / "m.db"
    sch = Scheduler(db_path=db_path)
    await sch.setup()
    yield sch
    await sch.teardown()


def _task(tmp_path, cmd: str, vb: str = "local") -> Task:
    return Task(
        id="t1", title="t", prompt="p", workdir=str(tmp_path),
        status=TaskStatus.VALIDATING, validation_cmd=cmd, validation_backend=vb,
    )


async def test_local_validation_passes(scheduler, tmp_path):
    res = await scheduler._run_validation(_task(tmp_path, "true"))
    assert res.success is True
    assert res.exit_code == 0


async def test_local_validation_fails_with_output(scheduler, tmp_path):
    res = await scheduler._run_validation(
        _task(tmp_path, "sh -c 'echo boom >&2; exit 3'")
    )
    assert res.success is False
    assert res.exit_code == 3
    assert "boom" in res.output


async def test_no_cmd_is_success(scheduler, tmp_path):
    t = Task(id="t", title="t", prompt="p", workdir=str(tmp_path),
             status=TaskStatus.VALIDATING)
    res = await scheduler._run_validation(t)
    assert res.success is True
```

> Note: `sh -c '…'` here is the **test command string**, split by `shlex` into `["sh","-c","echo boom >&2; exit 3"]` — a normal argv, not a shell wrapper added by the adapter.

Adjust `Scheduler(...)`/`setup()` in the fixture to match the real constructor if it differs; use the same construction the existing scheduler tests use.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_run_validation_local.py -v`
Expected: FAIL — `_resolve_validation_backend` missing / behavior mismatch.

- [ ] **Step 3: Implement resolver + local run**

In `scheduler.py`, add imports:

```python
from maestro.validator import (
    ValidationResult,
    Validator,
    build_validation_request,
    execution_result_to_validation,
)
```

Add the resolver method:

```python
    def _resolve_validation_backend(self, task: Task):
        """Resolve the backend for a task's validation run.

        'local' -> the bare LocalBackend; 'same' -> the task's own backend;
        a named value -> that backend. Preflight has already rejected any
        SSH target, so this only yields local-transport backends.
        """
        name = task.validation_backend
        if name == "local":
            return self._backends.resolve("local")
        if name == "same":
            return self._backends.resolve(task.backend)
        return self._backends.resolve(name)
```

Rewrite `_run_validation` (`:1843`):

```python
    async def _run_validation(self, task: Task) -> ValidationResult:
        """Run validation for a task through the execution layer.

        No validation_cmd -> trivially successful. Local backend -> run and
        wait (no durable handle; behavior-preserving). Non-local backend ->
        the durable path (see `_run_durable_validation`).
        """
        if not task.validation_cmd:
            return ValidationResult(
                success=True, exit_code=0, stdout="", stderr=""
            )
        backend = self._resolve_validation_backend(task)
        run_id = f"val-{task.id}-{task.retry_count + 1}"
        try:
            request = build_validation_request(
                task, backend_id=backend.id, run_id=run_id,
                attempt=task.retry_count + 1,
            )
        except ValueError as e:
            return ValidationResult(
                success=False, exit_code=None, stdout="", stderr="",
                error_message=str(e),
            )
        if backend.id == "local":
            handle = await backend.run(request)
            result = await handle.wait()
            return execution_result_to_validation(result)
        return await self._run_durable_validation(task, backend, request)
```

Add a temporary stub so the module imports (replaced in Task 7):

```python
    async def _run_durable_validation(self, task, backend, request):
        raise NotImplementedError("durable validation lands in Task 7")
```

- [ ] **Step 4: Run tests + types**

Run: `uv run pytest tests/test_run_validation_local.py -v && uv run pyrefly check`
Expected: PASS (durable path is untested here).

- [ ] **Step 5: Commit**

```bash
git add maestro/scheduler.py tests/test_run_validation_local.py
git commit -m "feat(scheduler): route local validation through LocalBackend"
```

---

## Task 7: Durable validation lifecycle + launch taxonomy

**Files:**
- Modify: `maestro/scheduler.py` — `_run_durable_validation`; the VALIDATING-transition split + launch taxonomy in `_handle_task_completion` (`:1519`); a `_validation_hold: set[str]` on the scheduler; reservation-release guard in `_monitor_running_tasks` (`:1443`).
- Test: `tests/test_durable_validation.py`

**Interfaces:**
- Consumes: `Database.start_execution(..., execution_phase="validation")` (Task 1); `_dispatch_committed_transition` (`:362`); `_finalize_running` / `ensure_finalize_task`; `LaunchNotStarted` (`ssh_backend`).
- Produces: `_run_durable_validation(task, backend, request) -> ValidationResult | None` (None = infra failure, task already routed to `NEEDS_REVIEW`); `self._validation_hold` names tasks whose `(workdir, scope)` reservation must not be released.

- [ ] **Step 1: Write the failing test (fake backend)**

```python
# tests/test_durable_validation.py
import pytest

from maestro.execution.models import (
    CollectResult, ExecutionHandleRef, ExecutionResult,
)
from maestro.execution.ssh_backend import LaunchNotStarted
from maestro.models import Task, TaskStatus
from maestro.scheduler import Scheduler
from datetime import UTC, datetime


class _FakeHandle:
    def __init__(self, result):
        self._r = result
        self.ref = ExecutionHandleRef(
            backend_id="sandbox", run_id="val", transport_ref="sandbox:val",
            started_at=datetime.now(UTC),
        )
    @property
    def os_pid(self): return None
    def poll(self): return self._r.exit_code
    async def wait(self): return self._r
    async def terminate(self, grace_seconds): ...
    async def kill(self): ...
    async def collect(self): return CollectResult(applied=False)
    async def cleanup(self): ...


class _FakeBackend:
    id = "sandbox"
    def __init__(self, *, result=None, raise_launch=None):
        self._result = result
        self._raise = raise_launch
    async def run(self, req):
        if self._raise is not None:
            raise self._raise
        return _FakeHandle(self._result)


@pytest.fixture
async def scheduler(tmp_path):
    sch = Scheduler(db_path=tmp_path / "m.db")
    await sch.setup()
    yield sch
    await sch.teardown()


async def _seed_validating(sch, tmp_path):
    task = Task(id="t1", title="t", prompt="p", workdir=str(tmp_path),
                status=TaskStatus.RUNNING, validation_cmd="pytest",
                validation_backend="sandbox", backend="sandbox")
    await sch._db.create_task(task)
    return task


async def test_durable_validation_success_marks_handle_cleaned(scheduler, tmp_path):
    task = await _seed_validating(scheduler, tmp_path)
    backend = _FakeBackend(result=ExecutionResult(
        exit_code=0, stdout_tail="ok", output_log_path=tmp_path / "l"))
    req = __import__("maestro.validator", fromlist=["build_validation_request"]) \
        .build_validation_request(task, backend_id="sandbox", run_id="v1", attempt=1)
    res = await scheduler._run_durable_validation(task, backend, req)
    assert res is not None and res.success is True
    rows = await scheduler._db.get_open_execution_handles()
    assert all(r["execution_phase"] != "validation" for r in rows)  # cleaned


async def test_launch_not_started_routes_review_no_hold(scheduler, tmp_path):
    task = await _seed_validating(scheduler, tmp_path)
    backend = _FakeBackend(raise_launch=LaunchNotStarted("nope"))
    req = __import__("maestro.validator", fromlist=["build_validation_request"]) \
        .build_validation_request(task, backend_id="sandbox", run_id="v1", attempt=1)
    res = await scheduler._run_durable_validation(task, backend, req)
    assert res is None
    got = await scheduler._db.get_task("t1")
    assert got.status == TaskStatus.NEEDS_REVIEW
    assert "t1" not in scheduler._validation_hold


async def test_unknown_launch_holds_and_preserves_handle(scheduler, tmp_path):
    task = await _seed_validating(scheduler, tmp_path)
    backend = _FakeBackend(raise_launch=RuntimeError("handshake lost"))
    req = __import__("maestro.validator", fromlist=["build_validation_request"]) \
        .build_validation_request(task, backend_id="sandbox", run_id="v1", attempt=1)
    res = await scheduler._run_durable_validation(task, backend, req)
    assert res is None
    got = await scheduler._db.get_task("t1")
    assert got.status == TaskStatus.NEEDS_REVIEW
    assert "t1" in scheduler._validation_hold
    rows = await scheduler._db.get_open_execution_handles()
    assert any(r["execution_phase"] == "validation"
               and r["state"] == "prepared" for r in rows)  # preserved
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_durable_validation.py -v`
Expected: FAIL — `_run_durable_validation` raises `NotImplementedError`; `_validation_hold` missing.

- [ ] **Step 3: Add the hold set to the scheduler**

In `Scheduler.__init__` (near `self._reservations = ReservationRegistry()`, `:293`):

```python
        self._validation_hold: set[str] = set()
```

- [ ] **Step 4: Implement `_run_durable_validation`**

```python
    async def _run_durable_validation(
        self, task: Task, backend, request
    ) -> ValidationResult | None:
        """Durable validation on a non-local backend.

        Atomically mints the RUNNING->VALIDATING transition together with a
        `validation`-phase execution_handles row, dispatches the committed
        transition (fires VALIDATION_STARTED), runs, and finalizes
        (wait -> terminal -> collect(none) -> collected -> cleanup -> cleaned).

        Returns the ValidationResult on a real pass/fail. Returns None when a
        launch failure routes the task to NEEDS_REVIEW (infrastructure
        failure, not a code validation failure).
        """
        execution_id = str(uuid.uuid4())
        attempt = task.retry_count + 1
        request = request.model_copy(
            update={"execution_id": execution_id, "attempt": attempt}
        )
        await self._db.start_execution(
            entity_kind="task",
            entity_id=task.id,
            expected_status=TaskStatus.RUNNING.value,
            running_status=TaskStatus.VALIDATING.value,
            execution_id=execution_id,
            backend_id=backend.id,
            transport_ref=f"{backend.id}:maestro-{execution_id}",
            attempt=attempt,
            execution_phase="validation",
        )
        validating = await self._db.get_task(task.id)
        await self._dispatch_committed_transition(
            validating, frm=TaskStatus.RUNNING
        )

        try:
            handle = await backend.run(request)
        except LaunchNotStarted:
            # Provably never launched: close the handle deterministically,
            # release is allowed (no live container), route to NEEDS_REVIEW.
            # Not a task attempt -> no retry consumed.
            await self._db.mark_execution_state(
                execution_id, "terminal", allowed_from=["prepared", "running"]
            )
            await self._db.mark_execution_state(
                execution_id, "cleaned", allowed_from=["terminal"]
            )
            await self._route_validation_infra_review(
                task.id, "validation launch provably not started"
            )
            return None
        except Exception:
            # Uncertain: a container may be live. Preserve the prepared handle,
            # HOLD the reservation, route immediately to NEEDS_REVIEW (no wait
            # for recovery). Recovery re-holds + probes the open handle.
            self._validation_hold.add(task.id)
            await self._route_validation_infra_review(
                task.id, "validation launch result unknown"
            )
            return None

        running_val = RunningTask(
            task=validating, handle=handle,
            started_at=datetime.now(UTC), log_file=request.log_path,
            execution_id=execution_id, backend_id=backend.id,
        )
        fin = await self._finalize_running(running_val)
        if fin.cleaned:
            await self._db.mark_execution_state(
                execution_id, "cleaned", allowed_from=["collected"]
            )
        return execution_result_to_validation(fin.execution)
```

Add the infra-review helper:

```python
    async def _route_validation_infra_review(self, task_id: str, reason: str) -> None:
        """Route a validation infrastructure failure to NEEDS_REVIEW.

        VALIDATING -> FAILED -> NEEDS_REVIEW (VALIDATING has no direct edge to
        NEEDS_REVIEW). This is NOT a code validation failure and consumes no
        task retry; there is no validation-only re-run in PR1 (documented
        limitation), so it is fail-closed for a human decision.
        """
        message = f"validation infrastructure failure: {reason}"
        await self._transition(
            task_id, TaskStatus.FAILED,
            expected_status=TaskStatus.VALIDATING,
            message=message, error_message=message,
        )
        await self._transition(
            task_id, TaskStatus.NEEDS_REVIEW,
            expected_status=TaskStatus.FAILED,
            message=message, error_message=message,
        )
```

Ensure `uuid` is imported in `scheduler.py` (it is — used at `:1221`).

- [ ] **Step 5: Split the VALIDATING transition in `_handle_task_completion`**

The durable path does its OWN RUNNING→VALIDATING mint, so the caller must not also plain-transition for non-local validation. Replace the block at `:1521-1551`:

```python
            if task.validation_cmd:
                backend = self._resolve_validation_backend(task)
                if backend.id == "local":
                    await self._transition(
                        task_id, TaskStatus.VALIDATING,
                        expected_status=TaskStatus.RUNNING,
                    )
                    validation_result = await self._run_validation(task)
                else:
                    validation_result = await self._run_durable_validation(
                        task, backend,
                        build_validation_request(
                            task, backend_id=backend.id,
                            run_id=f"val-{task_id}-{task.retry_count + 1}",
                            attempt=task.retry_count + 1,
                        ),
                    )
                    if validation_result is None:
                        return  # infra failure: already routed to NEEDS_REVIEW
                if validation_result.success:
                    done_task = await self._transition(
                        task_id, TaskStatus.DONE,
                        expected_status=TaskStatus.VALIDATING,
                        result_summary="Task completed successfully",
                    )
                    self._auto_commit_task(task)
                    outcome = await self._build_outcome(done_task, exit_code=0)
                    await self._try_report_outcome(done_task, outcome)
                    _obs_log.info(
                        "task.completed", task_id=task_id,
                        agent=task.routed_agent_type or task.agent_type.value,
                        validation_passed=True,
                        backend_id=running_task.backend_id,
                    )
                else:
                    error_msg = self._format_validation_error(validation_result)
                    await self._handle_validation_failure(
                        task_id, task, error_msg, validation_result
                    )
```

> `build_validation_request` may raise `ValueError` only for an empty/unparseable command; `task.validation_cmd` is truthy in this branch and the local path already maps `ValueError`. For the durable branch, wrap the `build_validation_request(...)` call in the same `try/except ValueError` returning a failed `ValidationResult` if you want symmetry; a well-formed cmd cannot hit it.

- [ ] **Step 6: Guard the reservation release**

In `_monitor_running_tasks`, the release decision (`:1443`):

```python
                if exec_id is None or fin.collect_succeeded:
                    if task_id not in self._validation_hold:
                        to_release.append(task_id)
```

Add a short comment: a held validation (uncertain launch) keeps the `(workdir, scope)` reservation until cleanup/recovery frees it (detail 6).

- [ ] **Step 7: Run tests + types**

Run: `uv run pytest tests/test_durable_validation.py tests/test_run_validation_local.py -v && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add maestro/scheduler.py tests/test_durable_validation.py
git commit -m "feat(scheduler): durable validation lifecycle + launch taxonomy"
```

---

## Task 8: Recovery — phase-split handle selection

**Files:**
- Modify: `maestro/recovery.py` — split `task_handles` by `execution_phase` (`:127`); `_recover_running_tasks` uses the task-phase map, `_recover_validating_tasks` uses the validation-phase map (`:161`, `:190`).
- Test: `tests/test_recovery_validation_phase.py`

**Interfaces:**
- Consumes: `get_open_execution_handles()` rows carrying `execution_phase` (Task 1).
- Produces: recovery selects the validation-phase handle for a `VALIDATING` task.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_recovery_validation_phase.py
import pytest
from unittest.mock import AsyncMock

from maestro.database import Database
from maestro.models import Task, TaskStatus
from maestro.recovery import RecoveryManager


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "m.db")
    await d.connect()
    await d.initialize_schema()
    yield d
    await d.close()


async def test_validating_recovery_selects_validation_handle(db, monkeypatch):
    # A task with BOTH a stale open task-phase handle and a live validation
    # handle. Recovery for VALIDATING must probe the VALIDATION handle.
    await db.create_task(
        Task(id="t1", title="t", prompt="p", workdir="/tmp",
             status=TaskStatus.RUNNING, backend="sandbox")
    )
    await db.start_execution(
        entity_kind="task", entity_id="t1", expected_status="running",
        running_status="running", execution_id="e-task", backend_id="sandbox",
        transport_ref="sandbox:e-task", attempt=1, execution_phase="task",
    )
    await db.update_task_status("t1", TaskStatus.VALIDATING,
                                expected_status=TaskStatus.RUNNING)
    await db.start_execution(
        entity_kind="task", entity_id="t1", expected_status="validating",
        running_status="validating", execution_id="e-val", backend_id="sandbox",
        transport_ref="sandbox:e-val", attempt=1, execution_phase="validation",
    )

    mgr = RecoveryManager(db, docker=AsyncMock())
    probed: list[str] = []

    async def fake_probe(execution_id, docker):
        probed.append(execution_id)
        class V: needs_review = True; reason = "container alive"
        return V()

    monkeypatch.setattr("maestro.recovery.probe_execution", fake_probe)
    await mgr.recover()

    assert probed == ["e-val"]  # validation handle, not the stale task handle
    got = await db.get_task("t1")
    assert got.status == TaskStatus.NEEDS_REVIEW
```

Match `RecoveryManager` construction to the real class name/constructor used in existing recovery tests.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_recovery_validation_phase.py -v`
Expected: FAIL — recovery probes `e-task` (or ambiguous) instead of `e-val`.

- [ ] **Step 3: Split the map by phase**

In `recovery.py` `recover()` (`:121-137`), replace the single `task_handles` construction and the two recovery calls:

```python
        open_handles = await self._db.get_open_execution_handles()

        def _by_phase(phase: str) -> dict[str, dict[str, Any]]:
            return {
                h["entity_id"]: h
                for h in open_handles
                if h["entity_kind"] == "task"
                and h["state"] in ("prepared", "running")
                and h.get("execution_phase", "task") == phase
            }

        task_phase = _by_phase("task")
        validation_phase = _by_phase("validation")

        running_recovered = await self._recover_running_tasks(task_phase)
        validating_recovered = await self._recover_validating_tasks(validation_phase)
```

`_recover_running_tasks` and `_recover_validating_tasks` keep their signatures — they already accept a `task_handles` map and pass it to `_route_docker_task_to_review`. No change to those bodies.

- [ ] **Step 4: Run tests + types**

Run: `uv run pytest tests/test_recovery_validation_phase.py -v && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/recovery.py tests/test_recovery_validation_phase.py
git commit -m "feat(recovery): select the validation-phase handle for VALIDATING"
```

---

## Task 9: Reservation re-hold for an open validation handle (recovery)

**Files:**
- Modify: `maestro/scheduler.py` — `_reconstruct_reservations` (`:747`) must re-hold for an open **validation**-phase handle too (it iterates open handles; ensure the phase does not exclude it and the SSH-task gate still applies to the reachable case).
- Test: `tests/test_reservation_rehold_validation.py`

**Interfaces:**
- Consumes: `get_open_execution_handles()` rows with `execution_phase`.
- Produces: on restart, an open validation handle for an SSH task re-holds its `(workdir, scope)` reservation.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reservation_rehold_validation.py
import pytest

from maestro.models import Task, TaskStatus
from maestro.scheduler import Scheduler


@pytest.fixture
async def scheduler(tmp_path):
    sch = Scheduler(db_path=tmp_path / "m.db")
    await sch.setup()
    yield sch
    await sch.teardown()


async def test_open_validation_handle_reholds_reservation(scheduler, tmp_path, monkeypatch):
    # An SSH task (armed workdir) whose validation handle is still open must
    # have its (workdir, scope) reservation reconstructed on restart.
    task = Task(id="t1", title="t", prompt="p", workdir=str(tmp_path),
                status=TaskStatus.VALIDATING, scope=["src/**"], backend="gpu")
    await scheduler._db.create_task(task)
    await scheduler._db.start_execution(
        entity_kind="task", entity_id="t1", expected_status="validating",
        running_status="validating", execution_id="e-val", backend_id="gpu",
        transport_ref="gpu:e-val", attempt=1, execution_phase="validation",
    )
    # Force the armed + ssh-task predicates true for this task's workdir.
    monkeypatch.setattr(scheduler, "_is_armed", lambda t: True)
    monkeypatch.setattr("maestro.scheduler.is_ssh_task", lambda t, e: True)

    await scheduler._reconstruct_reservations()
    # The reservation registry now holds t1 (a second acquire must fail).
    from maestro.execution.reservations import scope_to_reservation
    assert not scheduler._reservations.try_acquire(
        "other", scope_to_reservation(str(tmp_path), ["src/**"])
    )
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `uv run pytest tests/test_reservation_rehold_validation.py -v`
Expected: If `_reconstruct_reservations` already re-holds from any open non-local handle (it iterates all phases), this may PASS as-is — confirming detail 6's recovery half needs no code change beyond the phase column. If it FAILS (e.g. a phase filter excludes validation), proceed to Step 3.

- [ ] **Step 3: Ensure phase-agnostic re-hold**

Confirm `_reconstruct_reservations` (`:757-772`) does **not** filter by `execution_phase` (it must re-hold both phases). If any such filter exists, remove it. Add a clarifying comment:

```python
        # Any open (workdir, scope)-holding handle re-holds the reservation,
        # regardless of execution_phase: a still-open *validation* handle for
        # an SSH task keeps the scope locked exactly as a task handle does
        # (detail 6).
```

- [ ] **Step 4: Run tests + types**

Run: `uv run pytest tests/test_reservation_rehold_validation.py -v && uv run pyrefly check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/scheduler.py tests/test_reservation_rehold_validation.py
git commit -m "test(scheduler): validation handle re-holds (workdir,scope) reservation"
```

---

## Task 10: Full suite, docs, and CLAUDE.md note

**Files:**
- Modify: `maestro/CLAUDE.md` — one line on `validation_backend`.
- Test: whole suite.

- [ ] **Step 1: Run the full suite + types + lint**

Run: `uv run pytest -q && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: all green. Fix any regressions (formatting first, then types, then lint — per repo conventions).

- [ ] **Step 2: Add the CLAUDE.md note**

Under the scheduler-mode description in `maestro/CLAUDE.md`, add:

```markdown
- **Validation backend (PR1):** post-task validation runs through the execution
  layer as a second ExecutionRequest (`validation_backend: local | same | <name>`,
  default `local`). Non-local (docker/named-local) validation is durable (own
  execution_id + `execution_phase='validation'` handle, recovered by the Docker
  probe). SSH validation targets fail loud at scheduler start (PR2 follow-up;
  PR2 also flips the default to `same`).
```

- [ ] **Step 3: Commit**

```bash
git add maestro/CLAUDE.md
git commit -m "docs: note validation_backend (PR1) in CLAUDE.md"
```

- [ ] **Step 4: Push + open PR**

```bash
git push -u origin feat/validation-backend
gh pr create --title "feat: validation_backend PR1 — validation through the execution layer (local transports, durable)" --body "$(cat <<'BODY'
Implements PR1 of the validation_backend slice (spec:
docs/superpowers/specs/2026-07-25-validation-backend-slice-design.md).

- validation_backend: local | same | <name>, default local (backward-compatible).
- Non-local targets allowed only for transport.type == local (incl. Docker);
  SSH targets fail loud at scheduler start (no silent same->local).
- Durable validation lifecycle: own execution_id + execution_phase='validation'
  handle, atomic RUNNING->VALIDATING mint + committed dispatch (fires
  VALIDATION_STARTED), finalize (terminal->collected->cleaned), launch taxonomy
  (LaunchNotStarted -> NEEDS_REVIEW no retry; unknown -> preserved handle +
  reservation hold + NEEDS_REVIEW).
- Recovery selects the validation-phase handle for VALIDATING; reservation
  re-held for an open validation handle.

PR2 follow-up: SSH validation (fresh remote layout, real CollectPolicy(none),
SSH recovery), then flip default to same with a release note.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```

- [ ] **Step 5: Read Copilot review, iterate**

Follow the repo git-workflow rule: read GitHub Copilot's review; fix valid comments with new commits on this branch; rebut invalid ones. Do not merge — the user merges.

---

## Self-Review

**Spec coverage:**
- Contract (`local|same|<name>`, default local) → Tasks 2, 6. ✓
- Persistence / resume → Task 2. ✓
- Preflight SSH fail-loud (scheduler-only gate) → Task 5. ✓
- Adapter + capture_output + retry context → Task 3 (map), Task 6/7 (wired). ✓
- Durable mint + committed dispatch (detail 2) + VALIDATION_STARTED wiring → Tasks 4, 7. ✓
- Finalize callbacks between phases (detail 1) → Task 7 (`_finalize_running` reused). ✓
- Launch taxonomy (detail 3) → Task 7. ✓
- `execution_phase` in queries + recovery selection (detail 4) → Tasks 1, 8. ✓
- `backend.id != "local"` literal (detail 5) → Task 6 (`_resolve` + `backend.id == "local"` branch). ✓
- Reservation across validation (detail 6) → Task 7 (release guard) + Task 9 (re-hold). ✓
- Migrations idempotent → Tasks 1, 2. ✓
- Non-goals (SSH exec, real ssh collect=none, default flip) → untouched. ✓

**Placeholder scan:** the Task 6 `_run_durable_validation` stub is intentionally replaced in Task 7 (noted inline) — not a lingering placeholder. No TBD/TODO left.

**Type consistency:** `build_validation_request` / `execution_result_to_validation` signatures match between Task 3 (definition) and Tasks 6–7 (use). `start_execution(execution_phase=...)` matches between Task 1 and Task 7. `_validation_hold` defined in Task 7 Step 3, used in Task 7 Step 6. `_resolve_validation_backend` defined Task 6, used Task 7.
