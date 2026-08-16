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
    try:
        assert await db2.get_run_row() is None
    finally:
        await db2.close()


async def test_a_database_with_a_row_is_not_legacy(tmp_path):
    path = tmp_path / "state.db"
    db = await create_database(path)
    await db.create_run_row(
        run_id="RUN-A", repo_key="k", started_at="2026-08-15T09:00:00+00:00"
    )
    await db.close()
    info = await describe_database(path)
    assert info.status == "interrupted"
    assert info.run_id == "RUN-A"
