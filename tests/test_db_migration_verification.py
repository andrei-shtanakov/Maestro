"""Tests for migrations 13-15 (Stage B verification persistence).

Migration 13 (`workstreams_verification_columns`): five additive columns on
`workstreams` — `verification_run_id`, `verification_attempt`,
`verification_error_attempt`, `rework_attempt`, `resume_reason`.

Migration 14 (`verification_attempts_table`): the `verification_attempts`
index table (one row per `(run_id, attempt)`), plus the
`insert_verification_attempt` / `list_verification_attempts` /
`mark_attempts_materialized` DB methods.

Migration 15 (`execution_phase_verification`): widens the
`execution_handles.execution_phase` CHECK constraint to also accept
`'verification'`. SQLite cannot ALTER a CHECK constraint, so this rebuilds
the table (mirrors migration 9, `_migrate_ssh_handle_columns`).
"""

import aiosqlite
import pytest

from maestro.database import Database
from maestro.models import Workstream, WorkstreamStatus


def _seed_workstream(
    wid: str = "api", status: WorkstreamStatus = WorkstreamStatus.READY
):
    return Workstream(
        id=wid,
        title=wid,
        description="d",
        branch=f"feature/{wid}",
        status=status,
    )


# =============================================================================
# Migration 12: workstreams verification columns
# =============================================================================


