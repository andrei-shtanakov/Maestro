# `maestro workstream-rework` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the operator-initiated workstream rework command per the
approved spec `docs/superpowers/specs/2026-08-05-workstream-rework-design.md`.

**Architecture:** No new statuses or edges — the command performs
`NEEDS_REVIEW/FAILED -> READY` with `resume_reason='operator_rework'` via one
CAS UPDATE + one audit INSERT in a single transaction, after a fail-closed
liveness proof. The READY handler's resume dispatch becomes exhaustive and
routes `operator_rework` into the existing re-decomposition path with an
addendum loaded by the explicit `(workstream_id, operator_rework_seq)` key.
Recovery starts writing a durable recovery-ambiguity marker.

**Tech Stack:** Python 3.12, aiosqlite, Typer, pytest (existing suite
conventions: async tests via anyio, DB fixtures MUST close via
`yield d; await d.close()`).

## Global Constraints

- Run pytest in the FOREGROUND only, targeted files (workspace watchdog
  kills background runs). Full suite is carried by PR CI.
- `uv run pytest tests/<file> -q`, `uv run pyrefly check`,
  `uv run ruff format .`, `uv run ruff check .` after every task.
- TDD: failing test first, watch it fail, minimal code, watch it pass.
- Spec invariants (verbatim from the approved design):
  - allowed sources: NEEDS_REVIEW, FAILED only; everything else refused;
  - liveness proof: pid-NULL is necessary but NOT sufficient; open handles
    must probe proven-terminal; a recovery-ambiguity marker must be
    resolved (probe of preserved pid) or the command refuses; a sentinel
    marker (no pid) resolves ONLY via the explicit resolve command;
  - exactly ONE workstream-row write (CAS) + one audit INSERT per rework,
    same transaction; guard: status + both pids NULL + prior count +
    unchanged marker;
  - nothing is ever written to `gate_approvals`;
  - `--reason` never reaches the prompt; addendum comes from
    `--instructions` only, loaded by `(workstream_id, operator_rework_seq)`;
  - `--refresh-from`: same-ID workstream, description/scope only, hash over
    the exact bytes parsed; scope re-validated (normalize + overlap) before
    the transaction; refusal leaves zero trace;
  - Stage B `rework_attempt`/`rework_budget` untouched;
  - unknown `resume_reason` in the READY dispatch is an error
    (fail-closed to NEEDS_REVIEW), never a plain resume.

## File structure

- `maestro/database.py` — migration 18; `record_workstream_rework` (CAS+audit),
  `get_workstream_rework`, `resolve_recovery_ambiguity`, `count`-aware row map.
- `maestro/models.py` — `Workstream.operator_rework_count/operator_rework_seq/
  recovery_ambiguity` fields.
- `maestro/domain/resume.py` — `RESUME_OPERATOR_REWORK` constant +
  `KNOWN_RESUME_REASONS`.
- `maestro/rework.py` — NEW: pre-transaction validation (liveness proof,
  HEAD sha, refresh-from parsing/validation), `build_operator_rework_addendum`.
- `maestro/orchestrator.py` — recovery-ambiguity marker writes; exhaustive
  resume dispatch; operator addendum loading.
- `maestro/cli.py` — `workstream-rework`, `workstream-resolve-ambiguity`
  commands; count column + threshold warning in `_show_workstreams_status`.
- Tests: `tests/test_workstream_rework.py` (NEW), plus additions to
  `tests/test_orchestrator_recovery.py`-style files where noted.
- `CHANGELOG.md`, `CLAUDE.md` command list.

---

### Task 1: Migration 18 + model fields

**Files:**
- Modify: `maestro/database.py` (schema constant ~line 184, `ordered` list
  ~line 506, new `_migrate_workstream_rework_columns`, `_row_to_workstream`)
- Modify: `maestro/models.py` (Workstream fields, after `resume_reason`)
- Test: `tests/test_workstream_rework.py` (new file)

