"""A run records its own ending, so a second orchestration is not a cascade.

The defect this file exists for was invisible to any single-task review:
`set_run_outcome` and `set_run_suspended` were built and unit-tested (see
`tests/test_database_run_row.py`) and then never called, so `outcome` stayed
NULL on every run that ever finished. `classify_run` reported each of them
`interrupted`, a second `orchestrate` minted run 2, and from that instant
`select_resumable` saw two candidates and raised `AmbiguousRun` — which exits
1 in all fifteen resolving commands **and on every `maestro service run`
tick**. A unit test on `set_run_outcome` cannot see any of that; the test that
matters is the two orchestrations, end to end.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config import load_orchestrator_config
from maestro.database import create_database
from maestro.models import Workstream, WorkstreamStatus
from maestro.repo_identity import RepoKey
from maestro.run_bootstrap import RunIsLive, bootstrap_run
from maestro.run_lifecycle import (
    RunConclusion,
    conclude_run,
    conclusion_for_workstreams,
    record_conclusion,
)
from maestro.run_publish import create_run
from maestro.run_registry import (
    AllRunsTerminal,
    AmbiguousRun,
    describe_database,
    resolve_runs,
    select_resumable,
)
from maestro.service.locks import read_holder_run_id, stage_lock_path


if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from pathlib import Path

    from maestro.database import Database


KEY = RepoKey(host="github.com", owner="acme", repo="app")

runner = CliRunner()


def _write_orchestrator_config(base_dir: Path, repo_url: str) -> Path:
    """A minimal, preflight-valid `orchestrate` config (as in
    `tests/test_orchestrate_selection.py`)."""
    repo_dir = base_dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
    workspace_dir = base_dir / "workspaces"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "project": "orchestrator-test",
        "repo_url": repo_url,
        "repo_path": str(repo_dir),
        "workspace_base": str(workspace_dir),
        "max_concurrent": 1,
        "workstreams": [
            {
                "id": "z-new",
                "title": "New Workstream",
                "description": "Do work",
                "scope": ["*"],
            }
        ],
    }
    config_path = base_dir / "project.yaml"
    with config_path.open("w") as f:
        yaml.safe_dump(config, f)
    return config_path


def _invoke_orchestrate(
    config_path: Path,
    *extra: str,
    seed: Callable[[Database], Coroutine[Any, Any, None]] | None = None,
    failed: int = 0,
) -> Any:
    """Drive the real `orchestrate` command with a stubbed Orchestrator.

    Everything the run lifecycle touches — bootstrap, the run directory, the
    real database, the conclusion — runs for real; only the agent-spawning
    core is replaced. `seed` runs *inside* the stubbed `Orchestrator.run()`,
    against the run's own database, so the final workstream table is whatever
    the test says the run left behind.
    """
    stats = SimpleNamespace(
        total_workstreams=1, completed=1 - failed, failed=failed, prs_created=0
    )
    captured: dict[str, Any] = {}
    real_create_database = create_database

    async def _capture(path: Path) -> Database:
        db = await real_create_database(path)
        captured["db"] = db
        captured["path"] = path
        return db

    async def _stub_run() -> SimpleNamespace:
        if seed is not None:
            await seed(captured["db"])
        return stats

    with (
        patch("maestro.cli.create_database", side_effect=_capture),
        patch("maestro.cli.GitManager") as mock_git_mgr,
        patch("maestro.cli.WorkspaceManager"),
        patch("maestro.cli.ProjectDecomposer"),
        patch("maestro.cli.PRManager"),
        patch("maestro.cli.Orchestrator") as mock_orchestrator,
        patch("maestro.cli._acquire_pid_lock", return_value=99),
        patch("maestro.cli._release_pid_lock"),
    ):
        mock_git_mgr.return_value.repo_path = config_path.parent
        instance = MagicMock()
        instance.run = AsyncMock(side_effect=_stub_run)
        mock_orchestrator.return_value = instance
        return runner.invoke(app, ["orchestrate", str(config_path), *extra])


def _seed_workstream(
    workstream_id: str, status: WorkstreamStatus
) -> Callable[[Database], Coroutine[Any, Any, None]]:
    async def _seed(db: Database) -> None:
        await db.create_workstream(
            Workstream(
                id=workstream_id,
                title=workstream_id,
                description="seeded by the test",
                branch=f"feature/{workstream_id}",
                status=status,
            )
        )

    return _seed


def _statuses(home: Path) -> dict[str | None, str]:
    runs = asyncio.run(resolve_runs(KEY, home=home, lock_root=home))
    return {r.run_id: r.status for r in runs}


# =============================================================================
# The cascade itself. Two orchestrations of one repository, end to end.
# =============================================================================


def test_two_orchestrations_do_not_leave_the_repository_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Orchestrate twice; the repository must still be resolvable.

    Before the fix this failed at `select_resumable`: run 1 finished with
    `outcome` NULL, was classified `interrupted`, and run 2 made two
    candidates. `AmbiguousRun` is what every resolving command — and every
    scheduled tick — then died of.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")

    first = _invoke_orchestrate(config_path)
    assert first.exit_code == 0, first.output
    second = _invoke_orchestrate(config_path)
    assert second.exit_code == 0, second.output

    statuses = _statuses(home)
    assert len(statuses) == 2, statuses
    assert sorted(statuses.values()) == ["completed", "completed"]

    # The property under test, stated as the failure it prevents: resolution
    # still answers, rather than refusing because two runs look open.
    with pytest.raises(AllRunsTerminal):
        select_resumable(asyncio.run(resolve_runs(KEY, home=home, lock_root=home)))

    # And the operator-facing half: a resolving command does not exit 1
    # demanding `--run`.
    listing = runner.invoke(
        app, ["workstreams", "--config", str(config_path)], catch_exceptions=False
    )
    assert listing.exit_code == 0, listing.output


def test_a_finished_run_does_not_announce_leftovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 'leaving N non-terminal run(s) behind' warning is for real
    residue, and a completed run is not residue."""
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")

    _invoke_orchestrate(config_path)
    second = _invoke_orchestrate(config_path)

    assert "non-terminal run(s) behind" not in second.output


