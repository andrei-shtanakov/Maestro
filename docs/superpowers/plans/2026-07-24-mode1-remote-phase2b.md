# Mode-1 remote (Phase 2b, Safety-core) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a Mode-1 (`maestro run`) task execute on an SSH backend safely, guarded by a `(workdir, scope)` reservation lock and scope-bounded collect.

**Architecture:** A new pure-logic module `maestro/execution/reservations.py` computes static per-workdir arming, path-anchor scope reservations, and a conservative overlap test. The `Scheduler` arms workdirs at startup (fail-fast on unbounded SSH scope), acquires a reservation immediately before the atomic `start_execution` CAS, holds it across run→collect, and releases it on the durable `collected` transition. `ssh_collect` gains a scope reject/apply filter over its existing full-worktree baseline. `SshBackend` exposes a `LaunchNotStarted` exception so the scheduler can tell proven-not-started (release) from uncertain (hold).

**Tech Stack:** Python 3.12+, uv, pytest + anyio, Pydantic, aiosqlite. Existing `maestro/execution/` contract (`ExecutionRequest`, `CollectPolicy`, `SshBackend`, handle state machine `prepared→running→terminal→collected→cleaned`).

## Global Constraints

- Package manager: `uv` only — `uv add`, `uv run pytest`, `uv run pyrefly check`, `uv run ruff format .`, `uv run ruff check .`. Never `pip`.
- Type hints on all code; `uv run pyrefly check` clean after every task.
- Line length 88; `uv run ruff format .` + `uv run ruff check . --fix` before every commit.
- Async tests use `anyio`, not `asyncio`.
- Git: work on branch `feat/mode1-remote-phase2b` (already checked out). No direct commits to `master`. Commit after every task.
- Behavior-compatibility: a workdir with **no** SSH task must keep its current observable scheduling — the reservation registry is never consulted for it.
- SSH detection is by **`transport.type == "ssh"`** on the effective backend, never `backend != "local"` (local Docker must stay out of the reservation protocol).
- Commit message trailer (every commit):
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01E5eoepxD5CHkbWxARRSuBn
  ```
  (Trailer omitted from the `git commit` snippets below for brevity — add it.)

---

## File structure

- **Create `maestro/execution/reservations.py`** — pure reservation logic: `anchor_of`, `canonical_workdir`, `Reservation`, `scope_to_reservation`, `overlaps`, `ReservationRegistry`, plus arming helpers `effective_backend_name`, `is_ssh_task`, `compute_armed_workdirs`, `validate_ssh_scopes`, `UnboundedRemoteScopeError`.
- **Create `tests/test_reservations.py`** — unit tests for the module (no I/O, no DB).
- **Modify `maestro/execution/resolver.py`** — delete the Mode-2-only SSH guard (`_build`, lines 52-57).
- **Modify `maestro/execution/ssh_collect.py`** — add `path_in_scope` + a `scope` parameter to `plan_collect` that rejects out-of-scope changes and bounds the apply set.
- **Modify `maestro/execution/ssh_handle.py`** — `CollectSpec` gains `scope: list[str] | None`; `collect()` passes it to `plan_collect`.
- **Modify `maestro/execution/ssh_backend.py`** — thread `req.collect.include` into `CollectSpec.scope`; raise `LaunchNotStarted` for proven-pre-launch failures.
- **Modify `maestro/scheduler.py`** — keep `self._execution`; arming + fail-fast at `run()` start; acquire/hold/release + launch-stage rollback in the dispatch path; recovery reconstruction.
- **Modify `tests/test_scheduler_ssh_guard.py`** — flip from "guard raises" to "ssh resolves in scheduler mode".
- **Create `tests/test_scheduler_reservations.py`** — arming, fail-fast, contention, rollback, recovery (async, in-memory DB).

---

## Task 1: Path anchors and conservative overlap

**Files:**
- Create: `maestro/execution/reservations.py`
- Test: `tests/test_reservations.py`

**Interfaces:**
- Produces:
  - `anchor_of(glob: str) -> str` — longest leading wildcard-free path prefix of a glob, as a posix string; `""` means "workdir root" (reserves everything).
  - `_covers(a: str, b: str) -> bool` — anchor `a` covers anchor `b` (segment-boundary prefix; `""` covers all).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reservations.py
from maestro.execution.reservations import anchor_of, _covers


def test_anchor_of_literal_prefix():
    assert anchor_of("src/api/*.py") == "src/api"
    assert anchor_of("pkg/**") == "pkg"
    assert anchor_of("lib/**/x.py") == "lib"


def test_anchor_of_leading_wildcard_is_root():
    assert anchor_of("**") == ""
    assert anchor_of("*.py") == ""
    assert anchor_of("**/x") == ""


def test_anchor_of_pure_literal_is_itself():
    assert anchor_of("config.yaml") == "config.yaml"
    assert anchor_of("a/b/c.txt") == "a/b/c.txt"


def test_covers_prefix_and_root():
    assert _covers("", "anything/here") is True
    assert _covers("src", "src/api/x.py") is True
    assert _covers("src", "src") is True
    assert _covers("src", "srcfoo/x") is False  # segment boundary, not substring
    assert _covers("src/api", "src") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_reservations.py -v`
Expected: FAIL — `ModuleNotFoundError` / `ImportError: cannot import name 'anchor_of'`.

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/execution/reservations.py
"""(workdir, scope) reservations + static per-workdir arming for Mode-1 remote.

Pure path logic (no DB, no filesystem writes). The overlap test is
deliberately conservative in *possible-path* space: it may serialize two
disjoint scopes that share an ancestor (false positive), but it never lets a
real overlap through (no false negative). Exact-path matching lives in
`ssh_collect` against actual changed paths.
"""

_WILDCARD = set("*?[")


def anchor_of(glob: str) -> str:
    """Longest leading wildcard-free path prefix; '' == workdir root."""
    segments = glob.strip("/").split("/")
    literal: list[str] = []
    for seg in segments:
        if any(ch in _WILDCARD for ch in seg):
            break
        literal.append(seg)
    return "/".join(literal)


