"""Orchestrator wiring for #165: early tasks.md validation + retry fitness.

Two independent guarantees, proven at their real insertion points:

- a dangling dependency is caught after spec generation and **before any
  spawner**, so the run never costs an executor process;
- a failure whose typed `stop_reason` is unfit for automatic retry goes
  straight to NEEDS_REVIEW instead of paying another full re-decomposition,
  while every other reason — including unknown, dynamic and absent — keeps
  today's policy.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.database import Database
from maestro.models import (
    SPEC_PREFIX,
    OrchestratorConfig,
    Workstream,
    WorkstreamStatus,
)
from maestro.orchestrator import Orchestrator
from maestro.postmortem import MANIFEST_FILENAME
from maestro.tasks_spec import SELF_CONTAINED_DEPENDENCIES_INSTRUCTION


WS = "w-contracts"


def _init_workspace(workspace: Path) -> Path:
    """A real repo: the spawn path writes repo-local harness excludes (H-7),
    which needs a resolvable git common dir."""
    (workspace / "spec").mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    return workspace


def _tasks_md(*, dangling: bool) -> str:
    first = "### TASK-022: Continue the contract layer\n\n"
    first += "**Depends on:** [TASK-021]\n" if dangling else "**Depends on:** —\n"
    return (
        "# Tasks\n\n"
        + first
        + "\n### TASK-023: Follow-up\n\n**Depends on:** [TASK-022]\n"
    )


async def _build(
    tmp_path: Path, *, max_retries: int = 3
) -> tuple[Orchestrator, Database, MagicMock, MagicMock, Path]:
    workspace = _init_workspace(tmp_path / "wt")

    ws_mgr = MagicMock()
    ws_mgr.workspace_exists = MagicMock(return_value=True)
    ws_mgr.get_workspace_path = MagicMock(return_value=workspace)
    ws_mgr.create_workspace = MagicMock(return_value=workspace)
    decomposer = MagicMock()
    decomposer.generate_spec = AsyncMock(return_value=None)

    db = Database(tmp_path / "orch.db")
    await db.connect()
    cfg = OrchestratorConfig(
        project="p",
        repo_url="https://github.com/t/r",
        repo_path=str(tmp_path / "repo"),
        workspace_base=str(tmp_path / "ws"),
        workstreams=[],
    )
    orch = Orchestrator(
        db=db,
        workspace_mgr=ws_mgr,
        decomposer=decomposer,
        pr_manager=MagicMock(),
        config=cfg,
    )
    await db.create_workstream(
        Workstream(
            id=WS,
            title=WS,
            description="build the contract layer",
            scope=["src/**"],
            branch=f"feature/{WS}",
            status=WorkstreamStatus.READY,
            max_retries=max_retries,
        )
    )
    return orch, db, ws_mgr, decomposer, workspace


async def _seed_manifest(db: Database, *, stop_reason: str | None) -> None:
    """A committed archive whose manifest carries the typed stop reason."""
    archive = Path(db.db_path).parent / "postmortem" / WS / "20260811T000000Z-exec-1"
    archive.mkdir(parents=True)
    (archive / MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "schema": "maestro.postmortem-manifest/v1",
                "workstream_id": WS,
                "execution_id": "exec-1",
                "done": 1,
                "planned": 9,
                "noop_done": 0,
                "last_run_stop_reason": stop_reason,
                "last_run_stop_detail": "validation refused the spec",
            }
        )
    )
    await db.record_postmortem_archive(
        WS, "exec-1", path=str(archive), bytes_written=1, truncated=False
    )


class TestDanglingDependencyBlocksBeforeSpawn:
    @pytest.mark.anyio
    async def test_caught_before_any_spawner(self, tmp_path: Path) -> None:
        orch, db, _mgr, decomposer, workspace = await _build(tmp_path)
        try:
            (workspace / "spec" / f"{SPEC_PREFIX}tasks.md").write_text(
                _tasks_md(dangling=True)
            )
            spawn = AsyncMock()
            orch._spawn_process = spawn  # type: ignore[method-assign,attr-defined]

            await orch._spawn_workstream(WS)

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.NEEDS_REVIEW
            spawn.assert_not_awaited()
            decomposer.generate_spec.assert_awaited_once()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_message_names_the_missing_and_referencing_task(
        self, tmp_path: Path
    ) -> None:
        orch, db, _mgr, _dec, workspace = await _build(tmp_path)
        try:
            (workspace / "spec" / f"{SPEC_PREFIX}tasks.md").write_text(
                _tasks_md(dangling=True)
            )

            await orch._spawn_workstream(WS)

            error = (await db.get_workstream(WS)).error_message or ""
            assert "TASK-022 -> TASK-021" in error
            assert "revision" in error.lower()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_no_retry_is_consumed(self, tmp_path: Path) -> None:
        """Retrying would re-decompose — the exact spend #165 is about."""
        orch, db, _mgr, _dec, workspace = await _build(tmp_path)
        try:
            (workspace / "spec" / f"{SPEC_PREFIX}tasks.md").write_text(
                _tasks_md(dangling=True)
            )

            await orch._spawn_workstream(WS)

            assert (await db.get_workstream(WS)).retry_count == 0
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_valid_current_revision_passes(self, tmp_path: Path) -> None:
        """A well-formed file must not be blocked.

        Asserts the validator's verdict at its own insertion point rather than
        driving the whole spawn — the ordering ("before any spawner") is
        already pinned by `test_caught_before_any_spawner`, and continuing into
        a real spec-runner launch would test the environment, not this check.
        """
        orch, db, _mgr, _dec, workspace = await _build(tmp_path)
        try:
            (workspace / "spec" / f"{SPEC_PREFIX}tasks.md").write_text(
                _tasks_md(dangling=False)
            )

            assert await orch._validate_generated_tasks(WS, workspace)
            assert (await db.get_workstream(WS)).status is WorkstreamStatus.READY
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_unreadable_tasks_file_does_not_block(self, tmp_path: Path) -> None:
        """spec-runner still validates at run time; blocking on our own path
        assumption would turn it into an outage."""
        orch, db, _mgr, _dec, workspace = await _build(tmp_path)
        try:
            assert await orch._validate_generated_tasks(WS, workspace)
            assert (await db.get_workstream(WS)).status is WorkstreamStatus.READY
        finally:
            await db.close()