def test_an_interrupted_run_is_left_as_evidence_not_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely interrupted run keeps saying so, and the fresh run names
    the remedy instead of deciding for the operator (spec §B.3)."""
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")
    asyncio.run(
        create_run(
            KEY,
            "RUN-DIED",
            repo_key_text="github.com/acme/app",
            started_at="2026-08-15T09:00:00+00:00",
            home=home,
        )
    )

    fresh = _invoke_orchestrate(config_path)

    assert "RUN-DIED" in fresh.output
    assert "run-end" in fresh.output
    assert _statuses(home)["RUN-DIED"] == "interrupted"


def test_run_end_resolves_the_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`maestro run-end` is the escape hatch the refusal points at.

    Two genuinely interrupted runs — the residue the fresh-run path
    deliberately does not clean up on its own — are ambiguous until the
    operator decides. That decision is what this command records.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")
    for run_id, started in (
        ("RUN-DIED-A", "2026-08-15T09:00:00+00:00"),
        ("RUN-DIED-B", "2026-08-15T10:00:00+00:00"),
    ):
        asyncio.run(
            create_run(
                KEY,
                run_id,
                repo_key_text="github.com/acme/app",
                started_at=started,
                home=home,
            )
        )
    with pytest.raises(AmbiguousRun):
        select_resumable(asyncio.run(resolve_runs(KEY, home=home, lock_root=home)))

    ended = runner.invoke(
        app,
        [
            "run-end",
            "RUN-DIED-A",
            "--outcome",
            "superseded",
            "--config",
            str(config_path),
        ],
    )

    assert ended.exit_code == 0, ended.output
    assert _statuses(home)["RUN-DIED-A"] == "superseded"
    chosen = select_resumable(asyncio.run(resolve_runs(KEY, home=home, lock_root=home)))
    assert chosen.run_id == "RUN-DIED-B"


def test_run_end_refuses_an_outcome_the_run_owns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")
    asyncio.run(
        create_run(
            KEY,
            "RUN-A",
            repo_key_text="github.com/acme/app",
            started_at="2026-08-15T09:00:00+00:00",
            home=home,
        )
    )

    result = runner.invoke(
        app,
        ["run-end", "RUN-A", "--outcome", "completed", "--config", str(config_path)],
    )

    assert result.exit_code == 1
    assert "Unknown outcome" in result.stderr
    assert _statuses(home)["RUN-A"] == "interrupted"


def test_run_end_refuses_a_run_that_already_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§G: a run is completed exactly once."""
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")
    _invoke_orchestrate(config_path)
    run_id = next(iter(_statuses(home)))
    assert run_id is not None

    result = runner.invoke(
        app,
        ["run-end", run_id, "--outcome", "cancelled", "--config", str(config_path)],
    )

    assert result.exit_code == 1
    assert "recorded an outcome" in result.stderr
    assert _statuses(home)[run_id] == "completed"


# =============================================================================
# The three levels of §B.1.1, driven through the real command.
# =============================================================================


