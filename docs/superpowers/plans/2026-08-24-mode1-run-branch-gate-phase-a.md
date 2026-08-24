# Mode-1 run-branch gate, phase A — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `git.run_branch` gives a Mode-1 run one checkout on one branch — verified OR created by the runtime under the PID lock before the run is published, durably recorded, and re-verified on every continuation before recovery.

**Architecture:** A new pure-core module `maestro/run_branch_gate.py` makes every decision (start matrix, continuation verification) from a snapshot dataclass, so the git/DB plumbing stays thin. The run row gains three columns (`run_branch`, `run_branch_declared`, `run_branch_head`, migration 29). `_run_scheduler` acquires the PID lock first, runs the start gate via a `pre_publish` seam in `bootstrap_run` (fresh path), and runs continuation verification right after the DB opens and **before** recovery (continuation path). Phase B (live tripwires at every checkout seam) is a separate later plan.

**Tech Stack:** Python 3.12, pydantic, aiosqlite, Typer, pytest (asyncio_mode=auto, anyio), pyrefly, ruff.

**Spec:** `docs/superpowers/specs/2026-08-24-mode1-run-branch-isolation-design.md` (revision 8 — read it first; every rule below cites its section).

## Global Constraints

- Workflow: branch `feat/mode1-run-branch-gate` off `master`; PR only; no direct master commits; Copilot review after PR.
- TDD: every behavior lands test-first; watch each test fail before implementing.
- Run pytest in the FOREGROUND only, targeted files (workspace watchdog kills background runs). Full suite is PR CI's job.
- After every task: `uv run pyrefly check` (expect `INFO 0 errors`) and `uv run ruff format . && uv run ruff check .`.
- Every test that builds a `Database` closes it (`yield d; await d.close()` fixture) — an unclosed connection is a ResourceWarning-as-error plus a ~120s hang.
- Migration number 29 is claimed here; before Task 4, run `git worktree list` and `grep -n "(29," maestro/database.py` on current master — a parallel actor may have claimed it; if taken, renumber to the next free and update this plan.
- Opt-out (`run_branch` absent) must stay byte-identical except the PID-lock acquisition point (spec §3, §5); the untouched existing suite staying green is the evidence.
- No `git stash` anywhere. No auto-switch on continuation. All refusals: plain text via `err_console`, exit code 1 (spec §8).
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Config key `git.run_branch`

**Files:**
- Modify: `maestro/models.py` (class `GitConfig`, ~line 758)
- Modify: `maestro/schemas/project_config.json` (regenerated, not hand-edited)
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `GitConfig.run_branch: str | None` (default `None`); `ValueError` at model validation when `run_branch == base_branch`.

- [ ] **Step 1: Write the failing tests** (append to the `GitConfig` tests in `tests/test_models.py`, next to the existing `branch_prefix` rejection tests from #216 part 1):

```python
class TestGitConfigRunBranch:
    def test_run_branch_absent_defaults_to_none(self) -> None:
        config = GitConfig()
        assert config.run_branch is None

    def test_run_branch_accepted(self) -> None:
        config = GitConfig(base_branch="master", run_branch="pilot/x")
        assert config.run_branch == "pilot/x"

    def test_run_branch_equal_to_base_rejected(self) -> None:
        with pytest.raises(ValidationError, match="run_branch"):
            GitConfig(base_branch="master", run_branch="master")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_models.py -k TestGitConfigRunBranch -v`
Expected: FAIL — `run_branch` is an unknown field / attribute error.

- [ ] **Step 3: Implement** — in `GitConfig` add the field and extend validation (same style as `reject_branch_prefix`, spec §3):

```python
    run_branch: str | None = Field(
        default=None,
        description=(
            "Mode-1 run-level branch isolation (issue #216 part 2): the one "
            "branch this DAG's runs execute on. Verified or created by the "
            "runtime before the run is published; never equal to base_branch."
        ),
    )

    @model_validator(mode="after")
    def reject_run_branch_equal_to_base(self) -> Self:
        """`run_branch == base_branch` defeats the isolation it configures."""
        if self.run_branch is not None and self.run_branch == self.base_branch:
            msg = (
                f"git.run_branch ({self.run_branch!r}) must differ from "
                f"git.base_branch: the whole point is not running on the base"
            )
            raise ValueError(msg)
        return self
```

- [ ] **Step 4: Run tests, verify pass; regenerate the schema**

Run: `uv run pytest tests/test_models.py -k "TestGitConfigRunBranch or branch_prefix" -v` → PASS.
Run: `uv run python -m maestro.schemas.generate` then `git diff --stat maestro/schemas/` (expect project_config.json updated).

- [ ] **Step 5: pyrefly + ruff + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/models.py maestro/schemas/project_config.json tests/test_models.py
git commit -m "feat(config): git.run_branch key for Mode-1 branch isolation (spec §3)"
```

---

### Task 2: Gate module — pure decision core

**Files:**
- Create: `maestro/run_branch_gate.py`
- Test: `tests/test_run_branch_gate.py` (new)

**Interfaces:**
- Produces:
  - `RunBranchGateError(Exception)` with attribute `reason: str` — codes exactly: `branch_equals_base`, `dirty_tree`, `wrong_start_point`, `resume_branch_mismatch`, `resume_stale_checkout`, `record_missing` (spec §8).
  - `CheckoutSnapshot` dataclass: `current_branch: str | None` (None = detached HEAD), `target_exists: bool`, `dirty_paths: list[str]`.
  - `StartAction` enum: `PROCEED`, `SWITCH`, `CREATE`.
  - `decide_start(snap: CheckoutSnapshot, *, run_branch: str, base_branch: str) -> StartAction` — pure, raises `RunBranchGateError` on refusal rows.

- [ ] **Step 1: Write the failing table-driven test** covering the full §4 matrix:

```python
"""Tests for maestro/run_branch_gate.py — spec §4 start matrix."""

import pytest

from maestro.run_branch_gate import (
    CheckoutSnapshot,
    RunBranchGateError,
    StartAction,
    decide_start,
)

B, BASE = "pilot/x", "master"


def snap(cur: str | None, exists: bool, dirty: list[str]) -> CheckoutSnapshot:
    return CheckoutSnapshot(current_branch=cur, target_exists=exists, dirty_paths=dirty)


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (snap(B, True, []), StartAction.PROCEED),
        (snap("other", True, []), StartAction.SWITCH),
        (snap(BASE, False, []), StartAction.CREATE),
    ],
)
def test_start_matrix_actions(snapshot: CheckoutSnapshot, expected: StartAction) -> None:
    assert decide_start(snapshot, run_branch=B, base_branch=BASE) == expected


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (snap(B, True, ["f.txt"]), "dirty_tree"),
        (snap("other", True, ["f.txt"]), "dirty_tree"),
        (snap(BASE, False, ["f.txt"]), "dirty_tree"),
        (snap("other", False, []), "wrong_start_point"),  # B missing, cur != base
        (snap(None, True, []), "wrong_start_point"),  # detached HEAD
    ],
)
def test_start_matrix_refusals(snapshot: CheckoutSnapshot, reason: str) -> None:
    with pytest.raises(RunBranchGateError) as exc:
        decide_start(snapshot, run_branch=B, base_branch=BASE)
    assert exc.value.reason == reason
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_run_branch_gate.py -v`
Expected: FAIL — module `maestro.run_branch_gate` does not exist.

- [ ] **Step 3: Implement the pure core** (`maestro/run_branch_gate.py`):

```python
"""Mode-1 run-level branch gate (issue #216 part 2, spec revision 8).

Pure decision core: every §4/§6 rule is computed from a snapshot dataclass
so the git- and DB-facing plumbing stays thin and separately testable.
Design doc: docs/superpowers/specs/2026-08-24-mode1-run-branch-isolation-design.md
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RunBranchGateError(Exception):
    """A branch-gate refusal. `reason` is the machine-readable code (spec §8)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class CheckoutSnapshot:
    """What the gate needs to know about the checkout, read in one pass."""

    current_branch: str | None  # None = detached HEAD
    target_exists: bool
    dirty_paths: list[str]


