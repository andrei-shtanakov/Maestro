"""CLI-level tests for `maestro review-pr` (Copilot review, PR #149).

Covers argument handling, workstream selection, the version gate, exit
codes, dedup by (repo, PR), and `--gc` wiring — the behavior that lives
in `cli.py` rather than in the runner.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.database import Database
from maestro.models import Workstream, WorkstreamStatus


runner = CliRunner()

PR_URL = "https://github.com/o/r/pull/7"


@pytest.fixture
def project_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "project.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "project": "p",
                "repo_url": "https://github.com/o/r",
                "repo_path": str(tmp_path),
                "workspace_base": str(tmp_path / "ws"),
                "workstreams": [],
            }
        )
    )
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"

    async def _seed() -> None:
        db = Database(path)
        await db.connect()
        try:
            await db.create_workstream(
                Workstream(
                    id="ws-1",
                    title="W",
                    description="d",
                    branch="feature/pr",
                    status=WorkstreamStatus.DONE,
                    pr_url=PR_URL,
                )
            )
            await db.create_workstream(
                Workstream(
                    id="ws-dup",  # same PR — must be deduped by (repo, number)
                    title="W2",
                    description="d",
                    branch="feature/pr",
                    status=WorkstreamStatus.DONE,
                    pr_url=PR_URL + "/files",
                )
            )
            await db.create_workstream(
                Workstream(
                    id="ws-nopr",
                    title="W3",
                    description="d",
                    branch="feature/x",
                    status=WorkstreamStatus.DONE,
                )
            )
        finally:
            await db.close()

    asyncio.run(_seed())
    return path


def _invoke(project_yaml: Path, db_path: Path, *args: str):
    return runner.invoke(
        app, ["review-pr", str(project_yaml), *args, "--db", str(db_path)]
    )


# =============================================================================
# Argument handling
# =============================================================================


def test_requires_a_target(project_yaml: Path, db_path: Path) -> None:
    result = _invoke(project_yaml, db_path)
    assert result.exit_code == 1
    assert "workstream id" in result.output


def test_unknown_workstream_fails(project_yaml: Path, db_path: Path) -> None:
    with patch(
        "maestro.cli._probe_spec_runner_version", return_value=(0, "spec-runner 2.21.0")
    ):
        result = _invoke(project_yaml, db_path, "ws-missing")
    assert result.exit_code == 1
    assert "not found" in result.output


def test_workstream_without_pr_fails(project_yaml: Path, db_path: Path) -> None:
    with patch(
        "maestro.cli._probe_spec_runner_version", return_value=(0, "spec-runner 2.21.0")
    ):
        result = _invoke(project_yaml, db_path, "ws-nopr")
    assert result.exit_code == 1
    assert "no PR" in result.output


# =============================================================================
# Version gate (§7.1) — blocks before any invocation
# =============================================================================


@pytest.mark.parametrize(
    "probe",
    [(0, "spec-runner 2.20.0"), (0, "garbage"), (127, "")],
)
def test_version_gate_blocks(
    project_yaml: Path, db_path: Path, probe: tuple[int, str]
) -> None:
    with (
        patch("maestro.cli._probe_spec_runner_version", return_value=probe),
        patch("maestro.cli.run_review", new_callable=AsyncMock) as run_review,
    ):
        result = _invoke(project_yaml, db_path, "ws-1")
    assert result.exit_code == 1
    assert "spec-runner unsupported" in result.output
    run_review.assert_not_awaited()  # nothing ran, no run row


# =============================================================================
# Exit codes and dedup
# =============================================================================


@pytest.mark.parametrize("code", [0, 1, 2, 3])
def test_exit_code_passthrough(project_yaml: Path, db_path: Path, code: int) -> None:
    with (
        patch(
            "maestro.cli._probe_spec_runner_version",
            return_value=(0, "spec-runner 2.21.0"),
        ),
        patch("maestro.cli.run_review", new_callable=AsyncMock, return_value=code),
    ):
        result = _invoke(project_yaml, db_path, "ws-1")
    assert result.exit_code == code


def test_all_dedups_by_repo_and_pr(project_yaml: Path, db_path: Path) -> None:
    """ws-1 and ws-dup point at the same PR — one review, not two."""
    with (
        patch(
            "maestro.cli._probe_spec_runner_version",
            return_value=(0, "spec-runner 2.21.0"),
        ),
        patch(
            "maestro.cli.run_review", new_callable=AsyncMock, return_value=0
        ) as run_review,
    ):
        result = _invoke(project_yaml, db_path, "--all")
    assert result.exit_code == 0
    assert run_review.await_count == 1


def test_all_aggregates_infra_failure_over_needs_human(
    project_yaml: Path, db_path: Path
) -> None:
    calls = iter([2, 1])

    async def _fake(**_kwargs: object) -> int:
        return next(calls)

    seeded = _seed_second_pr(db_path)
    assert seeded
    with (
        patch(
            "maestro.cli._probe_spec_runner_version",
            return_value=(0, "spec-runner 2.21.0"),
        ),
        patch("maestro.cli.run_review", side_effect=_fake),
    ):
        result = _invoke(project_yaml, db_path, "--all")
    assert result.exit_code == 1  # infra dominates


def _seed_second_pr(db_path: Path) -> bool:
    async def _seed() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            await db.create_workstream(
                Workstream(
                    id="ws-2",
                    title="W",
                    description="d",
                    branch="feature/other",
                    status=WorkstreamStatus.DONE,
                    pr_url="https://github.com/o/r/pull/8",
                )
            )
        finally:
            await db.close()

    asyncio.run(_seed())
    return True


# =============================================================================
# --gc
# =============================================================================


def test_gc_sweeps_without_running_reviews(project_yaml: Path, db_path: Path) -> None:
    with (
        patch("maestro.cli.gc_pr", return_value=True) as gc_pr,
        patch("maestro.cli.run_review", new_callable=AsyncMock) as run_review,
        patch("maestro.cli._probe_spec_runner_version") as probe,
    ):
        result = _invoke(project_yaml, db_path, "--gc")
    assert result.exit_code == 0
    assert "Swept" in result.output
    run_review.assert_not_awaited()
    probe.assert_not_called()  # GC needs no spec-runner at all
    assert gc_pr.call_count >= 1


def test_gc_reports_zero_when_nothing_finished(
    project_yaml: Path, db_path: Path
) -> None:
    with patch("maestro.cli.gc_pr", return_value=False):
        result = _invoke(project_yaml, db_path, "--gc")
    assert result.exit_code == 0
    assert "Swept 0" in result.output
