import stat

from maestro.database import Database, create_database
from maestro.repo_identity import RepoKey
from maestro.run_publish import create_run
from maestro.state_paths import project_dir, runs_dir


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


async def test_an_explicitly_passed_home_gets_the_privacy_repair(tmp_path):
    """`home=` must reach `ensure_private_dir`, not just the path builders.

    The repair is bounded by the maestro home, so a `home=` that stops at
    `create_run` leaves the boundary at `maestro_home()`. Every path then
    looks "outside the home", only the leaf is chmod-ed, and a pre-existing
    0755 ancestor stays 0755 — the exact compounding this repair exists for,
    reintroduced for anyone who injects a home.
    """
    home = tmp_path / "home"
    project = project_dir(KEY, home=home)
    project.mkdir(parents=True)
    for wrong in (home, home / "projects", project):
        wrong.chmod(0o755)

    await create_run(KEY, "RUN-A", repo_key_text="k", started_at=STARTED, home=home)

    offenders = {
        str(p): oct(stat.S_IMODE(p.stat().st_mode))
        for p in (home, home / "projects", project, runs_dir(KEY, home=home))
        if stat.S_IMODE(p.stat().st_mode) != 0o700
    }
    assert offenders == {}


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


async def test_no_dot_prefixed_staging_entries_under_runs(tmp_path):
    """`Path.iterdir()` does not skip dot-prefixed entries the way shell
    globbing does, so staging must never live inside `runs/` — a sibling
    `.RUN-A.partial` there would be visible to any collector."""
    await create_run(KEY, "RUN-A", repo_key_text="k", started_at=STARTED, home=tmp_path)
    names = sorted(p.name for p in runs_dir(KEY, home=tmp_path).iterdir())
    assert names == ["RUN-A"]
    assert not any(name.startswith(".") for name in names)


async def test_runs_dir_is_empty_during_the_build_window(tmp_path, monkeypatch):
    """Observe `runs/` from inside the write of the run row itself: the
    directory must be either absent or empty at that point, never holding a
    database whose row has not landed yet."""
    original_create_run_row = Database.create_run_row
    observed: list[list[str]] = []

    async def spying_create_run_row(self, **kwargs):
        runs = runs_dir(KEY, home=tmp_path)
        observed.append([p.name for p in runs.iterdir()] if runs.exists() else [])
        return await original_create_run_row(self, **kwargs)

    monkeypatch.setattr(Database, "create_run_row", spying_create_run_row)

    await create_run(KEY, "RUN-A", repo_key_text="k", started_at=STARTED, home=tmp_path)

    assert observed == [[]]


async def test_create_run_persists_branch_binding(tmp_path):
    """create_run forwards run-branch binding kwargs to create_run_row."""
    path = await create_run(
        KEY,
        "RUN-A",
        repo_key_text="github.com/acme/app",
        started_at=STARTED,
        home=tmp_path,
        run_branch="pilot/x",
        run_branch_declared=1,
        run_branch_head="a" * 40,
    )
    db = await create_database(path)
    try:
        row = await db.get_run_row()
        assert row is not None and row["run_branch"] == "pilot/x"
        assert row["run_branch_declared"] == 1
        assert row["run_branch_head"] == "a" * 40
    finally:
        await db.close()
