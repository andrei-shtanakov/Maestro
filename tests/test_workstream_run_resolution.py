"""Workstream commands resolve `(repository, run)` before they open a database.

Spec §C.3: a workstream id is unique per database, not per repository, so a
command that took only a workstream id would pick a database by accident once
state is per-run. Spec §D: `~/.maestro` reports its own size so growth becomes
visible before it becomes a problem.
"""

import pytest

from maestro.repo_identity import RepoKey
from maestro.run_publish import create_run
from maestro.run_registry import AmbiguousRun, home_usage, resolve_run_for_command


KEY = RepoKey(host="github.com", owner="acme", repo="app")


async def test_single_run_resolves_without_a_flag(tmp_path):
    await create_run(
        KEY,
        "RUN-A",
        repo_key_text="k",
        started_at="2026-08-15T09:00:00+00:00",
        home=tmp_path,
    )
    info = await resolve_run_for_command(KEY, home=tmp_path, lock_root=tmp_path)
    assert info.run_id == "RUN-A"


async def test_two_runs_require_an_explicit_choice(tmp_path):
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
    with pytest.raises(AmbiguousRun):
        await resolve_run_for_command(KEY, home=tmp_path, lock_root=tmp_path)


async def test_explicit_run_id_wins(tmp_path):
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
    info = await resolve_run_for_command(
        KEY, run_id="RUN-A", home=tmp_path, lock_root=tmp_path
    )
    assert info.run_id == "RUN-A"


async def test_home_usage_counts_runs_and_bytes(tmp_path):
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
    usage = home_usage(home=tmp_path)
    assert len(usage) == 1
    key, run_count, size = usage[0]
    assert key.as_path_parts() == KEY.as_path_parts()
    assert run_count == 2
    assert size > 0