class TestWorkstreamsVerificationColumnsMigration:
    """_migrate_workstreams_verification_columns adds 5 columns idempotently."""

    @pytest.mark.anyio
    async def test_fresh_db_has_verification_columns(self, tmp_path) -> None:
        db = Database(tmp_path / "fresh.db")
        await db.connect()
        try:
            assert db._connection is not None
            cursor = await db._connection.execute("PRAGMA table_info(workstreams)")
            cols = {row["name"] for row in await cursor.fetchall()}
            assert {
                "verification_run_id",
                "verification_attempt",
                "verification_error_attempt",
                "rework_attempt",
                "resume_reason",
            } <= cols
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_old_db_migrates_in_place(self, tmp_path) -> None:
        """Pre-migration-12 workstreams table (no verification columns).

        Existing rows survive the ALTER with NULL/default values in the new
        columns, mirroring `TestArbiterRoutingMigration.test_legacy_db_migrates`.
        """
        db_path = tmp_path / "legacy.db"
        legacy_sql = """
        CREATE TABLE workstreams (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            branch TEXT NOT NULL,
            workspace_path TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            scope TEXT,
            priority INTEGER DEFAULT 0,
            pr_url TEXT,
            process_pid INTEGER,
            generation_pid INTEGER,
            subtask_progress TEXT,
            error_message TEXT,
            retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 2,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP
        )
        """
        async with aiosqlite.connect(str(db_path)) as conn:
            await conn.execute(legacy_sql)
            await conn.execute(
                "INSERT INTO workstreams (id, title, description, branch) "
                "VALUES ('w1', 'T', 'D', 'feature/w1')"
            )
            await conn.commit()

        db = Database(db_path)
        await db.connect()
        try:
            assert db._connection is not None
            cursor = await db._connection.execute("PRAGMA table_info(workstreams)")
            cols = {row["name"] for row in await cursor.fetchall()}
            assert "verification_run_id" in cols
            assert "resume_reason" in cols

            cursor = await db._connection.execute(
                "SELECT verification_run_id, verification_attempt, "
                "verification_error_attempt, rework_attempt, resume_reason "
                "FROM workstreams WHERE id = 'w1'"
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row["verification_run_id"] is None
            assert row["verification_attempt"] == 0
            assert row["verification_error_attempt"] == 0
            assert row["rework_attempt"] == 0
            assert row["resume_reason"] is None

            # Legacy row's original data is untouched.
            cursor = await db._connection.execute(
                "SELECT title, branch FROM workstreams WHERE id = 'w1'"
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row["title"] == "T"
            assert row["branch"] == "feature/w1"
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_migration_idempotent(self, tmp_path) -> None:
        db = Database(tmp_path / "idem.db")
        await db.connect()
        try:
            await db._migrate_workstreams_verification_columns()
            await db._migrate_workstreams_verification_columns()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_verification_columns_round_trip_via_model(self, tmp_path) -> None:
        """Workstream model fields persist through create/get."""
        db = Database(tmp_path / "rt.db")
        await db.connect()
        try:
            ws = Workstream(
                id="a",
                title="a",
                description="d",
                branch="feature/a",
                verification_run_id="run-1",
                verification_attempt=2,
                verification_error_attempt=1,
                rework_attempt=3,
                resume_reason="verification_rework",
            )
            await db.create_workstream(ws)
            fetched = await db.get_workstream("a")
            assert fetched.verification_run_id == "run-1"
            assert fetched.verification_attempt == 2
            assert fetched.verification_error_attempt == 1
            assert fetched.rework_attempt == 3
            assert fetched.resume_reason == "verification_rework"
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_verification_columns_default_when_unset(self, tmp_path) -> None:
        db = Database(tmp_path / "defaults.db")
        await db.connect()
        try:
            await db.create_workstream(_seed_workstream("b"))
            fetched = await db.get_workstream("b")
            assert fetched.verification_run_id is None
            assert fetched.verification_attempt == 0
            assert fetched.verification_error_attempt == 0
            assert fetched.rework_attempt == 0
            assert fetched.resume_reason is None
        finally:
            await db.close()


# =============================================================================
# Migration 13: verification_attempts index table
# =============================================================================


class TestVerificationAttemptsTableMigration:
    """_migrate_verification_attempts_table creates the ledger index table."""

    @pytest.mark.anyio
    async def test_fresh_db_has_verification_attempts_table(self, tmp_path) -> None:
        db = Database(tmp_path / "fresh.db")
        await db.connect()
        try:
            assert db._connection is not None
            cursor = await db._connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='verification_attempts'"
            )
            assert await cursor.fetchone() is not None
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_migration_idempotent(self, tmp_path) -> None:
        db = Database(tmp_path / "idem.db")
        await db.connect()
        try:
            await db._migrate_verification_attempts_table()
            await db._migrate_verification_attempts_table()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_insert_and_list_verification_attempts(self, tmp_path) -> None:
        db = Database(tmp_path / "t.db")
        await db.connect()
        try:
            await db.create_workstream(_seed_workstream("w1"))
            await db.insert_verification_attempt(
                run_id="r1",
                attempt=1,
                workstream_id="w1",
                verdict="FAIL",
                json_path="/evidence/w1/r1/attempt-001.json",
                protocol_error=None,
                artifact_sha256="a" * 64,
                md_path="/evidence/w1/r1/attempt-001.md",
                raw_path="/evidence/w1/r1/attempt-001.raw.txt",
            )
            await db.insert_verification_attempt(
                run_id="r1",
                attempt=2,
                workstream_id="w1",
                verdict="PASS",
                json_path="/evidence/w1/r1/attempt-002.json",
            )
            rows = await db.list_verification_attempts("r1")
            assert [r.attempt for r in rows] == [1, 2]
            assert rows[0].verdict == "FAIL"
            assert rows[0].artifact_sha256 == "a" * 64
            assert rows[0].materialized is False
            assert rows[1].verdict == "PASS"
            assert rows[1].md_path is None
            assert rows[1].raw_path is None
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_list_verification_attempts_scoped_to_run_id(self, tmp_path) -> None:
        db = Database(tmp_path / "t.db")
        await db.connect()
        try:
            await db.create_workstream(_seed_workstream("w1"))
            await db.insert_verification_attempt(
                run_id="r1",
                attempt=1,
                workstream_id="w1",
                verdict="PASS",
                json_path="/e/1.json",
            )
            await db.insert_verification_attempt(
                run_id="r2",
                attempt=1,
                workstream_id="w1",
                verdict="PASS",
                json_path="/e/2.json",
            )
            assert len(await db.list_verification_attempts("r1")) == 1
            assert len(await db.list_verification_attempts("r2")) == 1
            assert await db.list_verification_attempts("r-missing") == []
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_verification_attempts_pk_rejects_duplicate(self, tmp_path) -> None:
        db = Database(tmp_path / "t.db")
        await db.connect()
        try:
            await db.create_workstream(_seed_workstream("w1"))
            await db.insert_verification_attempt(
                run_id="r",
                attempt=1,
                workstream_id="w1",
                verdict="PASS",
                json_path="/e/1.json",
            )
            with pytest.raises(Exception):  # noqa: B017 - sqlite IntegrityError
                await db.insert_verification_attempt(
                    run_id="r",
                    attempt=1,
                    workstream_id="w1",
                    verdict="FAIL",
                    json_path="/e/1-dup.json",
                )
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_verification_attempts_verdict_check_constraint(
        self, tmp_path
    ) -> None:
        db = Database(tmp_path / "t.db")
        await db.connect()
        try:
            await db.create_workstream(_seed_workstream("w1"))
            with pytest.raises(Exception):  # noqa: B017 - sqlite IntegrityError
                await db.insert_verification_attempt(
                    run_id="r",
                    attempt=1,
                    workstream_id="w1",
                    verdict="BOGUS",
                    json_path="/e/1.json",
                )
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_mark_attempts_materialized(self, tmp_path) -> None:
        db = Database(tmp_path / "t.db")
        await db.connect()
        try:
            await db.create_workstream(_seed_workstream("w1"))
            await db.insert_verification_attempt(
                run_id="r1",
                attempt=1,
                workstream_id="w1",
                verdict="FAIL",
                json_path="/e/1.json",
            )
            await db.insert_verification_attempt(
                run_id="r1",
                attempt=2,
                workstream_id="w1",
                verdict="PASS",
                json_path="/e/2.json",
            )
            await db.insert_verification_attempt(
                run_id="r2",
                attempt=1,
                workstream_id="w1",
                verdict="PASS",
                json_path="/e/3.json",
            )

            await db.mark_attempts_materialized("r1")

            r1_rows = await db.list_verification_attempts("r1")
            assert all(r.materialized is True for r in r1_rows)
            r2_rows = await db.list_verification_attempts("r2")
            assert all(r.materialized is False for r in r2_rows)
        finally:
            await db.close()


# =============================================================================
# Migration 14: execution_phase widened to accept 'verification'
# =============================================================================


class TestExecutionPhaseVerificationMigration:
    """_migrate_execution_phase_verification widens the CHECK via rebuild."""

    @pytest.mark.anyio
    async def test_execution_phase_accepts_verification(self, tmp_path) -> None:
        db = Database(tmp_path / "t.db")
        await db.connect()
        try:
            await db.create_workstream(_seed_workstream("w1"))
            await db.start_execution(
                entity_kind="workstream",
                entity_id="w1",
                expected_status=WorkstreamStatus.READY.value,
                running_status=WorkstreamStatus.RUNNING.value,
                execution_id="e-verify",
                backend_id="sandbox",
                transport_ref="sandbox:e-verify",
                attempt=1,
                execution_phase="verification",
            )
            rows = await db.get_open_execution_handles()
            row = next(r for r in rows if r["execution_id"] == "e-verify")
            assert row["execution_phase"] == "verification"
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_execution_phase_still_rejects_invalid_value(self, tmp_path) -> None:
        db = Database(tmp_path / "t.db")
        await db.connect()
        try:
            assert db._connection is not None
            with pytest.raises(Exception):  # noqa: B017 - sqlite IntegrityError
                await db._connection.execute(
                    """
                    INSERT INTO execution_handles
                        (execution_id, entity_kind, entity_id, attempt,
                         backend_id, transport_ref, state, execution_phase,
                         created_at)
                    VALUES
                        ('bad', 'task', 't1', 1, 'local', 'ref', 'prepared',
                         'not-a-real-phase', '2026-01-01T00:00:00+00:00')
                    """
                )
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_old_execution_handles_migrate_in_place(self, tmp_path) -> None:
        """Pre-migration-14 execution_handles (task/validation CHECK only).

        Rebuild preserves existing rows and widens the CHECK, mirroring
        `test_db_migration_ssh_handles.py`'s migration-9 rebuild coverage.
        """
        db_path = tmp_path / "legacy.db"
        legacy_sql = """
        CREATE TABLE execution_handles (
            execution_id   TEXT PRIMARY KEY,
            entity_kind    TEXT NOT NULL CHECK (entity_kind IN ('task','workstream')),
            entity_id      TEXT NOT NULL,
            attempt        INTEGER NOT NULL,
            backend_id     TEXT NOT NULL,
            transport_ref  TEXT NOT NULL,
            state          TEXT NOT NULL CHECK (state IN ('prepared','running','terminal','collected','cleaned')),
            execution_phase TEXT NOT NULL DEFAULT 'task' CHECK (execution_phase IN ('task','validation')),
            created_at     TEXT NOT NULL,
            finished_at    TEXT,
            remote_host    TEXT,
            remote_dir     TEXT,
            status_marker  TEXT,
            collected_at   TEXT
        )
        """
        async with aiosqlite.connect(str(db_path)) as conn:
            await conn.execute(legacy_sql)
            await conn.execute(
                "CREATE INDEX ix_exec_state_backend ON execution_handles "
                "(state, backend_id)"
            )
            await conn.execute(
                "CREATE INDEX ix_exec_entity ON execution_handles "
                "(entity_kind, entity_id, attempt)"
            )
            await conn.execute(
                """
                INSERT INTO execution_handles
                    (execution_id, entity_kind, entity_id, attempt,
                     backend_id, transport_ref, state, execution_phase,
                     created_at)
                VALUES
                    ('e-old', 'task', 't1', 1, 'ssh', 'ref', 'terminal',
                     'validation', '2026-01-01T00:00:00+00:00')
                """
            )
            await conn.commit()

        db = Database(db_path)
        await db.connect()
        try:
            assert db._connection is not None

            # Old row survives the rebuild.
            cursor = await db._connection.execute(
                "SELECT entity_id, execution_phase FROM execution_handles "
                "WHERE execution_id = 'e-old'"
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row["entity_id"] == "t1"
            assert row["execution_phase"] == "validation"

            # Indexes survive.
            cursor = await db._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name IN ('ix_exec_state_backend', 'ix_exec_entity')"
            )
            names = {r["name"] for r in await cursor.fetchall()}
            assert names == {"ix_exec_state_backend", "ix_exec_entity"}

            # CHECK now accepts 'verification'.
            await db._connection.execute(
                """
                INSERT INTO execution_handles
                    (execution_id, entity_kind, entity_id, attempt,
                     backend_id, transport_ref, state, execution_phase,
                     created_at)
                VALUES
                    ('e-new', 'workstream', 'w1', 1, 'local', 'ref',
                     'prepared', 'verification', '2026-01-01T00:00:00+00:00')
                """
            )
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_migration_registered_and_idempotent(self, tmp_path) -> None:
        db_path = tmp_path / "m.db"
        db = Database(db_path)
        await db.connect()
        await db.close()

        db2 = Database(db_path)
        await db2.connect()
        try:
            assert db2._connection is not None
            cur = await db2._connection.execute(
                "SELECT version FROM schema_migrations WHERE version = 15"
            )
            assert await cur.fetchone() is not None
        finally:
            await db2.close()