def test_a_needs_human_pause_suspends_without_ending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`needs_human` is not a run outcome: `suspended_at` is set, `ended_at`
    stays NULL, and the same run id remains resumable (spec §B.1.1, §G)."""
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")

    result = _invoke_orchestrate(
        config_path, seed=_seed_workstream("z-1", WorkstreamStatus.NEEDS_REVIEW)
    )

    assert result.exit_code == 0, result.output
    runs = asyncio.run(resolve_runs(KEY, home=home, lock_root=home))
    assert len(runs) == 1
    row = runs[0].row
    assert row is not None
    assert row.outcome is None
    assert row.ended_at is None
    assert row.suspended_at is not None
    assert runs[0].status == "suspended"
    # Still resumable — the whole point of not making it terminal.
    assert select_resumable(runs).run_id == runs[0].run_id


def test_an_abandoned_workstream_ends_the_run_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ABANDONED has an empty transition set, so the run cannot advance —
    the one condition §B.1.1 admits `failed` under."""
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")

    _invoke_orchestrate(
        config_path,
        seed=_seed_workstream("z-1", WorkstreamStatus.ABANDONED),
        failed=1,
    )

    runs = asyncio.run(resolve_runs(KEY, home=home, lock_root=home))
    assert [r.status for r in runs] == ["failed"]
    row = runs[0].row
    assert row is not None
    assert row.ended_at is not None


def test_a_reworkable_failure_leaves_the_run_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILED transitions back to READY, so rework can still address it and
    the run must stay non-terminal (spec §B.1.1, §G)."""
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")

    _invoke_orchestrate(
        config_path, seed=_seed_workstream("z-1", WorkstreamStatus.FAILED), failed=1
    )

    runs = asyncio.run(resolve_runs(KEY, home=home, lock_root=home))
    assert [r.status for r in runs] == ["interrupted"]
    row = runs[0].row
    assert row is not None
    assert row.outcome is None
    assert row.suspended_at is None


# =============================================================================
# The scheduled tick — the failure mode with no escape hatch on an installed
# unit, so it is tested in both directions.
# =============================================================================


def test_service_run_survives_a_second_orchestration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`maestro service run` after two orchestrations: green, not exit 1.

    `service install` no longer bakes `--db` into the unit, so an installed
    tick has no escape hatch from a repository the resolver refuses.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")
    _invoke_orchestrate(config_path)
    _invoke_orchestrate(config_path)

    result = runner.invoke(app, ["service", "run", str(config_path)])

    assert result.exit_code == 0, result.output + result.stderr
    assert "noop_complete" in result.output


def test_service_run_still_refuses_when_no_run_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty `runs/` is a setup gap, not a finished repository: the
    refusal that tells the operator to orchestrate once must survive."""
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")

    result = runner.invoke(app, ["service", "run", str(config_path)])

    assert result.exit_code == 1
    assert "No resumable run" in result.stderr


# =============================================================================
# Liveness is observed, not inferred from a NULL (spec §B.3) — and observing
# it requires a *production* code path that writes the holder.
#
# `ScopedLock` has taken a `run_id` since the layout landed, and
# `tests/test_locks_run_attribution.py` proved the sidecar semantics by
# constructing the lock with one by hand. Nothing in the product passed it:
# `service/tick.py` built the only production `ScopedLock` with `key` and
# `stage` alone, so `read_holder_run_id` returned `None` on every machine,
# `classify_run` could never return `running`, and a run a tick was actively
# executing read as `interrupted` — which `select_resumable` then hands to a
# second invocation as a resumable candidate. Two orchestrators on one
# database is the exact hazard §B.3 was written to prevent, so the test that
# matters drives the real command, not the lock.
# =============================================================================


