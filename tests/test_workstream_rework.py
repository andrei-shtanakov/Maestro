"""Tests for `maestro workstream-rework` (issue #124).

Covers the design acceptance checklist of
docs/superpowers/specs/2026-08-05-workstream-rework-design.md:
migration 18, the recovery-ambiguity marker, the CAS+audit transaction,
the liveness proof, refresh validation, the CLI commands, and the
exhaustive READY resume dispatch.
"""

import json

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
