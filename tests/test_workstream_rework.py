"""Tests for `maestro workstream-rework` (issue #124).

Covers the design acceptance checklist of
docs/superpowers/specs/2026-08-05-workstream-rework-design.md:
migration 18, the recovery-ambiguity marker, the CAS+audit transaction,
the liveness proof, refresh validation, the CLI commands, and the
exhaustive READY resume dispatch.
"""

import json
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from pathlib import Path

import pytest

from maestro.database import Database
from maestro.models import Workstream, WorkstreamStatus


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "rework-test.db")
    await d.connect()
    yield d
    await d.close()


def make_ws(
    id_: str = "ws-1",
    status: WorkstreamStatus = WorkstreamStatus.NEEDS_REVIEW,
    **overrides: object,
) -> Workstream:
    ws = Workstream(
        id=id_,
        title=id_,
        description="original description",
        branch=f"feature/{id_}",
        status=status,
    )
    return ws.model_copy(update=overrides) if overrides else ws


class TestMigration18:
    @pytest.mark.anyio
    async def test_new_columns_default(self, db) -> None:
        await db.create_workstream(make_ws())
        ws = await db.get_workstream("ws-1")
        assert ws.operator_rework_count == 0
        assert ws.operator_rework_seq is None
        assert ws.recovery_ambiguity is None

    @pytest.mark.anyio
    async def test_rework_tables_exist(self, db) -> None:
        cur = await db._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('workstream_reworks', 'workstream_ambiguity_resolutions')"
        )
        names = {row["name"] for row in await cur.fetchall()}
        assert names == {"workstream_reworks", "workstream_ambiguity_resolutions"}

    @pytest.mark.anyio
    async def test_columns_survive_roundtrip(self, db) -> None:
        marker = json.dumps({"kind": "live_orphan", "pid": 7, "parked_at": "t"})
        await db.create_workstream(
            make_ws(
                operator_rework_count=2,
                operator_rework_seq=2,
                recovery_ambiguity=marker,
            )
        )
        ws = await db.get_workstream("ws-1")
        assert ws.operator_rework_count == 2
        assert ws.operator_rework_seq == 2
        assert ws.recovery_ambiguity is not None
        assert json.loads(ws.recovery_ambiguity)["pid"] == 7


async def _record(db: Database, ws_id: str = "ws-1", **overrides: object) -> int:
    """record_workstream_rework with sane defaults for tests."""
    kwargs: dict[str, Any] = {
        "prior_status": WorkstreamStatus.NEEDS_REVIEW,
        "prior_count": 0,
        "prior_marker": None,
        "reason": "reviewer rejected the diff",
        "instructions": "split the migration",
        "initiator": "andrei",
        "prior_error_message": "gate blocked",
        "prior_head_sha": "a" * 40,
        "liveness_evidence": None,
        "refresh": None,
    }
    kwargs.update(overrides)
    return await db.record_workstream_rework(ws_id, **kwargs)