**Interfaces:**
- Produces: columns `workstreams.operator_rework_count INTEGER NOT NULL
  DEFAULT 0`, `workstreams.operator_rework_seq INTEGER` (nullable),
  `workstreams.recovery_ambiguity TEXT` (nullable JSON); table
  `workstream_reworks(workstream_id TEXT NOT NULL, seq INTEGER NOT NULL,
  initiated_at TIMESTAMP NOT NULL, initiator TEXT NOT NULL, reason TEXT
  NOT NULL, instructions TEXT, prior_status TEXT NOT NULL,
  prior_error_message TEXT, prior_head_sha TEXT NOT NULL, liveness_evidence
  TEXT, refresh_config_path TEXT, refresh_config_hash TEXT,
  old_description TEXT, new_description TEXT, old_scope TEXT, new_scope
  TEXT, PRIMARY KEY (workstream_id, seq))`; table
  `workstream_ambiguity_resolutions(workstream_id TEXT NOT NULL,
  resolved_at TIMESTAMP NOT NULL, initiator TEXT NOT NULL, statement TEXT
  NOT NULL, marker_json TEXT NOT NULL)`; model fields
  `Workstream.operator_rework_count: int = 0`,
  `Workstream.operator_rework_seq: int | None = None`,
  `Workstream.recovery_ambiguity: str | None = None`.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for maestro workstream-rework (issue #124)."""

import pytest

from maestro.database import Database
from maestro.models import Workstream, WorkstreamStatus


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()


def make_ws(id_: str = "ws-1", status=WorkstreamStatus.NEEDS_REVIEW) -> Workstream:
    return Workstream(
        id=id_,
        title=id_,
        description="desc",
        branch=f"feature/{id_}",
        status=status,
    )


class TestMigration18:
    @pytest.mark.anyio
    async def test_new_columns_default(self, db) -> None:
        await db.create_workstream(make_ws())
        ws = await db.get_workstream("ws-1")
        assert ws.operator_rework_count == 0
        assert ws.operator_rework_seq is None
        assert ws.recovery_ambiguity is None

    @pytest.mark.anyio
    async def test_rework_tables_exist(self, db) -> None:
        cur = await db._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('workstream_reworks', 'workstream_ambiguity_resolutions')"
        )
        names = {row["name"] for row in await cur.fetchall()}
        assert names == {"workstream_reworks", "workstream_ambiguity_resolutions"}
```

(Adjust the fixture pattern to whatever `tests/test_database.py` actually
uses for `anyio` marks — mirror the existing convention exactly.)

- [ ] **Step 2: Run and watch fail**

Run: `uv run pytest tests/test_workstream_rework.py -q`
Expected: FAIL (`operator_rework_count` attribute missing / tables absent).

- [ ] **Step 3: Implement**

`maestro/models.py` — after `resume_reason` field on `Workstream`:

```python
    operator_rework_count: int = Field(default=0, ge=0)
    operator_rework_seq: int | None = Field(
        default=None,
        description="Audit seq of the operator rework this READY resumes",
    )
    recovery_ambiguity: str | None = Field(
        default=None,
        description=(
            "JSON marker written by startup recovery when parking a "
            "possibly-live workstream in NEEDS_REVIEW; blocks "
            "workstream-rework until resolved"
        ),
    )