def _covers(a: str, b: str) -> bool:
    """Anchor `a` covers anchor `b` on segment boundaries; '' covers all."""
    if a == "":
        return True
    return b == a or b.startswith(a + "/")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_reservations.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Type-check, format, commit**

```bash
uv run pyrefly check
uv run ruff format . && uv run ruff check . --fix
git add maestro/execution/reservations.py tests/test_reservations.py
git commit -m "feat(reservations): path anchor extraction + conservative cover test"
```

---

## Task 2: Reservation value + canonical workdir + overlap

**Files:**
- Modify: `maestro/execution/reservations.py`
- Test: `tests/test_reservations.py`

**Interfaces:**
- Consumes: `anchor_of`, `_covers` (Task 1).
- Produces:
  - `canonical_workdir(path: str | Path) -> Path` — `Path(path).expanduser().resolve()` (absolute, symlinks resolved; non-existent tail allowed).
  - `Reservation` — frozen dataclass `(workdir: Path, anchors: frozenset[str])`.
  - `scope_to_reservation(workdir: str | Path, scope: list[str]) -> Reservation` — empty scope ⇒ anchors `{""}` (whole workdir).
  - `overlaps(a: Reservation, b: Reservation) -> bool` — `False` for different canonical workdirs; else any anchor pair mutually covering.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_reservations.py
from pathlib import Path

from maestro.execution.reservations import (
    Reservation,
    canonical_workdir,
    overlaps,
    scope_to_reservation,
)


def test_scope_to_reservation_empty_is_whole_workdir():
    r = scope_to_reservation("/repo", [])
    assert r.anchors == frozenset({""})


def test_scope_to_reservation_anchors():
    r = scope_to_reservation("/repo", ["src/api/*.py", "docs/**"])
    assert r.anchors == frozenset({"src/api", "docs"})


def test_overlaps_same_workdir_shared_subtree():
    a = scope_to_reservation("/repo", ["src/**"])
    b = scope_to_reservation("/repo", ["src/api/x.py"])
    assert overlaps(a, b) is True


def test_disjoint_scopes_do_not_overlap():
    a = scope_to_reservation("/repo", ["src/**"])
    b = scope_to_reservation("/repo", ["docs/**"])
    assert overlaps(a, b) is False


def test_whole_workdir_overlaps_everything_on_same_workdir():
    a = scope_to_reservation("/repo", [])          # {""}
    b = scope_to_reservation("/repo", ["docs/**"])
    assert overlaps(a, b) is True


def test_different_workdirs_never_overlap():
    a = scope_to_reservation("/repo-a", [])
    b = scope_to_reservation("/repo-b", [])
    assert overlaps(a, b) is False


def test_canonical_workdir_is_absolute(tmp_path: Path):
    assert canonical_workdir(tmp_path).is_absolute()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_reservations.py -k "scope_to_reservation or overlaps or canonical" -v`
Expected: FAIL — `ImportError: cannot import name 'scope_to_reservation'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to maestro/execution/reservations.py
from dataclasses import dataclass
from pathlib import Path


def canonical_workdir(path: str | Path) -> Path:
    """Absolute, symlink-resolved workdir key (same policy everywhere)."""
    return Path(path).expanduser().resolve()


@dataclass(frozen=True)
class Reservation:
    workdir: Path
    anchors: frozenset[str]


def scope_to_reservation(workdir: str | Path, scope: list[str]) -> Reservation:
    """Empty/undeclared scope reserves the whole workdir (anchor '')."""
    anchors = frozenset(anchor_of(g) for g in scope) if scope else frozenset({""})
    return Reservation(workdir=canonical_workdir(workdir), anchors=anchors)


