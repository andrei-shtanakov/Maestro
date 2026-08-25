"""E2E: git.run_branch on a real repo with the announce agent (spec §4-§6).

No mocks: `_run_scheduler` is driven exactly as the CLI drives it, over a
real git repository and a real (fast, no-op) `announce` spawner. Unlike
`tests/test_cli.py`'s `TestRunBranchGateStart`/`TestOnAutoCommitWiring`
(which stub the scheduler or the bootstrap seam), this file proves the
whole stack wires together — gate, bootstrap-free `--db` path, and the
scheduler main loop — end to end.
"""

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
import yaml

from maestro.cli import _run_scheduler
from maestro.database import create_database
from maestro.models import TaskStatus


@pytest.fixture(autouse=True)
def _no_structlog_logger_caching() -> Iterator[None]:
    """Keep these tests from poisoning `structlog`-based tests that follow.

    `obs.init_logging` (reached here through `setup_logging`, which earlier
    CLI tests trigger) configures structlog with
    `cache_logger_on_first_use=True`. A module-level lazy proxy —
    `scheduler.py`'s `_obs_log` — then *caches* whatever pipeline is
    configured on its first emission, for the rest of the process. This file
    is the first test to emit through that proxy under a real run, so the
    JSONL pipeline got baked in and a later `structlog.testing.capture_logs()`
    in `tests/test_scheduler.py` saw nothing (green alone, red in the full
    suite). Emitting with caching off leaves the proxy re-reading the config
    every time, which is what `capture_logs` needs.
    """
    was_configured = structlog.is_configured()
    saved: dict[str, Any] = dict(structlog.get_config())
    uncached: dict[str, Any] = dict(saved)
    uncached["cache_logger_on_first_use"] = False
    structlog.configure(**uncached)
    try:
        yield
    finally:
        if was_configured:
            structlog.configure(**saved)
        else:
            structlog.reset_defaults()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    """git init -b master + one commit, mirroring test_run_branch_gate.py's
    `repo` fixture / test_cli.py's `_init_git_repo`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    return repo


def _write_config(
    base_dir: Path,
    repo: Path,
    *,
    git_block: dict,
    validation_cmd: str | None = None,
) -> Path:
    """Minimal scheduler (mode-1) config with one announce task — the
    consumer's-eye equivalent of `test_cli.py`'s `_write_scheduler_config`
    extended with a `git:` block, local to this file per the brief.

    ``validation_cmd``, when given, is the one way to get a real file
    change out of the `announce` agent (which only echoes) — it runs as a
    plain argv in the task's workdir (`maestro/validator.py`), so
    `"touch new.txt"` gives `auto_commit` something to actually land.
    """
    task: dict[str, Any] = {
        "id": "t1",
        "title": "T1",
        "prompt": "hi",
        "agent_type": "announce",
    }
    if validation_cmd is not None:
        task["validation_cmd"] = validation_cmd
    config = {
        "project": "run-branch-e2e",
        "repo": str(repo),
        "tasks": [task],
        "git": git_block,
    }
    config_path = base_dir / "project.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(config, f)
    return config_path


async def test_fresh_run_lands_commits_on_run_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    master_tip_before = _git(repo, "rev-parse", "master")
    config_path = _write_config(
        tmp_path,
        repo,
        git_block={
            "base_branch": "master",
            "auto_commit": True,
            "run_branch": "pilot/e2e",
        },
    )
    db_path = tmp_path / "state" / "run.db"
    db_path.parent.mkdir()

    await _run_scheduler(
        config_path=config_path, db_path=db_path, resume=False, log_dir=None
    )

    # The checkout ends on the run branch, never back on base — this is the
    # isolation claim: an operator inspecting the repo after the run finds it
    # parked on `pilot/e2e`, not on `master`.
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "pilot/e2e"
    # `master` itself is untouched by the run (the announce agent changes no
    # files, so `auto_commit` may or may not produce a commit on the run
    # branch — that is irrelevant to the isolation claim under test).
    assert _git(repo, "rev-parse", "master") == master_tip_before


async def test_second_fresh_run_iterates_on_same_branch(tmp_path: Path) -> None:
    """Run once, then a second fresh run (a second --db, same config) lands
    on the `cur == run_branch, target_exists, clean` PROCEED row of the
    start matrix — the consumer's iteration pattern (spec §4)."""
    repo = _init_repo(tmp_path)
    config_path = _write_config(
        tmp_path,
        repo,
        git_block={
            "base_branch": "master",
            "auto_commit": True,
            "run_branch": "pilot/e2e",
        },
    )

    first_db = tmp_path / "state" / "first.db"
    first_db.parent.mkdir()
    await _run_scheduler(
        config_path=config_path, db_path=first_db, resume=False, log_dir=None
    )
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "pilot/e2e"

    second_db = tmp_path / "state" / "second.db"
    await _run_scheduler(
        config_path=config_path, db_path=second_db, resume=False, log_dir=None
    )

    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "pilot/e2e"


async def test_gated_run_completes_green_with_tripwire_armed(tmp_path: Path) -> None:
    """Task 7: no false trip. The §7 tripwires fire at every seam of a
    gated run (spawn, validation, success_finalize) — this proves arming
    them end to end does not perturb an otherwise-clean run. A deterministic
    mid-run flip is covered at the scheduler level
    (`tests/test_run_branch_tripwire.py`); an e2e flip would be a race.

    `validation_cmd="touch new.txt"` is the one way to get a real file
    change out of the `announce` agent (which only echoes its prompt), so
    `auto_commit` has something to actually land on `pilot/e2e` — proving
    the whole chain reaches `DONE` with the tripwire armed, not just that
    it doesn't crash.
    """
    repo = _init_repo(tmp_path)
    config_path = _write_config(
        tmp_path,
        repo,
        git_block={
            "base_branch": "master",
            "auto_commit": True,
            "run_branch": "pilot/e2e",
        },
        validation_cmd="touch new.txt",
    )
    db_path = tmp_path / "state" / "run.db"
    db_path.parent.mkdir()

    await _run_scheduler(
        config_path=config_path, db_path=db_path, resume=False, log_dir=None
    )

    tip = _git(repo, "rev-parse", "pilot/e2e")
    # `init` plus exactly one commit the run made — the commit landed.
    assert _git(repo, "rev-list", "--count", "pilot/e2e") == "2"

    db = await create_database(db_path)
    try:
        tasks = await db.get_all_tasks()
        run_row = await db.get_run_row()
    finally:
        await db.close()

    assert len(tasks) == 1
    assert tasks[0].status == TaskStatus.DONE
    assert run_row is not None
    assert run_row["run_branch_head"] == tip
    assert run_row["suspended_at"] is None
