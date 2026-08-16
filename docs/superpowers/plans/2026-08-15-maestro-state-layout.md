# Per-project, per-run state databases — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Maestro's orchestration state from one global `~/.maestro/maestro.db` into one database per run, keyed by canonical repository identity, so two projects can never mix and a fresh run never erases the previous run's evidence.

**Architecture:** A pure identity module parses a git remote into a `RepoKey`; a path module turns that key plus a run id into `~/.maestro/projects/<host>/<owner>/<repo>/runs/<run-id>/state.db`. A new `run` table records the run's own start and typed outcome. Liveness is read from the stage lock **plus** a holder file naming the run that holds it — the lock alone is keyed per repository and cannot attribute liveness to a run. A registry enumerates runs and classifies each; commands select from that list by their own policy rather than by "newest id".

**Tech Stack:** Python 3.12+, `aiosqlite`, `typer`, `pydantic`, `ulid`, `pytest` (asyncio_mode=auto), `ruff`, `pyrefly`.

**Spec:** `docs/superpowers/specs/2026-08-15-maestro-state-layout-design.md` (revision 3, merged in #185)

## Global Constraints

- Python **3.12+**; the test suite runs on 3.12 and 3.13 in CI.
- Every new directory under `~/.maestro/` is created **`0700`**; every file **`0600`** (spec §D).
- Identity is **never** derived from `project:` in a config, and **never** from a filesystem path used as a portable identity (spec §3.2, §3.4). A path may appear only as a local fingerprint inside `_local/` (§3.3).
- **Never assume ULID lexicographic order equals start order.** Verified: six ids minted in one millisecond by `ulid 1.1.0` do not sort into mint order (spec §C.1). Order by `started_at` from the `run` row, with `run_id` only as a tiebreaker.
- **`outcome IS NULL` does not mean interrupted.** It means no terminal record; classification needs the lock (spec §B.3).
- Migrations are **appended at the tail** of the `ordered` list in `Database._apply_migrations` (`maestro/database.py:515`), never reordered. Current highest version is **26**; this plan adds **27**.
- Run tests with `uv run --with pytest python -m pytest` — the repo's `.venv` currently has no `pytest` console script, so `uv run pytest` fails to spawn.
- Lint and types must stay clean: `uv run ruff format --check .`, `uv run ruff check .`, `uv run pyrefly check`.

## File Structure

| File | Responsibility |
|---|---|
| `maestro/repo_identity.py` *(new)* | Parse a remote URL or a checkout into a `RepoKey`. Pure; no I/O except one `git` call in `identity_from_checkout`. |
| `maestro/state_paths.py` *(new)* | `RepoKey` + run id → directories and file paths under `~/.maestro`; private-mode creation. |
| `maestro/run_state.py` *(new)* | `RunRow`, `RunStatus`, and `classify_run` — the liveness rule in one place. |
| `maestro/run_publish.py` *(new)* | Atomic creation of a run directory: temp → db + row → close → rename. |
| `maestro/run_registry.py` *(new)* | Enumerate runs for a key, classify them, and the selection policies. |
| `maestro/database.py` *(modify)* | Migration 27 (`run` table) and its accessors. |
| `maestro/service/locks.py` *(modify)* | Lock key becomes `(RepoKey, stage)`; holder file names the run. |
| `maestro/cli.py` *(modify)* | Startup order, `orchestrate` selection semantics, `--run`, legacy `--db`. |

---

### Task 1: `RepoKey` and remote-URL parsing

**Files:**
- Create: `maestro/repo_identity.py`
- Test: `tests/test_repo_identity.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `RepoKey(host: str, owner: str, repo: str, local: bool)`, `RepoKey.as_path_parts() -> tuple[str, ...]`, `parse_remote_url(url: str) -> RepoKey`, `IdentityError`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_repo_identity.py
import pytest

from maestro.repo_identity import IdentityError, RepoKey, parse_remote_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/andrei-shtanakov/kapelle", ("github.com", "andrei-shtanakov", "kapelle")),
        ("https://github.com/andrei-shtanakov/kapelle.git", ("github.com", "andrei-shtanakov", "kapelle")),
        ("git@github.com:andrei-shtanakov/kapelle.git", ("github.com", "andrei-shtanakov", "kapelle")),
        ("ssh://git@gitlab.com/acme/app.git", ("gitlab.com", "acme", "app")),
        ("https://git.company.example/acme/app", ("git.company.example", "acme", "app")),
    ],
)
def test_parse_remote_url_forms(url, expected):
    key = parse_remote_url(url)
    assert (key.host, key.owner, key.repo) == expected
    assert key.local is False


def test_github_is_case_folded():
    a = parse_remote_url("https://github.com/Andrei-Shtanakov/Kapelle")
    b = parse_remote_url("https://github.com/andrei-shtanakov/kapelle")
    assert a.as_path_parts() == b.as_path_parts()


def test_other_hosts_are_not_case_folded():
    a = parse_remote_url("https://git.company.example/Acme/App")
    b = parse_remote_url("https://git.company.example/acme/app")
    assert a.as_path_parts() != b.as_path_parts()


def test_two_hosts_same_owner_repo_are_distinct():
    gh = parse_remote_url("https://github.com/acme/app")
    gl = parse_remote_url("https://gitlab.com/acme/app")
    assert gh.as_path_parts() != gl.as_path_parts()


@pytest.mark.parametrize("bad", ["", "not-a-url", "https://github.com/only-owner", "file:///tmp/x"])
def test_unparseable_remote_refuses(bad):
    with pytest.raises(IdentityError):
        parse_remote_url(bad)


def test_path_parts_are_filesystem_safe():
    key = parse_remote_url("https://github.com/acme/app")
    assert key.as_path_parts() == ("github.com", "acme", "app")
    assert all("/" not in part and part not in ("", ".", "..") for part in key.as_path_parts())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest tests/test_repo_identity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.repo_identity'`

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/repo_identity.py
"""Canonical repository identity for state layout (spec §3).

A repository is named by its remote — host, owner, name — never by a
filesystem path and never by the operator-chosen `project:` field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class IdentityError(Exception):
    """Identity could not be established; the run must refuse to start."""


# Hosts whose owner/repo names are case-insensitive.
_CASE_INSENSITIVE_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org"})

_SCP_LIKE = re.compile(r"^(?:(?P<user>[^@]+)@)?(?P<host>[^:/]+):(?P<path>.+)$")
_URL_LIKE = re.compile(r"^(?P<scheme>https?|ssh|git)://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+)$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class RepoKey:
    host: str
    owner: str
    repo: str
    local: bool = False

    def as_path_parts(self) -> tuple[str, ...]:
        """Path segments under `projects/`. Local keys are two segments."""
        if self.local:
            return ("_local", self.repo)
        return (self.host, self.owner, self.repo)


def _fold(host: str, owner: str, repo: str) -> tuple[str, str, str]:
    host = host.lower()
    if host in _CASE_INSENSITIVE_HOSTS:
        return host, owner.lower(), repo.lower()
    return host, owner, repo


def parse_remote_url(url: str) -> RepoKey:
    """Parse a git remote into a `RepoKey`, or raise `IdentityError`."""
    text = (url or "").strip()
    if not text:
        raise IdentityError("empty remote URL")

    match = _URL_LIKE.match(text) or _SCP_LIKE.match(text)
    if match is None:
        raise IdentityError(f"cannot parse remote URL: {url!r}")

    host = match.group("host")
    path = match.group("path").strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]

    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise IdentityError(f"remote URL has no owner/repo: {url!r}")

    owner, repo = parts[-2], parts[-1]
    if _UNSAFE.search(owner) or _UNSAFE.search(repo) or repo in {".", ".."}:
        raise IdentityError(f"remote URL yields unsafe path segments: {url!r}")

    host, owner, repo = _fold(host, owner, repo)
    return RepoKey(host=host, owner=owner, repo=repo)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest tests/test_repo_identity.py -v`
Expected: PASS — 11 tests

- [ ] **Step 5: Commit**

```bash
git add maestro/repo_identity.py tests/test_repo_identity.py
git commit -m "feat(identity): parse a git remote into a canonical RepoKey"
```

---

### Task 2: Identity from a config and from a checkout, including `_local`

**Files:**
- Modify: `maestro/repo_identity.py`
- Test: `tests/test_repo_identity_sources.py`

**Interfaces:**
- Consumes: `RepoKey`, `parse_remote_url`, `IdentityError` from Task 1.
- Produces: `identity_from_remote_url(url)` (alias kept for callers), `identity_from_checkout(repo_path: Path) -> RepoKey`, `local_key(repo_path: Path) -> RepoKey`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_repo_identity_sources.py
import subprocess
from pathlib import Path

import pytest

from maestro.repo_identity import IdentityError, identity_from_checkout, local_key


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    return path


def test_checkout_with_origin_uses_the_remote(tmp_path):
    repo = _init_repo(tmp_path / "work")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/app.git")
    key = identity_from_checkout(repo)
    assert key.as_path_parts() == ("github.com", "acme", "app")
    assert key.local is False


def test_checkout_without_origin_falls_into_local(tmp_path):
    repo = _init_repo(tmp_path / "solo")
    key = identity_from_checkout(repo)
    assert key.local is True
    assert key.as_path_parts()[0] == "_local"
    assert key.as_path_parts()[1].startswith("solo-")


def test_two_local_repos_with_the_same_basename_do_not_collide(tmp_path):
    a = _init_repo(tmp_path / "a" / "project")
    b = _init_repo(tmp_path / "b" / "project")
    assert identity_from_checkout(a).as_path_parts() != identity_from_checkout(b).as_path_parts()


def test_local_key_is_stable_across_calls(tmp_path):
    repo = _init_repo(tmp_path / "solo")
    assert local_key(repo).as_path_parts() == local_key(repo).as_path_parts()


def test_non_git_directory_refuses(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(IdentityError):
        identity_from_checkout(plain)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest tests/test_repo_identity_sources.py -v`
Expected: FAIL — `ImportError: cannot import name 'identity_from_checkout'`

- [ ] **Step 3: Write minimal implementation**

Append to `maestro/repo_identity.py`:

```python
import hashlib
import subprocess
from pathlib import Path


def identity_from_remote_url(url: str) -> RepoKey:
    """Alias for `parse_remote_url`, for callers that read better this way."""
    return parse_remote_url(url)


def local_key(repo_path: Path) -> RepoKey:
    """Identity for a checkout with no remote — a local fingerprint (spec §3.3).

    The hash is over the canonical *git common dir*, so worktrees of one
    repository resolve together while two unrelated checkouts that happen to
    share a basename do not.
    """
    common = _git_output(repo_path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    digest = hashlib.sha256(str(Path(common).resolve()).encode()).hexdigest()[:12]
    name = _UNSAFE.sub("-", repo_path.resolve().name).strip("-") or "repo"
    return RepoKey(host="_local", owner="", repo=f"{name}-{digest}", local=True)


def identity_from_checkout(repo_path: Path) -> RepoKey:
    """Identity for Mode 1: the checkout's `origin`, else a local key."""
    try:
        url = _git_output(repo_path, "remote", "get-url", "origin")
    except IdentityError:
        # No `origin` is resolvable (spec §3.4) — not a refusal.
        return local_key(repo_path)
    return parse_remote_url(url)


def _git_output(repo_path: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise IdentityError(f"git {' '.join(args)} failed in {repo_path}") from exc
    return proc.stdout.strip()
```

Note: `identity_from_checkout` on a non-git directory raises `IdentityError` from `local_key`'s `rev-parse`, which is the refusal §3.4 wants.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest tests/test_repo_identity_sources.py -v`
Expected: PASS — 5 tests

- [ ] **Step 5: Commit**

```bash
git add maestro/repo_identity.py tests/test_repo_identity_sources.py
git commit -m "feat(identity): derive a RepoKey from a checkout, with a _local fingerprint"
```

---

### Task 3: State paths under `~/.maestro`, created private

**Files:**
- Create: `maestro/state_paths.py`
- Test: `tests/test_state_paths.py`

**Interfaces:**
- Consumes: `RepoKey` from Task 1.
- Produces: `maestro_home() -> Path`, `project_dir(key, *, home=None)`, `runs_dir(key, *, home=None)`, `run_dir(key, run_id, *, home=None)`, `state_db_path(key, run_id, *, home=None)`, `locks_dir(key, *, home=None)`, `ensure_private_dir(path) -> Path`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_state_paths.py
import stat

from maestro.repo_identity import RepoKey
from maestro.state_paths import (
    ensure_private_dir,
    locks_dir,
    project_dir,
    run_dir,
    runs_dir,
    state_db_path,
)

KEY = RepoKey(host="github.com", owner="acme", repo="app")
LOCAL = RepoKey(host="_local", owner="", repo="app-abc123", local=True)


def test_project_dir_includes_host(tmp_path):
    assert project_dir(KEY, home=tmp_path) == tmp_path / "projects" / "github.com" / "acme" / "app"


def test_local_key_uses_two_segments(tmp_path):
    assert project_dir(LOCAL, home=tmp_path) == tmp_path / "projects" / "_local" / "app-abc123"


def test_run_paths(tmp_path):
    rid = "01M0000000000000000000000"
    assert runs_dir(KEY, home=tmp_path) == project_dir(KEY, home=tmp_path) / "runs"
    assert run_dir(KEY, rid, home=tmp_path) == runs_dir(KEY, home=tmp_path) / rid
    assert state_db_path(KEY, rid, home=tmp_path) == run_dir(KEY, rid, home=tmp_path) / "state.db"
    assert locks_dir(KEY, home=tmp_path) == project_dir(KEY, home=tmp_path) / "locks"


def test_ensure_private_dir_is_0700(tmp_path):
    target = ensure_private_dir(tmp_path / "a" / "b")
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "a").stat().st_mode) == 0o700