class TestRetryFitnessRouting:
    async def _fail(
        self, tmp_path: Path, *, stop_reason: str | None
    ) -> tuple[Workstream, Database]:
        orch, db, _mgr, _dec, _workspace = await _build(tmp_path)
        await _seed_manifest(db, stop_reason=stop_reason)
        await db.update_workstream_status(WS, WorkstreamStatus.RUNNING)
        await orch._handle_failure(
            WS,
            "spec-runner exited with code 1",
            stop_reason=await orch._last_stop_reason(WS),
        )
        return await db.get_workstream(WS), db

    @pytest.mark.anyio
    async def test_validation_failed_goes_straight_to_review(
        self, tmp_path: Path
    ) -> None:
        ws, db = await self._fail(tmp_path, stop_reason="validation_failed")
        try:
            assert ws.status is WorkstreamStatus.NEEDS_REVIEW
            assert ws.retry_count == 0
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_block_message_keeps_the_exact_stop_reason(
        self, tmp_path: Path
    ) -> None:
        ws, db = await self._fail(tmp_path, stop_reason="state_spec_mismatch")
        try:
            error = ws.error_message or ""
            assert "state_spec_mismatch" in error
            assert "retry" in error.lower()
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_transient_reason_keeps_its_retry(self, tmp_path: Path) -> None:
        ws, db = await self._fail(tmp_path, stop_reason="task_failed_stop")
        try:
            assert ws.status is WorkstreamStatus.READY
            assert ws.retry_count == 1
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_unknown_reason_keeps_its_retry(self, tmp_path: Path) -> None:
        ws, db = await self._fail(tmp_path, stop_reason="error_timeout")
        try:
            assert ws.status is WorkstreamStatus.READY
            assert ws.retry_count == 1
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_missing_reason_keeps_its_retry(self, tmp_path: Path) -> None:
        ws, db = await self._fail(tmp_path, stop_reason=None)
        try:
            assert ws.status is WorkstreamStatus.READY
            assert ws.retry_count == 1
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_no_archive_at_all_keeps_its_retry(self, tmp_path: Path) -> None:
        """A spec-generation failure never produced executor state."""
        orch, db, _mgr, _dec, _workspace = await _build(tmp_path)
        try:
            await db.update_workstream_status(WS, WorkstreamStatus.DECOMPOSING)

            assert await orch._last_stop_reason(WS) is None
            await orch._handle_failure(WS, "spec generation failed")

            ws = await db.get_workstream(WS)
            assert ws.status is WorkstreamStatus.READY
            assert ws.retry_count == 1
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_stop_detail_does_not_influence_the_decision(
        self, tmp_path: Path
    ) -> None:
        """The seeded detail says "validation refused the spec" for every
        case; only the typed reason may decide."""
        ws, db = await self._fail(tmp_path, stop_reason="max_consecutive_failures")
        try:
            assert ws.status is WorkstreamStatus.READY
        finally:
            await db.close()


class TestAddendumInstruction:
    @pytest.mark.anyio
    async def test_instruction_reaches_the_decomposer_on_a_rework(
        self, tmp_path: Path
    ) -> None:
        from maestro.domain import RESUME_OPERATOR_REWORK

        orch, db, _mgr, decomposer, workspace = await _build(tmp_path)
        try:
            # A dangling file on purpose: generation still happens (which is
            # what this test reads), and the validator then stops the path
            # cleanly instead of continuing into a real launch.
            (workspace / "spec" / f"{SPEC_PREFIX}tasks.md").write_text(
                _tasks_md(dangling=True)
            )
            await db.update_workstream_status(
                WS, WorkstreamStatus.READY, resume_reason=RESUME_OPERATOR_REWORK
            )

            await orch._spawn_workstream(WS)

            config = decomposer.generate_spec.await_args.args[0]
            assert SELF_CONTAINED_DEPENDENCIES_INSTRUCTION in config.description
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_correctness_does_not_depend_on_the_instruction(
        self, tmp_path: Path
    ) -> None:
        """The model may ignore it; the validator still blocks.

        This is the whole point of choosing a validator over a prompt rule:
        prevention is advisory, the check is not.
        """
        orch, db, _mgr, _dec, workspace = await _build(tmp_path)
        try:
            (workspace / "spec" / f"{SPEC_PREFIX}tasks.md").write_text(
                _tasks_md(dangling=True)
            )

            await orch._spawn_workstream(WS)

            assert (await db.get_workstream(WS)).status is (
                WorkstreamStatus.NEEDS_REVIEW
            )
        finally:
            await db.close()