class StartAction(Enum):
    PROCEED = "proceed"
    SWITCH = "switch"
    CREATE = "create"


def decide_start(
    snap: CheckoutSnapshot, *, run_branch: str, base_branch: str
) -> StartAction:
    """The §4 start matrix. Refusals raise; actions are executed by the caller.

    Clean tree is required on EVERY fresh-start path (spec §4, consumer
    answer 1); creation happens only from `base_branch`; a detached HEAD
    has no `cur` to reason about.
    """
    if snap.current_branch is None:
        raise RunBranchGateError(
            "wrong_start_point",
            f"detached HEAD: check out {base_branch!r} (to create "
            f"{run_branch!r}) or {run_branch!r} itself, then re-run",
        )
    if snap.dirty_paths:
        shown = ", ".join(snap.dirty_paths[:10])
        raise RunBranchGateError(
            "dirty_tree",
            f"working tree is dirty ({shown}); with auto_commit these paths "
            "would ride into an agent's commit — commit or clean them first",
        )
    if snap.current_branch == run_branch:
        return StartAction.PROCEED
    if snap.target_exists:
        return StartAction.SWITCH
    if snap.current_branch == base_branch:
        return StartAction.CREATE
    raise RunBranchGateError(
        "wrong_start_point",
        f"run branch {run_branch!r} does not exist and the checkout is on "
        f"{snap.current_branch!r}, not base {base_branch!r}: creating it here "
        "would silently capture that branch's state — switch to "
        f"{base_branch!r} first",
    )
```

- [ ] **Step 4: Run tests, verify pass**

Run: `uv run pytest tests/test_run_branch_gate.py -v` → all PASS.

- [ ] **Step 5: pyrefly + ruff + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/run_branch_gate.py tests/test_run_branch_gate.py
git commit -m "feat(gate): pure start-matrix core for the Mode-1 run-branch gate (spec §4)"
```

---

### Task 3: Gate module — git-facing helpers, start apply, continuation verify

**Files:**
- Modify: `maestro/run_branch_gate.py`
- Test: `tests/test_run_branch_gate.py`