def test_ensure_private_dir_is_idempotent(tmp_path):
    first = ensure_private_dir(tmp_path / "x")
    second = ensure_private_dir(tmp_path / "x")
    assert first == second
    assert stat.S_IMODE(second.stat().st_mode) == 0o700


def test_home_honours_env(tmp_path, monkeypatch):
    from maestro.state_paths import maestro_home

    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "custom"))
    assert maestro_home() == tmp_path / "custom"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest tests/test_state_paths.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.state_paths'`

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/state_paths.py
"""Filesystem layout for orchestration state (spec §3).

    ~/.maestro/projects/<host>/<owner>/<repo>/runs/<run-id>/{state.db,logs/}
                                             /locks/

Everything created here is private: directories 0700, files 0600. The state
carries prompts, absolute paths, costs and operator decisions.
"""

from __future__ import annotations

import os
from pathlib import Path

from maestro.repo_identity import RepoKey

DIR_MODE = 0o700
FILE_MODE = 0o600


def maestro_home() -> Path:
    """`~/.maestro`, or `$MAESTRO_HOME` when set (tests set it)."""
    override = os.environ.get("MAESTRO_HOME")
    return Path(override) if override else Path.home() / ".maestro"


def _home(home: Path | None) -> Path:
    return home if home is not None else maestro_home()


def project_dir(key: RepoKey, *, home: Path | None = None) -> Path:
    return _home(home).joinpath("projects", *key.as_path_parts())


def runs_dir(key: RepoKey, *, home: Path | None = None) -> Path:
    return project_dir(key, home=home) / "runs"


def run_dir(key: RepoKey, run_id: str, *, home: Path | None = None) -> Path:
    return runs_dir(key, home=home) / run_id


def state_db_path(key: RepoKey, run_id: str, *, home: Path | None = None) -> Path:
    return run_dir(key, run_id, home=home) / "state.db"


def locks_dir(key: RepoKey, *, home: Path | None = None) -> Path:
    return project_dir(key, home=home) / "locks"