class TestRecordRework:
    @pytest.mark.anyio
    async def test_cas_success_writes_row_and_audit(self, db) -> None:
        await db.create_workstream(make_ws())
        seq = await _record(db)
        assert seq == 1
        ws = await db.get_workstream("ws-1")
        assert ws.status is WorkstreamStatus.READY
        assert ws.resume_reason == "operator_rework"
        assert ws.operator_rework_count == 1
        assert ws.operator_rework_seq == 1
        assert ws.error_message is None
        assert ws.verification_run_id is None
        row = await db.get_workstream_rework("ws-1", 1)
        assert row is not None
        assert row["reason"] == "reviewer rejected the diff"
        assert row["instructions"] == "split the migration"
        assert row["prior_status"] == "needs_review"
        assert row["prior_head_sha"] == "a" * 40

    @pytest.mark.anyio
    async def test_cas_refuses_on_stale_status(self, db) -> None:
        await db.create_workstream(make_ws(status=WorkstreamStatus.READY))
        with pytest.raises(ValueError, match="refused"):
            await _record(db)  # prior_status=NEEDS_REVIEW is stale
        assert await db.get_workstream_rework("ws-1", 1) is None  # no audit

    @pytest.mark.anyio
    async def test_cas_refuses_on_live_pid(self, db) -> None:
        await db.create_workstream(make_ws(process_pid=123))
        with pytest.raises(ValueError, match="refused"):
            await _record(db)
        assert await db.get_workstream_rework("ws-1", 1) is None

    @pytest.mark.anyio
    async def test_cas_refuses_on_marker_change(self, db) -> None:
        marker = json.dumps({"kind": "live_orphan", "pid": 7, "parked_at": "t"})
        await db.create_workstream(make_ws(recovery_ambiguity=marker))
        with pytest.raises(ValueError, match="refused"):
            await _record(db, prior_marker=None)  # stale marker read
        assert await db.get_workstream_rework("ws-1", 1) is None

    @pytest.mark.anyio
    async def test_cas_clears_marker_it_read(self, db) -> None:
        marker = json.dumps({"kind": "live_orphan", "pid": 7, "parked_at": "t"})
        await db.create_workstream(make_ws(recovery_ambiguity=marker))
        seq = await _record(db, prior_marker=marker, liveness_evidence='{"ok":1}')
        assert seq == 1
        ws = await db.get_workstream("ws-1")
        assert ws.recovery_ambiguity is None

    @pytest.mark.anyio
    async def test_second_rework_gets_seq_2(self, db) -> None:
        await db.create_workstream(make_ws())
        assert await _record(db) == 1
        # Simulate the attempt failing again.
        await db.update_workstream_status(
            "ws-1", WorkstreamStatus.NEEDS_REVIEW, resume_reason=None
        )
        assert await _record(db, prior_count=1) == 2
        ws = await db.get_workstream("ws-1")
        assert ws.operator_rework_count == 2
        assert ws.operator_rework_seq == 2

    @pytest.mark.anyio
    async def test_refresh_updates_description_and_scope(self, db) -> None:
        from maestro.rework import RefreshEvidence

        await db.create_workstream(make_ws())
        refresh = RefreshEvidence(
            config_path="/p/project.yaml",
            config_hash="h" * 64,
            old_description="original description",
            new_description="updated description",
            old_scope=[],
            new_scope=["src/**"],
        )
        await _record(db, refresh=refresh)
        ws = await db.get_workstream("ws-1")
        assert ws.description == "updated description"
        assert ws.scope == ["src/**"]
        row = await db.get_workstream_rework("ws-1", 1)
        assert row is not None
        assert row["refresh_config_hash"] == "h" * 64
        assert json.loads(row["new_scope"]) == ["src/**"]

    @pytest.mark.anyio
    async def test_no_gate_approvals_written(self, db) -> None:
        await db.create_workstream(make_ws())
        await _record(db)
        approvals = await db.list_gate_approvals("ws-1")
        assert approvals == set()

    @pytest.mark.anyio
    async def test_resolve_ambiguity_roundtrip(self, db) -> None:
        marker = json.dumps({"kind": "spawn_uncertain", "pid": None})
        await db.create_workstream(make_ws(recovery_ambiguity=marker))
        await db.resolve_recovery_ambiguity(
            "ws-1", statement="verified by hand: no maestro procs", initiator="andrei"
        )
        ws = await db.get_workstream("ws-1")
        assert ws.recovery_ambiguity is None
        cur = await db._connection.execute(
            "SELECT statement, marker_json FROM workstream_ambiguity_resolutions"
        )
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["marker_json"] == marker

    @pytest.mark.anyio
    async def test_resolve_without_marker_refuses(self, db) -> None:
        await db.create_workstream(make_ws())
        with pytest.raises(ValueError):
            await db.resolve_recovery_ambiguity(
                "ws-1", statement="s", initiator="andrei"
            )


class TestResumeConstants:
    def test_known_resume_reasons(self) -> None:
        from maestro.domain.resume import (
            KNOWN_RESUME_REASONS,
            RESUME_OPERATOR_REWORK,
        )

        assert RESUME_OPERATOR_REWORK == "operator_rework"
        assert {
            "verification_rework",
            "verification_reverify",
            "operator_rework",
        } == KNOWN_RESUME_REASONS


