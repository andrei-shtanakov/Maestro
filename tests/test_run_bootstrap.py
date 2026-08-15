import os

import pytest

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