def ensure_private_dir(path: Path) -> Path:
    """Create `path` and every missing parent with mode 0700."""
    missing = [p for p in [path, *path.parents] if not p.exists()]
    for parent in reversed(missing):
        parent.mkdir(mode=DIR_MODE, exist_ok=True)
        parent.chmod(DIR_MODE)
    return path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest tests/test_state_paths.py -v`
Expected: PASS — 6 tests

- [ ] **Step 5: Commit**

```bash
git add maestro/state_paths.py tests/test_state_paths.py
git commit -m "feat(state): private per-project, per-run path layout under ~/.maestro"
```

---

### Task 4: The `run` table (migration 27) and its accessors

**Files:**
- Modify: `maestro/database.py` (append to the `ordered` list at `maestro/database.py:515`; add methods to `Database`)
- Test: `tests/test_database_run_row.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Database.create_run_row(*, run_id, repo_key, started_at)`, `Database.get_run_row() -> dict | None`, `Database.set_run_outcome(*, outcome, ended_at, reason=None)`, `Database.set_run_suspended(*, suspended_at, suspend_reason)`.

**Why a new table:** the schema is entirely per-entity; nothing describes the run itself. Deriving a terminal state from workstream statuses was rejected in the spec (§B.1) — `maestro-run3.db` ends 2 done / 1 needs_review / 3 ready / 1 pending, which is indistinguishable between "stopped early" and "waiting on a human".

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database_run_row.py
import pytest

from maestro.database import create_database

STARTED = "2026-08-15T10:00:00+00:00"


async def _db(tmp_path):
    return await create_database(tmp_path / "state.db")


async def test_run_row_absent_on_a_fresh_database(tmp_path):
    db = await _db(tmp_path)
    assert await db.get_run_row() is None


async def test_create_and_read_back(tmp_path):
    db = await _db(tmp_path)
    await db.create_run_row(run_id="01ABC", repo_key="github.com/acme/app", started_at=STARTED)
    row = await db.get_run_row()
    assert row["run_id"] == "01ABC"
    assert row["repo_key"] == "github.com/acme/app"
    assert row["started_at"] == STARTED
    assert row["outcome"] is None
    assert row["ended_at"] is None
    assert row["suspended_at"] is None


@pytest.mark.parametrize("outcome", ["completed", "cancelled", "superseded", "failed"])
async def test_every_terminal_outcome_round_trips(tmp_path, outcome):
    db = await _db(tmp_path)
    await db.create_run_row(run_id="01ABC", repo_key="k", started_at=STARTED)
    await db.set_run_outcome(outcome=outcome, ended_at="2026-08-15T11:00:00+00:00", reason="r")
    row = await db.get_run_row()
    assert row["outcome"] == outcome
    assert row["ended_at"] == "2026-08-15T11:00:00+00:00"


async def test_needs_human_is_not_a_valid_outcome(tmp_path):
    db = await _db(tmp_path)
    await db.create_run_row(run_id="01ABC", repo_key="k", started_at=STARTED)
    with pytest.raises(Exception):
        await db.set_run_outcome(outcome="needs_human", ended_at="x", reason=None)


async def test_suspension_does_not_end_the_run(tmp_path):
    db = await _db(tmp_path)
    await db.create_run_row(run_id="01ABC", repo_key="k", started_at=STARTED)
    await db.set_run_suspended(suspended_at="2026-08-15T10:30:00+00:00", suspend_reason="QG-5")
    row = await db.get_run_row()
    assert row["suspended_at"] == "2026-08-15T10:30:00+00:00"
    assert row["outcome"] is None
    assert row["ended_at"] is None


async def test_only_one_run_row_per_database(tmp_path):
    db = await _db(tmp_path)
    await db.create_run_row(run_id="01ABC", repo_key="k", started_at=STARTED)
    with pytest.raises(Exception):
        await db.create_run_row(run_id="01XYZ", repo_key="k", started_at=STARTED)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest tests/test_database_run_row.py -v`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'get_run_row'`

- [ ] **Step 3: Write minimal implementation**

Add the migration method to `Database`, then append it to `ordered` in `_apply_migrations` as version **27** (tail only — never reorder):

```python
    async def _migrate_run_table(self) -> None:
        """Spec §B.1 — the run's own row: identity, start, typed outcome.

        `needs_human` is deliberately absent from the CHECK: a human pause
        ends a tick, not a logical run (maestro/service/decide.py:30), and the
        database outlives it.
        """
        assert self._connection is not None
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS run (
                run_id         TEXT PRIMARY KEY,
                repo_key       TEXT NOT NULL,
                started_at     TEXT NOT NULL,
                outcome        TEXT CHECK (outcome IN
                                 ('completed','cancelled','superseded','failed')),
                ended_at       TEXT,
                reason         TEXT,
                suspended_at   TEXT,
                suspend_reason TEXT,
                singleton      INTEGER NOT NULL DEFAULT 1 UNIQUE CHECK (singleton = 1)
            )
            """
        )
```

```python
            (27, "run_table", self._migrate_run_table),
```

Accessors on `Database`:

```python
    async def create_run_row(self, *, run_id: str, repo_key: str, started_at: str) -> None:
        """Write the run's own row. Exactly once per database."""
        assert self._connection is not None
        await self._connection.execute(
            "INSERT INTO run (run_id, repo_key, started_at) VALUES (?, ?, ?)",
            (run_id, repo_key, started_at),
        )
        await self._connection.commit()

    async def get_run_row(self) -> dict[str, object] | None:
        assert self._connection is not None
        cursor = await self._connection.execute("SELECT * FROM run LIMIT 1")
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def set_run_outcome(
        self, *, outcome: str, ended_at: str, reason: str | None = None
    ) -> None:
        assert self._connection is not None
        await self._connection.execute(
            "UPDATE run SET outcome = ?, ended_at = ?, reason = ?", (outcome, ended_at, reason)
        )
        await self._connection.commit()

    async def set_run_suspended(self, *, suspended_at: str, suspend_reason: str) -> None:
        """A human pause. Never sets `ended_at` (spec §B.1.1)."""
        assert self._connection is not None
        await self._connection.execute(
            "UPDATE run SET suspended_at = ?, suspend_reason = ?", (suspended_at, suspend_reason)
        )
        await self._connection.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest tests/test_database_run_row.py -v`
Expected: PASS — 9 tests

- [ ] **Step 5: Run the whole suite — migration 27 must not disturb existing databases**

Run: `uv run --with pytest python -m pytest -q`
Expected: PASS, same count as before this task plus 9

- [ ] **Step 6: Commit**

```bash
git add maestro/database.py tests/test_database_run_row.py
git commit -m "feat(db): migration 27 — the run's own row with a typed outcome"
```

---

### Task 5: `classify_run` — the liveness rule in one place

**Files:**
- Create: `maestro/run_state.py`
- Test: `tests/test_run_state.py`

**Interfaces:**
- Consumes: nothing (takes plain values).
- Produces: `RunRow` dataclass, `RunStatus` literal, `classify_run(row: RunRow | None, *, lock_holder_run_id: str | None) -> RunStatus`, `run_row_from_mapping(mapping) -> RunRow`.

**The rule the spec insists on (§B.3):** a run is *running* only when the stage lock is held **and** the holder is that run. `outcome IS NULL` alone means nothing — a live run and a killed run look identical.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_state.py
from maestro.run_state import RunRow, classify_run

STARTED = "2026-08-15T10:00:00+00:00"


def _row(**kw) -> RunRow:
    base = dict(
        run_id="A", repo_key="github.com/acme/app", started_at=STARTED,
        outcome=None, ended_at=None, reason=None, suspended_at=None, suspend_reason=None,
    )
    base.update(kw)
    return RunRow(**base)


def test_no_row_is_legacy():
    assert classify_run(None, lock_holder_run_id=None) == "legacy"


def test_terminal_outcome_wins_over_everything():
    row = _row(outcome="completed", ended_at="t")
    assert classify_run(row, lock_holder_run_id="A") == "completed"


def test_lock_held_by_this_run_is_running():
    assert classify_run(_row(), lock_holder_run_id="A") == "running"


def test_lock_held_by_another_run_does_not_make_this_one_running():
    # The case the lock alone gets wrong: A is dead, B holds the repo-level lock.
    assert classify_run(_row(run_id="A"), lock_holder_run_id="B") == "interrupted"


def test_free_lock_and_no_outcome_is_interrupted():
    assert classify_run(_row(), lock_holder_run_id=None) == "interrupted"


def test_suspended_without_a_live_lock_is_suspended_not_interrupted():
    row = _row(suspended_at="t", suspend_reason="QG-5")
    assert classify_run(row, lock_holder_run_id=None) == "suspended"


def test_a_suspended_run_that_is_running_again_reports_running():
    row = _row(suspended_at="t", suspend_reason="QG-5")
    assert classify_run(row, lock_holder_run_id="A") == "running"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest tests/test_run_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.run_state'`

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/run_state.py
"""The run row and the single place that decides what a run's state means.

Liveness is *observed*, never inferred from a NULL: a running run and a killed
run both have `ended_at IS NULL` (spec §B.3). The stage lock proves that an
orchestration stage is live **in this repository**; the holder's run id is what
attributes that liveness to a particular run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

RunStatus = Literal[
    "running", "interrupted", "suspended",
    "completed", "cancelled", "superseded", "failed",
    "legacy",
]


@dataclass(frozen=True)
class RunRow:
    run_id: str
    repo_key: str
    started_at: str
    outcome: str | None = None
    ended_at: str | None = None
    reason: str | None = None
    suspended_at: str | None = None
    suspend_reason: str | None = None


def run_row_from_mapping(mapping: Mapping[str, object]) -> RunRow:
    return RunRow(
        run_id=str(mapping["run_id"]),
        repo_key=str(mapping["repo_key"]),
        started_at=str(mapping["started_at"]),
        outcome=_opt(mapping.get("outcome")),
        ended_at=_opt(mapping.get("ended_at")),
        reason=_opt(mapping.get("reason")),
        suspended_at=_opt(mapping.get("suspended_at")),
        suspend_reason=_opt(mapping.get("suspend_reason")),
    )


def _opt(value: object) -> str | None:
    return None if value is None else str(value)


def classify_run(row: RunRow | None, *, lock_holder_run_id: str | None) -> RunStatus:
    """Spec §B.3. Order matters: terminal, then observed liveness, then pause."""
    if row is None:
        return "legacy"
    if row.outcome is not None:
        return row.outcome  # type: ignore[return-value]
    if lock_holder_run_id is not None and lock_holder_run_id == row.run_id:
        return "running"
    if row.suspended_at is not None:
        return "suspended"
    return "interrupted"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest tests/test_run_state.py -v`
Expected: PASS — 7 tests

- [ ] **Step 5: Commit**

```bash
git add maestro/run_state.py tests/test_run_state.py
git commit -m "feat(state): classify a run from its row plus the lock holder"
```

---

### Task 6: Lock keyed by `RepoKey`, with a holder file naming the run

**Files:**
- Modify: `maestro/service/locks.py`
- Test: `tests/test_locks_run_attribution.py`

**Interfaces:**
- Consumes: `RepoKey` (Task 1), `locks_dir` (Task 3).
- Produces: `stage_lock_path(key: RepoKey, stage: Stage, *, root=None) -> Path`, `ScopedLock(*, key: RepoKey, stage: Stage, run_id: str | None = None, root=None)`, `ScopedLock.holder_file -> Path`, `read_holder_run_id(key, stage, *, root=None) -> str | None`.

**Two things this task resolves.** `project_key(project, db_path)` (`maestro/service/locks.py:55`) hashes the *database path* into the lock identity — circular once the path is derived from the key (spec §A.4). And the existing `<stage>.pid` sidecar carries a bare pid; attribution needs the run id.

The pid file's format is **not** repurposed. Its docstring claims `maestro service status` reads it; a grep for `pid_file` outside `locks.py` finds no such reader, but a sibling `<stage>.holder` costs nothing and cannot break an unknown consumer.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_locks_run_attribution.py
import pytest

from maestro.repo_identity import RepoKey
from maestro.service.locks import AlreadyRunning, ScopedLock, read_holder_run_id

KEY = RepoKey(host="github.com", owner="acme", repo="app")
OTHER = RepoKey(host="github.com", owner="acme", repo="other")


def test_holder_is_readable_while_held(tmp_path):
    with ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A", root=tmp_path):
        assert read_holder_run_id(KEY, "orchestrate", root=tmp_path) == "RUN-A"


def test_holder_is_cleared_on_release(tmp_path):
    with ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A", root=tmp_path):
        pass
    assert read_holder_run_id(KEY, "orchestrate", root=tmp_path) is None


def test_same_repo_and_stage_is_exclusive(tmp_path):
    with ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A", root=tmp_path):
        with pytest.raises(AlreadyRunning):
            with ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-B", root=tmp_path):
                pass


def test_different_repos_do_not_serialise(tmp_path):
    with ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A", root=tmp_path):
        with ScopedLock(key=OTHER, stage="orchestrate", run_id="RUN-B", root=tmp_path):
            assert read_holder_run_id(OTHER, "orchestrate", root=tmp_path) == "RUN-B"


def test_lock_identity_does_not_depend_on_a_database_path(tmp_path):
    from maestro.service.locks import stage_lock_path

    assert stage_lock_path(KEY, "orchestrate", root=tmp_path) == stage_lock_path(
        KEY, "orchestrate", root=tmp_path
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest tests/test_locks_run_attribution.py -v`
Expected: FAIL — `ImportError: cannot import name 'read_holder_run_id'`

- [ ] **Step 3: Write minimal implementation**

Replace `project_key`/`stage_lock_path` and extend `ScopedLock`:

```python
def stage_lock_path(key: RepoKey, stage: Stage, *, root: Path | None = None) -> Path:
    """Lock identity is (repository, stage) — no database path (spec §A.4)."""
    base = root if root is not None else maestro_home()
    return base.joinpath("locks", *key.as_path_parts(), f"{stage}.lock")


def read_holder_run_id(key: RepoKey, stage: Stage, *, root: Path | None = None) -> str | None:
    """The run id of the current holder, or None when the file is absent.

    Never consulted on its own: a holder file outlives the process that wrote
    it, so it attributes liveness the lock has already proven, and grants none.
    """
    path = stage_lock_path(key, stage, root=root).with_suffix(".holder")
    try:
        return json.loads(path.read_text())["run_id"]
    except (OSError, ValueError, KeyError):
        return None
```

In `ScopedLock.__init__`, take `key: RepoKey`, `run_id: str | None`; drop `project` and `db_path`. In `__enter__`, after `self.pid_file.write_text(...)`:

```python
        if self._run_id is not None:
            self.holder_file.write_text(json.dumps({"pid": os.getpid(), "run_id": self._run_id}))
            self.holder_file.chmod(0o600)
```

In `__exit__`, before releasing handles:

```python
        self.holder_file.unlink(missing_ok=True)
```

And the property:

```python
    @property
    def holder_file(self) -> Path:
        """Attribution for `read_holder_run_id`; meaningless without the lock."""
        return self._stage_path.with_suffix(".holder")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest tests/test_locks_run_attribution.py -v`
Expected: PASS — 5 tests

- [ ] **Step 5: Update the module docstring, which still describes the old identity**

`maestro/service/locks.py:17` says the scoped lock's identity is `(project-key, stage)`. Change it to `(repository identity, stage)` and add: *the `<stage>.holder` file names the run holding the lock and is load-bearing for attribution; `<stage>.pid` remains diagnostics only.*

- [ ] **Step 6: Run the whole suite and fix existing lock callers**

Run: `uv run --with pytest python -m pytest -q`
Expected: existing tests constructing `ScopedLock(project=..., db_path=...)` fail; update each call site to `ScopedLock(key=..., run_id=...)`. Re-run until green.

- [ ] **Step 7: Commit**

```bash
git add maestro/service/locks.py tests/test_locks_run_attribution.py
git commit -m "feat(locks): key on repository identity; name the holding run"
```

---

### Task 7: Atomic run creation

**Files:**
- Create: `maestro/run_publish.py`
- Test: `tests/test_run_publish.py`

**Interfaces:**
- Consumes: `RepoKey` (1), `state_paths` (3), `Database.create_run_row` (4).
- Produces: `async create_run(key, run_id, *, repo_key_text, started_at, home=None) -> Path` returning the final `state.db` path.

**Ordering is load-bearing (spec §D):** create under a temp name → write the `run` row → **close the database** → rename → reopen. Renaming a directory under an open SQLite connection strands the WAL and shm files against a path that no longer exists, and tears the database rather than raising.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_publish.py
import stat

from maestro.database import create_database
from maestro.repo_identity import RepoKey
from maestro.run_publish import create_run
from maestro.state_paths import runs_dir

KEY = RepoKey(host="github.com", owner="acme", repo="app")
STARTED = "2026-08-15T10:00:00+00:00"


async def test_creates_a_run_directory_with_a_row(tmp_path):
    path = await create_run(KEY, "RUN-A", repo_key_text="github.com/acme/app",
                            started_at=STARTED, home=tmp_path)
    assert path == runs_dir(KEY, home=tmp_path) / "RUN-A" / "state.db"
    db = await create_database(path)
    row = await db.get_run_row()
    assert row["run_id"] == "RUN-A"


async def test_no_wal_or_shm_survives_publication(tmp_path):
    path = await create_run(KEY, "RUN-A", repo_key_text="k", started_at=STARTED, home=tmp_path)
    assert not path.with_name("state.db-wal").exists()
    assert not path.with_name("state.db-shm").exists()


async def test_nothing_is_visible_under_runs_until_complete(tmp_path):
    await create_run(KEY, "RUN-A", repo_key_text="k", started_at=STARTED, home=tmp_path)
    entries = list(runs_dir(KEY, home=tmp_path).iterdir())
    assert [e.name for e in entries] == ["RUN-A"]
    # every published directory has its row
    assert (entries[0] / "state.db").exists()


async def test_permissions_are_private(tmp_path):
    path = await create_run(KEY, "RUN-A", repo_key_text="k", started_at=STARTED, home=tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


async def test_logs_directory_is_created(tmp_path):
    path = await create_run(KEY, "RUN-A", repo_key_text="k", started_at=STARTED, home=tmp_path)
    assert (path.parent / "logs").is_dir()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest tests/test_run_publish.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.run_publish'`

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/run_publish.py
"""Publish a run directory atomically (spec §D)."""

from __future__ import annotations

from pathlib import Path

from maestro.database import create_database
from maestro.repo_identity import RepoKey
from maestro.state_paths import FILE_MODE, ensure_private_dir, run_dir, runs_dir


async def create_run(
    key: RepoKey,
    run_id: str,
    *,
    repo_key_text: str,
    started_at: str,
    home: Path | None = None,
) -> Path:
    """Create `runs/<run_id>/` and return its `state.db`.

    The directory is built under a temporary name and renamed only after the
    database is closed: a collector must never see a database without its run
    row, and a directory renamed under an open SQLite handle strands its WAL.
    """
    final_dir = run_dir(key, run_id, home=home)
    if final_dir.exists():
        raise FileExistsError(f"run already exists: {final_dir}")

    ensure_private_dir(runs_dir(key, home=home))
    staging = ensure_private_dir(final_dir.with_name(f".{run_id}.partial"))
    ensure_private_dir(staging / "logs")

    db_path = staging / "state.db"
    db = await create_database(db_path)
    await db.create_run_row(run_id=run_id, repo_key=repo_key_text, started_at=started_at)
    await db.close()                      # WAL/shm checkpointed and removed
    db_path.chmod(FILE_MODE)

    staging.rename(final_dir)             # only now is the run discoverable
    return final_dir / "state.db"
```

`Database.close()` is at `maestro/database.py:486` and is what the existing tests use; it is the checkpoint-and-release the rename depends on.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest tests/test_run_publish.py -v`
Expected: PASS — 5 tests

- [ ] **Step 5: Commit**

```bash
git add maestro/run_publish.py tests/test_run_publish.py
git commit -m "feat(state): publish a run directory only after its row is durable"
```

---

### Task 8: The run registry and selection policies

**Files:**
- Create: `maestro/run_registry.py`
- Test: `tests/test_run_registry.py`

**Interfaces:**
- Consumes: `RepoKey` (1), `state_paths` (3), `Database.get_run_row` (4), `run_row_from_mapping`/`classify_run` (5), `read_holder_run_id` (6).
- Produces: `RunInfo(run_id, row, status, started_at, db_path)`, `async resolve_runs(key, *, stage="orchestrate", home=None, lock_root=None) -> list[RunInfo]`, `select_resumable(runs) -> RunInfo`, `live_run(runs) -> RunInfo | None`, `AmbiguousRun`, `NoResumableRun`.

**Ordering:** newest first by `started_at`, `run_id` only as a tiebreaker. Never by ULID order alone — see Global Constraints.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_registry.py
import pytest

from maestro.database import create_database
from maestro.repo_identity import RepoKey
from maestro.run_publish import create_run
from maestro.run_registry import (
    AmbiguousRun,
    NoResumableRun,
    live_run,
    resolve_runs,
    select_resumable,
)

KEY = RepoKey(host="github.com", owner="acme", repo="app")


async def _make(tmp_path, run_id, started_at, *, outcome=None):
    path = await create_run(KEY, run_id, repo_key_text="k", started_at=started_at, home=tmp_path)
    if outcome is not None:
        db = await create_database(path)
        await db.set_run_outcome(outcome=outcome, ended_at="2026-08-15T12:00:00+00:00")
        await db.close()
    return path


async def test_newest_first_by_started_at_not_by_id(tmp_path):
    # "ZZZ" sorts after "AAA" lexicographically but started earlier.
    await _make(tmp_path, "ZZZ", "2026-08-15T09:00:00+00:00")
    await _make(tmp_path, "AAA", "2026-08-15T11:00:00+00:00")
    runs = await resolve_runs(KEY, home=tmp_path, lock_root=tmp_path)
    assert [r.run_id for r in runs] == ["AAA", "ZZZ"]


async def test_terminal_runs_are_classified(tmp_path):
    await _make(tmp_path, "AAA", "2026-08-15T09:00:00+00:00", outcome="completed")
    runs = await resolve_runs(KEY, home=tmp_path, lock_root=tmp_path)
    assert runs[0].status == "completed"


async def test_select_resumable_picks_the_single_non_terminal(tmp_path):
    await _make(tmp_path, "AAA", "2026-08-15T09:00:00+00:00", outcome="completed")
    await _make(tmp_path, "BBB", "2026-08-15T10:00:00+00:00")
    runs = await resolve_runs(KEY, home=tmp_path, lock_root=tmp_path)
    assert select_resumable(runs).run_id == "BBB"


async def test_two_non_terminal_runs_refuse_rather_than_choose(tmp_path):
    await _make(tmp_path, "AAA", "2026-08-15T09:00:00+00:00")
    await _make(tmp_path, "BBB", "2026-08-15T10:00:00+00:00")
    runs = await resolve_runs(KEY, home=tmp_path, lock_root=tmp_path)
    with pytest.raises(AmbiguousRun):
        select_resumable(runs)


async def test_no_runs_raises_rather_than_returning_none(tmp_path):
    runs = await resolve_runs(KEY, home=tmp_path, lock_root=tmp_path)
    assert runs == []
    with pytest.raises(NoResumableRun):
        select_resumable(runs)


async def test_live_run_is_none_when_no_lock_is_held(tmp_path):
    await _make(tmp_path, "AAA", "2026-08-15T09:00:00+00:00")
    runs = await resolve_runs(KEY, home=tmp_path, lock_root=tmp_path)
    assert live_run(runs) is None


async def test_an_interrupted_run_is_not_running_while_another_holds_the_lock(tmp_path):
    from maestro.service.locks import ScopedLock

    await _make(tmp_path, "AAA", "2026-08-15T09:00:00+00:00")   # dead
    await _make(tmp_path, "BBB", "2026-08-15T10:00:00+00:00")   # will hold the lock
    with ScopedLock(key=KEY, stage="orchestrate", run_id="BBB", root=tmp_path):
        runs = {r.run_id: r.status for r in await resolve_runs(KEY, home=tmp_path, lock_root=tmp_path)}
    assert runs["BBB"] == "running"
    assert runs["AAA"] == "interrupted"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest tests/test_run_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.run_registry'`

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/run_registry.py
"""Enumerate and classify the runs of one repository (spec §C)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from maestro.database import create_database
from maestro.repo_identity import RepoKey
from maestro.run_state import RunRow, RunStatus, classify_run, run_row_from_mapping
from maestro.service.locks import read_holder_run_id
from maestro.state_paths import runs_dir

