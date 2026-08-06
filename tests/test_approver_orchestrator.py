"""Orchestrator wiring tests for the approver_cmd hook (#137).

Spec: docs/superpowers/specs/2026-08-06-expost-approver-cmd-design.md
(approved revision 4) — §6 guards as observations, §7 persist-at-block +
PASS transaction, §8 lifecycle. Companion to tests/test_approver.py
(contract/DB/runner units).
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.database import Database
from maestro.gate_approvals import build_approval_marker
from maestro.gates import GateDecision
from maestro.models import (
    ApproverConfig,
    GatesConfig,
    OrchestratorConfig,
    Workstream,
    WorkstreamStatus,
)
from maestro.orchestrator import Orchestrator


# A well-behaved approver command: echoes identity from the stdin
# envelope into a PASS verdict with one independent critic.
_PASS_SCRIPT = """
import json, sys
req = json.load(sys.stdin)
print(json.dumps({
    "schema": "maestro.approval-verdict/v1",
    "approval_run_id": req["approval_run_id"],
    "workstream_id": req["workstream_id"],
    "phase": req["phase"],
    "sha": req["sha"],
    "verdict": "PASS",
    "summary": "consensus: benign",
    "findings": [],
    "critics": [{"name": "critic", "harness": "codex_cli",
                 "model": "gpt-5.4", "verdict": "PASS"}],
    "cost_usd": 0.1,
}))
"""

_FAIL_SCRIPT = _PASS_SCRIPT.replace('"verdict": "PASS",', '"verdict": "FAIL",', 1)

_GARBAGE_SCRIPT = "print('this is not a verdict')\n"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A real repo: `main` with a base commit, HEAD on a feature branch
    with one committed change (the diff the critic would review)."""
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "feature/ws-006")
    (repo / "docs").mkdir()
    (repo / "docs" / "notes.md").write_text("escape\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")
    return repo


def _approver_cfg(tmp_path: Path, script: str, **overrides: object) -> ApproverConfig:
    path = tmp_path / "approver_script.py"
    path.write_text(script)
    defaults: dict = {
        "cmd": [sys.executable, str(path)],
        "timeout_seconds": 30,
    }
    defaults.update(overrides)
    return ApproverConfig(**defaults)


async def _make_orch(
    tmp_path: Path, approver: ApproverConfig | None
) -> tuple[Orchestrator, Database]:
    db = Database(tmp_path / "orch.db")
    await db.connect()
    config = OrchestratorConfig(
        project="p",
        repo_url="https://github.com/t/r",
        repo_path=str(tmp_path),
        workspace_base=str(tmp_path / "ws"),
        base_branch="main",
        workstreams=[],
        gates=GatesConfig(steward_bin="/nonexistent", approver=approver),
    )
    orch = Orchestrator(
        db=db,
        workspace_mgr=MagicMock(),
        decomposer=MagicMock(),
        pr_manager=MagicMock(),
        config=config,
        log_dir=tmp_path / "logs",
    )
    return orch, db


def _blocked_ws(sha: str, workspace: Path | None) -> Workstream:
    return Workstream(
        id="ws-006",
        title="W",
        description="d",
        branch="feature/ws-006",
        status=WorkstreamStatus.NEEDS_REVIEW,
        scope=["src/**"],
        workspace_path=str(workspace) if workspace else None,
        error_message=(
            "gates: human.owner_approval required (tier=high); "
            f"re-queue to approve. {build_approval_marker('ex_post', sha)}"
        ),
    )


async def _seed_context(db: Database, sha: str) -> None:
    context = {
        "tier": "high",
        "flags": ["scope_violation"],
        "block_reason": "gates: blocked",
        "declared_scope": ["src/**"],
        "changed_paths": ["docs/notes.md"],
        "escaped_paths": ["docs/notes.md"],
        "author": {"harness": "spec-runner", "model": None},
    }
    await db.record_gate_block_context("ws-006", "ex_post", sha, json.dumps(context))


