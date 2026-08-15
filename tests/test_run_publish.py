import stat

from maestro.database import create_database
from maestro.repo_identity import RepoKey
from maestro.run_publish import create_run
from maestro.state_paths import runs_dir


KEY = RepoKey(host="github.com", owner="acme", repo="app")
STARTED = "2026-08-15T10:00:00+00:00"


async def test_creates_a_run_directory_with_a_row(tmp_path):
    path = await create_run(
        KEY,
        "RUN-A",
        repo_key_text="github.com/acme/app",
        started_at=STARTED,
        home=tmp_path,
    )
    assert path == runs_dir(KEY, home=tmp_path) / "RUN-A" / "state.db"
    db = await create_database(path)
    try:
        row = await db.get_run_row()
        assert row is not None
        assert row["run_id"] == "RUN-A"
    finally:
        await db.close()


async def test_no_wal_or_shm_survives_publication(tmp_path):
    path = await create_run(
        KEY, "RUN-A", repo_key_text="k", started_at=STARTED, home=tmp_path
    )
    assert not path.with_name("state.db-wal").exists()
    assert not path.with_name("state.db-shm").exists()


async def test_nothing_is_visible_under_runs_until_complete(tmp_path):
    await create_run(KEY, "RUN-A", repo_key_text="k", started_at=STARTED, home=tmp_path)
    entries = list(runs_dir(KEY, home=tmp_path).iterdir())
    assert [e.name for e in entries] == ["RUN-A"]
    # every published directory has its row
    assert (entries[0] / "state.db").exists()


async def test_permissions_are_private(tmp_path):
    path = await create_run(
        KEY, "RUN-A", repo_key_text="k", started_at=STARTED, home=tmp_path
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


async def test_logs_directory_is_created(tmp_path):
    path = await create_run(
        KEY, "RUN-A", repo_key_text="k", started_at=STARTED, home=tmp_path
    )
    assert (path.parent / "logs").is_dir()