_TERMINAL: frozenset[str] = frozenset({"completed", "cancelled", "superseded", "failed"})


class NoResumableRun(Exception):
    """There is nothing to resume."""


class AmbiguousRun(Exception):
    """More than one run could be resumed; the operator must choose."""


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    row: RunRow | None
    status: RunStatus
    started_at: str | None
    db_path: Path


async def resolve_runs(
    key: RepoKey,
    *,
    stage: str = "orchestrate",
    home: Path | None = None,
    lock_root: Path | None = None,
) -> list[RunInfo]:
    """Every run of `key`, newest first by `started_at` (id only breaks ties)."""
    base = runs_dir(key, home=home)
    if not base.is_dir():
        return []

    holder = read_holder_run_id(key, stage, root=lock_root)  # type: ignore[arg-type]
    infos: list[RunInfo] = []
    for entry in sorted(base.iterdir()):
        db_path = entry / "state.db"
        if not entry.is_dir() or not db_path.exists():
            continue
        db = await create_database(db_path)
        mapping = await db.get_run_row()
        await db.close()
        row = run_row_from_mapping(mapping) if mapping is not None else None
        infos.append(
            RunInfo(
                run_id=entry.name,
                row=row,
                status=classify_run(row, lock_holder_run_id=holder),
                started_at=row.started_at if row else None,
                db_path=db_path,
            )
        )

    infos.sort(key=lambda i: (i.started_at or "", i.run_id), reverse=True)
    return infos