async def _run_scheduled(orch: Orchestrator) -> None:
    await orch._schedule_approver()
    tasks = list(orch._approver_tasks.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# =============================================================================
# 6a: persist-at-block
# =============================================================================


async def test_ex_post_block_persists_context(tmp_path: Path, worktree: Path) -> None:
    orch, db = await _make_orch(tmp_path, approver=None)
    try:
        sha = _git(worktree, "rev-parse", "HEAD")
        ws = _blocked_ws(sha, worktree)
        ws.status = WorkstreamStatus.RUNNING
        ws.error_message = None
        await db.create_workstream(ws)
        marker_reason = (
            "gates: human.owner_approval required (tier=high); "
            f"re-queue to approve. {build_approval_marker('ex_post', sha)}"
        )
        assert orch._gates is not None
        orch._gates.evaluate_ex_post = AsyncMock(  # type: ignore[method-assign]
            return_value=GateDecision(
                allow=False,
                reason=marker_reason,
                tier="high",
                flags=["scope_violation"],
                paths=["docs/notes.md", "src/ok.py"],
            )
        )

        allowed = await orch._gate_ex_post("ws-006", ws, worktree)

        assert allowed is False
        assert (await db.get_workstream("ws-006")).status == (
            WorkstreamStatus.NEEDS_REVIEW
        )
        raw = await db.get_gate_block_context("ws-006", "ex_post", sha)
        assert raw is not None
        context = json.loads(raw)
        assert context["tier"] == "high"
        assert context["changed_paths"] == ["docs/notes.md", "src/ok.py"]
        assert context["escaped_paths"] == ["docs/notes.md"]  # src/** covered
        assert context["author"] == {"harness": "spec-runner", "model": None}
    finally:
        await db.close()


# =============================================================================
# PASS path (§7.2) and outcome routing
# =============================================================================


async def test_pass_path_approves_and_requeues(tmp_path: Path, worktree: Path) -> None:
    cfg = _approver_cfg(tmp_path, _PASS_SCRIPT)
    orch, db = await _make_orch(tmp_path, approver=cfg)
    try:
        sha = _git(worktree, "rev-parse", "HEAD")
        await db.create_workstream(_blocked_ws(sha, worktree))
        await _seed_context(db, sha)

        await _run_scheduled(orch)

        ws = await db.get_workstream("ws-006")
        assert ws.status == WorkstreamStatus.READY
        # H-6 resume prerequisites: marker retained, approval recorded.
        assert ws.error_message is not None and sha in ws.error_message
        assert ("ex_post", sha) in await db.list_gate_approvals("ws-006")
        assert await db.count_agent_approvals("ws-006") == 1
        assert await db.list_started_approver_runs() == []
        assert orch._approver_tasks == {}
    finally:
        await db.close()


async def test_fail_verdict_stays_parked_and_human_can_still_approve(
    tmp_path: Path, worktree: Path
) -> None:
    cfg = _approver_cfg(tmp_path, _FAIL_SCRIPT)
    orch, db = await _make_orch(tmp_path, approver=cfg)
    try:
        sha = _git(worktree, "rev-parse", "HEAD")
        await db.create_workstream(_blocked_ws(sha, worktree))
        await _seed_context(db, sha)

        await _run_scheduled(orch)

        ws = await db.get_workstream("ws-006")
        assert ws.status == WorkstreamStatus.NEEDS_REVIEW
        assert await db.count_agent_approvals("ws-006") == 0
        # the agent FAIL never blocks the operator
        approved = await db.approve_workstream_with_gate_record(
            "ws-006", "ex_post", sha
        )
        assert approved.status == WorkstreamStatus.READY
    finally:
        await db.close()


async def test_protocol_garbage_is_error_not_approval(
    tmp_path: Path, worktree: Path
) -> None:
    cfg = _approver_cfg(tmp_path, _GARBAGE_SCRIPT)
    orch, db = await _make_orch(tmp_path, approver=cfg)
    try:
        sha = _git(worktree, "rev-parse", "HEAD")
        await db.create_workstream(_blocked_ws(sha, worktree))
        await _seed_context(db, sha)

        await _run_scheduled(orch)

        assert (await db.get_workstream("ws-006")).status == (
            WorkstreamStatus.NEEDS_REVIEW
        )
        assert await db.count_agent_approvals("ws-006") == 0
        assert await db.list_started_approver_runs() == []  # finalized error
    finally:
        await db.close()


async def test_stale_after_evaluation_no_approval(
    tmp_path: Path, worktree: Path
) -> None:
    """A commit lands during the critic run → error/stale, workstream kept."""
    cfg = _approver_cfg(tmp_path, _PASS_SCRIPT)
    orch, db = await _make_orch(tmp_path, approver=cfg)
    try:
        sha = _git(worktree, "rev-parse", "HEAD")
        await db.create_workstream(_blocked_ws(sha, worktree))
        await _seed_context(db, sha)
        other = "b" * 40
        orch._workspace_head = AsyncMock(side_effect=[sha, other, other])  # type: ignore[method-assign]

        await _run_scheduled(orch)

        assert (await db.get_workstream("ws-006")).status == (
            WorkstreamStatus.NEEDS_REVIEW
        )
        assert await db.count_agent_approvals("ws-006") == 0
        assert await db.list_started_approver_runs() == []
    finally:
        await db.close()


# =============================================================================
# §6 guards are observations — no attempt slot consumed
# =============================================================================


async def test_kill_switch_is_reversible(
    tmp_path: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _approver_cfg(tmp_path, _PASS_SCRIPT)
    orch, db = await _make_orch(tmp_path, approver=cfg)
    try:
        sha = _git(worktree, "rev-parse", "HEAD")
        await db.create_workstream(_blocked_ws(sha, worktree))
        await _seed_context(db, sha)

        monkeypatch.setenv("MAESTRO_APPROVER_DISABLED", "1")
        await _run_scheduled(orch)
        assert await db.has_approver_run("ws-006", "ex_post", sha) is False
        assert (await db.get_workstream("ws-006")).status == (
            WorkstreamStatus.NEEDS_REVIEW
        )

        monkeypatch.delenv("MAESTRO_APPROVER_DISABLED")
        await _run_scheduled(orch)  # same SHA gets evaluated after re-enable
        assert await db.has_approver_run("ws-006", "ex_post", sha) is True
        assert (await db.get_workstream("ws-006")).status == (WorkstreamStatus.READY)
    finally:
        await db.close()


async def test_non_gate_needs_review_never_evaluated(tmp_path: Path) -> None:
    cfg = _approver_cfg(tmp_path, _PASS_SCRIPT)
    orch, db = await _make_orch(tmp_path, approver=cfg)
    try:
        ws = _blocked_ws("a" * 40, None)
        ws.error_message = "recovery: possible live orphan"  # no marker
        await db.create_workstream(ws)

        await _run_scheduled(orch)

        assert await db.count_approver_runs("ws-006") == 0
    finally:
        await db.close()


async def test_missing_context_is_observed_not_attempted(
    tmp_path: Path, worktree: Path
) -> None:
    cfg = _approver_cfg(tmp_path, _PASS_SCRIPT)
    orch, db = await _make_orch(tmp_path, approver=cfg)
    try:
        sha = _git(worktree, "rev-parse", "HEAD")
        await db.create_workstream(_blocked_ws(sha, worktree))
        # no context row seeded

        await _run_scheduled(orch)

        assert await db.count_approver_runs("ws-006") == 0
    finally:
        await db.close()


@pytest.mark.parametrize(
    ("overrides", "seed"),
    [
        ({"max_evaluations": 0}, None),
        ({"max_auto_approvals": 0}, None),
        ({"max_escaped_paths": 0}, None),
        ({"max_cost_usd": 1.0}, "unknown_cost_attempt"),
    ],
)
async def test_budget_guards_block_without_attempt(
    tmp_path: Path, worktree: Path, overrides: dict, seed: str | None
) -> None:
    cfg = _approver_cfg(tmp_path, _PASS_SCRIPT, **overrides)
    orch, db = await _make_orch(tmp_path, approver=cfg)
    try:
        sha = _git(worktree, "rev-parse", "HEAD")
        await db.create_workstream(_blocked_ws(sha, worktree))
        await _seed_context(db, sha)
        if seed == "unknown_cost_attempt":
            # a prior attempt with unreported cost → budget unprovable
            await db.insert_approver_run_started("01X", "ws-006", "ex_post", "c" * 40)
            await db.finalize_approver_run("01X", "fail")

        await _run_scheduled(orch)

        assert await db.has_approver_run("ws-006", "ex_post", sha) is False
        assert (await db.get_workstream("ws-006")).status == (
            WorkstreamStatus.NEEDS_REVIEW
        )
    finally:
        await db.close()


async def test_oversize_diff_goes_to_human(tmp_path: Path, worktree: Path) -> None:
    cfg = _approver_cfg(tmp_path, _PASS_SCRIPT, max_diff_bytes=1)
    orch, db = await _make_orch(tmp_path, approver=cfg)
    try:
        sha = _git(worktree, "rev-parse", "HEAD")
        await db.create_workstream(_blocked_ws(sha, worktree))
        await _seed_context(db, sha)

        await _run_scheduled(orch)

        assert await db.has_approver_run("ws-006", "ex_post", sha) is False
    finally:
        await db.close()


async def test_already_attempted_not_rerun(tmp_path: Path, worktree: Path) -> None:
    cfg = _approver_cfg(tmp_path, _FAIL_SCRIPT)
    orch, db = await _make_orch(tmp_path, approver=cfg)
    try:
        sha = _git(worktree, "rev-parse", "HEAD")
        await db.create_workstream(_blocked_ws(sha, worktree))
        await _seed_context(db, sha)

        await _run_scheduled(orch)
        assert await db.count_approver_runs("ws-006") == 1
        await _run_scheduled(orch)  # second pass: one paid evaluation per SHA
        assert await db.count_approver_runs("ws-006") == 1
    finally:
        await db.close()


# =============================================================================
# Lifecycle (§8)
# =============================================================================


async def test_interrupted_runs_finalized_at_startup(tmp_path: Path) -> None:
    orch, db = await _make_orch(tmp_path, approver=None)
    try:
        await db.insert_approver_run_started("01A", "ws-006", "ex_post", "a" * 40)

        await orch._finalize_interrupted_approver_runs()

        assert await db.list_started_approver_runs() == []
        _, has_unknown = await db.approver_cost_stats("ws-006")
        assert has_unknown is True  # error row with unreported cost
    finally:
        await db.close()


async def test_inflight_evaluation_counts_as_active_work(tmp_path: Path) -> None:
    orch, db = await _make_orch(tmp_path, approver=None)
    try:

        async def _sleep() -> None:
            await asyncio.sleep(10)

        task = asyncio.create_task(_sleep())
        orch._approver_tasks["ws-006"] = task
        assert await orch._all_workstreams_complete() is False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await db.close()


async def test_drain_cancels_on_requested_shutdown(tmp_path: Path) -> None:
    orch, db = await _make_orch(tmp_path, approver=None)
    try:
        started = asyncio.Event()

        async def _hang() -> None:
            started.set()
            await asyncio.sleep(60)

        orch._approver_tasks["ws-006"] = asyncio.create_task(_hang())
        await started.wait()
        orch._shutdown_requested = True

        await asyncio.wait_for(orch._drain_approver_tasks(), timeout=5)
    finally:
        await db.close()
