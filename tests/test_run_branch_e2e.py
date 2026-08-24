"""E2E: git.run_branch on a real repo with the announce agent (spec §4-§6).

No mocks: `_run_scheduler` is driven exactly as the CLI drives it, over a
real git repository and a real (fast, no-op) `announce` spawner. Unlike
`tests/test_cli.py`'s `TestRunBranchGateStart`/`TestOnAutoCommitWiring`
(which stub the scheduler or the bootstrap seam), this file proves the
whole stack wires together — gate, bootstrap-free `--db` path, and the
scheduler main loop — end to end.
"""

import subprocess
from pathlib import Path

import yaml

from maestro.cli import _run_scheduler


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


def _write_config(base_dir: Path, repo: Path, *, git_block: dict) -> Path:
    """Minimal scheduler (mode-1) config with one announce task — the
    consumer's-eye equivalent of `test_cli.py`'s `_write_scheduler_config`
    extended with a `git:` block, local to this file per the brief."""
    config = {
        "project": "run-branch-e2e",
        "repo": str(repo),
        "tasks": [
            {"id": "t1", "title": "T1", "prompt": "hi", "agent_type": "announce"},
        ],
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