def live_run(runs: list[RunInfo]) -> RunInfo | None:
    for info in runs:
        if info.status == "running":
            return info
    return None


def select_resumable(runs: list[RunInfo]) -> RunInfo:
    """The one resumable run, or a refusal. Never a silent pick (spec §C.2)."""
    candidates = [r for r in runs if r.status not in _TERMINAL and r.status != "legacy"]
    if not candidates:
        raise NoResumableRun("no non-terminal run to resume")
    if len(candidates) > 1:
        ids = ", ".join(r.run_id for r in candidates)
        raise AmbiguousRun(f"several runs could be resumed: {ids}; pass --run <run-id>")
    return candidates[0]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest tests/test_run_registry.py -v`
Expected: PASS — 7 tests

- [ ] **Step 5: Commit**

```bash
git add maestro/run_registry.py tests/test_run_registry.py
git commit -m "feat(state): registry that classifies runs and refuses ambiguous resume"
```

---

### Task 9: Startup order — identity, run id, env, logging, database

**Files:**
- Modify: `maestro/cli.py` (the `orchestrate` command around `maestro/cli.py:1406-1440`, and `_service_run` at `maestro/cli.py:2433`)
- Create: `maestro/run_bootstrap.py`
- Test: `tests/test_run_bootstrap.py`

**Interfaces:**
- Consumes: everything from Tasks 1–8.
- Produces: `async bootstrap_run(config, *, resume: bool, run_id_override: str | None, home=None) -> BootstrapResult` with fields `key: RepoKey`, `run_id: str`, `db_path: Path`, `fresh: bool`.

**Why the order changes:** `_service_run` opens `Database(db_path)` as its second statement (`maestro/cli.py:2437`), before anything knows which run this is. The database path now depends on identity and on the fresh/resume decision, and `ORCHESTRA_PIPELINE_ID` must be exported **before** logging initialises — `maestro/_vendor/obs.py:144,164` reads it at setup and falls back to `ulid.new()` when it is absent, which is exactly the per-invocation id this design removes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_bootstrap.py
import os

import pytest

from maestro.repo_identity import RepoKey
from maestro.run_bootstrap import bootstrap_run
from maestro.run_publish import create_run


class _Config:
    def __init__(self, repo_url: str) -> None:
        self.repo_url = repo_url


KEY = RepoKey(host="github.com", owner="acme", repo="app")


async def test_fresh_mints_a_run_and_exports_the_pipeline_id(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCHESTRA_PIPELINE_ID", raising=False)
    result = await bootstrap_run(
        _Config("https://github.com/acme/app"), resume=False, run_id_override=None, home=tmp_path
    )
    assert result.fresh is True
    assert result.db_path.exists()
    assert os.environ["ORCHESTRA_PIPELINE_ID"] == result.run_id


async def test_resume_reuses_the_existing_run_id(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCHESTRA_PIPELINE_ID", raising=False)
    await create_run(KEY, "RUN-A", repo_key_text="github.com/acme/app",
                     started_at="2026-08-15T09:00:00+00:00", home=tmp_path)
    result = await bootstrap_run(
        _Config("https://github.com/acme/app"), resume=True, run_id_override=None, home=tmp_path
    )
    assert result.fresh is False
    assert result.run_id == "RUN-A"
    assert os.environ["ORCHESTRA_PIPELINE_ID"] == "RUN-A"


async def test_resume_with_no_runs_refuses(tmp_path):
    from maestro.run_registry import NoResumableRun

    with pytest.raises(NoResumableRun):
        await bootstrap_run(
            _Config("https://github.com/acme/app"), resume=True, run_id_override=None, home=tmp_path
        )


async def test_unresolvable_identity_refuses(tmp_path):
    from maestro.repo_identity import IdentityError

    with pytest.raises(IdentityError):
        await bootstrap_run(_Config("not-a-url"), resume=False, run_id_override=None, home=tmp_path)


async def test_run_override_selects_that_run(tmp_path):
    await create_run(KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00", home=tmp_path)
    await create_run(KEY, "RUN-B", repo_key_text="k", started_at="2026-08-15T10:00:00+00:00", home=tmp_path)
    result = await bootstrap_run(
        _Config("https://github.com/acme/app"), resume=True, run_id_override="RUN-A", home=tmp_path
    )
    assert result.run_id == "RUN-A"


async def test_a_resume_creates_no_second_run_directory(tmp_path):
    """A service tick resumes; it must never mint a run (spec §A.1)."""
    from maestro.state_paths import runs_dir

    await create_run(KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00", home=tmp_path)
    before = sorted(p.name for p in runs_dir(KEY, home=tmp_path).iterdir())
    await bootstrap_run(
        _Config("https://github.com/acme/app"), resume=True, run_id_override=None, home=tmp_path
    )
    after = sorted(p.name for p in runs_dir(KEY, home=tmp_path).iterdir())
    assert before == after == ["RUN-A"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest tests/test_run_bootstrap.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.run_bootstrap'`

