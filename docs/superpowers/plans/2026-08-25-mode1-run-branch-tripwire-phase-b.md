# Mode-1 Run-Branch Tripwire (Phase B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every point where the Mode-1 scheduler is about to use the shared checkout verifies "branch name AND tip == the run's record" first; a mismatch suspends the run with a drain (never a kill), leaving every task pre-terminal for resume.

**Architecture:** Phase A (PR #222) closed the start/continuation seams; this phase closes the mid-run window. A pure live-check (`check_live`) joins `maestro/run_branch_gate.py`; the `Scheduler` grows a sticky `_branch_tripwire(seam)` guard invoked at five seams (spawn, collect, validation launch, verifier preflight, success finalization), a suspension drain mode in the main loop, and a gated success-tail reorder (tripwire → auto-commit → `DONE`). The seam inventory is test-asserted the way `transitions.py` totality is. No schema migration: reuses `run.run_branch*` columns and `set_run_suspended` from spec §B.1.1. The spec gets revision 9 recording what the implementation actually does (R14 attribution in §6, logger-based §8 events, the §7 decisions below).

**Tech Stack:** Python 3.12+, asyncio scheduler, SQLite (aiosqlite), pytest (+anyio), real temp git repos in tests.

**Spec:** `docs/superpowers/specs/2026-08-24-mode1-run-branch-isolation-design.md` (§7 is the contract; §6/§8 get the revision-9 updates in Task 8). Issue thread: TODO item `@id:mode1-run-branch-tripwire`.

## Global Constraints

- **Opt-out byte-identical:** `git.run_branch` absent → every tripwire is a no-op and the success tail keeps today's `DONE → auto-commit` order. The untouched existing suite staying green is the evidence (spec §9).
- **Never kill on mismatch:** a trip suspends and drains; running processes are not terminated by the gate (spec §7, consumer answer 2). The task's own timeout keeps its pre-existing terminate.
- **Name AND tip at every seam:** compare both against `run.run_branch_head` (spec §7, round-5 major 2). Two `git` invocations per seam.
- **Pre-terminal preservation:** on a trip, the pending mutation does not happen — no spawn, no collect, no validation launch, no verifier, no commit, no terminal transition (spec §7, round-3 major 1).
- **Fail-closed:** a gated run whose run row cannot be read at a seam trips; it never proceeds on missing evidence.
- **Refusal surface:** plain text on stderr via `err_console` with `escape()` (branch names are operator strings), exit code 1. Structured obs events are best-effort telemetry; the stderr text is the contract (spec §8).
- **Line length 88, type hints everywhere, `uv run pyrefly check` + `uv run ruff check .` clean after every task. pytest in the FOREGROUND only (workspace watchdog kills background runs).**
- **Git workflow:** branch `feat/mode1-run-branch-tripwire`, commits per task, PR at the end (no direct master commits, no self-merge).

---

### Task 0: Branch

- [ ] **Step 1: Create the feature branch**

```bash
git switch master && git pull --ff-only
git switch -c feat/mode1-run-branch-tripwire
```

---

### Task 1: `check_live` — the pure live check

**Files:**
- Modify: `maestro/run_branch_gate.py` (append after `verify_continuation`)
- Test: `tests/test_run_branch_gate.py` (append a `TestCheckLive` class)

**Interfaces:**
- Consumes: existing `RunBranchRecord`, `RunBranchGateError`, `read_snapshot`-style git helpers (`_run_git`, `branch_tip`), `_emit`.
- Produces: `check_live(workdir: Path, record: RunBranchRecord) -> None` — raises `RunBranchGateError` with reason `live_branch_mismatch` (name moved) or `live_stale_checkout` (tip moved); returns `None` on pass. Emits `run_branch_gate.refused` itself on raise (same pattern as `verify_continuation`); emits nothing on pass — this runs at every seam and a per-seam `.verified` would be noise.

- [ ] **Step 1: Write the failing tests**

The existing file already has a `repo` fixture-style helper (`git init -b master` + one commit) and a `_git` runner — reuse them exactly as `TestVerifyContinuation` does. Add:

```python
class TestCheckLive:
    """Spec §7: the per-seam live tripwire — name AND tip vs the record."""

    def test_passes_when_name_and_tip_match(self, repo: Path) -> None:
        _git(repo, "switch", "-c", "pilot/x")
        tip = _git(repo, "rev-parse", "refs/heads/pilot/x")
        check_live(repo, RunBranchRecord(branch="pilot/x", head=tip))  # no raise

    def test_branch_flip_trips_with_live_branch_mismatch(self, repo: Path) -> None:
        _git(repo, "switch", "-c", "pilot/x")
        tip = _git(repo, "rev-parse", "refs/heads/pilot/x")
        _git(repo, "switch", "master")
        with pytest.raises(RunBranchGateError) as exc:
            check_live(repo, RunBranchRecord(branch="pilot/x", head=tip))
        assert exc.value.reason == "live_branch_mismatch"

    def test_foreign_commit_same_branch_trips_stale(self, repo: Path) -> None:
        """Round-5 major 2: a commit on the SAME branch moves the state."""
        _git(repo, "switch", "-c", "pilot/x")
        recorded = _git(repo, "rev-parse", "refs/heads/pilot/x")
        (repo / "foreign.txt").write_text("x")
        _git(repo, "add", "foreign.txt")
        _git(repo, "commit", "-m", "foreign")
        with pytest.raises(RunBranchGateError) as exc:
            check_live(repo, RunBranchRecord(branch="pilot/x", head=recorded))
        assert exc.value.reason == "live_stale_checkout"

    def test_none_head_degrades_to_name_only(self, repo: Path) -> None:
        _git(repo, "switch", "-c", "pilot/x")
        (repo / "b.txt").write_text("b")
        _git(repo, "add", "b.txt")
        _git(repo, "commit", "-m", "moved")
        check_live(repo, RunBranchRecord(branch="pilot/x", head=None))  # no raise

    def test_detached_head_trips_mismatch(self, repo: Path) -> None:
        tip = _git(repo, "rev-parse", "HEAD")
        _git(repo, "switch", "-c", "pilot/x")
        _git(repo, "checkout", "--detach", tip)
        with pytest.raises(RunBranchGateError) as exc:
            check_live(repo, RunBranchRecord(branch="pilot/x", head=tip))
        assert exc.value.reason == "live_branch_mismatch"
```

Add `check_live` to the file's imports from `maestro.run_branch_gate`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_run_branch_gate.py -k TestCheckLive -v`
Expected: FAIL — `ImportError: cannot import name 'check_live'`.

- [ ] **Step 3: Implement `check_live`**

Append to `maestro/run_branch_gate.py`:

```python
def check_live(workdir: Path, record: RunBranchRecord) -> None:
    """Spec §7 per-seam tripwire: branch name AND tip must equal the record.

    §6's invariant is state immobility, not name stability — a foreign
    commit landed on the *same* branch mid-run moves the state as surely
    as a flip (round-5 major 2). The run's own commits keep the recorded
    head current (`on_auto_commit`), so only foreign movement trips.
    Raises on mismatch (emitting the refusal event, like
    `verify_continuation`); a pass emits nothing — this runs at every
    seam and a per-seam `.verified` would be noise. A `None` head
    degrades to name-only: phase A always records a head for a bound
    run, so this is tolerance for a hand-edited row, not a mode.
    """
    try:
        head = _run_git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD")
        current = head.stdout.strip() if head.returncode == 0 else None
        if current != record.branch:
            raise RunBranchGateError(
                "live_branch_mismatch",
                f"checkout moved to {current!r} mid-run but the run is "
                f"bound to {record.branch!r}; run: git switch {record.branch}",
            )
        tip = branch_tip(workdir, record.branch)
        if record.head is not None and tip != record.head:
            raise RunBranchGateError(
                "live_stale_checkout",
                f"branch {record.branch!r} tip moved mid-run: recorded "
                f"{record.head[:12]}, observed {tip[:12]} — foreign "
                "movement on the run branch",
            )
    except RunBranchGateError as e:
        _emit("run_branch_gate.refused", reason=e.reason, branch=record.branch)
        raise
```

Also update the module docstring's first line to mention §7 alongside §4/§6.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_run_branch_gate.py -v` then `uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add maestro/run_branch_gate.py tests/test_run_branch_gate.py
git commit -m "feat(gate): check_live — spec §7 name-AND-tip live check"
```

---

### Task 2: Scheduler tripwire plumbing (`_branch_tripwire`, sticky suspension)

**Files:**
- Modify: `maestro/scheduler.py` — `SchedulerConfig` (~line 253), `Scheduler.__init__`, new module constant + method; imports (`RunBranchGateError`, `RunBranchRecord`, `check_live` from `maestro.run_branch_gate`)
- Test: `tests/test_run_branch_tripwire.py` (new file)

**Interfaces:**
- Consumes: `check_live` (Task 1), `Database.get_run_row()`, `Database.set_run_suspended(suspended_at=..., suspend_reason=...)` (both exist).
- Produces:
  - `SchedulerConfig.run_branch: str | None = None` — the bound branch; `None` = ungated.
  - `CHECKOUT_SEAMS: frozenset[str]` module constant = `{"spawn", "collect", "validation", "verifier_preflight", "success_finalize"}`.
  - `Scheduler.branch_trip: RunBranchGateError | None` public attribute (initialized `None` in `__init__`).
  - `async Scheduler._branch_tripwire(seam: str) -> bool` — `True` = proceed, `False` = suspended (later tasks call this at each seam).

Note on the seam set: spec §7 names four seams; `collect` is the fifth, discovered here — `_finalize_running` applies a remote execution's results into the working tree, which §6 itself names as checkout-mutating ("finalizing open SSH handles collects task results into the working tree"). Round-2 major 2's rule is "every point where the scheduler is about to use the checkout", so it claims a tripwire; Task 8 records it in the spec.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_run_branch_tripwire.py`. Follow `tests/test_scheduler.py`'s conventions: the autouse `_fake_execution_backend` fixture, `_git` helper, a real repo builder, and a closed-database fixture (`yield d; await d.close()` — mandatory, an unclosed aiosqlite connection hangs ~120s).

```python
"""Phase B of the run-branch gate (spec §7): per-seam tripwires.

Scheduler-level: a real temp git repo + a real Database carrying a bound
run row, a MagicMock spawner — mirrors tests/test_scheduler.py's fixtures.
"""

import subprocess
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from maestro.dag import DAG
from maestro.database import Database, create_database
from maestro.models import AgentType, Task, TaskStatus
from maestro.run_branch_gate import RunBranchGateError
from maestro.scheduler import CHECKOUT_SEAMS, Scheduler, SchedulerConfig
from tests.fakes.fake_execution_backend import FakeExecutionBackend


@pytest.fixture(autouse=True)
def _fake_execution_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "maestro.execution.resolver.LocalBackend", FakeExecutionBackend
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _make_repo_on_branch(base_dir: Path, branch: str = "pilot/x") -> Path:
    repo = base_dir / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    _git(repo, "switch", "-c", branch)
    return repo


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[Database, None]:
    d = await create_database(tmp_path / "test.db")
    yield d
    await d.close()


async def _bound_db(db: Database, repo: Path, branch: str = "pilot/x") -> Database:
    """Write the run row phase A would have written for a gated fresh start."""
    tip = _git(repo, "rev-parse", f"refs/heads/{branch}")
    await db.create_run_row(
        run_id="01TESTRUN",
        repo_key="test/repo",
        started_at=datetime.now(UTC).isoformat(),
        run_branch=branch,
        run_branch_declared=1,
        run_branch_head=tip,
    )
    return db


def _make_task(task_id: str = "t1", **overrides: object) -> Task:
    defaults: dict[str, object] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "prompt": "do",
        "agent_type": AgentType.CLAUDE_CODE,
        "workdir": "/tmp",
        "status": TaskStatus.READY,
    }
    defaults.update(overrides)
    return Task(**defaults)  # type: ignore[arg-type]


def _make_scheduler(db: Database, repo: Path, *, run_branch: str | None) -> Scheduler:
    config = SchedulerConfig(
        max_concurrent=2, workdir=repo, log_dir=repo.parent / "logs",
        auto_commit=True, run_branch=run_branch,
    )
    dag = DAG([])
    return Scheduler(db, dag, {}, config)


class TestBranchTripwire:
    async def test_ungated_is_noop_true(self, tmp_path: Path, db: Database) -> None:
        repo = _make_repo_on_branch(tmp_path)
        s = _make_scheduler(db, repo, run_branch=None)
        assert await s._branch_tripwire("spawn") is True
        assert s.branch_trip is None

    async def test_matching_state_passes(self, tmp_path: Path, db: Database) -> None:
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        s = _make_scheduler(db, repo, run_branch="pilot/x")
        assert await s._branch_tripwire("spawn") is True
        assert s.branch_trip is None

    async def test_flip_trips_and_suspends_run(
        self, tmp_path: Path, db: Database
    ) -> None:
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        _git(repo, "switch", "master")
        s = _make_scheduler(db, repo, run_branch="pilot/x")
        assert await s._branch_tripwire("spawn") is False
        assert isinstance(s.branch_trip, RunBranchGateError)
        assert s.branch_trip.reason == "live_branch_mismatch"
        row = await db.get_run_row()
        assert row is not None and row["suspended_at"] is not None
        assert "spawn" in str(row["suspend_reason"])

    async def test_trip_is_sticky_even_after_branch_restored(
        self, tmp_path: Path, db: Database
    ) -> None:
        """Once suspending, every later seam refuses without re-reading git:
        a branch restored mid-drain must not let half the completions
        finalize while the run is already recorded suspended."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        _git(repo, "switch", "master")
        s = _make_scheduler(db, repo, run_branch="pilot/x")
        assert await s._branch_tripwire("spawn") is False
        _git(repo, "switch", "pilot/x")
        assert await s._branch_tripwire("validation") is False

    async def test_missing_run_row_fails_closed(
        self, tmp_path: Path, db: Database
    ) -> None:
        repo = _make_repo_on_branch(tmp_path)  # no run row written
        s = _make_scheduler(db, repo, run_branch="pilot/x")
        assert await s._branch_tripwire("spawn") is False
        assert s.branch_trip is not None

    async def test_unknown_seam_is_a_programming_error(
        self, tmp_path: Path, db: Database
    ) -> None:
        repo = _make_repo_on_branch(tmp_path)
        s = _make_scheduler(db, repo, run_branch=None)
        with pytest.raises(AssertionError):
            await s._branch_tripwire("not-a-seam")

    def test_seam_inventory_is_exactly_the_spec_set(self) -> None:
        assert CHECKOUT_SEAMS == {
            "spawn", "collect", "validation",
            "verifier_preflight", "success_finalize",
        }
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_run_branch_tripwire.py -v`
Expected: FAIL — `ImportError: cannot import name 'CHECKOUT_SEAMS'` / no `run_branch` field.

- [ ] **Step 3: Implement**

In `maestro/scheduler.py`:

1. Imports: `from maestro.run_branch_gate import RunBranchGateError, RunBranchRecord, check_live`.

2. Module constant near the top (after the existing module-level helpers):

```python
#: Every checkout-using seam on the Mode-1 run path (spec §7). A new seam
#: must be added here AND claim a `_branch_tripwire(...)` call — the
#: inventory test (tests/test_run_branch_tripwire.py) asserts both, the
#: way transitions.py's totality test forces a table entry for a new
#: status. `collect` is here although spec §7 lists four: finalizing a
#: remote handle applies results into the working tree (§6 names collect
#: as checkout-mutating), and §7's rule is "every point about to use the
#: checkout".
CHECKOUT_SEAMS: frozenset[str] = frozenset(
    {"spawn", "collect", "validation", "verifier_preflight", "success_finalize"}
)
```

3. `SchedulerConfig` gains a field (with the other optional fields):

```python
    #: The run's bound branch (spec §7 phase B). None = ungated: every
    #: tripwire is a no-op and the success tail keeps today's order.
    run_branch: str | None = None
```

4. `Scheduler.__init__` gains `self.branch_trip: RunBranchGateError | None = None` (public: the CLI reads it after `run()` returns).

5. The method (place near `_invoke_on_auto_commit`):

```python
    async def _branch_tripwire(self, seam: str) -> bool:
        """Spec §7 live check before a checkout-using seam. True = proceed.

        No-op (True) on ungated runs. Sticky after the first trip: the
        run is suspending, and a branch restored mid-drain must not let
        later completions finalize under a run already recorded
        suspended. A gated run whose run row cannot be read fails
        closed — missing evidence is never green. The suspension write
        is durable (`set_run_suspended`, §B.1.1: resumable, not an
        outcome); the CLI renders the stderr refusal after the drain.
        """
        assert seam in CHECKOUT_SEAMS, f"unregistered checkout seam: {seam}"
        if self._config.run_branch is None:
            return True
        if self.branch_trip is not None:
            return False
        try:
            row = await self._db.get_run_row()
            if row is None:
                raise RunBranchGateError(
                    "live_stale_checkout",
                    "run row unreadable mid-run; refusing to touch the "
                    "checkout on missing evidence",
                )
            head = row.get("run_branch_head")
            check_live(
                self._config.workdir,
                RunBranchRecord(
                    branch=self._config.run_branch,
                    head=str(head) if head is not None else None,
                ),
            )
        except RunBranchGateError as e:
            self.branch_trip = e
            logger.error("run-branch tripwire at %s: %s", seam, e)
            try:
                await self._db.set_run_suspended(
                    suspended_at=datetime.now(UTC).isoformat(),
                    suspend_reason=f"run-branch tripwire at {seam}: {e}",
                )
            except Exception:
                # The in-memory trip still drains and the CLI still
                # refuses; only the durable marker is missing.
                logger.warning("suspend record failed", exc_info=True)
            return False
        return True
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_run_branch_tripwire.py -v && uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add maestro/scheduler.py tests/test_run_branch_tripwire.py
git commit -m "feat(scheduler): _branch_tripwire — sticky per-seam live check (spec §7)"
```

---

### Task 3: Spawn + collect seams, suspension drain in the main loop

**Files:**
- Modify: `maestro/scheduler.py` — `_main_loop` (~1072), `_spawn_task` (~1180), `_monitor_running_tasks` (~1765); new `_drain_running_tasks`
- Test: `tests/test_run_branch_tripwire.py` (extend)

**Interfaces:**
- Consumes: `_branch_tripwire` (Task 2).
- Produces: drain semantics later tasks rely on — after a trip the loop spawns nothing, never finalizes (no collect/cleanup/transition), waits for live processes to exit (honoring the task's own timeout terminate), and exits with `branch_trip` set; DB keeps `RUNNING` rows and open execution handles — exactly the crash shape resume recovery already reconciles after §6 re-verification.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_run_branch_tripwire.py`. Build the scheduler with one READY task and a MagicMock spawner (copy `tests/test_scheduler.py`'s `BaseSpawner` mock pattern: a spawner whose handle's `poll()` is scripted). Key cases:

```python
class TestSpawnSeamAndDrain:
    async def test_flip_before_spawn_no_spawn_run_suspended(
        self, tmp_path: Path, db: Database
    ) -> None:
        """Mid-run flip → no further spawns, run suspended, task READY."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task("t1", workdir=str(repo))
        await db.create_task(task)
        _git(repo, "switch", "master")
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        launched = await s._spawn_task("t1")
        assert launched is False
        assert s.branch_trip is not None
        spawner = s._spawners[AgentType.CLAUDE_CODE.value]
        spawner.spawn.assert_not_called()          # nothing touched the checkout
        assert (await db.get_task("t1")).status == TaskStatus.READY

    async def test_drain_waits_for_exit_without_finalize(
        self, tmp_path: Path, db: Database
    ) -> None:
        """A tripped run's live task is not killed and not finalized:
        it is dropped from tracking once its process exits, its DB row
        stays RUNNING for resume recovery."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task("t1", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task)
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        handle = _scripted_handle(poll_results=[None, 0])  # alive, then exited
        s._running_tasks["t1"] = _running_task(task, handle)
        s.branch_trip = RunBranchGateError("live_branch_mismatch", "test")
        await s._monitor_running_tasks()   # first pass: still alive, untouched
        handle.terminate.assert_not_called()
        await s._monitor_running_tasks()   # second pass: exited -> drained
        assert "t1" not in s._running_tasks
        handle.terminate.assert_not_called()
        assert (await db.get_task("t1")).status == TaskStatus.RUNNING

    async def test_trip_at_collect_seam_leaves_task_for_drain(
        self, tmp_path: Path, db: Database
    ) -> None:
        """First trip discovered at the collect seam: the exited process
        is NOT finalized (no collect onto a moved checkout) and the run
        suspends."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task("t1", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task)
        _git(repo, "switch", "master")
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        handle = _scripted_handle(poll_results=[0, 0])
        s._running_tasks["t1"] = _running_task(task, handle)
        await s._monitor_running_tasks()
        assert s.branch_trip is not None
        assert (await db.get_task("t1")).status == TaskStatus.RUNNING

    async def test_main_loop_exits_after_drain(
        self, tmp_path: Path, db: Database
    ) -> None:
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        s.branch_trip = RunBranchGateError("live_branch_mismatch", "test")
        await asyncio.wait_for(s._main_loop(), timeout=5)  # no running tasks -> returns
```

Write the small local helpers this needs (`_make_scheduler_with_mock_spawner`, `_scripted_handle` returning a MagicMock whose `poll` pops from a list and whose `terminate` is an `AsyncMock`, `_running_task` building a `RunningTask` with `execution_id=None, backend_id="local"`), mirroring `tests/test_scheduler.py`'s existing doubles.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_run_branch_tripwire.py -k "SpawnSeam or Drain" -v`
Expected: FAIL — spawn proceeds / completion finalizes / loop hangs.

- [ ] **Step 3: Implement**

1. `_spawn_task` — first line of the body, before the DB read:

```python
        if not await self._branch_tripwire("spawn"):
            return False
```

2. `_monitor_running_tasks` — head:

```python
        if self.branch_trip is not None:
            await self._drain_running_tasks()
            return
```

and in the process-finished branch, before `fin = await self._finalize_running(running_task)`:

```python
            if return_code is not None:
                if not await self._branch_tripwire("collect"):
                    # First trip discovered here: leave the exited task
                    # un-finalized (no collect onto a moved checkout);
                    # the drain pass reaps it next iteration.
                    continue
```

3. New method (near `_monitor_running_tasks`):

```python
    async def _drain_running_tasks(self) -> None:
        """Suspension drain (spec §7): never kill, never finalize.

        Waits for each live process to exit on its own — honoring the
        task's own timeout terminate, which predates this gate — and
        drops it from tracking WITHOUT collect/cleanup/transition. The
        DB keeps the RUNNING row and any open execution handle: exactly
        the crash shape resume recovery reconciles after §6
        re-verification. Finalizing here would collect into a checkout
        that just proved wrong.
        """
        drained: list[str] = []
        for task_id, running_task in self._running_tasks.items():
            if running_task.handle.poll() is not None:
                drained.append(task_id)
                continue
            elapsed = datetime.now(UTC) - running_task.started_at
            if elapsed.total_seconds() > running_task.task.timeout_minutes * 60:
                await running_task.handle.terminate(grace_seconds=10.0)
                drained.append(task_id)
        for task_id in drained:
            del self._running_tasks[task_id]
```

4. `_main_loop` — replace the body of the `while` with a trip-aware shape (existing logic unchanged on the untripped path):

```python
        while not self._shutdown_requested:
            if self.branch_trip is not None:
                # Suspension drain: no new work, monitor until live
                # processes exit, then leave. `_cleanup` then has
                # nothing to terminate or reset.
                if not self._running_tasks:
                    break
                await self._monitor_running_tasks()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self._config.poll_interval,
                    )
                continue
            # ... existing body verbatim (completed_ids, all-complete
            # break, resolve, spawn, monitor, tick, outcome pass, wait)
```

Note `_cleanup` is intentionally untouched: after a drain `self._running_tasks` is empty, so its terminate/FAILED→READY loop is a no-op; on Ctrl-C mid-drain it behaves exactly as today (the operator's kill, not the gate's).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_run_branch_tripwire.py tests/test_scheduler.py -v` (foreground) then `uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS (existing scheduler suite untouched = opt-out evidence), clean.

- [ ] **Step 5: Commit**

```bash
git add maestro/scheduler.py tests/test_run_branch_tripwire.py
git commit -m "feat(scheduler): spawn/collect tripwires + suspension drain (spec §7)"
```

---

### Task 4: Completion-path seams — validation launch + gated success tail

**Files:**
- Modify: `maestro/scheduler.py` — `_handle_task_completion` (~1877–1991), new `_finalize_success` helper (place before `_handle_task_completion`)
- Test: `tests/test_run_branch_tripwire.py` (extend)

**Interfaces:**
- Consumes: `_branch_tripwire`; existing `_transition`, `_auto_commit_task`, `_invoke_on_auto_commit`.
- Produces: `async Scheduler._finalize_success(task_id: str, task: Task, *, expected_status: TaskStatus, result_summary: str) -> Task | None` — the single success tail. Ungated: `DONE → auto-commit` (today's order, byte-identical). Gated: `tripwire → auto-commit → on_auto_commit → DONE`; returns `None` when the tripwire suspended (task left in `expected_status`). Task 5 reuses it for the verifier PASS tail.

- [ ] **Step 1: Write the failing tests**

```python
class TestCompletionSeams:
    async def test_flip_between_exit_and_validation_preserves_running(
        self, tmp_path: Path, db: Database
    ) -> None:
        """Round-2 major 2: no validation launch, no transition."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task(
            "t1", workdir=str(repo), status=TaskStatus.RUNNING,
            validation_cmd="true",
        )
        await db.create_task(task)
        _git(repo, "switch", "master")
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        rt = _running_task(task, _scripted_handle(poll_results=[0]))
        await s._handle_task_completion("t1", rt, 0)
        assert s.branch_trip is not None
        assert (await db.get_task("t1")).status == TaskStatus.RUNNING

    async def test_gated_success_tail_commits_before_done(
        self, tmp_path: Path, db: Database
    ) -> None:
        """Round-3 major 1: tripwire -> auto-commit -> DONE. Verified by
        the commit sha the run records: on the gated path the DONE row
        exists only after HEAD already moved."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        (repo / "work.txt").write_text("agent output")
        task = _make_task("t1", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task)
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        rt = _running_task(task, _scripted_handle(poll_results=[0]))
        await s._handle_task_completion("t1", rt, 0)
        assert (await db.get_task("t1")).status == TaskStatus.DONE
        assert s.branch_trip is None
        # the auto-commit landed and is on the run branch
        assert "work.txt" in _git(repo, "show", "--name-only", "HEAD")

    async def test_flip_before_success_tail_no_commit_no_done(
        self, tmp_path: Path, db: Database
    ) -> None:
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        head_before = _git(repo, "rev-parse", "HEAD")
        (repo / "work.txt").write_text("agent output")
        task = _make_task("t1", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task)
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch="pilot/x")
        # trip is discovered at the collect seam in monitor normally; here
        # drive the completion handler directly after a flip
        _git(repo, "switch", "master")
        rt = _running_task(task, _scripted_handle(poll_results=[0]))
        await s._handle_task_completion("t1", rt, 0)
        assert (await db.get_task("t1")).status == TaskStatus.RUNNING
        assert _git(repo, "rev-parse", "refs/heads/pilot/x") == head_before

    @pytest.mark.parametrize(
        ("run_branch", "expected_order"),
        [
            (None, ["transition", "commit"]),        # ungated: today's order
            ("pilot/x", ["commit", "transition"]),   # gated: spec §7 reorder
        ],
    )
    async def test_success_tail_order(
        self,
        tmp_path: Path,
        db: Database,
        run_branch: str | None,
        expected_order: list[str],
    ) -> None:
        """Ungated stays DONE -> auto-commit byte-identically; gated
        reorders to auto-commit -> DONE. Asserted by recording the CALL
        ORDER of the two steps."""
        repo = _make_repo_on_branch(tmp_path)
        if run_branch is not None:
            await _bound_db(db, repo, run_branch)
        task = _make_task("t1", workdir=str(repo), status=TaskStatus.RUNNING)
        await db.create_task(task)
        s = _make_scheduler_with_mock_spawner(db, repo, run_branch=run_branch)
        order: list[str] = []
        real_transition = s._transition
        real_commit = s._auto_commit_task

        async def spy_transition(*args: object, **kwargs: object) -> Task:
            order.append("transition")
            return await real_transition(*args, **kwargs)  # type: ignore[arg-type]

        def spy_commit(t: Task) -> str | None:
            order.append("commit")
            return real_commit(t)

        with (
            mock.patch.object(s, "_transition", spy_transition),
            mock.patch.object(s, "_auto_commit_task", spy_commit),
        ):
            done_task = await s._finalize_success(
                "t1",
                task,
                expected_status=TaskStatus.RUNNING,
                result_summary="Task completed successfully",
            )
        assert done_task is not None
        assert order == expected_order
```

(Add `from unittest import mock` to the file's imports.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_run_branch_tripwire.py -k CompletionSeams -v`
Expected: FAIL — validation launches / DONE lands despite flip / order wrong.

- [ ] **Step 3: Implement**

1. New helper:

```python
    async def _finalize_success(
        self,
        task_id: str,
        task: Task,
        *,
        expected_status: TaskStatus,
        result_summary: str,
    ) -> Task | None:
        """The success tail. Ungated: DONE -> auto-commit — today's
        order, byte-identical. Gated (spec §7, round-3 major 1):
        tripwire -> auto-commit -> DONE, because `DONE` is terminal and
        a gate between DONE and the commit would strand a
        terminal-yet-uncommitted task that resume never revisits. On a
        trip the task stays in `expected_status` (pre-terminal,
        re-entered by resume) and None is returned. The residual window
        between the passed check and the git invocation it guards is
        one git call wide — stated, not hidden (spec §7).
        """
        if self._config.run_branch is None:
            done_task = await self._transition(
                task_id,
                TaskStatus.DONE,
                expected_status=expected_status,
                result_summary=result_summary,
            )
            await self._invoke_on_auto_commit(self._auto_commit_task(task))
            return done_task
        if not await self._branch_tripwire("success_finalize"):
            return None
        await self._invoke_on_auto_commit(self._auto_commit_task(task))
        return await self._transition(
            task_id,
            TaskStatus.DONE,
            expected_status=expected_status,
            result_summary=result_summary,
        )
```

2. In `_handle_task_completion`, the validation branch (`if task.validation_cmd:`) gains a guard as its first statement:

```python
                if not await self._branch_tripwire("validation"):
                    return  # task stays RUNNING, preserved for resume
```

(One guard covers both the local and the durable sub-branches — it sits before either transition/mint.)

3. Replace the two success tails with the helper. Post-validation (`else` of `_verifier_enabled`, currently ~1952–1958):

```python
                        done_task = await self._finalize_success(
                            task_id,
                            task,
                            expected_status=TaskStatus.VALIDATING,
                            result_summary="Task completed successfully",
                        )
                        if done_task is None:
                            return
```

No-validation path (~1976–1982): same replacement with `expected_status=TaskStatus.RUNNING`. Keep the subsequent `_build_outcome` / `_try_report_outcome` / `_obs_log.info` lines unchanged in both.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_run_branch_tripwire.py tests/test_scheduler.py tests/test_scheduler_arbiter_integration.py -v` then `uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS — existing completion tests are the ungated byte-identical evidence.

- [ ] **Step 5: Commit**

```bash
git add maestro/scheduler.py tests/test_run_branch_tripwire.py
git commit -m "feat(scheduler): validation seam + gated success-tail reorder (spec §7)"
```

---

### Task 5: Verifier seams — preflight tripwire + PASS-tail reorder

**Files:**
- Modify: `maestro/scheduler.py` — `_run_verifier` (~2193): head guard + PASS branch (~2332–2339)
- Test: `tests/test_run_branch_tripwire.py` (extend)

**Interfaces:**
- Consumes: `_branch_tripwire`, `_finalize_success` (Task 4).
- Produces: nothing new — the last two seams claim their guards.

- [ ] **Step 1: Write the failing tests**

```python
class TestVerifierSeams:
    async def test_flip_before_verifier_preflight_judge_never_invoked(
        self, tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round-3 major 2: a flip between validation and the judge
        would have the judge rule on unrelated tree state. Task stays
        VALIDATING (pre-terminal; recovery routes VALIDATING -> READY)."""
        repo = _make_repo_on_branch(tmp_path)
        await _bound_db(db, repo)
        task = _make_task(
            "t1", workdir=str(repo), status=TaskStatus.VALIDATING,
            validation_cmd="true", scope=["*.txt"],
            verifier_baseline_sha=_git(repo, "rev-parse", "HEAD"),
        )
        await db.create_task(task)
        _git(repo, "switch", "master")
        s = _make_verifier_scheduler(db, repo, run_branch="pilot/x")
        judge_invoked = []
        monkeypatch.setattr(
            "maestro.scheduler.ClaudeDiffJudge",
            lambda **kw: judge_invoked.append(kw),  # would explode if called
        )
        rt = _running_task(task, _scripted_handle(poll_results=[0]))
        await s._run_verifier("t1", task, rt)
        assert judge_invoked == []
        assert s.branch_trip is not None
        assert (await db.get_task("t1")).status == TaskStatus.VALIDATING
```

`_make_verifier_scheduler` builds the scheduler with a minimal `VerifierConfig(model=..., runner="claude")` following `tests/test_verifier_gate.py`'s existing construction pattern (copy its minimal fixture, not its whole harness). A second test asserts the PASS tail: monkeypatch the judge to return a PASS result and assert commit-before-DONE via the same call-order patch technique as Task 4 (gated: `["commit", "transition"]`).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_run_branch_tripwire.py -k VerifierSeams -v`
Expected: FAIL — preflight builds the patch / old DONE-then-commit order.

- [ ] **Step 3: Implement**

1. `_run_verifier` — after the `cfg is None` early return, before `worktree = ...`:

```python
        if not await self._branch_tripwire("verifier_preflight"):
            return  # task stays VALIDATING, preserved for resume
```

2. PASS branch: replace the `_transition(DONE, expected_status=VERIFYING, ...)` + `_invoke_on_auto_commit(...)` pair (~2333–2339) with:

```python
        if result.outcome is VerdictValue.PASS:
            done_task = await self._finalize_success(
                task_id,
                task,
                expected_status=TaskStatus.VERIFYING,
                result_summary="Task completed successfully (verifier PASS)",
            )
            if done_task is None:
                # Tripped between the judge and finalization: the task
                # stays VERIFYING; crash-recovery for VERIFYING routes
                # fail-closed to NEEDS_REVIEW (never auto-re-run), which
                # is this gate's own philosophy — preserved, not softened.
                return
```

Keep the `_build_outcome`/`_try_report_outcome`/events lines after it unchanged.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_run_branch_tripwire.py tests/test_verifier_gate.py -v` then `uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/scheduler.py tests/test_run_branch_tripwire.py
git commit -m "feat(scheduler): verifier preflight + PASS-tail tripwires (spec §7)"
```

---

### Task 6: Seam-inventory test (test-asserted inventory)

**Files:**
- Test: `tests/test_run_branch_tripwire.py` (extend)

**Interfaces:**
- Consumes: `CHECKOUT_SEAMS`, `inspect.getsource` over `maestro.scheduler`.
- Produces: the enforcement mechanism spec §7 demands — "a new checkout-using seam must claim a tripwire, the way a new status must claim a transitions-table entry".

- [ ] **Step 1: Write the test (it should PASS immediately — it locks the state Tasks 2–5 built; flip one seam name to watch it fail, then restore)**

```python
class TestSeamInventory:
    """Spec §7: the tripwire inventory is a claim about the scheduler,
    asserted here. Extending the scheduler with a new checkout-using
    seam must (a) add the seam to CHECKOUT_SEAMS and (b) call
    _branch_tripwire at that seam — either half alone fails this class.
    """

    def test_every_registered_seam_is_claimed_in_source(self) -> None:
        import re

        source = inspect.getsource(maestro.scheduler)
        claimed = set(re.findall(r'_branch_tripwire\("([a-z_]+)"\)', source))
        assert claimed == CHECKOUT_SEAMS

    @pytest.mark.parametrize(
        ("method", "seam"),
        [
            ("_spawn_task", "spawn"),
            ("_monitor_running_tasks", "collect"),
            ("_handle_task_completion", "validation"),
            ("_run_verifier", "verifier_preflight"),
            ("_finalize_success", "success_finalize"),
        ],
    )
    def test_seam_guard_lives_at_its_checkout_site(
        self, method: str, seam: str
    ) -> None:
        source = inspect.getsource(getattr(Scheduler, method))
        assert f'_branch_tripwire("{seam}")' in source
```

(Imports: `import inspect`, `import maestro.scheduler`.)

- [ ] **Step 2: Run + mutation check**

Run: `uv run pytest tests/test_run_branch_tripwire.py -k SeamInventory -v` — PASS. Then temporarily rename one seam string in `scheduler.py`, re-run, confirm FAIL, revert.

- [ ] **Step 3: Commit**

```bash
git add tests/test_run_branch_tripwire.py
git commit -m "test(scheduler): assert the checkout-seam inventory (spec §7)"
```

---

### Task 7: CLI wiring — pass the binding in, render the suspension out

**Files:**
- Modify: `maestro/cli.py` — `create_scheduler_from_config` call (~1080–1108), post-`scheduler.run()` tail (~1130–1160)
- Modify: `maestro/scheduler.py` — `create_scheduler_from_config` signature (~2767) forwards `run_branch`
- Test: `tests/test_cli.py` (extend the existing `TestOnAutoCommitWiring`-style class), `tests/test_run_branch_e2e.py` (extend)

**Interfaces:**
- Consumes: `bound_branch` (already computed in `_run_scheduler` for `on_auto_commit`), `Scheduler.branch_trip`.
- Produces: `create_scheduler_from_config(..., run_branch: str | None = None)` → `SchedulerConfig.run_branch`; a tripped run exits 1 with the refusal on stderr after the drain.

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli.py`, next to the existing run-branch CLI tests: a test that stubs `create_scheduler_from_config` (as `TestOnAutoCommitWiring` does) and asserts it receives `run_branch == "pilot/x"` on a gated run and `run_branch is None` on an ungated one; and a test where the stubbed scheduler's `run()` sets `branch_trip = RunBranchGateError("live_branch_mismatch", "moved to 'master'")` — assert exit code 1 and `"run-branch tripwire"` in stderr.

In `tests/test_run_branch_e2e.py`: one new e2e proving no false trip — a gated `--db` run with the `announce` agent and `auto_commit: true` completes green with the tripwire armed end-to-end: exit 0, task DONE, commit landed on `pilot/...`, `run.run_branch_head` equals the branch tip, `suspended_at` NULL. (A deterministic mid-run flip is covered at the scheduler level in Tasks 3–5; an e2e flip would be a race.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_cli.py -k "run_branch or tripwire" tests/test_run_branch_e2e.py -v`
Expected: FAIL — `create_scheduler_from_config` has no `run_branch` parameter.

- [ ] **Step 3: Implement**

1. `scheduler.py::create_scheduler_from_config`: add parameter `run_branch: str | None = None` (documented: "the run's bound branch; arms the spec-§7 tripwires"), forward into `SchedulerConfig(run_branch=run_branch, ...)`.

2. `cli.py::_run_scheduler`: in the `create_scheduler_from_config(...)` call add `run_branch=bound_branch,` (right beside `on_auto_commit=...`, which already keys off the same variable).

3. After `await scheduler.run()` and the git-summary/table display, before `_announce_conclusion`:

```python
            if scheduler.branch_trip is not None:
                # The tripwire already wrote the durable suspension
                # (spec §B.1.1) and the drain preserved every live task
                # pre-terminal; nothing to conclude — say why we stopped
                # and exit 1 (spec §7/§8: stderr is the contract).
                err_console.print(
                    "[red]run-branch tripwire:[/red] "
                    f"{escape(str(scheduler.branch_trip))} — run suspended, "
                    "live work preserved; fix the checkout and re-run to "
                    "resume (continuation re-verifies the branch)",
                    soft_wrap=True,
                )
                raise typer.Exit(1)
```

Note: the suspend marker is deliberately NOT cleared on the next resume. `classify_run` puts observed liveness above `suspended_at`, and a suspended run is exactly what `maestro service` must not auto-resume — the checkout needs a human. A later completed resume writes `outcome`, which wins. Priced and recorded in the spec revision (Task 8).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_cli.py tests/test_run_branch_e2e.py -v` then `uv run pyrefly check && uv run ruff format . && uv run ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/cli.py maestro/scheduler.py tests/test_cli.py tests/test_run_branch_e2e.py
git commit -m "feat(cli): arm the phase-B tripwires and render suspension (spec §7)"
```

---

### Task 8: Spec revision 9 + docs + TODO close-out

**Files:**
- Modify: `docs/superpowers/specs/2026-08-24-mode1-run-branch-isolation-design.md`
- Modify: `CLAUDE.md` (one sentence), `CHANGELOG.md` (entry), `TODO.md` (tick the item)

The TODO item explicitly bundles the spec revision: §6/§8 must describe what shipped, not what round 6 last froze.

- [ ] **Step 1: Spec header** — add above the revision-8 block:

```markdown
**Status:** revision 9 — phase B shipped; §6/§8 revised to the
as-implemented state (ruling R14; logger-based events)
**Revision 9 (phase B implementation, 2026-08-25):** (1) §6 — the head
record is maintained by ATTRIBUTION, not observation (ruling R14, PR
#222): `on_auto_commit` hands over the sha of a commit the run itself
made; the earlier "refreshed on graceful suspension/stop" is dropped —
an observational refresh would normalize a foreign commit into the
run's record and pass the next continuation green over it, the exact
hole the stale check exists to catch. Priced cost, found twice (codex
round 4, PR #222): a run whose agent commits by itself (auto_commit
off) leaves the record behind the branch and stale-refuses on the next
continuation, where `--accept-branch-tip` is the audited way through.
(2) §7 — as-built decisions recorded in place: a fifth seam (`collect`),
sticky suspension, drain semantics, VERIFYING preservation, and the
undisturbed suspend marker. (3) §8 — the event surface is implemented
as structured obs records (`Attributes.event` + kwargs) through a
per-call logger, not as `EventType` members; reasons gain
`live_branch_mismatch` / `live_stale_checkout`.
```

- [ ] **Step 2: §6 body** — in the `run_branch_head` bullet, replace `", updated after each auto-commit the run itself makes, and refreshed on graceful suspension/stop"` with `", and updated by attribution only — after each commit the run itself makes (ruling R14; revision 9 dropped the graceful-stop observational refresh, which would normalize foreign commits into the record)"`.

- [ ] **Step 3: §7 body** — append after the "Resume then passes through §6's verification as usual." bullet:

```markdown
- **As implemented (revision 9):** the inventory gained a fifth seam,
  `collect` — finalizing a remote handle applies results into the
  working tree, which §6 itself names as checkout-mutating; the
  inventory test (`tests/test_run_branch_tripwire.py`) asserts all
  five. The trip is **sticky**: once the run is suspending, every later
  seam refuses without re-reading git — a branch restored mid-drain
  must not let half the completions finalize under a run already
  recorded suspended. The drain never finalizes: exited processes are
  dropped from tracking with their RUNNING rows and open execution
  handles intact — the crash shape resume recovery already reconciles
  after §6 re-verification (the task's own timeout keeps its
  pre-existing terminate; it is the task's policy, not the gate's). A
  task tripped in VERIFYING stays VERIFYING and resolves through the
  verifier's fail-closed crash recovery (NEEDS_REVIEW, never
  auto-re-run) — preserved, not softened. The suspend marker is not
  cleared on resume: `classify_run` ranks observed liveness above it,
  a completed resume's outcome wins over it, and `maestro service`
  reading "suspended = human required" is the safe direction for a
  checkout only a human can fix.
```

- [ ] **Step 4: §8** — extend the reason list with `live_branch_mismatch`, `live_stale_checkout`; append: "As implemented, the structured events are obs records emitted through a per-call logger (`run_branch_gate.py::_emit`) — the event name lands in `Attributes.event` — rather than `EventType` members; telemetry only, the stderr text remains the contract."

- [ ] **Step 5: CLAUDE.md** — in the Mode-1 CLI/commands context where phase A would be described (if absent, in the Key Design Decisions list), add one sentence: "`git.run_branch` (Mode-1, opt-in): run-level branch isolation — start/continuation gates (phase A) plus per-seam live tripwires that suspend-with-drain on foreign branch movement (phase B, spec §7)."

- [ ] **Step 6: CHANGELOG.md** — under Unreleased: "Mode-1 `git.run_branch` phase B: per-seam checkout tripwires (spawn/collect/validation/verifier/finalize); a mid-run branch flip or foreign commit suspends the run with a drain — nothing killed, tasks preserved pre-terminal, resume re-verifies."

- [ ] **Step 7: TODO.md** — tick `mode1-run-branch-tripwire` `[x]` with `(closed by feat/mode1-run-branch-tripwire)` appended AFTER the first line's existing text is left verbatim (Robin keys on the normalized first line — append, never rephrase).

- [ ] **Step 8: Verify + commit**

Run: `uv run pytest tests/test_run_branch_tripwire.py tests/test_run_branch_gate.py tests/test_run_branch_e2e.py tests/test_cli.py -v && uv run pyrefly check && uv run ruff check .`

```bash
git add docs/superpowers/specs/2026-08-24-mode1-run-branch-isolation-design.md CLAUDE.md CHANGELOG.md TODO.md
git commit -m "docs(spec): revision 9 — §6 R14 attribution, §7 as-built, §8 reasons"
```

---

### Task 9: Final verification + PR

- [ ] **Step 1: Targeted foreground verification** (never the full suite in background — workspace watchdog): run the touched areas in halves:

```bash
uv run pytest tests/test_run_branch_tripwire.py tests/test_run_branch_gate.py tests/test_run_branch_e2e.py -v
uv run pytest tests/test_scheduler.py tests/test_scheduler_arbiter_integration.py tests/test_verifier_gate.py tests/test_cli.py -v
uv run pyrefly check   # the "INFO N errors" completion line is the evidence
uv run ruff format . && uv run ruff check .
```

- [ ] **Step 2: Push + PR** (no self-merge; request Copilot review if it doesn't appear):

```bash
git push -u origin feat/mode1-run-branch-tripwire
gh pr create --title "feat: Mode-1 run-branch tripwire (phase B, spec §7)" --body "..."
```

PR body: what phase B closes (mid-run windows of phase A), the five seams, suspend-with-drain semantics, spec revision 9, test evidence. Let PR CI carry the full suite.

---

## Self-Review Notes

- **Spec §7 coverage:** every-seam check (Tasks 3–5, five seams incl. the added `collect`), name-AND-tip (Task 1), completion-path-before-terminal reorder incl. verifier PASS (Tasks 4–5), suspend-not-kill with drain (Task 3), stderr refusal (Task 7), test-asserted inventory (Task 6), residual one-git-call window stated in `_finalize_success` docstring.
- **Spec §9 phase-B test lines:** mid-run flip / completion-path flip / verifier flip / same-branch foreign commit / inventory — all present. Opt-out evidence = existing suites in Tasks 4–5 Step 4 runs.
- **TODO item's spec-revision half (R14 + §8 logger-based):** Task 8.
- **No migration needed:** `run_branch*` columns and `set_run_suspended` exist (phase A / state-layout spec).
- **Known deliberate choices to defend in review:** fifth seam beyond the spec's four (recorded in spec rev 9); sticky trip vs re-check (recorded); suspend marker left set on resume (recorded, safe direction).
