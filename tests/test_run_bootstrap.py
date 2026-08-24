import os

import pytest

from maestro.database import create_database
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
        _Config("https://github.com/acme/app"),
        resume=False,
        run_id_override=None,
        home=tmp_path,
    )
    assert result.fresh is True
    assert result.db_path.exists()
    assert os.environ["ORCHESTRA_PIPELINE_ID"] == result.run_id


async def test_resume_reuses_the_existing_run_id(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCHESTRA_PIPELINE_ID", raising=False)
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="github.com/acme/app",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    result = await bootstrap_run(
        _Config("https://github.com/acme/app"),
        resume=True,
        run_id_override=None,
        home=tmp_path,
    )
    assert result.fresh is False
    assert result.run_id == "RUN-A"
    assert os.environ["ORCHESTRA_PIPELINE_ID"] == "RUN-A"


async def test_resume_with_no_runs_refuses(tmp_path):
    from maestro.run_registry import NoResumableRun

    with pytest.raises(NoResumableRun):
        await bootstrap_run(
            _Config("https://github.com/acme/app"),
            resume=True,
            run_id_override=None,
            home=tmp_path,
        )


async def test_unknown_run_override_refuses_and_names_the_known_ids(tmp_path):
    """A typo in `--run` is a refusal, never `RuntimeError: coroutine raised
    StopIteration` — which is what a bare `next(...)` produced here."""
    from maestro.run_registry import NoResumableRun

    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )

    with pytest.raises(NoResumableRun) as excinfo:
        await bootstrap_run(
            _Config("https://github.com/acme/app"),
            resume=False,
            run_id_override="RUN-Z",
            home=tmp_path,
        )

    assert "RUN-Z" in str(excinfo.value)
    assert "known runs: RUN-A" in str(excinfo.value)


async def test_unknown_run_override_on_an_empty_repository_says_none(tmp_path):
    from maestro.run_registry import NoResumableRun

    with pytest.raises(NoResumableRun, match="known runs: none"):
        await bootstrap_run(
            _Config("https://github.com/acme/app"),
            resume=False,
            run_id_override="RUN-Z",
            home=tmp_path,
        )


async def test_unresolvable_identity_refuses(tmp_path):
    from maestro.repo_identity import IdentityError

    with pytest.raises(IdentityError):
        await bootstrap_run(
            _Config("not-a-url"), resume=False, run_id_override=None, home=tmp_path
        )


async def test_run_override_selects_that_run(tmp_path):
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    await create_run(
        KEY,
        "RUN-B",
        repo_key_text="k",
        started_at="2026-08-15T10:00:00+00:00",
        home=tmp_path,
    )
    result = await bootstrap_run(
        _Config("https://github.com/acme/app"),
        resume=True,
        run_id_override="RUN-A",
        home=tmp_path,
    )
    assert result.run_id == "RUN-A"


async def test_a_resume_creates_no_second_run_directory(tmp_path):
    """A service tick resumes; it must never mint a run (spec §A.1)."""
    from maestro.state_paths import runs_dir

    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    before = sorted(p.name for p in runs_dir(KEY, home=tmp_path).iterdir())
    await bootstrap_run(
        _Config("https://github.com/acme/app"),
        resume=True,
        run_id_override=None,
        home=tmp_path,
    )
    after = sorted(p.name for p in runs_dir(KEY, home=tmp_path).iterdir())
    assert before == after == ["RUN-A"]


async def test_pre_publish_runs_before_create_run_and_feeds_row(tmp_path, monkeypatch):
    """Task 6 seam: `pre_publish`'s return dict is splatted into `create_run`,
    so the published row carries the run-branch binding it computed."""
    monkeypatch.delenv("ORCHESTRA_PIPELINE_ID", raising=False)

    async def _pre_publish(run_id: str) -> dict[str, object]:
        assert run_id  # the minted id, already available before create_run
        return {
            "run_branch": "pilot/x",
            "run_branch_declared": 1,
            "run_branch_head": "a" * 40,
        }

    result = await bootstrap_run(
        _Config("https://github.com/acme/app"),
        resume=False,
        run_id_override=None,
        home=tmp_path,
        pre_publish=_pre_publish,
    )

    db = await create_database(result.db_path)
    try:
        row = await db.get_run_row()
    finally:
        await db.close()
    assert row is not None
    assert row["run_branch"] == "pilot/x"
    assert row["run_branch_declared"] == 1
    assert row["run_branch_head"] == "a" * 40


async def test_pre_publish_exception_publishes_nothing(tmp_path, monkeypatch):
    """A `pre_publish` failure aborts publication: nothing is staged, so a
    later resolve sees no run at all (spec: the gate runs before the run
    becomes discoverable)."""
    monkeypatch.delenv("ORCHESTRA_PIPELINE_ID", raising=False)

    async def _pre_publish(run_id: str) -> dict[str, object]:
        raise RuntimeError("gate refused")

    with pytest.raises(RuntimeError, match="gate refused"):
        await bootstrap_run(
            _Config("https://github.com/acme/app"),
            resume=False,
            run_id_override=None,
            home=tmp_path,
            pre_publish=_pre_publish,
        )

    from maestro.run_registry import resolve_runs

    runs = await resolve_runs(KEY, home=tmp_path, lock_root=tmp_path)
    assert runs == []


async def test_pre_publish_not_called_on_resume(tmp_path, monkeypatch):
    """The seam is fresh-path only: a resume must never invoke it."""
    from unittest.mock import AsyncMock

    monkeypatch.delenv("ORCHESTRA_PIPELINE_ID", raising=False)
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="github.com/acme/app",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )

    pre_publish = AsyncMock()
    result = await bootstrap_run(
        _Config("https://github.com/acme/app"),
        resume=True,
        run_id_override=None,
        home=tmp_path,
        pre_publish=pre_publish,
    )

    assert result.run_id == "RUN-A"
    pre_publish.assert_not_called()