- [ ] **Step 3: Write minimal implementation**

```python
# maestro/run_bootstrap.py
"""Resolve identity, choose the run, and export its id before logging starts.

Order matters (spec §A.3): `maestro/_vendor/obs.py` reads
`ORCHESTRA_PIPELINE_ID` when logging is set up and mints a fresh ULID when it
is missing. Exporting late leaves the first records under a different id.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import ulid

from maestro.repo_identity import RepoKey, parse_remote_url
from maestro.run_publish import create_run
from maestro.run_registry import live_run, resolve_runs, select_resumable

PIPELINE_ID_ENV = "ORCHESTRA_PIPELINE_ID"


@dataclass(frozen=True)
class BootstrapResult:
    key: RepoKey
    run_id: str
    db_path: Path
    fresh: bool


async def bootstrap_run(
    config: object,
    *,
    resume: bool,
    run_id_override: str | None,
    home: Path | None = None,
) -> BootstrapResult:
    key = parse_remote_url(getattr(config, "repo_url"))
    repo_key_text = "/".join(key.as_path_parts())

    if resume or run_id_override is not None:
        runs = await resolve_runs(key, home=home, lock_root=home)
        if run_id_override is not None:
            chosen = next(r for r in runs if r.run_id == run_id_override)
        else:
            chosen = select_resumable(runs)
        run_id, db_path, fresh = chosen.run_id, chosen.db_path, False
    else:
        run_id = str(ulid.new())
        db_path = await create_run(
            key,
            run_id,
            repo_key_text=repo_key_text,
            started_at=datetime.now(UTC).isoformat(),
            home=home,
        )
        fresh = True

    os.environ[PIPELINE_ID_ENV] = run_id     # before logging setup
    return BootstrapResult(key=key, run_id=run_id, db_path=db_path, fresh=fresh)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest tests/test_run_bootstrap.py -v`