class TestLivenessProof:
    @pytest.mark.anyio
    async def test_pids_null_no_marker_no_handles_passes(self, db) -> None:
        from maestro.rework import prove_no_live_process

        await db.create_workstream(make_ws())
        evidence = await prove_no_live_process(db, await db.get_workstream("ws-1"))
        assert evidence is None

    @pytest.mark.anyio
    async def test_nonnull_pid_refuses(self, db) -> None:
        from maestro.rework import ReworkRefused, prove_no_live_process

        await db.create_workstream(make_ws(process_pid=123))
        with pytest.raises(ReworkRefused):
            await prove_no_live_process(db, await db.get_workstream("ws-1"))

    @pytest.mark.anyio
    async def test_sentinel_generation_pid_refuses(self, db) -> None:
        from maestro import orchestrator as orch_mod
        from maestro.rework import ReworkRefused, prove_no_live_process

        await db.create_workstream(make_ws(generation_pid=orch_mod._SPAWNING_SENTINEL))
        with pytest.raises(ReworkRefused):
            await prove_no_live_process(db, await db.get_workstream("ws-1"))

    @pytest.mark.anyio
    async def test_marker_with_dead_pid_passes_with_evidence(
        self, db, monkeypatch
    ) -> None:
        import maestro.rework as rework_mod
        from maestro.rework import prove_no_live_process

        monkeypatch.setattr(rework_mod, "_is_pid_alive", lambda _pid: False)
        marker = json.dumps({"kind": "live_orphan", "pid": 4242, "parked_at": "t"})
        await db.create_workstream(make_ws(recovery_ambiguity=marker))
        evidence = await prove_no_live_process(db, await db.get_workstream("ws-1"))
        assert evidence is not None
        parsed = json.loads(evidence)
        assert parsed["pid"] == 4242
        assert parsed["alive"] is False

    @pytest.mark.anyio
    async def test_marker_with_live_pid_refuses(self, db, monkeypatch) -> None:
        import maestro.rework as rework_mod
        from maestro.rework import ReworkRefused, prove_no_live_process

        monkeypatch.setattr(rework_mod, "_is_pid_alive", lambda _pid: True)
        marker = json.dumps({"kind": "live_orphan", "pid": 4242, "parked_at": "t"})
        await db.create_workstream(make_ws(recovery_ambiguity=marker))
        with pytest.raises(ReworkRefused):
            await prove_no_live_process(db, await db.get_workstream("ws-1"))

    @pytest.mark.anyio
    async def test_sentinel_marker_always_refuses(self, db, monkeypatch) -> None:
        import maestro.rework as rework_mod
        from maestro.rework import ReworkRefused, prove_no_live_process

        monkeypatch.setattr(rework_mod, "_is_pid_alive", lambda _pid: False)
        marker = json.dumps({"kind": "spawn_uncertain", "pid": None, "parked_at": "t"})
        await db.create_workstream(make_ws(recovery_ambiguity=marker))
        with pytest.raises(ReworkRefused, match="resolve"):
            await prove_no_live_process(db, await db.get_workstream("ws-1"))

    @pytest.mark.anyio
    async def test_open_handle_refuses(self, db) -> None:
        from maestro.rework import ReworkRefused, prove_no_live_process

        await db.create_workstream(make_ws(status=WorkstreamStatus.READY))
        await db.start_execution(
            entity_kind="workstream",
            entity_id="ws-1",
            expected_status=WorkstreamStatus.READY.value,
            running_status=WorkstreamStatus.RUNNING.value,
            execution_id="exec-1",
            backend_id="docker",
            transport_ref="docker:maestro-exec-1",
            attempt=1,
        )
        # Park it the way recovery would, pids cleared.
        await db.update_workstream_status(
            "ws-1", WorkstreamStatus.FAILED, process_pid=None
        )
        await db.update_workstream_status("ws-1", WorkstreamStatus.NEEDS_REVIEW)
        with pytest.raises(ReworkRefused, match="handle"):
            await prove_no_live_process(db, await db.get_workstream("ws-1"))


class TestRefreshValidation:
    def _write_config(self, tmp_path, description: str, scope: list[str]) -> "Path":
        import yaml as yaml_mod

        cfg = {
            "project": "test",
            "repo_url": "https://github.com/user/test",
            "repo_path": "/nonexistent",
            "workspace_base": "/tmp/maestro-ws/test",
            "workstreams": [
                {
                    "id": "ws-1",
                    "title": "ws-1",
                    "description": description,
                    "scope": scope,
                    "depends_on": [],
                },
                {
                    "id": "ws-2",
                    "title": "ws-2",
                    "description": "sibling",
                    "scope": ["docs/**"],
                    "depends_on": [],
                },
            ],
        }
        path = tmp_path / "project.yaml"
        path.write_text(yaml_mod.safe_dump(cfg))
        return path

    def test_description_change_produces_evidence(self, tmp_path) -> None:
        import hashlib

        from maestro.rework import validate_refresh

        path = self._write_config(tmp_path, "updated description", [])
        evidence = validate_refresh(make_ws(), path)
        assert evidence is not None
        assert evidence.new_description == "updated description"
        assert evidence.old_description == "original description"
        assert evidence.config_hash == hashlib.sha256(path.read_bytes()).hexdigest()

    def test_unchanged_returns_none(self, tmp_path) -> None:
        from maestro.rework import validate_refresh

        path = self._write_config(tmp_path, "original description", [])
        assert validate_refresh(make_ws(), path) is None

    def test_topology_change_refuses(self, tmp_path) -> None:
        import yaml as yaml_mod

        from maestro.rework import ReworkRefused, validate_refresh

        path = self._write_config(tmp_path, "updated", [])
        cfg = yaml_mod.safe_load(path.read_text())
        cfg["workstreams"][0]["depends_on"] = ["ws-2"]
        path.write_text(yaml_mod.safe_dump(cfg))
        with pytest.raises(ReworkRefused, match="depends_on"):
            validate_refresh(make_ws(), path)

    def test_missing_id_refuses(self, tmp_path) -> None:
        from maestro.rework import ReworkRefused, validate_refresh

        path = self._write_config(tmp_path, "u", [])
        with pytest.raises(ReworkRefused, match="no workstream"):
            validate_refresh(make_ws(id_="other"), path)

    def test_overlapping_refreshed_scope_refuses(self, tmp_path) -> None:
        from maestro.rework import ReworkRefused, validate_refresh

        path = self._write_config(tmp_path, "updated", ["docs/**"])
        with pytest.raises(ReworkRefused, match="overlap"):
            validate_refresh(make_ws(), path)