def overlaps(a: Reservation, b: Reservation) -> bool:
    if a.workdir != b.workdir:
        return False
    return any(
        _covers(x, y) or _covers(y, x) for x in a.anchors for y in b.anchors
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_reservations.py -v`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Type-check, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check . --fix
git add maestro/execution/reservations.py tests/test_reservations.py
git commit -m "feat(reservations): Reservation, canonical workdir, overlap test"
```

---

## Task 3: Arming and fail-fast helpers

**Files:**
- Modify: `maestro/execution/reservations.py`
- Test: `tests/test_reservations.py`

**Interfaces:**
- Consumes: `canonical_workdir` (Task 2); `ExecutionConfig`/`BackendSpec`/`SshTransport` from `maestro.execution.exec_config`; `Task` from `maestro.models`.
- Produces:
  - `UnboundedRemoteScopeError(Exception)`.
  - `effective_backend_name(task: Task, execution: ExecutionConfig) -> str` — `task.backend or execution.default_backend`.
  - `is_ssh_task(task: Task, execution: ExecutionConfig) -> bool` — effective backend's spec has `transport.type == "ssh"`; unknown name ⇒ `False`.
  - `compute_armed_workdirs(tasks, execution) -> set[Path]` — canonical workdirs hosting ≥1 SSH task.
  - `validate_ssh_scopes(tasks, execution) -> None` — raise `UnboundedRemoteScopeError` if any SSH task has empty `scope`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_reservations.py
import pytest

from maestro.execution.exec_config import (
    BackendSpec,
    ExecutionConfig,
    SshTransport,
)
from maestro.execution.reservations import (
    UnboundedRemoteScopeError,
    compute_armed_workdirs,
    is_ssh_task,
    validate_ssh_scopes,
)
from maestro.models import AgentType, Task, TaskStatus


def _task(tid: str, workdir: str, backend: str | None, scope: list[str]) -> Task:
    return Task(
        id=tid,
        title=tid,
        prompt="do x",
        agent_type=AgentType.CLAUDE_CODE,
        workdir=workdir,
        status=TaskStatus.PENDING,
        backend=backend,
        scope=scope,
    )


def _exec_with_ssh() -> ExecutionConfig:
    return ExecutionConfig(
        default_backend="local",
        backends={"remote": BackendSpec(transport=SshTransport(host="h"))},
    )


def test_is_ssh_task_by_transport_not_name():
    ex = _exec_with_ssh()
    assert is_ssh_task(_task("t1", "/r", "remote", ["src/**"]), ex) is True
    assert is_ssh_task(_task("t2", "/r", "local", []), ex) is False
    assert is_ssh_task(_task("t3", "/r", "unknown", []), ex) is False


def test_compute_armed_workdirs(tmp_path):
    ex = _exec_with_ssh()
    wd_armed = str(tmp_path / "armed")
    wd_plain = str(tmp_path / "plain")
    tasks = [
        _task("t1", wd_armed, "remote", ["src/**"]),
        _task("t2", wd_armed, "local", []),
        _task("t3", wd_plain, "local", []),
    ]
    armed = compute_armed_workdirs(tasks, ex)
    from maestro.execution.reservations import canonical_workdir

    assert canonical_workdir(wd_armed) in armed
    assert canonical_workdir(wd_plain) not in armed


def test_validate_ssh_scopes_rejects_unbounded():
    ex = _exec_with_ssh()
    with pytest.raises(UnboundedRemoteScopeError):
        validate_ssh_scopes([_task("t1", "/r", "remote", [])], ex)


def test_validate_ssh_scopes_accepts_bounded():
    ex = _exec_with_ssh()
    validate_ssh_scopes([_task("t1", "/r", "remote", ["src/**"])], ex)  # no raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_reservations.py -k "ssh_task or armed or ssh_scopes" -v`
Expected: FAIL — `ImportError: cannot import name 'is_ssh_task'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to maestro/execution/reservations.py
from maestro.execution.exec_config import ExecutionConfig
from maestro.models import Task


class UnboundedRemoteScopeError(Exception):
    """A Mode-1 SSH task declared no scope — remote collect would be unbounded."""


def effective_backend_name(task: Task, execution: ExecutionConfig) -> str:
    return task.backend or execution.default_backend


def is_ssh_task(task: Task, execution: ExecutionConfig) -> bool:
    spec = execution.normalized().get(effective_backend_name(task, execution))
    return spec is not None and spec.transport.type == "ssh"


def compute_armed_workdirs(
    tasks: list[Task], execution: ExecutionConfig
) -> set[Path]:
    return {
        canonical_workdir(t.workdir)
        for t in tasks
        if is_ssh_task(t, execution)
    }


def validate_ssh_scopes(tasks: list[Task], execution: ExecutionConfig) -> None:
    for t in tasks:
        if is_ssh_task(t, execution) and not t.scope:
            raise UnboundedRemoteScopeError(
                f"SSH task {t.id!r} has no scope: remote Mode-1 execution "
                "requires a bounding scope (parent design §2/§7)"
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_reservations.py -v`
Expected: PASS (all reservation tests).

- [ ] **Step 5: Type-check, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check . --fix
git add maestro/execution/reservations.py tests/test_reservations.py
git commit -m "feat(reservations): static per-workdir arming + unbounded-scope fail-fast"
```

---

## Task 4: ReservationRegistry (acquire / release / reconstruct)

**Files:**
- Modify: `maestro/execution/reservations.py`
- Test: `tests/test_reservations.py`

**Interfaces:**
- Consumes: `Reservation`, `overlaps` (Task 2).
- Produces `ReservationRegistry`:
  - `try_acquire(owner: str, r: Reservation) -> bool` — `False` (no state change) if `r` overlaps any currently-held reservation with a *different* owner; else records and returns `True`. Idempotent for the same owner.
  - `reconstruct(owner: str, r: Reservation) -> None` — unconditionally records (recovery; overlap is impossible across a restart of a consistent run).
  - `release(owner: str) -> None` — drop the owner's reservation (no-op if absent).
  - `holds(owner: str) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_reservations.py
from maestro.execution.reservations import ReservationRegistry


def test_acquire_disjoint_both_succeed():
    reg = ReservationRegistry()
    assert reg.try_acquire("a", scope_to_reservation("/r", ["src/**"])) is True
    assert reg.try_acquire("b", scope_to_reservation("/r", ["docs/**"])) is True


def test_acquire_overlap_second_fails_and_is_not_recorded():
    reg = ReservationRegistry()
    assert reg.try_acquire("a", scope_to_reservation("/r", ["src/**"])) is True
    assert reg.try_acquire("b", scope_to_reservation("/r", ["src/api/x"])) is False
    assert reg.holds("b") is False


def test_release_frees_the_scope():
    reg = ReservationRegistry()
    reg.try_acquire("a", scope_to_reservation("/r", ["src/**"]))
    reg.release("a")
    assert reg.try_acquire("b", scope_to_reservation("/r", ["src/api/x"])) is True


def test_acquire_same_owner_is_idempotent():
    reg = ReservationRegistry()
    r = scope_to_reservation("/r", ["src/**"])
    assert reg.try_acquire("a", r) is True
    assert reg.try_acquire("a", r) is True  # re-acquire own reservation


def test_reconstruct_records_unconditionally():
    reg = ReservationRegistry()
    reg.reconstruct("a", scope_to_reservation("/r", ["src/**"]))
    assert reg.holds("a") is True
    assert reg.try_acquire("b", scope_to_reservation("/r", ["src/x"])) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_reservations.py -k "acquire or release or reconstruct" -v`
Expected: FAIL — `ImportError: cannot import name 'ReservationRegistry'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to maestro/execution/reservations.py
class ReservationRegistry:
    """In-memory owner->Reservation map with a conservative overlap gate."""

    def __init__(self) -> None:
        self._held: dict[str, Reservation] = {}

    def try_acquire(self, owner: str, r: Reservation) -> bool:
        for other, held in self._held.items():
            if other != owner and overlaps(held, r):
                return False
        self._held[owner] = r
        return True

    def reconstruct(self, owner: str, r: Reservation) -> None:
        self._held[owner] = r

    def release(self, owner: str) -> None:
        self._held.pop(owner, None)

    def holds(self, owner: str) -> bool:
        return owner in self._held
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_reservations.py -v`
Expected: PASS (entire file).

- [ ] **Step 5: Type-check, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check . --fix
git add maestro/execution/reservations.py tests/test_reservations.py
git commit -m "feat(reservations): ReservationRegistry acquire/release/reconstruct"
```

---

## Task 5: Remove the Mode-2-only SSH resolver guard

**Files:**
- Modify: `maestro/execution/resolver.py:45-59` (`_build`)
- Test: `tests/test_scheduler_ssh_guard.py` (rewrite)

**Interfaces:**
- Consumes: nothing new.
- Produces: `BackendResolver(execution, mode="scheduler").resolve("remote")` returns an `SshBackend` instead of raising.

- [ ] **Step 1: Rewrite the test to assert the new behavior**

Replace the body of `tests/test_scheduler_ssh_guard.py` with:

```python
"""Phase 2b lifts the Mode-1 SSH guard: an ssh backend now resolves in
scheduler mode (safety moved to the scheduler's reservation lock)."""

from maestro.execution.exec_config import (
    BackendSpec,
    ExecutionConfig,
    SshTransport,
)
from maestro.execution.resolver import BackendResolver
from maestro.execution.ssh_backend import SshBackend


def test_ssh_resolves_in_scheduler_mode():
    ex = ExecutionConfig(
        default_backend="local",
        backends={"remote": BackendSpec(transport=SshTransport(host="h"))},
    )
    resolver = BackendResolver(ex, mode="scheduler")
    backend = resolver.resolve("remote")
    assert isinstance(backend, SshBackend)
    assert backend.id == "remote"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scheduler_ssh_guard.py -v`
Expected: FAIL — `ExecutionConfigError: ... SSH backends are Mode-2 (orchestrator) only until Phase 2b`.

- [ ] **Step 3: Delete the guard**

In `maestro/execution/resolver.py`, `_build`, remove the scheduler-mode block so the SSH branch reads:

```python
        if isinstance(transport, SshTransport):
            return self._build_ssh(name, spec, transport)
```

(Delete the `if self._mode == "scheduler": raise ExecutionConfigError(...)` lines. `self._mode` may now be unused by `_build` — leave the constructor field; recovery/tests still pass `mode`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_scheduler_ssh_guard.py -v`
Expected: PASS.

- [ ] **Step 5: Type-check, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check . --fix
git add maestro/execution/resolver.py tests/test_scheduler_ssh_guard.py
git commit -m "feat(resolver): lift Mode-2-only SSH guard (Phase 2b)"
```

---

## Task 6: Scope-bounded collect in ssh_collect

**Files:**
- Modify: `maestro/execution/ssh_collect.py` (`plan_collect`, add `path_in_scope`)
- Test: `tests/test_ssh_collect_scope.py` (create)

**Interfaces:**
- Consumes: existing `capture_baseline`, `apply_collect`, `CollectConflict`, `CollectPlan`.
- Produces:
  - `path_in_scope(rel: str, scope: list[str]) -> bool` — a changed path matches a scope entry itself, its subtree, or its glob (fnmatch). `scope=[]`/`None` handling stays in the caller.
  - `plan_collect(worktree, staging, baseline, *, forbidden, scope=None)` — when `scope` is a non-empty list, any modified/deleted path not in scope raises `CollectConflict`; `CollectPlan` is bounded to in-scope paths. `scope=None` preserves today's whole-worktree behavior.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ssh_collect_scope.py
import pytest

from maestro.execution.ssh_collect import (
    CollectConflict,
    capture_baseline,
    path_in_scope,
    plan_collect,
)


def test_path_in_scope_matches_subtree_and_glob():
    assert path_in_scope("src/api/x.py", ["src/**"]) is True
    assert path_in_scope("src/api/x.py", ["src/api"]) is True
    assert path_in_scope("src/api/x.py", ["src/api/*.py"]) is True
    assert path_in_scope("docs/readme.md", ["src/**"]) is False


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_plan_collect_rejects_out_of_scope_change(tmp_path):
    worktree = tmp_path / "wt"
    staging = tmp_path / "st"
    _write(worktree / "src" / "a.py", "orig")
    _write(worktree / "docs" / "r.md", "orig")
    baseline = capture_baseline(worktree, excludes=[])
    # Remote changed BOTH an in-scope and an out-of-scope file:
    _write(staging / "src" / "a.py", "changed")
    _write(staging / "docs" / "r.md", "changed")
    with pytest.raises(CollectConflict):
        plan_collect(
            worktree, staging, baseline, forbidden=[".git"], scope=["src/**"]
        )


def test_plan_collect_bounds_plan_to_scope(tmp_path):
    worktree = tmp_path / "wt"
    staging = tmp_path / "st"
    _write(worktree / "src" / "a.py", "orig")
    _write(worktree / "src" / "b.py", "orig")
    baseline = capture_baseline(worktree, excludes=[])
    _write(staging / "src" / "a.py", "changed")
    _write(staging / "src" / "b.py", "orig")  # unchanged
    plan = plan_collect(
        worktree, staging, baseline, forbidden=[".git"], scope=["src/**"]
    )
    assert plan.modified == ["src/a.py"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ssh_collect_scope.py -v`
Expected: FAIL — `ImportError: cannot import name 'path_in_scope'` (and `plan_collect` has no `scope` kwarg).

- [ ] **Step 3: Implement scope filtering**

In `maestro/execution/ssh_collect.py` add near `_excluded`:

```python
def path_in_scope(rel: str, scope: list[str]) -> bool:
    """True if `rel` is covered by any scope entry (self, subtree, or glob)."""
    for pat in scope:
        norm = pat.strip("/")
        if rel == norm or rel.startswith(norm + "/"):
            return True
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, norm + "/*"):
            return True
    return False
```

Change the `plan_collect` signature and add the scope gate + bounding. The current body computes `modified`/`deleted`; insert the scope handling right after they are computed (`ssh_collect.py:88-89`) and before the per-path conflict loop:

```python
def plan_collect(
    worktree: Path,
    staging: Path,
    baseline: dict[str, str],
    *,
    forbidden: list[str],
    scope: list[str] | None = None,
) -> CollectPlan:
    """Preflight; raises CollectConflict on any violation. No side effects.

    When `scope` is a non-empty list (Mode-1 remote), any change outside the
    scope is a CollectConflict and the returned plan is bounded to in-scope
    paths. `scope=None` keeps the whole-worktree behavior (Mode-2).
    """
    # ... existing symlink/traversal guard unchanged ...
    modified = sorted(r for r, sha in remote.items() if baseline.get(r) != sha)
    deleted = sorted(r for r in baseline if r not in remote)

    if scope:
        for rel in [*modified, *deleted]:
            if not path_in_scope(rel, scope):
                raise CollectConflict(f"out-of-scope change rejected: {rel}")

    # ... existing forbidden/divergence loop unchanged ...
    # ... existing structural-conflict loop unchanged ...
    return CollectPlan(modified=modified, deleted=deleted)
```

(With `scope` non-empty, only in-scope paths survive the reject, so `modified`/`deleted` are already scope-bounded — no extra filtering needed.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ssh_collect_scope.py tests/test_ssh_collect*.py -v`
Expected: PASS (new tests + existing ssh_collect tests still green — `scope=None` default preserves behavior).

- [ ] **Step 5: Type-check, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check . --fix
git add maestro/execution/ssh_collect.py tests/test_ssh_collect_scope.py
git commit -m "feat(ssh-collect): scope reject + scope-bounded plan over full baseline"
```

---

## Task 7: Thread scope into the handle + LaunchNotStarted contract

**Files:**
- Modify: `maestro/execution/ssh_handle.py` (`CollectSpec`, `collect()`)
- Modify: `maestro/execution/ssh_backend.py` (`run()`, new `LaunchNotStarted`)
- Test: `tests/test_ssh_backend_launch.py` (create)

**Interfaces:**
- Consumes: `CollectSpec` (ssh_handle), `plan_collect(scope=...)` (Task 6).
- Produces:
  - `CollectSpec.scope: list[str] | None = None`.
  - `LaunchNotStarted(Exception)` in `ssh_backend.py` — raised only when the run provably did not launch remotely (materialize failure / pre-dispatch failure). Any post-dispatch failure raises the existing `RuntimeError` (uncertain).
  - `SshBackend.run` sets `CollectSpec(scope=req.collect.include or None)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ssh_backend_launch.py
from maestro.execution.ssh_backend import LaunchNotStarted
from maestro.execution.ssh_handle import CollectSpec


def test_collectspec_has_scope_field():
    spec = CollectSpec(
        worktree=None, staging_dir=None, journal_dir=None, baseline={},
        scope=["src/**"],
    )
    assert spec.scope == ["src/**"]


def test_launch_not_started_is_exception():
    assert issubclass(LaunchNotStarted, Exception)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ssh_backend_launch.py -v`
Expected: FAIL — `ImportError: cannot import name 'LaunchNotStarted'` / `CollectSpec` has no `scope`.

- [ ] **Step 3: Implement**

In `maestro/execution/ssh_handle.py`, add the field to `CollectSpec` (dataclass at `:31`):

```python
@dataclass
class CollectSpec:
    worktree: Path
    staging_dir: Path
    journal_dir: Path
    baseline: dict[str, str]
    scope: list[str] | None = None
```

In `collect()` (`ssh_handle.py:224`), pass the scope through:

```python
        plan = plan_collect(
            self._collect.worktree,
            staging,
            self._collect.baseline,
            forbidden=[".git", ".maestro"],
            scope=self._collect.scope,
        )
```

In `maestro/execution/ssh_backend.py`, define the exception near the top:

```python
class LaunchNotStarted(Exception):
    """The remote run provably never launched — safe to release/rollback."""
```

Wrap the pre-launch stages in `run()` so a failure *before* the supervisor is dispatched is classified as not-started. Materialize is pre-launch:

```python
        try:
            await self._materialize_remote(req, layout)
        except Exception as exc:  # nothing launched remotely yet
            raise LaunchNotStarted(str(exc)) from exc
```

Leave `_launch_supervisor` + the handshake check raising the existing
`RuntimeError` — a missing handshake after dispatch is **uncertain**, not
not-started. Set the scope on the collect spec (`ssh_backend.py:132`):

```python
            collect_spec=CollectSpec(
                req.workdir, staging, journal, baseline,
                scope=req.collect.include or None,
            ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ssh_backend_launch.py tests/test_ssh_backend*.py tests/test_ssh_handle*.py -v`
Expected: PASS (new + existing SSH backend/handle tests green).

- [ ] **Step 5: Type-check, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check . --fix
git add maestro/execution/ssh_handle.py maestro/execution/ssh_backend.py tests/test_ssh_backend_launch.py
git commit -m "feat(ssh): thread collect scope into handle; LaunchNotStarted contract"
```

---

## Task 8: Scheduler arming + start-time fail-fast

**Files:**
- Modify: `maestro/scheduler.py` (`__init__` around `:280`, `run()` around `:697`)
- Test: `tests/test_scheduler_reservations.py` (create)

**Interfaces:**
- Consumes: `compute_armed_workdirs`, `validate_ssh_scopes`, `ReservationRegistry`, `UnboundedRemoteScopeError` (Tasks 3-4); `db.get_all_tasks()`.
- Produces on `Scheduler`:
  - `self._execution: ExecutionConfig` (kept from the ctor arg).
  - `self._armed: set[Path]` (empty until `run()` computes it).
  - `self._reservations: ReservationRegistry`.
  - `_arm_workdirs()` — async; loads all tasks, sets `self._armed`, calls `validate_ssh_scopes` (raises `UnboundedRemoteScopeError` → surfaced as `SchedulerError`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler_reservations.py
import pytest

from maestro.dag import DAG
from maestro.database import Database
from maestro.execution.exec_config import (
    BackendSpec,
    ExecutionConfig,
    SshTransport,
)
from maestro.models import AgentType, Task, TaskStatus
from maestro.scheduler import Scheduler, SchedulerConfig, SchedulerError

pytestmark = pytest.mark.anyio


async def _db(tmp_path):
    db = Database(tmp_path / "m.db")
    await db.connect()
    await db.initialize_schema()
    return db


def _ssh_exec():
    return ExecutionConfig(
        default_backend="local",
        backends={"remote": BackendSpec(transport=SshTransport(host="h"))},
    )


def _sched(db, tmp_path, execution):
    # Empty DAG + no spawners: the reservation helpers under test never touch
    # dag/spawners. If direct construction breaks, mirror the DAG/mock_spawner
    # fixtures in tests/test_scheduler.py:287-398.
    return Scheduler(
        db,
        DAG([]),
        spawners={},
        config=SchedulerConfig(workdir=tmp_path),
        execution=execution,
    )


async def _add(db, tid, workdir, backend, scope):
    await db.create_task(
        Task(
            id=tid, title=tid, prompt="do x", agent_type=AgentType.CLAUDE_CODE,
            workdir=workdir, status=TaskStatus.PENDING, backend=backend,
            scope=scope,
        )
    )


async def test_arm_workdirs_marks_ssh_workdir(tmp_path):
    db = await _db(tmp_path)
    wd = str(tmp_path / "wd")
    await _add(db, "t1", wd, "remote", ["src/**"])
    sched = _sched(db, tmp_path, _ssh_exec())
    await sched._arm_workdirs()
    from maestro.execution.reservations import canonical_workdir

    assert canonical_workdir(wd) in sched._armed
    await db.close()


async def test_arm_workdirs_fail_fast_on_unbounded_scope(tmp_path):
    db = await _db(tmp_path)
    await _add(db, "t1", str(tmp_path / "wd"), "remote", [])
    sched = _sched(db, tmp_path, _ssh_exec())
    with pytest.raises(SchedulerError):
        await sched._arm_workdirs()
    await db.close()
```

(`DAG([])` construction and `create_task`/`initialize_schema` names: confirm against `maestro/dag.py` / `maestro/database.py` before running — if `DAG([])` rejects an empty list, build it from the `TaskConfig`s instead, as `tests/test_scheduler.py:392` does.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scheduler_reservations.py -k arm -v`
Expected: FAIL — `AttributeError: 'Scheduler' object has no attribute '_arm_workdirs'`.

- [ ] **Step 3: Implement**

In `Scheduler.__init__` (near `self._backends = BackendResolver(...)`, `:280`), keep the config and add state:

```python
        from maestro.execution.exec_config import ExecutionConfig
        from maestro.execution.reservations import ReservationRegistry

        self._execution = execution or ExecutionConfig()
        self._backends = BackendResolver(self._execution, mode="scheduler")
        self._armed: set[Path] = set()
        self._reservations = ReservationRegistry()
```

Add the method:

```python
    async def _arm_workdirs(self) -> None:
        """Static per-workdir arming + start-time unbounded-scope fail-fast."""
        from maestro.execution.reservations import (
            UnboundedRemoteScopeError,
            compute_armed_workdirs,
            validate_ssh_scopes,
        )

        tasks = await self._db.get_all_tasks()
        try:
            validate_ssh_scopes(tasks, self._execution)
        except UnboundedRemoteScopeError as exc:
            raise SchedulerError(str(exc)) from exc
        self._armed = compute_armed_workdirs(tasks, self._execution)
```

Call it in `run()` right after the DB-connected check, before `_main_loop()`:

```python
        await self._arm_workdirs()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_scheduler_reservations.py -k arm -v`
Expected: PASS.

- [ ] **Step 5: Type-check, format, commit**

```bash
uv run pyrefly check && uv run ruff format . && uv run ruff check . --fix
git add maestro/scheduler.py tests/test_scheduler_reservations.py
git commit -m "feat(scheduler): static workdir arming + start-time unbounded-scope fail-fast"
```

---

## Task 9: Acquire / hold / release + launch-stage rollback

**Files:**
- Modify: `maestro/scheduler.py` — dispatch path (`:1128-1171`), collect-policy rewrite (after `build_request`, `:1058`), finalization release (`:1278-1290`)
- Test: `tests/test_scheduler_reservations.py`

**Interfaces:**
- Consumes: `self._reservations`, `self._armed`, `scope_to_reservation`, `is_ssh_task`, `canonical_workdir`; `db.mark_execution_state`.
- Produces:
  - `_is_armed(task) -> bool` — `canonical_workdir(task.workdir) in self._armed`.
  - `_try_reserve(task) -> bool` — for an armed task, `scope_to_reservation(task.workdir, task.scope)` → `try_acquire(task.id, r)`; non-armed ⇒ always `True` (no-op).
  - Collect-policy rewrite: armed SSH task gets `CollectPolicy(mode="scope_paths", include=task.scope)`.
  - Release points: SSH task on `mark_execution_state("collected")`; local armed task on terminal transition.

- [ ] **Step 1: Write the failing test (contention + rollback)**

```python
# append to tests/test_scheduler_reservations.py
from maestro.execution.reservations import scope_to_reservation


async def test_overlapping_reservation_blocks_second(tmp_path):
    db = await _db(tmp_path)
    wd = str(tmp_path / "wd")
    await _add(db, "t1", wd, "remote", ["src/**"])
    await _add(db, "t2", wd, "remote", ["src/api/x.py"])
    sched = _sched(db, tmp_path, _ssh_exec())
    await sched._arm_workdirs()
    t1 = await db.get_task("t1")
    t2 = await db.get_task("t2")
    assert sched._try_reserve(t1) is True
    assert sched._try_reserve(t2) is False    # overlap → blocked
    sched._reservations.release("t1")
    assert sched._try_reserve(t2) is True      # freed
    await db.close()


async def test_non_armed_task_reserve_is_noop(tmp_path):
    db = await _db(tmp_path)
    wd = str(tmp_path / "plain")
    await _add(db, "t1", wd, "local", [])
    sched = _sched(db, tmp_path, _ssh_exec())
    await sched._arm_workdirs()
    t1 = await db.get_task("t1")
    assert sched._try_reserve(t1) is True
    assert sched._reservations.holds("t1") is False   # nothing recorded
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scheduler_reservations.py -k "reservation or noop" -v`
Expected: FAIL — `AttributeError: 'Scheduler' object has no attribute '_try_reserve'`.

- [ ] **Step 3: Implement the helpers + wiring**

Add helpers to `Scheduler`:

```python
    def _is_armed(self, task: Task) -> bool:
        from maestro.execution.reservations import canonical_workdir

        return canonical_workdir(task.workdir) in self._armed

    def _try_reserve(self, task: Task) -> bool:
        """Acquire the (workdir, scope) reservation; no-op off armed workdirs."""
        if not self._is_armed(task):
            return True
        from maestro.execution.reservations import scope_to_reservation

        r = scope_to_reservation(task.workdir, task.scope)
        return self._reservations.try_acquire(task.id, r)
```

**Acquire before dispatch.** In the dispatch method, immediately before the
`if backend.id != "local":` / `start_execution` block (`:1128`), gate on the
reservation. If it cannot be acquired, abandon this dispatch attempt without
consuming a slot (return the same "not dispatched" value the slot-full path
uses — inspect the method's contract at `:1000`):

```python
            if not self._try_reserve(task):
                # Overlapping active reservation on an armed workdir — retry a
                # later tick; consume no slot, make no state transition.
                return False
```

**Launch-stage rollback.** Wrap the `start_execution` + `backend.run()` region
(`:1138-1159`) so a *proven-not-started* failure releases the reservation, an
*uncertain* failure holds it:

```python
            try:
                # ... existing start_execution + get_task + committed transition
                #     (backend.id != "local") OR local _transition ...
                handle = await backend.run(request)
            except ConcurrentModificationError:
                self._reservations.release(task.id)   # CAS lost: nothing launched
                raise
            except LaunchNotStarted:
                self._reservations.release(task.id)   # proven not started
                raise
            except Exception:
                # Uncertain (SSH may have launched, handshake lost): HOLD the
                # reservation; the open handle drives release via recovery/collect.
                raise
```

Import `LaunchNotStarted` from `maestro.execution.ssh_backend` and
`ConcurrentModificationError` from `maestro.database` at module top.

**Collect-policy rewrite.** Right after `request = spawner.build_request(...)`
(`:1058-1066`), for an armed SSH task replace the `none` collect policy:

```python
            if self._is_armed(task) and is_ssh_task(task, self._execution):
                from maestro.execution.models import CollectPolicy

                request = request.model_copy(
                    update={
                        "collect": CollectPolicy(
                            mode="scope_paths", include=list(task.scope)
                        )
                    }
                )
```

(Import `is_ssh_task` at module top.)

**Release on collect / terminal.** In the finalization path where the handle
transitions to `collected`/`cleaned` (`:1281-1290`), release after the durable
`collected` write:

```python
                    await self._db.mark_execution_state(
                        exec_id, "collected", allowed_from=["terminal"]
                    )
                    self._reservations.release(running_task.task.id)
```

For a **local** armed task (no execution handle, no `collected` transition),
release at the single reap chokepoint where a finished task is removed from
`self._running_tasks` (grep `del self._running_tasks` / `_running_tasks.pop` in
the completion handler). Add `self._reservations.release(task_id)` there — it
covers both local and SSH owners, and releasing an owner that never reserved is
a safe no-op, so this chokepoint is the leak-proof place even though SSH also
releases earlier on `collected`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_scheduler_reservations.py -v`
Expected: PASS (arm + contention + noop).

- [ ] **Step 5: Regression + type-check + commit**

```bash
uv run pytest tests/test_scheduler.py -v
uv run pyrefly check && uv run ruff format . && uv run ruff check . --fix
git add maestro/scheduler.py tests/test_scheduler_reservations.py
git commit -m "feat(scheduler): reservation acquire/hold/release + launch-stage rollback"
```

---

## Task 10: Recovery reconstruction

**Files:**
- Modify: `maestro/scheduler.py` (call from `run()`/recovery, after `_arm_workdirs`)
- Test: `tests/test_scheduler_reservations.py`

**Interfaces:**
- Consumes: `db.get_open_execution_handles()` (non-cleaned non-local handle rows: `execution_id`, `state`, `entity_id`, `backend_id`, …); `self._reservations.reconstruct`; `scope_to_reservation`.
- Produces:
  - `_reconstruct_reservations() -> None` — async; for each open handle whose `state` is `prepared`/`running`/`terminal`, look up its task, and if it is an armed SSH task, `reconstruct(task.id, scope_to_reservation(task.workdir, task.scope))`. Skip `collected` (scope already unblocked; still a cleanup candidate handled by existing recovery).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_scheduler_reservations.py


async def test_recovery_reconstructs_held_reservation(tmp_path):
    db = await _db(tmp_path)
    wd = str(tmp_path / "wd")
    await _add(db, "t1", wd, "remote", ["src/**"])
    # Simulate a crash mid-run: task RUNNING + an open handle in state 'running'.
    await db.start_execution(
        entity_kind="task", entity_id="t1",
        expected_status=TaskStatus.PENDING.value,   # match the row we inserted
        running_status=TaskStatus.RUNNING.value,
        execution_id="e1", backend_id="remote",
        transport_ref="remote:maestro-e1", attempt=1,
    )
    await db.mark_execution_state("e1", "running", allowed_from=["prepared"])

    sched = _sched(db, tmp_path, _ssh_exec())
    await sched._arm_workdirs()
    await sched._reconstruct_reservations()

    # A fresh overlapping task cannot reserve — the recovered reservation holds.
    await _add(db, "t2", wd, "remote", ["src/api/x.py"])
    t2 = await db.get_task("t2")
    assert sched._try_reserve(t2) is False
    await db.close()


async def test_recovery_skips_collected_handle(tmp_path):
    db = await _db(tmp_path)
    wd = str(tmp_path / "wd")
    await _add(db, "t1", wd, "remote", ["src/**"])
    await db.start_execution(
        entity_kind="task", entity_id="t1",
        expected_status=TaskStatus.PENDING.value,
        running_status=TaskStatus.RUNNING.value,
        execution_id="e1", backend_id="remote",
        transport_ref="remote:maestro-e1", attempt=1,
    )
    await db.mark_execution_state("e1", "running", allowed_from=["prepared"])
    await db.mark_execution_state("e1", "terminal", allowed_from=["running"])
    await db.mark_execution_state("e1", "collected", allowed_from=["terminal"])

    sched = _sched(db, tmp_path, _ssh_exec())
    await sched._arm_workdirs()
    await sched._reconstruct_reservations()

    assert sched._reservations.holds("t1") is False   # collected → scope free
    await db.close()
```

(Confirm `mark_execution_state`'s `allowed_from` values and `get_open_execution_handles`'s row keys against `maestro/database.py:1431` / `:1535`; adapt the field names if they differ.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scheduler_reservations.py -k recovery -v`
Expected: FAIL — `AttributeError: ... '_reconstruct_reservations'`.

- [ ] **Step 3: Implement**

```python
    async def _reconstruct_reservations(self) -> None:
        """Rebuild held reservations from durable open SSH handles (§6).

        prepared/running/terminal -> reconstruct + hold; collected/cleaned are
        not scope-blocking (collected stays a cleanup candidate handled by the
        existing recovery path).
        """
        from maestro.execution.reservations import (
            is_ssh_task,
            scope_to_reservation,
        )

        HOLD_STATES = {"prepared", "running", "terminal"}
        for row in await self._db.get_open_execution_handles():
            if row.get("state") not in HOLD_STATES:
                continue
            task = await self._db.get_task(row["entity_id"])
            if task is None or not is_ssh_task(task, self._execution):
                continue
            if self._is_armed(task):
                self._reservations.reconstruct(
                    task.id, scope_to_reservation(task.workdir, task.scope)
                )
```

Call it in `run()` right after `await self._arm_workdirs()`:

```python
        await self._reconstruct_reservations()
```

(`get_open_execution_handles` already filters `backend_id != 'local'`; the
`is_ssh_task` guard additionally excludes any non-ssh non-local backend such as
docker, which needs no reservation.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_scheduler_reservations.py -v`
Expected: PASS.

- [ ] **Step 5: Full regression + type-check + commit**

```bash
uv run pytest -q
uv run pyrefly check && uv run ruff format . && uv run ruff check . --fix
git add maestro/scheduler.py tests/test_scheduler_reservations.py
git commit -m "feat(scheduler): reconstruct held reservations from open SSH handles on recovery"
```

---

## Task 11: End-to-end localhost-SSH Mode-1 (opt-in)

**Files:**
- Test: `tests/test_mode1_ssh_e2e.py` (create; opt-in, `skipif` on an env flag mirroring the Phase 2a e2e test)

**Interfaces:**
- Consumes: the full stack (arming → reserve → SSH run → scope-collect → release).

- [ ] **Step 1: Locate the Phase 2a e2e pattern to mirror**

Run: `grep -rln "localhost" tests/ | grep -i ssh` and read the skip/opt-in guard (env var name, ssh-to-localhost setup) used by the Phase 2a workstream e2e. Reuse the same guard and helpers.

- [ ] **Step 2: Write the e2e test**

Model it on the Phase 2a e2e: config with a `remote` ssh backend pointing at `localhost`, one Mode-1 task with `backend: remote` and `scope: ["out/**"]`, whose agent writes a file under `out/`. Assert: task reaches `DONE`, the in-scope file was collected into the shared workdir, and an out-of-scope write (if the fake agent makes one) routes to `NEEDS_REVIEW`.

```python
# tests/test_mode1_ssh_e2e.py — skeleton; fill helpers from the 2a e2e
import os
import pytest

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("MAESTRO_SSH_E2E") != "1",
        reason="opt-in localhost-SSH e2e (set MAESTRO_SSH_E2E=1)",
    ),
]


async def test_mode1_ssh_task_collects_in_scope(tmp_path):
    # 1. shared workdir with a trivial repo + an 'announce'-style agent that
    #    writes out/result.txt
    # 2. ExecutionConfig with a 'remote' SshTransport(host='localhost')
    # 3. run Scheduler over one task {backend: remote, scope: ['out/**']}
    # 4. assert task DONE and (workdir/'out'/'result.txt').exists()
    ...
```

- [ ] **Step 3: Run (opt-in)**

Run: `MAESTRO_SSH_E2E=1 uv run pytest tests/test_mode1_ssh_e2e.py -v`
Expected: PASS where localhost SSH is available; SKIPPED in CI without the flag.

- [ ] **Step 4: Confirm default suite skips it**

Run: `uv run pytest tests/test_mode1_ssh_e2e.py -v`
Expected: SKIPPED (1 skipped).

- [ ] **Step 5: Commit**

```bash
git add tests/test_mode1_ssh_e2e.py
git commit -m "test(mode1-ssh): opt-in localhost e2e — arm, reserve, run, scope-collect"
```

---

## Task 12: Docs — CLAUDE.md communication note + known limitations

**Files:**
- Modify: `CLAUDE.md` (the "Communication" / distributed-execution bullet under Key Design Decisions)

**Interfaces:** none (docs only).

- [ ] **Step 1: Update the distributed-execution note**

In `CLAUDE.md`, extend the Phase 2a communication bullet with a Phase 2b sentence:

> Phase 2b enables **Mode-1 remote** (`maestro run` on an ssh backend): a static
> per-workdir arming gate + a `(workdir, scope)` reservation lock (conservative
> path-anchor overlap) + scope-bounded collect. Validation still runs locally
> after collect (`validation_backend` deferred); collect is `scope_paths` only
> (patch-collect deferred); local Docker Mode-1 is unchanged.

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: note Mode-1 remote (Phase 2b) in CLAUDE.md"
```

---

## Self-review notes (for the executor)

- **Spec coverage:** §1 arming → Task 3/8; §2 fail-fast → Task 3/8; §3
  reservations (anchor overlap, acquire/hold/release, launch-stage) → Tasks
  1-2-4/7/9; §4 scope collect → Task 6/7; §5 guard removal → Task 5; §6 recovery
  → Task 10. Behavior-compat (non-armed untouched) → Task 9
  `test_non_armed_task_reserve_is_noop`. Known limitations → Task 12.
- **Verify-before-code:** Tasks 8-10 depend on exact names in
  `maestro/database.py` (`get_all_tasks`, `create_task`, `initialize_schema`,
  `get_open_execution_handles` row keys, `mark_execution_state` `allowed_from`)
  and the `Scheduler` ctor/dispatch-return contract. Each of those tasks says to
  confirm and adapt — do that first, don't assume.
- **No new deps.** Everything uses stdlib (`fnmatch`, `pathlib`) + existing
  modules.