Expected: PASS — 6 tests

- [ ] **Step 5: Wire it into `orchestrate` and `_service_run`**

In `orchestrate_command`, call `bootstrap_run` *before* `setup_logging` and before `create_database`, and pass `result.db_path` onward. In `_service_run` (`maestro/cli.py:2433`), replace `db = Database(db_path)` with the same call, keeping `--db` as an override that skips the resolver entirely.

- [ ] **Step 6: Run the whole suite**

Run: `uv run --with pytest python -m pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add maestro/run_bootstrap.py maestro/cli.py tests/test_run_bootstrap.py
git commit -m "feat(cli): resolve identity and run id before logging and the database"
```

---

### Task 10: `orchestrate` selection semantics and `--run`

**Files:**
- Modify: `maestro/cli.py` (`orchestrate` options and the clearing branch at `maestro/cli.py:1427-1445`)
- Test: `tests/test_orchestrate_selection.py`

**Interfaces:**
- Consumes: `resolve_runs`, `live_run`, `select_resumable`, `AmbiguousRun`, `NoResumableRun` (Task 8); `bootstrap_run` (Task 9).
- Produces: a `--run <run-id>` option on `orchestrate`.

**What must not change:** plain `orchestrate` starts a **fresh** run. Today it clears existing workstreams and says so — *"Clearing N existing workstreams state (use --resume to continue where you left off)"* (`maestro/cli.py:1437`). Storage layout does not get to turn a destructive-by-design command into an auto-resume (spec §C.2). What *does* change: clearing is no longer in place, because a fresh run gets its own directory and the previous run survives.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrate_selection.py
import pytest

from maestro.repo_identity import RepoKey
from maestro.run_bootstrap import bootstrap_run
from maestro.run_publish import create_run
from maestro.run_registry import resolve_runs

KEY = RepoKey(host="github.com", owner="acme", repo="app")


class _Config:
    repo_url = "https://github.com/acme/app"