def _git_worktree(tmp_path) -> str:
    """A real git repo with one commit, usable as a workstream worktree."""
    import subprocess

    repo = tmp_path / "wt"
    repo.mkdir()
    for cmd in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True
    )
    return str(repo)


class TestCliHelpers:
    @pytest.mark.anyio
    async def test_rework_happy_path(self, db, tmp_path) -> None:
        from maestro.cli import _rework_workstream

        wt = _git_worktree(tmp_path)
        await db.create_workstream(
            make_ws(workspace_path=wt, error_message="gate blocked")
        )
        seq = await _rework_workstream(
            db, "ws-1", reason="rejected", instructions="fix it", refresh_from=None
        )
        assert seq == 1
        ws = await db.get_workstream("ws-1")
        assert ws.status is WorkstreamStatus.READY
        assert ws.resume_reason == "operator_rework"
        row = await db.get_workstream_rework("ws-1", 1)
        assert row is not None
        assert row["prior_error_message"] == "gate blocked"
        assert len(row["prior_head_sha"]) == 40

    @pytest.mark.anyio
    async def test_rework_refuses_running(self, db, tmp_path) -> None:
        from maestro.cli import _rework_workstream
        from maestro.rework import ReworkRefused

        wt = _git_worktree(tmp_path)
        await db.create_workstream(
            make_ws(status=WorkstreamStatus.RUNNING, workspace_path=wt)
        )
        with pytest.raises(ReworkRefused, match="not reworkable"):
            await _rework_workstream(
                db, "ws-1", reason="r", instructions=None, refresh_from=None
            )

    @pytest.mark.anyio
    async def test_rework_refuses_sentinel_marker_with_hint(self, db, tmp_path) -> None:
        from maestro.cli import _rework_workstream
        from maestro.rework import ReworkRefused

        wt = _git_worktree(tmp_path)
        marker = json.dumps({"kind": "spawn_uncertain", "pid": None})
        await db.create_workstream(
            make_ws(workspace_path=wt, recovery_ambiguity=marker)
        )
        with pytest.raises(ReworkRefused, match="workstream-resolve-ambiguity"):
            await _rework_workstream(
                db, "ws-1", reason="r", instructions=None, refresh_from=None
            )

    @pytest.mark.anyio
    async def test_rework_refuses_missing_worktree(self, db) -> None:
        from maestro.cli import _rework_workstream
        from maestro.rework import ReworkRefused

        await db.create_workstream(make_ws(workspace_path=None))
        with pytest.raises(ReworkRefused, match="worktree"):
            await _rework_workstream(
                db, "ws-1", reason="r", instructions=None, refresh_from=None
            )

    @pytest.mark.anyio
    async def test_resolve_helper_roundtrip(self, db) -> None:
        from maestro.cli import _resolve_ambiguity

        marker = json.dumps({"kind": "spawn_uncertain", "pid": None})
        await db.create_workstream(make_ws(recovery_ambiguity=marker))
        await _resolve_ambiguity(db, "ws-1", statement="verified: nothing runs")
        ws = await db.get_workstream("ws-1")
        assert ws.recovery_ambiguity is None