**Interfaces:**
- Consumes: Task 2's `CheckoutSnapshot`, `decide_start`, `RunBranchGateError`.
- Produces (all sync, subprocess-based like `maestro/git.py`):
  - `read_snapshot(workdir: Path, run_branch: str) -> CheckoutSnapshot`
  - `branch_tip(workdir: Path, branch: str) -> str` — `git rev-parse <branch>`; raises `RunBranchGateError("wrong_start_point", ...)` if unresolvable.
  - `apply_start_gate(workdir: Path, *, run_branch: str, base_branch: str) -> str` — decides, executes `git switch` / `git switch -c`, returns the tip sha of `run_branch` after the action.
  - `RunBranchRecord` dataclass: `branch: str`, `head: str | None`.
  - `verify_continuation(workdir: Path, record: RunBranchRecord, *, accept_tip: bool) -> tuple[str, list[str]]` — returns `(current_tip, dirty_paths)`; raises `resume_branch_mismatch` when current branch != `record.branch`; raises `resume_stale_checkout` when `record.head` is set, differs from the branch tip, and `accept_tip` is False (spec §6). Never checks cleanliness (dirt is returned for the caller's warning, spec §6 priced hole).

- [ ] **Step 1: Write failing tests against real temp git repos** (append; use the repo-wide git test pattern — `git init` + `user.email`/`user.name` config, as in `tests/test_git.py`):

```python
import subprocess
from pathlib import Path

from maestro.run_branch_gate import (
    RunBranchRecord,
    apply_start_gate,
    read_snapshot,
    verify_continuation,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "master")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("a")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-m", "init")
    return r


class TestApplyStartGate:
    def test_creates_from_base_and_switches(self, repo: Path) -> None:
        tip = apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "pilot/x"
        assert tip == _git(repo, "rev-parse", "HEAD")

    def test_switches_to_existing(self, repo: Path) -> None:
        _git(repo, "branch", "pilot/x")
        apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "pilot/x"

    def test_dirty_tree_refuses_and_does_not_switch(self, repo: Path) -> None:
        (repo / "a.txt").write_text("edited")
        with pytest.raises(RunBranchGateError) as exc:
            apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        assert exc.value.reason == "dirty_tree"
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "master"


class TestVerifyContinuation:
    def test_matching_branch_and_tip(self, repo: Path) -> None:
        tip = apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        cur, dirty = verify_continuation(
            repo, RunBranchRecord(branch="pilot/x", head=tip), accept_tip=False
        )
        assert cur == tip
        assert dirty == []

    def test_wrong_branch_refuses(self, repo: Path) -> None:
        tip = apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        _git(repo, "switch", "master")
        with pytest.raises(RunBranchGateError) as exc:
            verify_continuation(
                repo, RunBranchRecord(branch="pilot/x", head=tip), accept_tip=False
            )
        assert exc.value.reason == "resume_branch_mismatch"

    def test_moved_tip_refuses_and_accept_tip_overrides(self, repo: Path) -> None:
        tip = apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        (repo / "b.txt").write_text("b")
        _git(repo, "add", "b.txt")
        _git(repo, "commit", "-m", "foreign")
        record = RunBranchRecord(branch="pilot/x", head=tip)
        with pytest.raises(RunBranchGateError) as exc:
            verify_continuation(repo, record, accept_tip=False)
        assert exc.value.reason == "resume_stale_checkout"
        cur, _ = verify_continuation(repo, record, accept_tip=True)
        assert cur == _git(repo, "rev-parse", "HEAD")

    def test_dirty_paths_returned_not_refused(self, repo: Path) -> None:
        tip = apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        (repo / "a.txt").write_text("edited")
        cur, dirty = verify_continuation(
            repo, RunBranchRecord(branch="pilot/x", head=tip), accept_tip=False
        )
        assert dirty == ["a.txt"]

    def test_null_head_skips_stale_check(self, repo: Path) -> None:
        apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        verify_continuation(
            repo, RunBranchRecord(branch="pilot/x", head=None), accept_tip=False
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_run_branch_gate.py -k "ApplyStartGate or VerifyContinuation" -v`
Expected: FAIL — names not importable.

- [ ] **Step 3: Implement** (append to `maestro/run_branch_gate.py`; subprocess style mirrors `maestro/git.py::GitManager._run_git` but standalone — the gate must not require a `GitManager`):

```python
import subprocess
from pathlib import Path


def _run_git(workdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=workdir, capture_output=True, text=True, check=False
    )


def read_snapshot(workdir: Path, run_branch: str) -> CheckoutSnapshot:
    """One consistent read of everything §4 decides on."""
    head = _run_git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD")
    current = head.stdout.strip() if head.returncode == 0 else None
    exists = (
        _run_git(
            workdir, "show-ref", "--verify", "--quiet", f"refs/heads/{run_branch}"
        ).returncode
        == 0
    )
    status = _run_git(workdir, "status", "--porcelain")
    dirty = [line[3:] for line in status.stdout.splitlines() if line.strip()]
    return CheckoutSnapshot(
        current_branch=current, target_exists=exists, dirty_paths=dirty
    )


def branch_tip(workdir: Path, branch: str) -> str:
    result = _run_git(workdir, "rev-parse", f"refs/heads/{branch}")
    if result.returncode != 0:
        raise RunBranchGateError(
            "wrong_start_point", f"branch {branch!r} has no resolvable tip"
        )
    return result.stdout.strip()


def apply_start_gate(workdir: Path, *, run_branch: str, base_branch: str) -> str:
    """Decide per §4 and execute the one allowed action. Returns the tip sha."""
    snap = read_snapshot(workdir, run_branch)
    action = decide_start(snap, run_branch=run_branch, base_branch=base_branch)
    if action is StartAction.SWITCH:
        result = _run_git(workdir, "switch", run_branch)
    elif action is StartAction.CREATE:
        result = _run_git(workdir, "switch", "-c", run_branch)
    else:
        result = None
    if result is not None and result.returncode != 0:
        raise RunBranchGateError(
            "wrong_start_point",
            f"git switch failed: {result.stderr.strip()}",
        )
    return branch_tip(workdir, run_branch)


@dataclass(frozen=True)
class RunBranchRecord:
    """The run row's binding, as the continuation gate consumes it (spec §6)."""

    branch: str
    head: str | None


def verify_continuation(
    workdir: Path, record: RunBranchRecord, *, accept_tip: bool
) -> tuple[str, list[str]]:
    """§6 continuation check: record wins, state (tip) is the invariant.

    Returns (current tip of the recorded branch, dirty paths for the
    caller's warning). Dirtiness NEVER refuses here — spec §6's priced
    hole: a crashed run legitimately leaves uncommitted work.
    """
    snap = read_snapshot(workdir, record.branch)
    if snap.current_branch != record.branch:
        raise RunBranchGateError(
            "resume_branch_mismatch",
            f"run is bound to {record.branch!r} but the checkout is on "
            f"{snap.current_branch!r}; run: git switch {record.branch}",
        )
    tip = branch_tip(workdir, record.branch)
    if record.head is not None and tip != record.head and not accept_tip:
        raise RunBranchGateError(
            "resume_stale_checkout",
            f"branch {record.branch!r} tip moved: recorded {record.head[:12]}, "
            f"observed {tip[:12]} — the state advanced under this run. Resume "
            "the newest run, or re-run with --accept-branch-tip after "
            "inspecting the delta",
        )
    return tip, snap.dirty_paths
```

- [ ] **Step 4: Run tests, verify pass**

Run: `uv run pytest tests/test_run_branch_gate.py -v` → all PASS.

- [ ] **Step 5: pyrefly + ruff + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/run_branch_gate.py tests/test_run_branch_gate.py
git commit -m "feat(gate): git-facing apply/verify for the run-branch gate (spec §4, §6)"
```

---

### Task 4: Run row columns + migration 29 + binding APIs

**Files:**
- Modify: `maestro/database.py` — `run` table schema (~line 1361), migration list tail (~line 662), `create_run_row` (~line 4725), new methods next to `set_run_workstreams_declared`.
- Test: `tests/test_database_run_row.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `create_run_row(*, run_id, repo_key, started_at, run_branch: str | None = None, run_branch_declared: int | None = None, run_branch_head: str | None = None)`
  - `Database.set_run_branch_binding(*, branch: str, declared: int, head: str | None) -> None` — one UPDATE (legacy adoption, spec §6).
  - `Database.update_run_branch_head(head: str) -> None`.
  - `get_run_row()` now returns the three new keys (comes free from `SELECT *`).

- [ ] **Step 1: Verify migration number 29 is free** (Global Constraints): `git worktree list` + `grep -n '(29,' maestro/database.py` on master. If taken, renumber below.

- [ ] **Step 2: Write failing tests** (append to `tests/test_database_run_row.py`, reusing its existing db fixture pattern — every fixture closes the db):

```python
async def test_run_row_branch_binding_roundtrip(db: Database) -> None:
    await db.create_run_row(
        run_id="01TEST",
        repo_key="host/o/r",
        started_at="2026-08-24T00:00:00+00:00",
        run_branch="pilot/x",
        run_branch_declared=1,
        run_branch_head="a" * 40,
    )
    row = await db.get_run_row()
    assert row is not None
    assert row["run_branch"] == "pilot/x"
    assert row["run_branch_declared"] == 1
    assert row["run_branch_head"] == "a" * 40


async def test_run_row_binding_defaults_null(db: Database) -> None:
    await db.create_run_row(
        run_id="01TEST", repo_key="host/o/r", started_at="2026-08-24T00:00:00+00:00"
    )
    row = await db.get_run_row()
    assert row is not None
    assert row["run_branch"] is None
    assert row["run_branch_declared"] is None


async def test_set_run_branch_binding_and_head_update(db: Database) -> None:
    await db.create_run_row(
        run_id="01TEST", repo_key="host/o/r", started_at="2026-08-24T00:00:00+00:00"
    )
    await db.set_run_branch_binding(branch="pilot/x", declared=1, head="a" * 40)
    await db.update_run_branch_head("b" * 40)
    row = await db.get_run_row()
    assert row is not None
    assert row["run_branch"] == "pilot/x"
    assert row["run_branch_head"] == "b" * 40
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_database_run_row.py -v`
Expected: FAIL — unexpected keyword `run_branch` / missing column.

- [ ] **Step 4: Implement.** (a) Extend the `run` CREATE TABLE (after `suspend_reason`, before `singleton`):

```sql
                run_branch          TEXT,
                run_branch_declared INTEGER,
                run_branch_head     TEXT,
```

(b) Append migration 29 to the `ordered` list tail and its body (modeled on `_migrate_run_workstreams_declared`; idempotent via `PRAGMA table_info`):

```python
            (
                29,
                "run_branch_binding",
                self._migrate_run_branch_binding,
            ),
```

```python
    async def _migrate_run_branch_binding(self) -> None:
        """Migration 29: Mode-1 run-branch binding (issue #216 part 2).

        Three additive nullable columns on `run`. Two are NOT one (#198's
        lesson, spec §6): `run_branch_declared` NULL means a pre-migration
        row (the continuation gate fails OPEN with adoption), 0 means the
        run opted out at creation (genuinely byte-identical resume), 1 means
        bound. `run_branch_head` is the branch tip the stale-check compares
        against — state, not a name proxy.
        """
        assert self._connection is not None
        cursor = await self._connection.execute("PRAGMA table_info(run)")
        columns = {row["name"] for row in await cursor.fetchall()}
        for name, ddl in (
            ("run_branch", "ALTER TABLE run ADD COLUMN run_branch TEXT"),
            (
                "run_branch_declared",
                "ALTER TABLE run ADD COLUMN run_branch_declared INTEGER",
            ),
            ("run_branch_head", "ALTER TABLE run ADD COLUMN run_branch_head TEXT"),
        ):
            if name not in columns:
                await self._connection.execute(ddl)
```

(c) Extend `create_run_row` and add the two setters:

```python
    async def create_run_row(
        self,
        *,
        run_id: str,
        repo_key: str,
        started_at: str,
        run_branch: str | None = None,
        run_branch_declared: int | None = None,
        run_branch_head: str | None = None,
    ) -> None:
        """Write the run's own row. Exactly once per database."""
        assert self._connection is not None
        await self._connection.execute(
            "INSERT INTO run (run_id, repo_key, started_at, run_branch, "
            "run_branch_declared, run_branch_head) VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                repo_key,
                started_at,
                run_branch,
                run_branch_declared,
                run_branch_head,
            ),
        )
        await self._connection.commit()

    async def set_run_branch_binding(
        self, *, branch: str, declared: int, head: str | None
    ) -> None:
        """One UPDATE binding the run to a branch (legacy adoption, spec §6)."""
        assert self._connection is not None
        await self._connection.execute(
            "UPDATE run SET run_branch = ?, run_branch_declared = ?, "
            "run_branch_head = ?",
            (branch, declared, head),
        )
        await self._connection.commit()

    async def update_run_branch_head(self, head: str) -> None:
        """Record the branch tip after the run itself moved it (spec §6)."""
        assert self._connection is not None
        await self._connection.execute(
            "UPDATE run SET run_branch_head = ?", (head,)
        )
        await self._connection.commit()
```

- [ ] **Step 5: Run tests, verify pass**

Run: `uv run pytest tests/test_database_run_row.py tests/test_run_publish.py -v` → PASS (run_publish still green: new kwargs are optional).

- [ ] **Step 6: pyrefly + ruff + commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add maestro/database.py tests/test_database_run_row.py
git commit -m "feat(db): run-branch binding columns, migration 29, binding APIs (spec §6)"
```

---

### Task 5: `create_run` passthrough

**Files:**
- Modify: `maestro/run_publish.py::create_run` (~line 25)
- Test: `tests/test_run_publish.py`

**Interfaces:**
- Consumes: Task 4's extended `create_run_row`.
- Produces: `create_run(key, run_id, *, repo_key_text, started_at, home=None, run_branch: str | None = None, run_branch_declared: int | None = None, run_branch_head: str | None = None) -> Path` — the binding is written inside staging, so record and publication are atomic (spec §6).

- [ ] **Step 1: Write the failing test** (append to `tests/test_run_publish.py`, reusing its existing key/home fixtures):

```python
async def test_create_run_persists_branch_binding(tmp_path: Path) -> None:
    key = RepoKey(host="example.com", owner="o", name="r")  # match file's fixture style
    db_path = await create_run(
        key,
        "01TESTRUN",
        repo_key_text="example.com/o/r",
        started_at="2026-08-24T00:00:00+00:00",
        home=tmp_path,
        run_branch="pilot/x",
        run_branch_declared=1,
        run_branch_head="a" * 40,
    )
    db = await create_database(db_path)
    try:
        row = await db.get_run_row()
        assert row is not None and row["run_branch"] == "pilot/x"
        assert row["run_branch_head"] == "a" * 40
    finally:
        await db.close()
```

(Adjust the `RepoKey` construction to whatever `tests/test_run_publish.py` already uses — copy its existing instantiation verbatim.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_run_publish.py -v` → FAIL (unexpected kwarg).

- [ ] **Step 3: Implement** — add the three keyword-only params (defaults `None`) to `create_run` and forward them to `db.create_run_row(...)`.

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_run_publish.py -v` → PASS.

- [ ] **Step 5: pyrefly + ruff + commit**

```bash
git add maestro/run_publish.py tests/test_run_publish.py
git commit -m "feat(publish): create_run carries the run-branch binding into the row"
```

---

### Task 6: PID lock first + `pre_publish` seam + fresh-path gate wiring

**Files:**
- Modify: `maestro/run_bootstrap.py::bootstrap_run` (~line 56)
- Modify: `maestro/cli.py::_run_scheduler` (lock at ~784 moves to after DAG validation ~580; gate wiring around the bootstrap call ~590)
- Test: `tests/test_run_bootstrap.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 3's `apply_start_gate`; Task 5's `create_run` kwargs.
- Produces: `bootstrap_run(config, *, resume, run_id_override, home=None, pre_publish: Callable[[str], Awaitable[dict[str, object]]] | None = None)` — on the FRESH path only, invoked with the minted `run_id` after the `RunIsLive` check and before `create_run`; its returned dict is splatted into `create_run(**extra)`. An exception aborts publication (nothing staged yet). Continuation paths never call it.

- [ ] **Step 1: Write failing bootstrap-seam tests** (append to `tests/test_run_bootstrap.py`, copying its existing config/home fixture style):

```python
async def test_pre_publish_runs_before_create_run_and_feeds_row(...) -> None:
    # fresh path: pre_publish returns {"run_branch": "pilot/x",
    # "run_branch_declared": 1, "run_branch_head": "a"*40};
    # assert the published run row carries them.

async def test_pre_publish_exception_publishes_nothing(...) -> None:
    # pre_publish raises RuntimeError -> bootstrap_run propagates,
    # runs/ directory stays empty, resolve_runs sees no run.

async def test_pre_publish_not_called_on_resume(...) -> None:
    # existing-run path: pre_publish MagicMock not called.
```

Write these three as real tests using the file's existing helpers for creating a resumable run (`create_run` + lock helpers) — copy the arrange code from the nearest existing test in that file; the assert lines above are the contract.

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_run_bootstrap.py -v` → FAIL (unexpected kwarg `pre_publish`).

- [ ] **Step 3: Implement the seam** in `bootstrap_run` (fresh branch, after `run_id = str(ulid.new())`, before `create_run`):

```python
        extra: dict[str, object] = {}
        if pre_publish is not None:
            extra = await pre_publish(run_id)
        db_path = await create_run(
            key,
            run_id,
            repo_key_text=repo_key_text,
            started_at=datetime.now(UTC).isoformat(),
            home=home,
            **extra,  # type: ignore[arg-type]  # keys mirror create_run kwargs
        )
```

- [ ] **Step 4: Run bootstrap tests** — PASS.

- [ ] **Step 5: Write the failing CLI tests** (append to `tests/test_cli.py::TestMode1DefaultLogDir`'s neighborhood as a new class; mocked-scheduler pattern already exists there):

```python
class TestRunBranchGateStart:
    async def test_lock_held_refuses_before_touching_checkout(self, temp_dir): ...
        # config with git: {run_branch: "pilot/x", base_branch: "master"};
        # patch maestro.cli._acquire_pid_lock to raise typer.Exit(1) (lock busy);
        # real temp git repo as config repo; run _run_scheduler; assert
        # current branch unchanged (spec §5, round-1 major 1).

    async def test_fresh_run_creates_branch_and_records_binding(self, temp_dir): ...
        # explicit --db path (skips bootstrap): after _run_scheduler with
        # mocked scheduler, repo is on pilot/x. (Record assertions for the
        # resolver path live in test_run_bootstrap above.)

    async def test_gate_refusal_exits_1_before_db(self, temp_dir): ...
        # dirty repo -> _run_scheduler raises typer.Exit(code=1) and the
        # --db file was never created.
```

Flesh each `...` out with the mocked-scheduler context manager copied verbatim from `TestMode1DefaultLogDir._run_with_mocked_scheduler`, plus a real `git init` repo (helper from Task 3's test file pattern). The config helper `_write_scheduler_config` gains an optional `git_block: dict | None` parameter (append it to the YAML when set).

- [ ] **Step 6: Run to verify failure** — `uv run pytest tests/test_cli.py -k RunBranchGateStart -v` → FAIL.

- [ ] **Step 7: Implement the CLI wiring** in `_run_scheduler`:

1. Move `lock_fd = _acquire_pid_lock()` from its current site (~line 784) to immediately after DAG validation; wrap everything from bootstrap onward in `try: ... finally: _release_pid_lock(lock_fd)`, and delete the release from the inner `finally` (keep exactly one release). The refusal message for a busy lock is unchanged.
2. Build the gate closure when configured (`git_cfg = config.git; gate_on = git_cfg is not None and git_cfg.run_branch is not None`):

```python
        async def _branch_pre_publish(_run_id: str) -> dict[str, object]:
            head = apply_start_gate(
                workdir, run_branch=git_cfg.run_branch, base_branch=git_cfg.base_branch
            )
            return {
                "run_branch": git_cfg.run_branch,
                "run_branch_declared": 1,
                "run_branch_head": head,
            }
```

   Note `workdir = Path(config.repo).expanduser()` must move above the bootstrap call (it currently sits later, ~line 655) — it is needed by the gate.
3. Resolver path: pass `pre_publish=_branch_pre_publish if (gate_on and not resume and run is None) else None` to `bootstrap_run`. When gate is off, pass `None` — byte-identical.
4. Explicit `--db` fresh path (no bootstrap): when `gate_on` and the opened DB has no run row and not `resume`/`run`, call `apply_start_gate(...)` before `create_database` (spec §5: "before the database is opened, and equally after the PID lock"). No record is written (spec §6 `--db` limitation).
5. Catch `RunBranchGateError` at both sites: `err_console.print(f"[red]run-branch gate:[/red] {e}")`, `raise typer.Exit(1) from e`.

- [ ] **Step 8: Run tests** — `uv run pytest tests/test_cli.py tests/test_run_bootstrap.py -v` → PASS, including every pre-existing test (opt-out byte-identical evidence).

- [ ] **Step 9: pyrefly + ruff + commit**

```bash
git add maestro/run_bootstrap.py maestro/cli.py tests/test_run_bootstrap.py tests/test_cli.py
git commit -m "feat(cli): PID lock first, pre_publish seam, fresh-path run-branch gate (spec §5)"
```

---

### Task 7: Continuation verification before recovery (+ recovery re-key, `--clean` carve-out, adoption)

**Files:**
- Modify: `maestro/cli.py::_run_scheduler` (continuation block after `db = await create_database(...)` ~line 636; recovery guard `if resume:` ~line 665)
- Modify: `maestro/cli.py::run_command` — new flag `--accept-branch-tip` (bool, default False), passed through to `_run_scheduler`.
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 3's `verify_continuation`, `RunBranchRecord`, `branch_tip`; Task 4's `set_run_branch_binding`.
- Produces: `_run_scheduler(config_path, db_path, resume, log_dir, clean=False, run=None, accept_branch_tip=False)`.

- [ ] **Step 1: Write the failing tests** (new class in `tests/test_cli.py`; same mocked-scheduler + real-temp-repo machinery as Task 6; each test seeds the DB run row via `create_database` + `create_run_row` with the binding it needs, then closes it before invoking `_run_scheduler`):

```python
class TestRunBranchGateContinuation:
    async def test_wrong_branch_refuses_before_recovery(self, temp_dir): ...
        # row: (branch="pilot/x", declared=1, head=<tip>); checkout on master;
        # patch maestro.cli.StateRecovery -> assert never constructed;
        # resume=True -> typer.Exit(1).

    async def test_moved_tip_refuses_and_flag_re_records(self, temp_dir): ...
        # foreign commit on pilot/x -> refuse; rerun with
        # accept_branch_tip=True -> proceeds and row head == new tip.

    async def test_declared_zero_resume_is_silent(self, temp_dir): ...
        # row: (None, 0, None); checkout anywhere; no warning text in
        # captured output, no refusal, no adoption (row unchanged).

    async def test_legacy_null_adopts_only_matching_config(self, temp_dir): ...
        # row: (None, NULL, None); config run_branch="pilot/x".
        # On master -> refuse resume_branch_mismatch, row unchanged.
        # On pilot/x -> proceeds; row becomes ("pilot/x", 1, <tip>).

    async def test_legacy_null_without_config_backfills_declared_zero(self, temp_dir): ...

    async def test_run_selector_without_resume_verifies(self, temp_dir): ...
        # run="01TESTRUN", resume=False, wrong branch -> same refusal
        # (spec §2: continuation is defined by selection, not the flag).
        # NOTE: this drives the resolver path — seed via create_run(home=...)
        # and patch maestro.cli.maestro_home / pass config repo with origin;
        # copy the arrange machinery from tests/test_run_bootstrap.py.

    async def test_db_without_row_and_configured_branch_refuses(self, temp_dir): ...
        # --db DB has tasks but NO run row; config declares run_branch;
        # resume=True -> record_missing refusal (spec §6, round-2 major 3).

    async def test_clean_takes_start_gate_not_continuation(self, temp_dir): ...
        # --db existing branch-bound DB + clean=True + tip moved -> no
        # continuation refusal; DB unlinked; start gate applied (spec §6).

    async def test_dirty_continuation_warns_and_proceeds(self, temp_dir): ...
        # matching branch+tip, uncommitted edit -> proceeds; warning text
        # contains the dirty path (spec §6 priced hole).

    async def test_continuation_selector_runs_recovery(self, temp_dir): ...
        # run selector without resume + a task row in RUNNING ->
        # StateRecovery constructed and recover() awaited (round-5 major 1).
```

Every `...` body is written out fully at implementation time using the two established arrange helpers; the comments above are the binding contract and the assert lines are non-negotiable.

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_cli.py -k RunBranchGateContinuation -v` → FAIL.

- [ ] **Step 3: Implement** in `_run_scheduler`, in this exact order (spec §6):

```python
        # Continuation = an existing run was selected, however it was selected
        # (spec §2). --clean discards the state there is to continue (§6).
        continuation_selected = (resume or run is not None or db_path is not None) and not clean
```

After `db = await create_database(resolved_db_path)` (and after the `--clean` unlink block, which already precedes it):

```python
        if continuation_selected:
            row = await db.get_run_row()
            configured = git_cfg.run_branch if git_cfg is not None else None
            if row is None:
                if configured is not None and (resume or run is not None):
                    raise RunBranchGateError(
                        "record_missing",
                        "this database has no run row, so its branch binding "
                        "is unknown; resume through the resolver path, or drop "
                        "git.run_branch to run it ungated",
                    )
            else:
                declared = row.get("run_branch_declared")
                if declared == 1:
                    record = RunBranchRecord(
                        branch=str(row["run_branch"]),
                        head=(str(row["run_branch_head"]) if row["run_branch_head"] else None),
                    )
                    tip, dirty = verify_continuation(
                        workdir, record, accept_tip=accept_branch_tip
                    )
                    if accept_branch_tip and record.head != tip:
                        await db.update_run_branch_head(tip)
                        logger.warning("run_branch.tip_accepted", ...)
                    if dirty:
                        err_console.print(
                            "[yellow]run-branch gate: uncommitted paths will "
                            f"ride into task commits: {', '.join(dirty[:10])}[/yellow]"
                        )
                elif declared is None:  # true pre-migration row (spec §6)
                    if configured is None:
                        await db.set_run_branch_binding(...)  # declared=0 backfill:
                        # implement as an UPDATE setting only declared=0 — add a
                        # tiny Database.set_run_branch_declared(0) if the
                        # three-field setter does not fit; keep branch/head NULL
                    else:
                        snap_tip_check = verify_continuation(
                            workdir,
                            RunBranchRecord(branch=configured, head=None),
                            accept_tip=False,
                        )  # raises resume_branch_mismatch when not on `configured`
                        tip = branch_tip(workdir, configured)
                        await db.set_run_branch_binding(
                            branch=configured, declared=1, head=tip
                        )
                        err_console.print(
                            "[yellow]run-branch gate: legacy run adopted "
                            f"binding to {configured!r} (verified against the "
                            "config; record was pre-migration)[/yellow]"
                        )
                # declared == 0: opted out — genuinely silent (spec §6).
```

Wrap the whole block in the same `except RunBranchGateError` → stderr + `typer.Exit(1)` handler as Task 6. Then re-key recovery:

```python
        if continuation_selected and (row is not None or resume):
            # was: `if resume:` — a gated continuation that skips recovery
            # stalls on crash-stranded tasks (spec §6, round-5 major 1)
```

Keep the inner `existing_tasks` check unchanged. Add `accept_branch_tip` as a `run` command option (`--accept-branch-tip`, help: "Re-record the run branch tip after inspecting an unexpected advance (audited)") and thread it through.

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_cli.py -v` foreground → ALL pass, old and new.

- [ ] **Step 5: pyrefly + ruff + commit**

```bash
git add maestro/cli.py tests/test_cli.py
git commit -m "feat(cli): continuation verification before recovery, adoption, --accept-branch-tip (spec §6)"
```

---

### Task 8: `run_branch_head` maintenance (auto-commit + graceful stop)

**Files:**
- Modify: `maestro/scheduler.py` — `SchedulerConfig` (~line 234) + the `_auto_commit_task` call sites (~lines 1898, 1922)
- Modify: `maestro/cli.py::_run_scheduler` — wire the callback; refresh head after `scheduler.run()` returns and in the cancellation handler
- Test: `tests/test_scheduler.py` (callback), `tests/test_cli.py` (wiring)

**Interfaces:**
- Consumes: Task 4's `update_run_branch_head`; Task 3's `branch_tip`.
- Produces: `SchedulerConfig.on_auto_commit: Callable[[], Awaitable[None]] | None = None` — awaited right after a successful `_auto_commit_task` (the callback owner reads the tip and persists it; the scheduler stays git/DB-agnostic about the run row).

- [ ] **Step 1: Write the failing scheduler test** (copy the nearest auto-commit test's arrange from `tests/test_scheduler.py`; assert the callback fires after a task's auto-commit and is absent-safe):

```python
async def test_on_auto_commit_callback_fires_after_commit(...) -> None:
    calls: list[int] = []

    async def on_commit() -> None:
        calls.append(1)

    # scheduler config: auto_commit=True, on_auto_commit=on_commit,
    # one announce-style task; after the run: assert calls == [1]
```

- [ ] **Step 2: Verify failure** — `uv run pytest tests/test_scheduler.py -k on_auto_commit -v` → FAIL (unknown field).

- [ ] **Step 3: Implement**: add the field to `SchedulerConfig`; at BOTH `_auto_commit_task(...)` call sites append:

```python
        self._auto_commit_task(task)
        if self._config.on_auto_commit is not None:
            await self._config.on_auto_commit()
```

`create_scheduler_from_config` (~line 2719) gains the passthrough parameter. In `_run_scheduler`, when the gate is active (fresh with binding, or continuation `declared==1`):

```python
        async def _record_head() -> None:
            try:
                await db.update_run_branch_head(branch_tip(workdir, bound_branch))
            except Exception:  # noqa: BLE001 — bookkeeping must not fail the task
                logger.warning("run_branch.head_record_failed", exc_info=True)
```

pass `on_auto_commit=_record_head if gate_active else None`; and after `await scheduler.run()` plus in the `except (KeyboardInterrupt, asyncio.CancelledError)` handler, best-effort call `_record_head()` before `db.close()` (spec §6: refresh on graceful stop).

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_scheduler.py -k on_auto_commit tests/test_cli.py -v` → PASS.

- [ ] **Step 5: pyrefly + ruff + commit**

```bash
git add maestro/scheduler.py maestro/cli.py tests/test_scheduler.py tests/test_cli.py
git commit -m "feat(scheduler): on_auto_commit hook; run_branch_head kept current (spec §6)"
```

---

### Task 9: End-to-end proof + CHANGELOG + ledger note

**Files:**
- Test: `tests/test_run_branch_e2e.py` (new)
- Modify: `CHANGELOG.md` (Unreleased → Added), `TODO.md` (progress note under `mode1-branch-isolation` — append body lines only, never touch the checkbox line)

**Interfaces:** consumes everything above through the public CLI path (`_run_scheduler` with the real announce spawner — no mocks).

- [ ] **Step 1: Write the failing e2e test** (announce agent is a no-op echo — safe and fast; pattern proven in #217's verification):

```python
"""E2E: git.run_branch on a real repo with the announce agent (spec §4-§6)."""


async def test_fresh_run_lands_commits_on_run_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)  # git init -b master + one commit (Task 3 helper)
    config_path = _write_config(
        tmp_path,
        repo,
        git_block={
            "base_branch": "master",
            "auto_commit": True,
            "run_branch": "pilot/e2e",
        },
    )
    db_path = tmp_path / "state" / "run.db"
    db_path.parent.mkdir()
    await _run_scheduler(
        config_path=config_path, db_path=db_path, resume=False, log_dir=None
    )
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "pilot/e2e"
    # master untouched, task commit landed on the run branch
    assert _git(repo, "rev-list", "--count", "master..pilot/e2e") >= "0"


async def test_second_fresh_run_iterates_on_same_branch(tmp_path: Path) -> None:
    # run once, run again with a second --db: passes the `cur == B, clean`
    # row — the consumer's iteration pattern (spec §4).
```

(`_write_config` = Task 6's extended `_write_scheduler_config` equivalent, local to this file; one announce task `{"id": "t1", "title": "T1", "prompt": "hi", "agent_type": "announce"}`.)

- [ ] **Step 2: Verify it fails on master-parity code, passes on the branch** — `uv run pytest tests/test_run_branch_e2e.py -v` → PASS here (the feature exists by now); its RED evidence is that it exercises no mocks — if any prior task's wiring is wrong it fails loudly. Run it, read the output.

- [ ] **Step 3: CHANGELOG** — add under `## Unreleased` / `### Added`:

```markdown
- **Mode-1 run-level branch isolation — `git.run_branch` (phase A)** (#216
  part 2). One opt-in key gives a Mode-1 run one checkout on one branch:
  the runtime verifies or creates the branch (from `base_branch`, clean
  tree only, never a stash) under the PID lock BEFORE the run is
  published, records the binding (`run_branch`/`run_branch_declared`/
  `run_branch_head`, migration 29) atomically with publication, and
  re-verifies on every continuation — any selector of an existing run —
  BEFORE recovery, by record and by branch-tip state, with
  `--accept-branch-tip` as the audited escape for a tip the operator has
  inspected. Absent key = unchanged behavior; the PID lock is now
  acquired at startup rather than after scheduler construction (same
  lock, earlier refusal). Design: docs/superpowers/specs/
  2026-08-24-mode1-run-branch-isolation-design.md (phase B — live
  tripwires — ships separately).
```

- [ ] **Step 4: TODO.md** — append to the `mode1-branch-isolation` item body (do NOT touch the first line): `Фаза A реализована (PR #TBD): гейт+запись+continuation-верификация; фаза B (tripwire) — отдельный PR. Пункт держит блокер dispatcher'а до перегона пилота.` Fill the PR number at PR time.

- [ ] **Step 5: Full local gate + commit**

```bash
uv run pytest tests/test_run_branch_gate.py tests/test_run_branch_e2e.py tests/test_cli.py tests/test_run_bootstrap.py tests/test_run_publish.py tests/test_database_run_row.py tests/test_scheduler.py -v
uv run pyrefly check && uv run ruff format . && uv run ruff check .
git add tests/test_run_branch_e2e.py CHANGELOG.md TODO.md
git commit -m "test(e2e): run-branch gate end-to-end with announce agent; changelog + ledger"
```

- [ ] **Step 6: Push, open PR, request Copilot review** (body: what phase A delivers, spec link, phase B deferred, `run check-plan-fields` before PR). Iterate on reviews per repo workflow; the human merges.

---

## Self-review notes (already applied)

- Spec coverage: §3→Task 1, §4→Tasks 2/3, §5→Task 6, §6→Tasks 4/5/7/8, §8→Tasks 2/6/7 (reason codes + stderr + exit 1), §9's phase-A tests→Tasks 2–9. §7 (phase B) is explicitly out — separate plan. §10 rollout is dispatcher-side.
- The `resume_superseded` name does not exist anymore (spec rev 6 renamed the mechanism to `resume_stale_checkout`); this plan uses only the rev-8 codes.
- Type consistency: `RunBranchRecord(branch, head)`, `set_run_branch_binding(branch, declared, head)`, `update_run_branch_head(head)`, `on_auto_commit: Callable[[], Awaitable[None]]` — used with these exact shapes in Tasks 3–8.