async def test_plain_orchestrate_does_not_resume(tmp_path):
    await create_run(KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00", home=tmp_path)
    result = await bootstrap_run(_Config(), resume=False, run_id_override=None, home=tmp_path)
    assert result.fresh is True
    assert result.run_id != "RUN-A"


async def test_the_previous_run_survives_a_fresh_start(tmp_path):
    await create_run(KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00", home=tmp_path)
    await bootstrap_run(_Config(), resume=False, run_id_override=None, home=tmp_path)
    ids = {r.run_id for r in await resolve_runs(KEY, home=tmp_path, lock_root=tmp_path)}
    assert "RUN-A" in ids
    assert len(ids) == 2


async def test_plain_orchestrate_refuses_while_a_run_is_live(tmp_path):
    from maestro.run_bootstrap import RunIsLive
    from maestro.service.locks import ScopedLock

    await create_run(KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00", home=tmp_path)
    with ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A", root=tmp_path):
        with pytest.raises(RunIsLive):
            await bootstrap_run(_Config(), resume=False, run_id_override=None, home=tmp_path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest tests/test_orchestrate_selection.py -v`
Expected: FAIL — `ImportError: cannot import name 'RunIsLive'`

- [ ] **Step 3: Write minimal implementation**

Add to `maestro/run_bootstrap.py`:

```python
class RunIsLive(Exception):
    """A run of this repository is live; refuse to start a second one."""
```

and, in the fresh branch of `bootstrap_run`, before minting:

```python
        existing = await resolve_runs(key, home=home, lock_root=home)
        alive = live_run(existing)
        if alive is not None:
            raise RunIsLive(
                f"run {alive.run_id} is live for {repo_key_text}; "
                "wait for it, or pass --run <run-id> --resume"
            )
```

Import `live_run` alongside the existing registry imports.

Add the CLI option to `orchestrate`:

```python
    run: str | None = typer.Option(
        None, "--run", help="Act on this run id instead of the resolver's choice."
    ),
```

and pass it through as `run_id_override=run`. When `resolve_runs` reports non-terminal runs and the command is starting fresh, print them so the operator sees what is being left behind rather than discovering it later.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest tests/test_orchestrate_selection.py -v`
Expected: PASS — 3 tests

- [ ] **Step 5: Commit**

```bash
git add maestro/run_bootstrap.py maestro/cli.py tests/test_orchestrate_selection.py
git commit -m "feat(cli): keep orchestrate fresh-by-default, add --run, refuse while live"
```

---

### Task 11: Legacy `--db` databases without a `run` row

**Files:**
- Modify: `maestro/run_registry.py`
- Test: `tests/test_legacy_database.py`

**Interfaces:**
- Consumes: `classify_run` (5), `resolve_runs` (8).
- Produces: `async describe_database(db_path: Path, *, key=None, stage="orchestrate", lock_root=None) -> RunInfo` for a database reached directly by `--db`.

**Rule (spec §E):** such a database is read as one anonymous run — `run_id` unknown, `outcome` unknown, liveness unknown — labelled **legacy**, and **never** written a `run` row retroactively. Inventing a `started_at` and a `repo_key` for rows whose origin is exactly what is in question would manufacture the evidence this design exists to preserve.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_legacy_database.py
from maestro.database import create_database
from maestro.run_registry import describe_database


async def test_a_database_without_a_run_row_is_legacy(tmp_path):
    path = tmp_path / "maestro.db"
    db = await create_database(path)
    await db.close()
    info = await describe_database(path)
    assert info.status == "legacy"
    assert info.row is None


async def test_describe_does_not_write_a_run_row(tmp_path):
    path = tmp_path / "maestro.db"
    db = await create_database(path)
    await db.close()
    await describe_database(path)
    db2 = await create_database(path)
    assert await db2.get_run_row() is None


async def test_a_database_with_a_row_is_not_legacy(tmp_path):
    path = tmp_path / "state.db"
    db = await create_database(path)
    await db.create_run_row(run_id="RUN-A", repo_key="k", started_at="2026-08-15T09:00:00+00:00")
    await db.close()
    info = await describe_database(path)
    assert info.status == "interrupted"
    assert info.run_id == "RUN-A"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest tests/test_legacy_database.py -v`
Expected: FAIL — `ImportError: cannot import name 'describe_database'`

- [ ] **Step 3: Write minimal implementation**

```python
async def describe_database(
    db_path: Path,
    *,
    key: RepoKey | None = None,
    stage: str = "orchestrate",
    lock_root: Path | None = None,
) -> RunInfo:
    """Classify a database reached directly by `--db` (spec §E).

    A database with no `run` row is *legacy*, not *interrupted*, and is never
    backfilled: inventing `started_at` and `repo_key` would manufacture the
    provenance that is precisely in question.
    """
    db = await create_database(db_path)
    mapping = await db.get_run_row()
    await db.close()
    row = run_row_from_mapping(mapping) if mapping is not None else None
    holder = read_holder_run_id(key, stage, root=lock_root) if key is not None else None
    return RunInfo(
        run_id=row.run_id if row else db_path.stem,
        row=row,
        status=classify_run(row, lock_holder_run_id=holder),
        started_at=row.started_at if row else None,
        db_path=db_path,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest tests/test_legacy_database.py -v`
Expected: PASS — 3 tests

- [ ] **Step 5: Commit**

```bash
git add maestro/run_registry.py tests/test_legacy_database.py
git commit -m "feat(state): report a row-less database as legacy, never backfill it"
```

---

### Task 12: Workstream commands resolve a run; `~/.maestro` reports its size

**Files:**
- Modify: `maestro/cli.py` (workstream subcommands), `maestro/run_registry.py`
- Test: `tests/test_workstream_run_resolution.py`

**Interfaces:**
- Consumes: `resolve_runs`, `select_resumable`, `AmbiguousRun` (Task 8); `RepoKey` (Task 1).
- Produces: `resolve_run_for_command(key, *, run_id=None, home=None, lock_root=None) -> RunInfo`, `home_usage(*, home=None) -> HomeUsage` (a dataclass of `repositories: tuple[RepoUsage, ...]`, `unreadable`, `legacy_db`, `legacy_db_size`; each `RepoUsage` carries `key`, `run_count`, `size`, `unreadable`).

**Why (spec §C.3):** most workstream commands take only a workstream id. Once state is per-run, the same workstream id exists in many databases, so a command that does not resolve `(repository, run)` first would pick a database by accident — the original defect in a new place.

**Why the size report (spec §D):** retention is deliberately not designed, but the design commits to making growth *visible* before it becomes a problem. The evidence that this is real rather than theoretical: 13 130 log directories today, 91 % of them empty.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_workstream_run_resolution.py
import pytest

from maestro.repo_identity import RepoKey
from maestro.run_publish import create_run
from maestro.run_registry import AmbiguousRun, home_usage, resolve_run_for_command

KEY = RepoKey(host="github.com", owner="acme", repo="app")


async def test_single_run_resolves_without_a_flag(tmp_path):
    await create_run(KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00", home=tmp_path)
    info = await resolve_run_for_command(KEY, home=tmp_path, lock_root=tmp_path)
    assert info.run_id == "RUN-A"


async def test_two_runs_require_an_explicit_choice(tmp_path):
    await create_run(KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00", home=tmp_path)
    await create_run(KEY, "RUN-B", repo_key_text="k", started_at="2026-08-15T10:00:00+00:00", home=tmp_path)
    with pytest.raises(AmbiguousRun):
        await resolve_run_for_command(KEY, home=tmp_path, lock_root=tmp_path)


async def test_explicit_run_id_wins(tmp_path):
    await create_run(KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00", home=tmp_path)
    await create_run(KEY, "RUN-B", repo_key_text="k", started_at="2026-08-15T10:00:00+00:00", home=tmp_path)
    info = await resolve_run_for_command(KEY, run_id="RUN-A", home=tmp_path, lock_root=tmp_path)
    assert info.run_id == "RUN-A"


async def test_home_usage_counts_runs_and_bytes(tmp_path):
    await create_run(KEY, "RUN-A", repo_key_text="k", started_at="2026-08-15T09:00:00+00:00", home=tmp_path)
    await create_run(KEY, "RUN-B", repo_key_text="k", started_at="2026-08-15T10:00:00+00:00", home=tmp_path)
    usage = home_usage(home=tmp_path)
    assert len(usage) == 1
    key, run_count, size = usage[0]
    assert key.as_path_parts() == KEY.as_path_parts()
    assert run_count == 2
    assert size > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest tests/test_workstream_run_resolution.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_run_for_command'`

- [ ] **Step 3: Write minimal implementation**

Append to `maestro/run_registry.py`:

```python
async def resolve_run_for_command(
    key: RepoKey,
    *,
    run_id: str | None = None,
    home: Path | None = None,
    lock_root: Path | None = None,
) -> RunInfo:
    """The run a workstream command should act on (spec §C.3).

    Workstream ids are unique per database, not per repository, so a command
    that skipped this would open a database by accident.
    """
    runs = await resolve_runs(key, home=home, lock_root=lock_root)
    if run_id is not None:
        for info in runs:
            if info.run_id == run_id:
                return info
        raise NoResumableRun(f"no run {run_id} for {'/'.join(key.as_path_parts())}")
    return select_resumable(runs)


def home_usage(*, home: Path | None = None) -> list[tuple[RepoKey, int, int]]:
    """`(key, run count, bytes)` per repository — growth made visible (spec §D)."""
    from maestro.state_paths import maestro_home

    base = (home if home is not None else maestro_home()) / "projects"
    if not base.is_dir():
        return []

    report: list[tuple[RepoKey, int, int]] = []
    for runs_path in sorted(base.rglob("runs")):
        if not runs_path.is_dir():
            continue
        parts = runs_path.relative_to(base).parts[:-1]
        key = (
            RepoKey(host="_local", owner="", repo=parts[1], local=True)
            if parts and parts[0] == "_local"
            else RepoKey(host=parts[0], owner=parts[1], repo=parts[2])
        )
        run_dirs = [d for d in runs_path.iterdir() if d.is_dir()]
        size = sum(f.stat().st_size for f in runs_path.rglob("*") if f.is_file())
        report.append((key, len(run_dirs), size))
    return report
```

Then add `--run` to the workstream subcommands in `maestro/cli.py` and route each through `resolve_run_for_command`, and surface `home_usage` as `maestro state usage`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest tests/test_workstream_run_resolution.py -v`
Expected: PASS — 4 tests

- [ ] **Step 5: Commit**

```bash
git add maestro/run_registry.py maestro/cli.py tests/test_workstream_run_resolution.py
git commit -m "feat(cli): resolve a run for workstream commands; report state size"
```

---

### Task 13: Freeze the legacy default and document the layout

**Files:**
- Modify: `maestro/cli.py:115-116`, `README.md`, `CHANGELOG.md`
- Test: `tests/test_legacy_default_frozen.py`

**Interfaces:**
- Consumes: everything above.
- Produces: no new API. `DEFAULT_DB_PATH` remains defined for the `--db` default and for reading, but no code path writes to it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_legacy_default_frozen.py
import ast
from pathlib import Path

SRC = Path("maestro")


def test_no_module_creates_a_database_at_the_legacy_default():
    """DEFAULT_DB_PATH may be read; it must not be handed to a writer."""
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name not in {"create_database", "create_run"}:
                continue
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                if isinstance(arg, ast.Name) and arg.id == "DEFAULT_DB_PATH":
                    offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"legacy default used as a write target: {offenders}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest tests/test_legacy_default_frozen.py -v`
Expected: FAIL, listing the call sites that still pass `DEFAULT_DB_PATH` to a writer

- [ ] **Step 3: Remove those write paths**

Route each offender through `bootstrap_run`. Keep `DEFAULT_DB_PATH` as the `--db` option default so an explicit `--db` still reaches the legacy file read-only (Task 11).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest tests/test_legacy_default_frozen.py -v`
Expected: PASS

- [ ] **Step 5: Document the layout**

Add to `README.md` a short "Where state lives" section with the tree from spec §3, the rule that identity comes from the remote, and the note that `~/.maestro/maestro.db` is a frozen legacy file that is read but never written. Add a `CHANGELOG.md` entry under the next version marked **BREAKING**: the state path changed and lock identity no longer includes the database path, so two runs of one project against two `--db` files now serialise per stage.

- [ ] **Step 6: Full verification**

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyrefly check
uv run --with pytest python -m pytest -q
```
Expected: all clean

- [ ] **Step 7: Commit**

```bash
git add maestro/cli.py README.md CHANGELOG.md tests/test_legacy_default_frozen.py
git commit -m "feat(state): freeze the legacy database as read-only and document the layout"
```

---

## After the plan

`dispatcher#147` is already filed and describes the consumer side: enumerate `~/.maestro/projects/*/*/*/runs/*/state.db`, report newest-first per repository, and distinguish running from interrupted by the lock rather than by `ended_at`. It is not part of this plan — dispatcher is a neighbouring repository (ADR-ECO-007 `write_scope`).

The window this opens is stated in spec §F: once Task 12 lands, the dashboard freezes until `dispatcher#147` is done. That is deliberate and smaller than the current failure, where the dashboard changed just often enough to look alive.