def test_a_run_a_tick_is_executing_is_reported_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While `maestro service run` holds the stage lock, its run is `running`.

    Observed from inside the tick's own subprocess call — the window in
    which a second invocation would otherwise resume this database.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")
    # FAILED transitions back to READY, so the run stays non-terminal and the
    # tick has something to advance (`decide_orchestrate` -> "resume").
    _invoke_orchestrate(
        config_path, seed=_seed_workstream("z-1", WorkstreamStatus.FAILED), failed=1
    )
    before = asyncio.run(resolve_runs(KEY, home=home, lock_root=home))
    assert [r.status for r in before] == ["interrupted"]
    run_id = before[0].run_id

    observed: list[tuple[str | None, str]] = []
    refused: list[str] = []

    async def _observe(_argv: list[str], **_kwargs: object) -> int:
        observed.extend(
            (info.run_id, info.status)
            for info in await resolve_runs(KEY, home=home, lock_root=home)
        )
        # The operator-facing half of the same fact: a fresh orchestration
        # of a repository whose run is live must refuse. `RunIsLive` is
        # raised off `live_run()`, so with no holder ever written it was
        # unreachable code — the refusal existed and never fired.
        try:
            await bootstrap_run(
                load_orchestrator_config(config_path),
                resume=False,
                run_id_override=None,
                home=home,
            )
        except RunIsLive as exc:
            refused.append(str(exc))
        return 0

    with patch("maestro.cli.run_argv", _observe):
        result = runner.invoke(app, ["service", "run", str(config_path)])

    assert result.exit_code == 0, result.output + result.stderr
    assert observed == [(run_id, "running")]
    assert refused and run_id is not None and run_id in refused[0]

    # And the claim does not outlive the tick: a sidecar left behind would
    # grant liveness to a dead run, which is the mirror-image failure.
    after = asyncio.run(resolve_runs(KEY, home=home, lock_root=home))
    assert [r.status for r in after] == ["interrupted"]


def test_a_legacy_db_tick_attributes_the_lock_to_no_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `--db` tick has no run identity, and must not invent one.

    Deriving the run id from the database's parent directory would answer
    `~/.maestro/maestro.db` with the run id `maestro`. The honest value is
    `None`: no holder is written, and liveness stays *unobserved* (§B.3, §E).
    """
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    config_path = _write_orchestrator_config(tmp_path, "https://github.com/acme/app")
    legacy = tmp_path / ".maestro" / "maestro.db"
    legacy.parent.mkdir()

    async def _seed_legacy() -> None:
        db = await create_database(legacy)
        try:
            await db.create_workstream(
                Workstream(
                    id="z-1",
                    title="z-1",
                    description="seeded by the test",
                    branch="feature/z-1",
                    status=WorkstreamStatus.FAILED,
                )
            )
        finally:
            await db.close()

    asyncio.run(_seed_legacy())

    holder_file = stage_lock_path(KEY, "orchestrate", root=home).with_suffix(".holder")
    observed: list[object] = []

    async def _observe(_argv: list[str], **_kwargs: object) -> int:
        # Non-vacuous: the stage lock IS held here, so a holder file would
        # have been read rather than merely missing.
        observed.append(stage_lock_path(KEY, "orchestrate", root=home).exists())
        observed.append(holder_file.exists())
        observed.append(read_holder_run_id(KEY, "orchestrate", root=home))
        return 0

    with patch("maestro.cli.run_argv", _observe):
        result = runner.invoke(
            app, ["service", "run", str(config_path), "--db", str(legacy)]
        )

    assert result.exit_code == 0, result.output + result.stderr
    assert observed == [True, False, None]

    # Nor is the database itself given an identity by having been ticked.
    info = asyncio.run(describe_database(legacy, key=KEY, lock_root=home))
    assert info.run_id is None
    assert info.status == "legacy"


# =============================================================================
# Unit level — the classifier and the writer, including what they refuse.
# =============================================================================


def test_advanceable_work_outranks_a_pause() -> None:
    """`decide_orchestrate`'s own order: a run with work left is neither
    finished nor waiting."""
    conclusion = conclude_run(total=3, advanceable=1, needs_human=1, unadvanceable=1)
    assert conclusion.outcome is None
    assert conclusion.suspended is False


def test_an_empty_run_completes_rather_than_lingering() -> None:
    assert (
        conclude_run(total=0, advanceable=0, needs_human=0, unadvanceable=0).outcome
        == "completed"
    )


def test_conclusion_for_workstreams_reads_the_real_enum() -> None:
    def _ws(status: WorkstreamStatus) -> Workstream:
        return Workstream(id="z", title="z", description="d", branch="b", status=status)

    assert conclusion_for_workstreams([_ws(WorkstreamStatus.DONE)]).outcome == (
        "completed"
    )
    assert conclusion_for_workstreams([_ws(WorkstreamStatus.RUNNING)]).outcome is None


async def test_record_conclusion_leaves_a_database_without_a_run_row_alone(
    tmp_path: Path,
) -> None:
    """Spec §E: a pre-split database is never written a `run` row, and that
    includes writing an outcome into an empty `run` table."""
    db = await create_database(tmp_path / "state.db")
    try:
        written = await record_conclusion(
            db, RunConclusion("completed", False, "irrelevant")
        )
        assert written is False
        assert await db.get_run_row() is None
    finally:
        await db.close()