```

`maestro/database.py`:
1. Extend the `CREATE TABLE IF NOT EXISTS workstreams` schema constant with
   the three columns (mirror the Stage B comment style):

```sql
    -- Operator rework (#124)
    operator_rework_count INTEGER NOT NULL DEFAULT 0,
    operator_rework_seq INTEGER,
    recovery_ambiguity TEXT
```

2. Add to the schema script (next to the other CREATE TABLE statements):

```sql
CREATE TABLE IF NOT EXISTS workstream_reworks (
    workstream_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    initiated_at TIMESTAMP NOT NULL,
    initiator TEXT NOT NULL,
    reason TEXT NOT NULL,
    instructions TEXT,
    prior_status TEXT NOT NULL,
    prior_error_message TEXT,
    prior_head_sha TEXT NOT NULL,
    liveness_evidence TEXT,
    refresh_config_path TEXT,
    refresh_config_hash TEXT,
    old_description TEXT,
    new_description TEXT,
    old_scope TEXT,
    new_scope TEXT,
    PRIMARY KEY (workstream_id, seq),
    FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workstream_ambiguity_resolutions (
    workstream_id TEXT NOT NULL,
    resolved_at TIMESTAMP NOT NULL,
    initiator TEXT NOT NULL,
    statement TEXT NOT NULL,
    marker_json TEXT NOT NULL,
    FOREIGN KEY (workstream_id) REFERENCES workstreams(id) ON DELETE CASCADE
);
```

3. Migration entry `(18, "workstream_rework", self._migrate_workstream_rework)`
   appended to `ordered`; the migration mirrors
   `_migrate_workstreams_verification_columns` (PRAGMA table_info idempotent
   ALTERs for the three columns) and executes the two CREATE TABLE IF NOT
   EXISTS statements.
4. `_row_to_workstream`: map the three columns with
   `row["operator_rework_count"] or 0` style defaults (follow how
   `rework_attempt`/`resume_reason` are mapped).
5. Check `create_workstream` INSERT column list — if it enumerates columns
   explicitly, add the three new ones (follow `resume_reason` precedent).

- [ ] **Step 4: Run tests, pyrefly, ruff**

Run: `uv run pytest tests/test_workstream_rework.py tests/test_database.py -q`
Expected: PASS. Then `uv run pyrefly check` (0 errors),
`uv run ruff format . && uv run ruff check .`.

- [ ] **Step 5: Commit**

```bash
git add maestro/database.py maestro/models.py tests/test_workstream_rework.py
git commit -m "feat(db): migration 18 — operator rework columns + audit tables (#124)"
```

---

### Task 2: Recovery writes the durable recovery-ambiguity marker

**Files:**
- Modify: `maestro/orchestrator.py` (the two NEEDS_REVIEW parking branches
  in startup recovery, ~lines 520-570: `handle_needs_review` branch and
  `live_orphan` branch)
- Test: `tests/test_workstream_rework.py` (class `TestRecoveryMarker`) —
  or extend the existing recovery test file if one covers these branches
  (grep `handle_needs_review` in tests/ first; mirror its harness).

**Interfaces:**
- Consumes: `Workstream.recovery_ambiguity` (Task 1).
- Produces: marker JSON shape (module-level helper in orchestrator.py):

```python
def _ambiguity_marker(kind: str, pid: int | None) -> str:
    """kind: 'live_orphan' | 'spawn_uncertain' | 'live_handle'."""
    return json.dumps(
        {
            "kind": kind,
            "pid": None if pid == _SPAWNING_SENTINEL else pid,
            "parked_at": datetime.now(UTC).isoformat(),
        }
    )
```

  `pid=None` in the marker means "no probeable evidence" (sentinel or
  handle case) — per the spec such a marker is resolvable ONLY by the
  explicit resolve command, never by an automatic probe.

- [ ] **Step 1: Write the failing test**

Simulate the recovery branch outcome: create a RUNNING workstream with a
live fake pid, run the orchestrator recovery entry point used by the
existing recovery tests (mirror their setup exactly — they monkeypatch
`_is_pid_alive`), then assert:

```python
    ws = await db.get_workstream("ws-1")
    assert ws.status is WorkstreamStatus.NEEDS_REVIEW
    marker = json.loads(ws.recovery_ambiguity)
    assert marker["kind"] == "live_orphan"
    assert marker["pid"] == 4242
```

Add a second test for the sentinel case (`generation_pid=_SPAWNING_SENTINEL`,
DECOMPOSING) asserting `kind == "spawn_uncertain"` and `pid is None`, and a
third for the possibly-live-handle branch asserting `kind == "live_handle"`.

- [ ] **Step 2: Run and watch fail** — `recovery_ambiguity` stays None.

- [ ] **Step 3: Implement**

In both parking transitions add the field (they already pass
`process_pid=None, generation_pid=None`):

```python
    recovery_ambiguity=_ambiguity_marker(
        "spawn_uncertain" if orphan_pid == _SPAWNING_SENTINEL else "live_orphan",
        orphan_pid,
    ),
```

and in the `handle_needs_review` branch:

```python
    recovery_ambiguity=_ambiguity_marker("live_handle", None),
```

Confirm `_transition(**fields)` forwards arbitrary columns (it already
forwards `process_pid`/`generation_pid`); if the underlying update method
whitelists columns, extend the whitelist.

- [ ] **Step 4: Run tests + pyrefly + ruff** (targeted recovery test files).

- [ ] **Step 5: Commit** — `feat(recovery): durable recovery-ambiguity marker (#124)`

---

### Task 3: DB API — CAS+audit transaction, seq lookup, ambiguity resolution

**Files:**
- Modify: `maestro/database.py` (three new methods next to
  `approve_workstream_with_gate_record`, which is the pattern to mirror)
- Create: `maestro/rework.py` (minimal module: `RefreshEvidence` dataclass +
  `ReworkRefused` exception only — Task 4 extends it with the logic)
- Test: `tests/test_workstream_rework.py` (class `TestRecordRework`)

**Interfaces:**
- Produces:

```python
async def record_workstream_rework(
    self,
    workstream_id: str,
    *,
    prior_status: WorkstreamStatus,
    prior_count: int,
    prior_marker: str | None,
    reason: str,
    instructions: str | None,
    initiator: str,
    prior_error_message: str | None,
    prior_head_sha: str,
    liveness_evidence: str | None,
    refresh: "RefreshEvidence | None",
) -> int:  # returns the new seq
```

with (created in THIS task, `maestro/rework.py`):

```python
@dataclass(frozen=True)
class RefreshEvidence:
    config_path: str
    config_hash: str          # sha256 over the exact bytes parsed
    old_description: str
    new_description: str
    old_scope: list[str]
    new_scope: list[str]


class ReworkRefused(Exception):
    """Fail-closed refusal; message is operator-facing."""
```

  Behavior: single `self.transaction()`; ONE CAS UPDATE setting
  status='ready', resume_reason='operator_rework',
  operator_rework_count=prior_count+1, operator_rework_seq=prior_count+1,
  error_message=NULL, verification_run_id=NULL, recovery_ambiguity=NULL,
  plus description/scope when `refresh` is given, guarded by
  `WHERE id=? AND status=? AND process_pid IS NULL AND generation_pid IS
  NULL AND operator_rework_count=? AND recovery_ambiguity IS ?`;
  `cursor.rowcount == 0` -> raise `ValueError("state changed under the
  operator — rework refused")` (transaction context rolls back, audit row
  never lands). Then the audit INSERT with seq=prior_count+1.

```python
async def get_workstream_rework(
    self, workstream_id: str, seq: int
) -> dict[str, Any] | None
```

```python
async def resolve_recovery_ambiguity(
    self, workstream_id: str, *, statement: str, initiator: str
) -> None
```

  One transaction: read `recovery_ambiguity` (must be non-NULL, else
  ValueError), INSERT the resolution row (statement + marker_json), CAS
  `UPDATE workstreams SET recovery_ambiguity=NULL WHERE id=? AND
  recovery_ambiguity=?`; rowcount 0 -> ValueError.

- [ ] **Step 1: Write failing tests** (all via the `db` fixture):

```python
class TestRecordRework:
    @pytest.mark.anyio
    async def test_cas_success_writes_row_and_audit(self, db) -> None:
        await db.create_workstream(make_ws())
        seq = await db.record_workstream_rework(
            "ws-1",
            prior_status=WorkstreamStatus.NEEDS_REVIEW,
            prior_count=0,
            prior_marker=None,
            reason="reviewer rejected the diff",
            instructions="split the migration",
            initiator="andrei",
            prior_error_message="gate blocked",
            prior_head_sha="a" * 40,
            liveness_evidence=None,
            refresh=None,
        )
        assert seq == 1
        ws = await db.get_workstream("ws-1")
        assert ws.status is WorkstreamStatus.READY
        assert ws.resume_reason == "operator_rework"
        assert ws.operator_rework_count == 1
        assert ws.operator_rework_seq == 1
        assert ws.error_message is None
        row = await db.get_workstream_rework("ws-1", 1)
        assert row["reason"] == "reviewer rejected the diff"
        assert row["instructions"] == "split the migration"

    @pytest.mark.anyio
    async def test_cas_refuses_on_stale_status(self, db) -> None:
        await db.create_workstream(make_ws(status=WorkstreamStatus.READY))
        with pytest.raises(ValueError):
            await db.record_workstream_rework(
                "ws-1",
                prior_status=WorkstreamStatus.NEEDS_REVIEW,  # stale read
                prior_count=0,
                prior_marker=None,
                reason="r",
                instructions=None,
                initiator="andrei",
                prior_error_message=None,
                prior_head_sha="a" * 40,
                liveness_evidence=None,
                refresh=None,
            )
        assert await db.get_workstream_rework("ws-1", 1) is None  # no audit
```

Plus: `test_cas_refuses_on_live_pid` (row with process_pid=123),
`test_second_rework_gets_seq_2`, `test_no_gate_approvals_written` (assert
`list_gate_approvals` stays empty), `test_resolve_ambiguity_roundtrip`
(marker set -> resolve -> NULL + resolution row; resolve on NULL marker
raises).

- [ ] **Step 2: Run, watch fail** (methods missing).
- [ ] **Step 3: Implement** per the Produces block, raw SQL inside
  `self.transaction()` exactly like `approve_workstream_with_gate_record`.
  Note SQLite `IS ?` works for NULL-safe comparison of the marker guard.
- [ ] **Step 4: Run tests + pyrefly + ruff.**
- [ ] **Step 5: Commit** — `feat(db): record_workstream_rework CAS+audit, ambiguity resolution (#124)`

---

### Task 4: `maestro/rework.py` — liveness proof + refresh validation + addendum

**Files:**
- Create: `maestro/rework.py`
- Modify: `maestro/domain/resume.py` (add constant)
- Test: `tests/test_workstream_rework.py` (classes `TestLivenessProof`,
  `TestRefreshValidation`, `TestAddendum`)

**Interfaces:**
- Consumes: `Database.get_open_execution_handles()` (existing),
  `_maybe_live_orphan`-equivalent pid probe, `parse/normalize` from
  `maestro.scope_gate`, `validate_project` overlap machinery from
  `maestro.preflight`, `load_orchestrator_config` from `maestro.config`.
- Produces (exact signatures later tasks use):

```python
# maestro/domain/resume.py
RESUME_OPERATOR_REWORK = "operator_rework"
KNOWN_RESUME_REASONS = frozenset(
    {RESUME_REWORK, RESUME_REVERIFY, RESUME_OPERATOR_REWORK}
)

# maestro/rework.py (RefreshEvidence and ReworkRefused exist since Task 3)
async def prove_no_live_process(db: Database, ws: Workstream) -> str | None:
    """Return liveness-evidence JSON on success; raise ReworkRefused."""

def validate_refresh(
    ws: Workstream, config_path: Path
) -> RefreshEvidence | None:
    """None when description AND scope are unchanged; raise ReworkRefused
    on topology drift / missing ID / scope validation failure."""

def build_operator_rework_addendum(reason_row: dict[str, Any]) -> str | None:
    """Addendum from the audit row's `instructions` (None -> None)."""
```

- [ ] **Step 1: Write failing tests**

```python
class TestLivenessProof:
    @pytest.mark.anyio
    async def test_pids_null_no_marker_no_handles_passes(self, db) -> None:
        await db.create_workstream(make_ws())
        ws = await db.get_workstream("ws-1")
        evidence = await prove_no_live_process(db, ws)
        assert evidence is None or "probe" in evidence

    @pytest.mark.anyio
    async def test_nonnull_pid_refuses(self, db) -> None:
        ws = make_ws().model_copy(update={"process_pid": 123})
        await db.create_workstream(ws)
        with pytest.raises(ReworkRefused):
            await prove_no_live_process(db, await db.get_workstream("ws-1"))

    @pytest.mark.anyio
    async def test_marker_with_dead_pid_passes_with_evidence(
        self, db, monkeypatch
    ) -> None:
        import maestro.rework as rework_mod

        monkeypatch.setattr(rework_mod, "_is_pid_alive", lambda pid: False)
        ws = make_ws().model_copy(
            update={
                "recovery_ambiguity": json.dumps(
                    {"kind": "live_orphan", "pid": 4242, "parked_at": "t"}
                )
            }
        )
        await db.create_workstream(ws)
        evidence = await prove_no_live_process(db, await db.get_workstream("ws-1"))
        assert "4242" in evidence  # probe result recorded

    @pytest.mark.anyio
    async def test_marker_with_live_pid_refuses(self, db, monkeypatch) -> None:
        import maestro.rework as rework_mod

        monkeypatch.setattr(rework_mod, "_is_pid_alive", lambda pid: True)
        ws = make_ws().model_copy(
            update={
                "recovery_ambiguity": json.dumps(
                    {"kind": "live_orphan", "pid": 4242, "parked_at": "t"}
                )
            }
        )
        await db.create_workstream(ws)
        with pytest.raises(ReworkRefused):
            await prove_no_live_process(db, await db.get_workstream("ws-1"))

    @pytest.mark.anyio
    async def test_sentinel_marker_always_refuses(self, db, monkeypatch) -> None:
        import maestro.rework as rework_mod

        monkeypatch.setattr(rework_mod, "_is_pid_alive", lambda pid: False)
        ws = make_ws().model_copy(
            update={
                "recovery_ambiguity": json.dumps(
                    {"kind": "spawn_uncertain", "pid": None, "parked_at": "t"}
                )
            }
        )
        await db.create_workstream(ws)
        with pytest.raises(ReworkRefused, match="resolve"):
            await prove_no_live_process(db, await db.get_workstream("ws-1"))

    @pytest.mark.anyio
    async def test_open_handle_refuses(self, db) -> None:
        # insert an open execution handle row for ws-1 via the db helper the
        # execution layer uses (grep tests for create/insert of execution
        # handles and mirror), then expect ReworkRefused.
        ...
```

`TestRefreshValidation`: same-ID description change accepted (evidence has
old/new + sha256 of file bytes); changed `depends_on` -> ReworkRefused;
missing ID -> ReworkRefused; overlapping refreshed scope vs a sibling in
the config -> ReworkRefused; unchanged description+scope -> None.
`TestAddendum`: instructions -> rendered addendum containing the text and a
header line `## Operator rework instructions`; instructions None -> None.
Also: `KNOWN_RESUME_REASONS == {"verification_rework",
"verification_reverify", "operator_rework"}`.

- [ ] **Step 2: Run, watch fail.**
- [ ] **Step 3: Implement** `maestro/rework.py`:
  - `_is_pid_alive(pid)` — copy the semantics used by orchestrator
    recovery (os.kill(pid, 0) wrapped; import if importable without cycle,
    else duplicate the 4-line helper with a comment naming the original).
  - `prove_no_live_process`: (1) both pids NULL else refuse; (2)
    `get_open_execution_handles()` filtered to this workstream — any open
    row refuses with "open execution handle <id>; wait for recovery/GC or
    investigate" (CLI-context cannot safely run backend probes that may
    need SSH config; an open handle after recovery ran means genuinely
    unresolved — fail closed. The orchestrator-side probe already closed
    handles for dead executions during recovery); (3) marker: absent ->
    pass; `pid` present -> probe `_is_pid_alive`: dead -> return evidence
    JSON `{"probe": "pid", "pid": N, "alive": false, "checked_at": ...}`;
    alive -> refuse; `pid` null (sentinel / live_handle kinds) -> refuse
    with message naming `maestro workstream-resolve-ambiguity`.
  - `validate_refresh`: read bytes once (`config_path.read_bytes()`),
    `hashlib.sha256(data).hexdigest()`, parse via
    `load_orchestrator_config` on a NamedTemporaryFile? No — parse from
    the same bytes: `yaml.safe_load(data)` then
    `OrchestratorConfig.model_validate(...)` the way
    `load_orchestrator_config` does (reuse its internals if importable;
    otherwise call `load_orchestrator_config(config_path)` for parsing but
    STILL hash the pre-read bytes and pass those bytes to a `from_bytes`
    helper — the plan choice: add
    `load_orchestrator_config_from_bytes(data: bytes, source: Path)` to
    `maestro/config.py`, a thin split of the existing loader, and make the
    path variant delegate to it. This keeps hash==parsed-bytes provable.)
    Then: find same-ID workstream (else refuse); compare topology fields
    (`depends_on`, `priority`) — any diff refuses; if description and
    scope both unchanged -> None; validate new scope: build a config copy
    with the refreshed workstream and run
    `validate_project(cfg, check_fs=False)`; any NEW error, or any
    warning-severity `scope-overlap` naming this workstream, refuses.
  - `build_operator_rework_addendum`: mirror
    `maestro/domain/addendum.py::build_rework_addendum` formatting:

```python
def build_operator_rework_addendum(reason_row: dict[str, Any]) -> str | None:
    instructions = reason_row.get("instructions")
    if not instructions:
        return None
    return (
        "## Operator rework instructions\n\n"
        "The previous attempt was rejected by the operator. Apply the\n"
        "following instructions in this attempt:\n\n"
        f"{instructions}\n"
    )
```

- [ ] **Step 4: Run tests + pyrefly + ruff.**
- [ ] **Step 5: Commit** — `feat(rework): liveness proof, refresh validation, addendum (#124)`

---

### Task 5: CLI — `workstream-rework` and `workstream-resolve-ambiguity`

**Files:**
- Modify: `maestro/cli.py` (two commands next to
  `workstream_approve_command`, which is the template)
- Test: `tests/test_workstream_rework.py` (class `TestCli`, via
  `typer.testing.CliRunner` — mirror how `tests/test_cli.py` invokes
  workstream commands)

**Interfaces:**
- Consumes: Tasks 3-4 APIs verbatim.
- Produces: commands

```
maestro workstream-rework <id> --reason TEXT [--instructions TEXT]
    [--refresh-from PATH] [--db PATH]
maestro workstream-resolve-ambiguity <id> --statement TEXT [--db PATH]
```

Command flow (`_rework_workstream(db, workstream_id, reason, instructions,
refresh_from) -> int` async helper returning the new seq):

```python
    ws = await db.get_workstream(workstream_id)          # NotFound -> error
    if ws.status not in (WorkstreamStatus.NEEDS_REVIEW, WorkstreamStatus.FAILED):
        raise ReworkRefused(f"status {ws.status.value} is not reworkable")
    evidence = await prove_no_live_process(db, ws)
    if not ws.workspace_path:
        raise ReworkRefused("no worktree recorded — nothing to rework")
    prior_head_sha = await read_head_sha(Path(ws.workspace_path))  # rework.py
    refresh = validate_refresh(ws, Path(refresh_from)) if refresh_from else None
    seq = await db.record_workstream_rework(
        workstream_id,
        prior_status=ws.status,
        prior_count=ws.operator_rework_count,
        prior_marker=ws.recovery_ambiguity,
        reason=reason,
        instructions=instructions,
        initiator=getpass.getuser(),
        prior_error_message=ws.error_message,
        prior_head_sha=prior_head_sha,
        liveness_evidence=evidence,
        refresh=refresh,
    )
    return seq
```

`read_head_sha(worktree: Path) -> str` lives in `maestro/rework.py`: runs
`git -C <worktree> rev-parse HEAD` (asyncio subprocess, same pattern as
`changed_paths._run_git`); missing dir / non-zero exit -> ReworkRefused.
Output on success: green message with new seq + operator_rework_count,
warning when count >= 3 ("N operator reworks; consider whether this
workstream needs redesign instead"), and the resume hint line (copy the
approve command's wording). All `ReworkRefused`/ValueError -> red message,
exit 1.

- [ ] **Step 1: Write failing CLI tests** — success path (NEEDS_REVIEW row
  with a real tmp git worktree fixture: `git init` + one commit so
  rev-parse works); refusal paths: RUNNING status, live pid, sentinel
  marker (asserting the resolve-command hint in output), missing worktree;
  resolve-ambiguity success + refusal-on-no-marker; threshold warning at
  count 3 (pre-seed `operator_rework_count=3` via two prior reworks or a
  direct update helper).
- [ ] **Step 2: Run, watch fail.**
- [ ] **Step 3: Implement** both commands per the flow above.
- [ ] **Step 4: Run tests + pyrefly + ruff.**
- [ ] **Step 5: Commit** — `feat(cli): maestro workstream-rework + workstream-resolve-ambiguity (#124)`

---

### Task 6: Exhaustive READY dispatch + operator addendum in the respawn path

**Files:**
- Modify: `maestro/orchestrator.py` (the READY resume dispatch, ~lines
  1469-1495: currently `if resume_reason == RESUME_REVERIFY: ... ;
  is_rework_resume = resume_reason == RESUME_REWORK`)
- Test: `tests/test_workstream_rework.py` (class `TestResumeDispatch`) —
  mirror the harness of the existing Stage B rework-resume tests (grep
  `RESUME_REWORK` in tests/).

**Interfaces:**
- Consumes: `KNOWN_RESUME_REASONS`, `RESUME_OPERATOR_REWORK`,
  `build_operator_rework_addendum`, `Database.get_workstream_rework`.
- Produces: dispatch behavior —

```python
        if workstream.resume_reason == RESUME_REVERIFY:
            ...existing...
            return
        if (
            workstream.resume_reason is not None
            and workstream.resume_reason not in KNOWN_RESUME_REASONS
        ):
            # Fail-closed: an unknown resume_reason must never silently
            # plain-resume (spec #124).
            await self._transition(
                workstream_id,
                WorkstreamStatus.NEEDS_REVIEW,
                expected_status=workstream.status,
                message=f"unknown resume_reason {workstream.resume_reason!r}",
            )
            return
        rework_addendum: str | None = None
        if workstream.resume_reason == RESUME_REWORK:
            rework_addendum = await self._load_rework_addendum(workstream)
        elif workstream.resume_reason == RESUME_OPERATOR_REWORK:
            rework_addendum = await self._load_operator_rework_addendum(workstream)
```

with

```python
    async def _load_operator_rework_addendum(
        self, workstream: Workstream
    ) -> str | None:
        """Addendum keyed explicitly by (id, operator_rework_seq) — spec #124."""
        if workstream.operator_rework_seq is None:
            return None
        row = await self._db.get_workstream_rework(
            workstream.id, workstream.operator_rework_seq
        )
        if row is None:
            return None
        return build_operator_rework_addendum(row)
```

Check where `resume_reason` is cleared for RESUME_REWORK today
(`_update_fields(..., resume_reason=None)` at ~line 1752 — the "reason not
lost until the new attempt exists" point) and confirm the operator path
flows through the same clearing site; the seq column intentionally stays.
Also verify the NEEDS_REVIEW transition used for the unknown-reason error
is legal from READY (it is: READY -> NEEDS_REVIEW is a valid edge).

- [ ] **Step 1: Write failing tests** — (a) unknown reason
  `resume_reason='garbage'` on READY -> workstream lands NEEDS_REVIEW,
  never spawns; (b) `operator_rework` resume regenerates spec with the
  addendum text present in the description passed to
  `generate_spec` (mirror how existing tests capture the
  `WorkstreamConfig` handed to a fake decomposer) and `resume_reason`
  cleared afterwards; (c) `operator_rework` with instructions=None ->
  description unchanged (no addendum), still re-decomposes.
- [ ] **Step 2: Run, watch fail.**
- [ ] **Step 3: Implement** per the Produces block.
- [ ] **Step 4: Run targeted orchestrator test files + pyrefly + ruff.**
- [ ] **Step 5: Commit** — `feat(orchestrator): exhaustive resume dispatch + operator rework addendum (#124)`

---

### Task 7: Visibility, docs, changelog

**Files:**
- Modify: `maestro/cli.py` (`_show_workstreams_status`: add a `Reworks`
  column shown when any count > 0, with `[yellow]` style at >= 3)
- Modify: `CLAUDE.md` (command list: add both commands one line each)
- Modify: `CHANGELOG.md` (Added entry)
- Test: extend `TestCli` with a workstreams-display assertion
  (count rendered; warning style at threshold).

- [ ] **Step 1: Write failing display test.**
- [ ] **Step 2: Run, watch fail.**
- [ ] **Step 3: Implement** display + docs:

CHANGELOG entry (under Unreleased / Added):

```markdown
- **`maestro workstream-rework <id>` (#124).** Sanctioned operator rework
  for a gate-blocked/failed workstream: `NEEDS_REVIEW/FAILED -> READY`
  with `resume_reason='operator_rework'` into the existing
  re-decomposition path (same worktree, same lineage, idempotent harness
  state cleanup). Mandatory `--reason` (audit-only), optional
  `--instructions` (next-attempt addendum) and `--refresh-from
  <project.yaml>` (description/scope only, re-validated; topology fields
  refused). Fail-closed liveness proof: pid-NULL alone is insufficient —
  open execution handles and the new durable recovery-ambiguity marker
  block the command until proven terminal or explicitly resolved via
  `maestro workstream-resolve-ambiguity`. One CAS UPDATE + append-only
  audit row per rework; nothing is ever written to `gate_approvals`;
  Stage B rework budget untouched. Unknown `resume_reason` values now
  fail closed to NEEDS_REVIEW instead of silently plain-resuming.
```

- [ ] **Step 4: Run full targeted set:**
  `uv run pytest tests/test_workstream_rework.py tests/test_cli.py tests/test_database.py tests/test_preflight.py -q`
  plus the recovery/orchestrator files touched in Tasks 2/6; pyrefly; ruff.
- [ ] **Step 5: Commit** — `feat: workstream-rework visibility + docs (#124)`

---

## Final verification (before PR)

- [ ] Re-read the spec acceptance checklist
  (`docs/superpowers/specs/2026-08-05-workstream-rework-design.md`) — every
  bullet must map to a passing test in `tests/test_workstream_rework.py`.
- [ ] `uv run pyrefly check` — 0 errors; `uv run ruff format . && uv run
  ruff check .` — clean.
- [ ] Branch `feat/workstream-rework`, PR body maps spec bullets to tests,
  `Closes #124`.
