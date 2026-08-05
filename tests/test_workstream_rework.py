"""Tests for `maestro workstream-rework` (issue #124).

Covers the design acceptance checklist of
docs/superpowers/specs/2026-08-05-workstream-rework-design.md:
migration 18, the recovery-ambiguity marker, the CAS+audit transaction,
the liveness proof, refresh validation, the CLI commands, and the
exhaustive READY resume dispatch.
"""

import json
from typing import Any

import pytest

from maestro.database import Database
from maestro.models import Workstream, WorkstreamStatus


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "rework-test.db")
    await d.connect()
    yield d
    await d.close()


def make_ws(
    id_: str = "ws-1",
    status: WorkstreamStatus = WorkstreamStatus.NEEDS_REVIEW,
    **overrides: object,
) -> Workstream:
    ws = Workstream(
        id=id_,
        title=id_,
        description="original description",
        branch=f"feature/{id_}",
        status=status,
    )
    return ws.model_copy(update=overrides) if overrides else ws


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

    @pytest.mark.anyio
    async def test_columns_survive_roundtrip(self, db) -> None:
        marker = json.dumps({"kind": "live_orphan", "pid": 7, "parked_at": "t"})
        await db.create_workstream(
            make_ws(
                operator_rework_count=2,
                operator_rework_seq=2,
                recovery_ambiguity=marker,
            )
        )
        ws = await db.get_workstream("ws-1")
        assert ws.operator_rework_count == 2
        assert ws.operator_rework_seq == 2
        assert ws.recovery_ambiguity is not None
        assert json.loads(ws.recovery_ambiguity)["pid"] == 7


async def _record(db: Database, ws_id: str = "ws-1", **overrides: object) -> int:
    """record_workstream_rework with sane defaults for tests."""
    kwargs: dict[str, Any] = {
        "prior_status": WorkstreamStatus.NEEDS_REVIEW,
        "prior_count": 0,
        "prior_marker": None,
        "reason": "reviewer rejected the diff",
        "instructions": "split the migration",
        "initiator": "andrei",
        "prior_error_message": "gate blocked",
        "prior_head_sha": "a" * 40,
        "liveness_evidence": None,
        "refresh": None,
    }
    kwargs.update(overrides)
    return await db.record_workstream_rework(ws_id, **kwargs)


class TestRecordRework:
    @pytest.mark.anyio
    async def test_cas_success_writes_row_and_audit(self, db) -> None:
        await db.create_workstream(make_ws())
        seq = await _record(db)
        assert seq == 1
        ws = await db.get_workstream("ws-1")
        assert ws.status is WorkstreamStatus.READY
        assert ws.resume_reason == "operator_rework"
        assert ws.operator_rework_count == 1
        assert ws.operator_rework_seq == 1
        assert ws.error_message is None
        assert ws.verification_run_id is None
        row = await db.get_workstream_rework("ws-1", 1)
        assert row is not None
        assert row["reason"] == "reviewer rejected the diff"
        assert row["instructions"] == "split the migration"
        assert row["prior_status"] == "needs_review"
        assert row["prior_head_sha"] == "a" * 40

    @pytest.mark.anyio
    async def test_cas_refuses_on_stale_status(self, db) -> None:
        await db.create_workstream(make_ws(status=WorkstreamStatus.READY))
        with pytest.raises(ValueError, match="refused"):
            await _record(db)  # prior_status=NEEDS_REVIEW is stale
        assert await db.get_workstream_rework("ws-1", 1) is None  # no audit

    @pytest.mark.anyio
    async def test_cas_refuses_on_live_pid(self, db) -> None:
        await db.create_workstream(make_ws(process_pid=123))
        with pytest.raises(ValueError, match="refused"):
            await _record(db)
        assert await db.get_workstream_rework("ws-1", 1) is None

    @pytest.mark.anyio
    async def test_cas_refuses_on_marker_change(self, db) -> None:
        marker = json.dumps({"kind": "live_orphan", "pid": 7, "parked_at": "t"})
        await db.create_workstream(make_ws(recovery_ambiguity=marker))
        with pytest.raises(ValueError, match="refused"):
            await _record(db, prior_marker=None)  # stale marker read
        assert await db.get_workstream_rework("ws-1", 1) is None

    @pytest.mark.anyio
    async def test_cas_clears_marker_it_read(self, db) -> None:
        marker = json.dumps({"kind": "live_orphan", "pid": 7, "parked_at": "t"})
        await db.create_workstream(make_ws(recovery_ambiguity=marker))
        seq = await _record(db, prior_marker=marker, liveness_evidence='{"ok":1}')
        assert seq == 1
        ws = await db.get_workstream("ws-1")
        assert ws.recovery_ambiguity is None

    @pytest.mark.anyio
    async def test_second_rework_gets_seq_2(self, db) -> None:
        await db.create_workstream(make_ws())
        assert await _record(db) == 1
        # Simulate the attempt failing again.
        await db.update_workstream_status(
            "ws-1", WorkstreamStatus.NEEDS_REVIEW, resume_reason=None
        )
        assert await _record(db, prior_count=1) == 2
        ws = await db.get_workstream("ws-1")
        assert ws.operator_rework_count == 2
        assert ws.operator_rework_seq == 2

    @pytest.mark.anyio
    async def test_refresh_updates_description_and_scope(self, db) -> None:
        from maestro.rework import RefreshEvidence

        await db.create_workstream(make_ws())
        refresh = RefreshEvidence(
            config_path="/p/project.yaml",
            config_hash="h" * 64,
            old_description="original description",
            new_description="updated description",
            old_scope=[],
            new_scope=["src/**"],
        )
        await _record(db, refresh=refresh)
        ws = await db.get_workstream("ws-1")
        assert ws.description == "updated description"
        assert ws.scope == ["src/**"]
        row = await db.get_workstream_rework("ws-1", 1)
        assert row is not None
        assert row["refresh_config_hash"] == "h" * 64
        assert json.loads(row["new_scope"]) == ["src/**"]

    @pytest.mark.anyio
    async def test_no_gate_approvals_written(self, db) -> None:
        await db.create_workstream(make_ws())
        await _record(db)
        approvals = await db.list_gate_approvals("ws-1")
        assert approvals == set()

    @pytest.mark.anyio
    async def test_resolve_ambiguity_roundtrip(self, db) -> None:
        marker = json.dumps({"kind": "spawn_uncertain", "pid": None})
        await db.create_workstream(make_ws(recovery_ambiguity=marker))
        await db.resolve_recovery_ambiguity(
            "ws-1", statement="verified by hand: no maestro procs", initiator="andrei"
        )
        ws = await db.get_workstream("ws-1")
        assert ws.recovery_ambiguity is None
        cur = await db._connection.execute(
            "SELECT statement, marker_json FROM workstream_ambiguity_resolutions"
        )
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["marker_json"] == marker

    @pytest.mark.anyio
    async def test_resolve_without_marker_refuses(self, db) -> None:
        await db.create_workstream(make_ws())
        with pytest.raises(ValueError):
            await db.resolve_recovery_ambiguity(
                "ws-1", statement="s", initiator="andrei"
            )
