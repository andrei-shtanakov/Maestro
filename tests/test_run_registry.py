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
    path = await create_run(
        KEY, run_id, repo_key_text="k", started_at=started_at, home=tmp_path
    )
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

    await _make(tmp_path, "AAA", "2026-08-15T09:00:00+00:00")  # dead
    await _make(tmp_path, "BBB", "2026-08-15T10:00:00+00:00")  # will hold the lock
    with ScopedLock(key=KEY, stage="orchestrate", run_id="BBB", root=tmp_path):
        runs = {
            r.run_id: r.status
            for r in await resolve_runs(KEY, home=tmp_path, lock_root=tmp_path)
        }
    assert runs["BBB"] == "running"
    assert runs["AAA"] == "interrupted"
