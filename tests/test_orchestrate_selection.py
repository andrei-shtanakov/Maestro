import pytest

from maestro.repo_identity import RepoKey
from maestro.run_bootstrap import bootstrap_run
from maestro.run_publish import create_run
from maestro.run_registry import resolve_runs


KEY = RepoKey(host="github.com", owner="acme", repo="app")


class _Config:
    repo_url = "https://github.com/acme/app"


async def test_plain_orchestrate_does_not_resume(tmp_path):
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    result = await bootstrap_run(
        _Config(), resume=False, run_id_override=None, home=tmp_path
    )
    assert result.fresh is True
    assert result.run_id != "RUN-A"


async def test_the_previous_run_survives_a_fresh_start(tmp_path):
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    await bootstrap_run(_Config(), resume=False, run_id_override=None, home=tmp_path)
    ids = {r.run_id for r in await resolve_runs(KEY, home=tmp_path, lock_root=tmp_path)}
    assert "RUN-A" in ids
    assert len(ids) == 2


async def test_plain_orchestrate_refuses_while_a_run_is_live(tmp_path):
    from maestro.run_bootstrap import RunIsLive
    from maestro.service.locks import ScopedLock

    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    with (
        ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A", root=tmp_path),
        pytest.raises(RunIsLive),
    ):
        await bootstrap_run(
            _Config(), resume=False, run_id_override=None, home=tmp_path
        )