class TestResumeDispatch:
    """READY dispatch is exhaustive; operator_rework re-decomposes with the
    addendum keyed by (workstream_id, operator_rework_seq)."""

    def _make_orch(self, db, tmp_path):
        import subprocess
        from unittest.mock import MagicMock

        from maestro.models import OrchestratorConfig
        from maestro.orchestrator import Orchestrator
        from tests.fakes.fake_execution_backend import FakeTaskHandle

        worktree = tmp_path / "wt"
        if not (worktree / ".git").exists():
            worktree.mkdir(exist_ok=True)
            for cmd in (
                ["git", "init", "-b", "main"],
                ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"],
            ):
                subprocess.run(cmd, cwd=worktree, check=True, capture_output=True)
            (worktree / "f.txt").write_text("x")
            subprocess.run(
                ["git", "add", "."], cwd=worktree, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "init"],
                cwd=worktree,
                check=True,
                capture_output=True,
            )

        class _WsMgr:
            def workspace_exists(self, workstream_id: str) -> bool:
                return True

            def get_workspace_path(self, workstream_id: str):
                return worktree

            def create_workspace(self, workstream_id: str, branch: str):
                return worktree

            def setup_spec_runner(self, workspace, config) -> None:
                pass

            def cleanup_workspace(self, workstream_id: str) -> None:
                pass

        class _Decomposer:
            def __init__(self) -> None:
                self.generate_spec_calls: list[Any] = []

            async def generate_spec(
                self, workstream_config, workspace, *, on_pid=None
            ) -> None:
                self.generate_spec_calls.append(workstream_config)

        class _Backend:
            id = "local"

            async def healthcheck(self):
                from maestro.execution.models import BackendHealth

                return BackendHealth(reachable=True)

            async def can_run(self, req):
                from maestro.execution.models import CapabilityResult

                return CapabilityResult(ok=True)

            async def run(self, req):
                return FakeTaskHandle(exit_code=0, pid=4321)

        config = OrchestratorConfig(
            project="test",
            repo_url="https://github.com/user/test",
            repo_path=str(worktree),
            workspace_base=str(tmp_path),
            base_branch="main",
            auto_pr=False,
            max_concurrent=1,
            workstreams=[],
        )
        decomposer = _Decomposer()
        orch = Orchestrator(
            db=db,
            workspace_mgr=_WsMgr(),  # type: ignore[arg-type]
            decomposer=decomposer,  # type: ignore[arg-type]
            pr_manager=MagicMock(),
            config=config,
            log_dir=tmp_path / "logs",
        )
        orch._backends._cache["local"] = _Backend()  # type: ignore[assignment]
        return orch, decomposer

    @pytest.mark.anyio
    async def test_unknown_resume_reason_fails_closed(self, db, tmp_path) -> None:
        orch, decomposer = self._make_orch(db, tmp_path)
        await db.create_workstream(
            make_ws(status=WorkstreamStatus.READY, resume_reason="garbage")
        )
        await orch._spawn_workstream("ws-1")
        ws = await db.get_workstream("ws-1")
        assert ws.status is WorkstreamStatus.NEEDS_REVIEW
        assert decomposer.generate_spec_calls == []

    @pytest.mark.anyio
    async def test_operator_rework_appends_instructions_addendum(
        self, db, tmp_path
    ) -> None:
        orch, decomposer = self._make_orch(db, tmp_path)
        await db.create_workstream(
            make_ws(workspace_path=str(tmp_path / "wt"), error_message="blocked")
        )
        # Sanctioned path: the CAS sets READY + reason + seq.
        await _record(db, instructions="split the migration")
        await orch._spawn_workstream("ws-1")
        assert len(decomposer.generate_spec_calls) == 1
        description = decomposer.generate_spec_calls[0].description
        assert "original description" in description
        assert "split the migration" in description
        assert "Operator rework instructions" in description
        # reason is audit-only and never enters the prompt
        assert "reviewer rejected the diff" not in description
        ws = await db.get_workstream("ws-1")
        assert ws.resume_reason is None  # cleared once the attempt exists

    @pytest.mark.anyio
    async def test_operator_rework_without_instructions_plain_description(
        self, db, tmp_path
    ) -> None:
        orch, decomposer = self._make_orch(db, tmp_path)
        await db.create_workstream(make_ws(workspace_path=str(tmp_path / "wt")))
        await _record(db, instructions=None)
        await orch._spawn_workstream("ws-1")
        assert len(decomposer.generate_spec_calls) == 1
        description = decomposer.generate_spec_calls[0].description
        assert description == "original description"


class TestAddendum:
    def test_instructions_render(self) -> None:
        from maestro.rework import build_operator_rework_addendum

        addendum = build_operator_rework_addendum(
            {"instructions": "split the migration", "reason": "secret"}
        )
        assert addendum is not None
        assert "split the migration" in addendum
        assert "Operator rework instructions" in addendum
        assert "secret" not in addendum  # reason never enters the prompt

    def test_no_instructions_no_addendum(self) -> None:
        from maestro.rework import build_operator_rework_addendum

        assert build_operator_rework_addendum({"instructions": None}) is None
