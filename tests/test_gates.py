"""Gates runtime (WS-006 handoff M-1..M-3): GateKeeper, verdict records, fail-closed."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path  # noqa: TC003

import pytest

from maestro.gates import (
    APPROVAL_MARKER_PREFIX,
    GateKeeper,
    GateVerdictRecord,
    parse_approval_marker,
    pipeline_log_dir,
)
from maestro.models import GatesConfig, OrchestratorConfig, WorkstreamStatus


_SHA_RE = r"[0-9a-f]{40}"

# A stand-in steward binary: implements just enough of `risk-classify` for the
# tests — tier comes from the TIER file next to it; scope_violation is computed
# from facts.json exactly like the real classifier would flag it.
_FAKE_STEWARD = """#!/usr/bin/env python3
import json, pathlib, sys

args = sys.argv[1:]
assert args[0] == "risk-classify", args
here = pathlib.Path(__file__).parent
tier = (here / "TIER").read_text().strip()

def arg(flag):
    return args[args.index(flag) + 1] if flag in args else None

flags = []
phase = "ex_ante"
sha = "0" * 40
if arg("--declared"):
    data = json.loads(pathlib.Path(arg("--declared")).read_text())
    sha = data["sha"]
elif arg("--no-fs"):
    phase = "ex_post"
    data = json.loads(pathlib.Path(arg("--no-fs")).read_text())
    sha = data["sha"]
    declared = data.get("declared_scope")
    if declared is not None:
        import fnmatch
        for p in data["paths"]:
            if not any(fnmatch.fnmatch(p, g) or p.startswith(g.rstrip("*/")) for g in declared):
                flags.append("scope_violation")
                tier = "high"
                break
else:
    sys.exit(2)

gates = {
    "low": [],
    "medium": ["steward.gate_check"],
    "high": ["steward.gate_check", "maestro.validate_strict",
             "human.owner_approval", "git.required_reviews"],
    "critical": ["steward.gate_check", "maestro.validate_strict",
                 "human.owner_approval", "git.required_reviews",
                 "human.transition_approval"],
}[tier]
print(json.dumps({
    "tier": tier, "phase": phase,
    "inputs": {"change_class": "code", "blast_radius": "single-repo",
               "trust_boundary": "none"},
    "dominant_axis": "change_class", "floor_profile": "lite",
    "mandatory_gates": gates, "flags": flags, "sha": sha,
    "risk_model_version": "sha256:" + "f" * 64,
}))
"""


@pytest.fixture()
def fake_steward(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    steward = bin_dir / "steward"
    steward.write_text(_FAKE_STEWARD)
    steward.chmod(steward.stat().st_mode | stat.S_IEXEC)
    (bin_dir / "TIER").write_text("low")
    return steward


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A tiny git repo with one commit on master."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, env=env
        )

    git("init", "-b", "master")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-m", "base")
    return repo


def _keeper(fake_steward: Path, repo: Path, tmp_path: Path) -> GateKeeper:
    gates = GatesConfig(steward_bin=str(fake_steward))
    return GateKeeper(
        gates,
        project="demo",
        repo_path=repo,
        base_branch="master",
        log_dir=tmp_path / "logs" / "01TEST",
    )


def _make_keeper(tmp_path: Path) -> GateKeeper:
    """A GateKeeper for `_decide`-only tests (pure method — no steward/git).

    `_decide` never shells out, so unlike `_keeper` this needs no
    `fake_steward`/`repo` fixtures.
    """
    return GateKeeper(
        GatesConfig(steward_bin="/nonexistent"),
        project="demo",
        repo_path=tmp_path,
        base_branch="master",
        log_dir=tmp_path / "logs",
    )


def _set_tier(fake_steward: Path, tier: str) -> None:
    (fake_steward.parent / "TIER").write_text(tier)


# ---------------------------------------------------------------- ex-ante


async def test_ex_ante_low_tier_allows_and_writes_records(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_ante("ws-1", ["src/**"], approvals=set())
    assert decision.allow
    gate_ids = [r.gate_id for r in decision.records]
    assert "steward.risk_classify_ex_ante" in gate_ids
    jsonl = tmp_path / "logs" / "01TEST" / "gate_verdicts.jsonl"
    lines = [json.loads(line) for line in jsonl.read_text().splitlines()]
    assert lines and lines[0]["risk_model_version"].startswith("sha256:")
    assert all(len(rec["sha"]) == 40 for rec in lines if rec.get("sha"))


async def test_ex_ante_high_tier_blocks_for_approval(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    _set_tier(fake_steward, "high")
    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_ante("ws-1", ["src/**"], approvals=set())
    assert not decision.allow
    assert decision.reason and APPROVAL_MARKER_PREFIX in decision.reason
    approvals = [r for r in decision.records if r.gate_id == "human.owner_approval"]
    assert approvals and approvals[0].verdict == "missing"
    assert approvals[0].enforcement == "mandatory"


async def test_ex_ante_operator_requeue_counts_as_approval(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    # Blocked once -> operator approves (gates v1.3: recorded in the DB
    # approvals set, same phase + same sha) -> re-evaluation allows.
    _set_tier(fake_steward, "high")
    keeper = _keeper(fake_steward, repo, tmp_path)
    first = await keeper.evaluate_ex_ante("ws-1", ["src/**"], approvals=set())
    assert not first.allow
    marker = parse_approval_marker(first.reason)
    assert marker is not None
    second = await keeper.evaluate_ex_ante(
        "ws-1", ["src/**"], approvals={(marker.phase, marker.sha)}
    )
    assert second.allow
    approvals = [r for r in second.records if r.gate_id == "human.owner_approval"]
    assert approvals and approvals[0].verdict == "pass"


async def test_ex_ante_approval_invalidated_by_new_sha(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    # M-3/DESIGN-608: a new commit changes the sha, so the approval
    # recorded for the old sha no longer matches.
    _set_tier(fake_steward, "high")
    keeper = _keeper(fake_steward, repo, tmp_path)
    first = await keeper.evaluate_ex_ante("ws-1", ["src/**"], approvals=set())
    marker = parse_approval_marker(first.reason)
    assert marker is not None
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    (repo / "src" / "a.py").write_text("x = 2\n")
    subprocess.run(  # noqa: ASYNC221
        ["git", "-C", str(repo), "commit", "-am", "more"],
        check=True,
        capture_output=True,
        env=env,
    )
    second = await keeper.evaluate_ex_ante(
        "ws-1", ["src/**"], approvals={(marker.phase, marker.sha)}
    )
    assert not second.allow


async def test_missing_binary_fails_closed(repo: Path, tmp_path: Path) -> None:
    gates = GatesConfig(steward_bin=str(tmp_path / "nope"))
    keeper = GateKeeper(
        gates,
        project="demo",
        repo_path=repo,
        base_branch="master",
        log_dir=tmp_path / "logs" / "01TEST",
    )
    decision = await keeper.evaluate_ex_ante("ws-1", ["src/**"], approvals=set())
    assert not decision.allow
    errs = [r for r in decision.records if r.verdict == "error"]
    assert errs and errs[0].enforcement == "mandatory"


async def test_steward_failure_fails_closed(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    fake_steward.write_text("#!/bin/sh\nexit 2\n")  # noqa: ASYNC240
    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_ante("ws-1", ["src/**"], approvals=set())
    assert not decision.allow
    assert any(r.verdict == "error" for r in decision.records)


# ---------------------------------------------------------------- ex-post


async def test_ex_post_within_scope_allows(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    workspace = _branch_with_change(repo, "src/a.py", "x = 3\n")
    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_post(
        "ws-1", ["src/**"], workspace=workspace, approvals=set()
    )
    assert decision.allow


async def test_ex_post_scope_violation_blocks(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    workspace = _branch_with_change(repo, "rogue.txt", "outside\n")
    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_post(
        "ws-1", ["src/**"], workspace=workspace, approvals=set()
    )
    assert not decision.allow


async def test_evaluate_ex_post_uses_branch_point_no_false_escape(tmp_path):
    """Base advanced by a sibling commit must not appear in ex-post diff paths."""
    import asyncio

    from maestro.changed_paths import changed_paths_since

    async def g(repo, *a):
        p = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo),
            *a,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        o, e = await p.communicate()
        assert p.returncode == 0, e.decode()
        return o.decode()

    repo = tmp_path / "r"
    repo.mkdir()
    await g(repo, "init", "-b", "main")
    await g(repo, "config", "user.email", "t@t.t")
    await g(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("1\n")
    await g(repo, "add", "-A")
    await g(repo, "commit", "-m", "0")
    await g(repo, "checkout", "-b", "feature")
    (repo / "mine.py").write_text("m\n")
    await g(repo, "add", "-A")
    await g(repo, "commit", "-m", "f")
    await g(repo, "checkout", "main")
    (repo / "sibling.py").write_text("s\n")
    await g(repo, "add", "-A")
    await g(repo, "commit", "-m", "s")
    assert await changed_paths_since("main", "feature", repo) == ["mine.py"]


async def test_evaluate_ex_post_ignores_advanced_base_no_false_scope_violation(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    """Regression (final review Important #1): pin the branch-point delegation
    at gates.py:180 THROUGH the public `evaluate_ex_post` method, not just via
    `changed_paths_since` directly (see the test above) — every other ex-post
    test uses a single commit off base, where two-dot `base..HEAD` happens to
    equal the branch-point diff, so a revert to that two-dot form would keep
    the rest of the suite green.
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, env=env
        )

    # In-scope commit on feature/ws-1, branched off master.
    workspace = _branch_with_change(repo, "src/a.py", "x = 3\n")
    # Base branch advances AFTER the branch point with an out-of-scope change.
    git("checkout", "master")
    (repo / "docs").mkdir()
    (repo / "docs" / "evil.md").write_text("evil\n")
    git("add", "-A")
    git("commit", "-m", "docs: sibling change")
    # Back on the feature branch, so HEAD is the workstream's own commit —
    # mirrors a real worktree, which stays checked out on the feature branch.
    git("checkout", "feature/ws-1")

    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_post(
        "ws-1", ["src/**"], workspace=workspace, approvals=set()
    )
    # Under the two-dot bug, docs/evil.md would leak into the diff paths and
    # the fake steward would flag scope_violation -> allow is False.
    assert decision.allow is True, (
        decision.reason,
        [r.note for r in decision.records],
    )
    assert not any("scope_violation" in (r.note or "") for r in decision.records)


def _branch_with_change(repo: Path, rel: str, content: str) -> Path:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, env=env
        )

    git("switch", "-c", "feature/ws-1")
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git("add", "-A")
    git("commit", "-m", "change")
    return repo


# ---------------------------------------------------------------- config + state machine


def test_gates_config_parses_from_yaml() -> None:
    cfg = OrchestratorConfig.model_validate(
        {
            "project": "demo",
            "repo_url": "https://example.com/x",
            "repo_path": "/tmp/x",
            "workspace_base": "/tmp/ws",
            "workstreams": [],
            "gates": {"steward_bin": "/usr/local/bin/steward", "profile": "team"},
        }
    )
    assert cfg.gates is not None
    assert cfg.gates.profile == "team"
    assert cfg.gates.mode == "fail_closed"


def test_gates_absent_by_default() -> None:
    cfg = OrchestratorConfig(
        project="demo",
        repo_url="https://example.com/x",
        repo_path="/tmp/x",
        workspace_base="/tmp/ws",
        workstreams=[],
    )
    assert cfg.gates is None


def test_ready_to_needs_review_is_legal() -> None:
    # gates need a legal edge from READY to human review
    assert (
        WorkstreamStatus.NEEDS_REVIEW
        in WorkstreamStatus.valid_transitions()[WorkstreamStatus.READY]
    )


def test_pipeline_log_dir_honors_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path / "custom"))
    assert pipeline_log_dir() == tmp_path / "custom"


# ---------------------------------------------------------------- preflight


def _config(**gates: str) -> OrchestratorConfig:
    return OrchestratorConfig.model_validate(
        {
            "project": "demo",
            "repo_url": "https://example.com/x",
            "repo_path": "/tmp/x",
            "workspace_base": "/tmp/ws",
            "workstreams": [],
            "gates": dict(gates) or None,
        }
    )


def test_preflight_gates_missing_binary_is_error(tmp_path: Path) -> None:
    from maestro.preflight import validate_project

    report = validate_project(
        _config(steward_bin=str(tmp_path / "nope")), check_fs=True
    )
    assert any(i.code == "gates-steward-missing" for i in report.errors)


def test_preflight_gates_missing_risk_model_is_error(
    fake_steward: Path, tmp_path: Path
) -> None:
    from maestro.preflight import validate_project

    report = validate_project(
        _config(
            steward_bin=str(fake_steward),
            risk_model=str(tmp_path / "no-model.yaml"),
        ),
        check_fs=True,
    )
    assert any(i.code == "gates-risk-model-missing" for i in report.errors)


def test_preflight_gates_ok_with_valid_binary(fake_steward: Path) -> None:
    from maestro.preflight import validate_project

    report = validate_project(_config(steward_bin=str(fake_steward)), check_fs=True)
    assert not any(i.code.startswith("gates-") for i in report.errors)


def test_preflight_gates_skips_fs_checks_when_no_fs(tmp_path: Path) -> None:
    from maestro.preflight import validate_project

    report = validate_project(
        _config(steward_bin=str(tmp_path / "nope")), check_fs=False
    )
    assert not any(i.code.startswith("gates-") for i in report.errors)


# ---------------------------------------------------------------- orchestrator hooks


class _FakeKeeper:
    def __init__(self, allow: bool) -> None:
        from maestro.gates import GateDecision

        self._decision = GateDecision(
            allow=allow, reason=None if allow else "gates: blocked. marker"
        )
        self.calls: list[str] = []

    async def evaluate_ex_ante(self, *a: object, **kw: object):
        self.calls.append("ex_ante")
        return self._decision

    async def evaluate_ex_post(self, *a: object, **kw: object):
        self.calls.append("ex_post")
        return self._decision


def _workstream(status: WorkstreamStatus):
    from maestro.models import Workstream

    return Workstream(
        id="ws-1",
        title="t",
        description="d",
        scope=["src/**"],
        status=status,
        branch="feature/ws-1",
    )


async def test_orchestrator_ex_ante_block_routes_to_needs_review(
    tmp_path: Path,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from maestro.orchestrator import Orchestrator
    from maestro.transitions import TransitionDispatcher

    orch = Orchestrator.__new__(Orchestrator)
    orch._gates = _FakeKeeper(allow=False)  # type: ignore[assignment]
    orch._db = MagicMock()

    async def _update_workstream_status(
        workstream_id: str, new_status: WorkstreamStatus, **kw: object
    ):
        # `_transition` reads the return value to build the dispatcher
        # subject — must be a real Workstream, not a bare AsyncMock.
        return _workstream(new_status)

    orch._db.update_workstream_status = AsyncMock(side_effect=_update_workstream_status)
    orch._db.list_gate_approvals = AsyncMock(return_value=set())
    orch._logger = MagicMock()
    # `_transition` fires through the dispatcher (Task 7 wiring) — a
    # no-op one here since this test only asserts the DB write.
    orch._dispatcher = TransitionDispatcher(
        notifier=None, event_logger_getter=lambda: None, status_change_cb=None
    )
    allowed = await orch._gate_ex_ante("ws-1", _workstream(WorkstreamStatus.READY))
    assert allowed is False
    kwargs = orch._db.update_workstream_status.call_args.kwargs
    args = orch._db.update_workstream_status.call_args.args
    assert (
        WorkstreamStatus.NEEDS_REVIEW in args
        or kwargs.get("status") == WorkstreamStatus.NEEDS_REVIEW
    )
    assert "gates:" in kwargs.get("error_message", "")


async def test_orchestrator_gates_disabled_is_noop() -> None:
    from unittest.mock import MagicMock

    from maestro.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._gates = None
    orch._db = MagicMock()
    assert await orch._gate_ex_ante("ws-1", _workstream(WorkstreamStatus.READY))


async def test_structurally_invalid_steward_json_fails_closed(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    # Copilot (PR #73): JSON that parses but lacks the contract shape must
    # become an error verdict, not a KeyError escaping the guard.
    fake_steward.write_text('#!/bin/sh\necho "{}"\n')  # noqa: ASYNC240
    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_ante("ws-1", ["src/**"], approvals=set())
    assert not decision.allow
    assert any(r.verdict == "error" for r in decision.records)


async def test_non_object_steward_json_fails_closed(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    fake_steward.write_text('#!/bin/sh\necho "[1, 2]"\n')  # noqa: ASYNC240
    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_ante("ws-1", ["src/**"], approvals=set())
    assert not decision.allow


# ------------------------------------------- gates v1.1 (governed-run findings H-3..H-5)

# NOTE (gates v1.3, H-9): the H-3 verdict-store fallback (`_prior_block_recorded`
# reading gate_verdicts.jsonl to infer a re-queue approval) is DELETED — the DB
# `gate_approvals` set is now the single authority. The three tests that used to
# exercise that fallback (test_h3_approval_survives_via_verdict_store,
# test_h3_store_approval_still_invalidated_by_new_sha,
# test_h3_other_workstream_block_does_not_approve) are removed with it; their
# sha-binding and per-workstream-isolation guarantees are re-covered under the
# new contract by TestApprovalsAuthority below and
# test_ex_ante_approval_invalidated_by_new_sha above.


async def test_h4_orchestrator_managed_paths_do_not_violate_scope(
    fake_steward: Path, repo: Path, tmp_path: Path
) -> None:
    # H-4 (narrowed in gates v1.2/H-7): Maestro itself commits
    # spec/maestro-* + spec/.executor-* into the branch; those infra paths
    # must not trip the declared-scope check.
    workspace = _branch_with_change(repo, "src/a.py", "x = 9\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    spec = workspace / "spec"
    spec.mkdir(exist_ok=True)
    (spec / "maestro-tasks.md").write_text("# t\n")
    (spec / ".executor-maestro-state.db").write_text("sqlite\n")
    subprocess.run(  # noqa: ASYNC221
        ["git", "-C", str(workspace), "add", "-A"],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(  # noqa: ASYNC221
        ["git", "-C", str(workspace), "commit", "-m", "maestro: add spec"],
        check=True,
        capture_output=True,
        env=env,
    )
    keeper = _keeper(fake_steward, repo, tmp_path)
    decision = await keeper.evaluate_ex_post(
        "ws-1", ["src/**"], workspace=workspace, approvals=set()
    )
    assert decision.allow, (decision.reason, [r.note for r in decision.records])


def test_workstream_approve_records_durable_approval(tmp_path: Path) -> None:
    """gates v1.3: the sanctioned CLI point writes the gate_approvals row."""
    import asyncio as _asyncio

    sha = "a" * 40

    async def scenario():
        from maestro.cli import _approve_workstream
        from maestro.database import Database
        from maestro.gates import APPROVAL_MARKER_PREFIX
        from maestro.models import Workstream, WorkstreamStatus

        db = Database(tmp_path / "t.db")
        await db.connect()
        try:
            await db.create_workstream(
                Workstream(
                    id="ws-1",
                    title="t",
                    description="d",
                    scope=["s"],
                    branch="feature/ws-1",
                    status=WorkstreamStatus.NEEDS_REVIEW,
                    error_message=(
                        "gates: human.owner_approval required (tier=high); "
                        "re-queue to approve. "
                        f"{APPROVAL_MARKER_PREFIX} phase=ex_post sha={sha}"
                    ),
                )
            )
            marker = await _approve_workstream(db, "ws-1")
            after = await db.get_workstream("ws-1")
            approvals = await db.list_gate_approvals("ws-1")
            return marker, after, approvals
        finally:
            await db.close()

    marker, after, approvals = _asyncio.run(scenario())
    assert after.status == WorkstreamStatus.READY
    assert after.error_message and "phase=ex_post" in after.error_message
    assert approvals == {("ex_post", sha)}
    assert marker is not None and marker.phase == "ex_post" and marker.sha == sha


def test_workstream_approve_without_marker_records_nothing(tmp_path: Path) -> None:
    import asyncio as _asyncio

    async def scenario():
        from maestro.cli import _approve_workstream
        from maestro.database import Database
        from maestro.models import Workstream, WorkstreamStatus

        db = Database(tmp_path / "t.db")
        await db.connect()
        try:
            await db.create_workstream(
                Workstream(
                    id="ws-2",
                    title="t",
                    description="d",
                    scope=["s"],
                    branch="feature/ws-2",
                    status=WorkstreamStatus.NEEDS_REVIEW,
                    error_message="Base merge failed: conflict",
                )
            )
            marker = await _approve_workstream(db, "ws-2")
            return (
                marker,
                await db.list_gate_approvals("ws-2"),
                (await db.get_workstream("ws-2")).status,
            )
        finally:
            await db.close()

    marker, approvals, status = _asyncio.run(scenario())
    assert marker is None
    assert approvals == set()
    assert status == WorkstreamStatus.READY


def test_workstream_approve_takes_effect_in_next_gate_evaluation(
    tmp_path: Path,
) -> None:
    """End-to-end CLI -> DB -> gate loop (Task 3 final-review gap).

    Seeds a NEEDS_REVIEW workstream with an ex_post marker for sha X,
    approves it via the sanctioned CLI entry point, then feeds the
    resulting DB approvals set straight into `GateKeeper._decide` for
    that same phase+sha and asserts the gate now allows. Before this
    task's rewire, `_approve_workstream` recorded nothing in
    `gate_approvals`, so this evaluation would still block.
    """
    import asyncio as _asyncio

    sha = "c" * 40

    async def scenario():
        from maestro.cli import _approve_workstream
        from maestro.database import Database
        from maestro.gates import APPROVAL_MARKER_PREFIX
        from maestro.models import Workstream, WorkstreamStatus

        db = Database(tmp_path / "loop.db")
        await db.connect()
        try:
            await db.create_workstream(
                Workstream(
                    id="ws-loop",
                    title="t",
                    description="d",
                    scope=["s"],
                    branch="feature/ws-loop",
                    status=WorkstreamStatus.NEEDS_REVIEW,
                    error_message=(
                        "gates: human.owner_approval required (tier=high); "
                        "re-queue to approve. "
                        f"{APPROVAL_MARKER_PREFIX} phase=ex_post sha={sha}"
                    ),
                )
            )
            await _approve_workstream(db, "ws-loop")
            return await db.list_gate_approvals("ws-loop")
        finally:
            await db.close()

    approvals = _asyncio.run(scenario())
    keeper = _make_keeper(tmp_path)
    decision = keeper._decide(
        "ex_post",
        "ws-loop",
        sha,
        {"tier": "high", "mandatory_gates": [], "flags": []},
        approvals=approvals,
    )
    assert decision.allow is True


# --------------------------------- gates v1.2 (H-6): resume via approval marker


class TestOrchestratorManagedNarrowing:
    """gates v1.2 (H-7): only maestro-namespaced artifacts are infra.

    `_orchestrator_managed` moved to `maestro.changed_paths` (Task 4) — this
    class now exercises it there, since `changed_paths_since` is the sole
    caller after `GateKeeper.evaluate_ex_post` was rewired to delegate to it.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "spec/maestro-tasks.md",
            "spec/maestro-requirements.md",
            "spec/.executor-maestro-state.db",
            "spec/.executor-stop",
            # H-8: dot-before-prefix harness files (spec-runner writes these).
            "spec/.maestro-task-history.log",
            "spec/.maestro-spec.lock",
        ],
    )
    def test_harness_paths_excluded(self, path: str) -> None:
        from maestro.changed_paths import _orchestrator_managed

        assert _orchestrator_managed(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "spec/00-charter.md",  # target repo's own governance doc
            "spec/tasks.md",  # target repo's own dogfood spec
            "spec/.gitignore",  # dot-prefixed but not maestro- (H-8 no over-match)
            "spec-runner.config.yaml",  # tracked-config clobber must be visible
            "src/main.py",
        ],
    )
    def test_target_repo_paths_visible(self, path: str) -> None:
        from maestro.changed_paths import _orchestrator_managed

        assert _orchestrator_managed(path) is False


class TestParseApprovalMarker:
    """gates v1.2 (H-6): parse the phase+sha marker out of a stored block reason."""

    def test_round_trip_with_blocked_reason_format(self) -> None:
        from maestro.gates import APPROVAL_MARKER_PREFIX, parse_approval_marker

        sha = "a" * 40
        reason = (
            "gates: human.owner_approval required (tier=high); "
            f"re-queue to approve. {APPROVAL_MARKER_PREFIX} phase=ex_post sha={sha}"
        )
        marker = parse_approval_marker(reason)
        assert marker is not None
        assert marker.phase == "ex_post"
        assert marker.sha == sha

    def test_ex_ante_phase_parses(self) -> None:
        from maestro.gates import APPROVAL_MARKER_PREFIX, parse_approval_marker

        sha = "b" * 40
        marker = parse_approval_marker(
            f"{APPROVAL_MARKER_PREFIX} phase=ex_ante sha={sha}"
        )
        assert marker is not None
        assert marker.phase == "ex_ante"
        assert marker.sha == sha

    def test_none_and_garbage_return_none(self) -> None:
        from maestro.gates import parse_approval_marker

        assert parse_approval_marker(None) is None
        assert parse_approval_marker("") is None
        assert parse_approval_marker("spec-runner exited with code 2") is None
        # Prefix present but malformed tail -> no marker.
        assert parse_approval_marker("gates:approval-required phase=bogus") is None


class TestApprovalsAuthority:
    """gates v1.3 (H-9): the DB approvals set is the ONLY approval source."""

    def test_approved_when_pair_in_set(self, tmp_path) -> None:
        keeper = _make_keeper(tmp_path)  # use the file's existing helper/fixture
        sha = "a" * 40
        decision = keeper._decide(
            "ex_post",
            "z1",
            sha,
            {"tier": "high", "mandatory_gates": [], "flags": []},
            approvals={("ex_post", sha)},
        )
        assert decision.allow is True

    def test_marker_in_message_does_NOT_grant_approval(self, tmp_path) -> None:
        """KEY regression guard: pre-v1.3 the marker in prior_error granted
        approval; now only the DB set does."""
        keeper = _make_keeper(tmp_path)
        sha = "a" * 40
        decision = keeper._decide(
            "ex_post",
            "z1",
            sha,
            {"tier": "high", "mandatory_gates": [], "flags": []},
            approvals=set(),
        )
        assert decision.allow is False
        assert "re-queue to approve" in (decision.reason or "")

    def test_sha_bound(self, tmp_path) -> None:
        """DESIGN-608: an approval for sha X never approves sha Y."""
        keeper = _make_keeper(tmp_path)
        decision = keeper._decide(
            "ex_post",
            "z1",
            "b" * 40,  # evaluated at sha Y...
            {"tier": "high", "mandatory_gates": [], "flags": []},
            approvals={("ex_post", "a" * 40)},  # ...approved at sha X
        )
        assert decision.allow is False

    def test_prior_block_recorded_is_gone(self) -> None:
        from maestro.gates import GateKeeper

        assert not hasattr(GateKeeper, "_prior_block_recorded")

    def test_block_reason_uses_exported_prefix(self, tmp_path) -> None:
        from maestro.gates import BLOCK_REASON_PREFIX

        keeper = _make_keeper(tmp_path)
        decision = keeper._decide(
            "ex_ante",
            "z1",
            "c" * 40,
            {"tier": "high", "mandatory_gates": [], "flags": []},
            approvals=set(),
        )
        assert (decision.reason or "").startswith(BLOCK_REASON_PREFIX)


# ------------------------------------- gates v1.3 (H-9): cross-restart e2e


def test_cross_phase_approval_survives_restart(tmp_path) -> None:
    """gates v1.3: an ex-ante approval recorded in the DB survives an
    ex-post block message AND a new GateKeeper with a fresh log_dir
    (= orchestrator restart, new pipeline ULID)."""
    import asyncio as _asyncio

    sha = "a" * 40

    async def scenario():
        from maestro.database import Database

        db = Database(tmp_path / "t.db")
        await db.connect()
        try:
            await db.record_gate_approval("z1", "ex_ante", sha)
            return await db.list_gate_approvals("z1")
        finally:
            await db.close()

    approvals = _asyncio.run(scenario())
    # A brand-new keeper (fresh log_dir simulates the restart) approves
    # ex-ante from the DB set alone; the ex-post pair is still missing.
    keeper = _make_keeper(tmp_path / "fresh-logs")
    ex_ante = keeper._decide(
        "ex_ante",
        "z1",
        sha,
        {"tier": "high", "mandatory_gates": [], "flags": []},
        approvals=approvals,
    )
    ex_post = keeper._decide(
        "ex_post",
        "z1",
        sha,
        {"tier": "high", "mandatory_gates": [], "flags": []},
        approvals=approvals,
    )
    assert ex_ante.allow is True
    assert ex_post.allow is False  # per-phase, not per-workstream


class TestPreserveApprovalMarker:
    def test_appends_marker_from_prior(self) -> None:
        from maestro.gates import APPROVAL_MARKER_PREFIX, preserve_approval_marker

        marker = f"{APPROVAL_MARKER_PREFIX} phase=ex_post sha={'a' * 40}"
        out = preserve_approval_marker("spec-runner exited 1", f"blocked. {marker}")
        assert out == f"spec-runner exited 1 | {marker}"

    def test_idempotent_no_duplication(self) -> None:
        from maestro.gates import APPROVAL_MARKER_PREFIX, preserve_approval_marker

        marker = f"{APPROVAL_MARKER_PREFIX} phase=ex_post sha={'a' * 40}"
        once = preserve_approval_marker("err2", f"err1 | {marker}")
        assert once.count(APPROVAL_MARKER_PREFIX) == 1
        # New message already carrying the marker is returned as-is.
        assert preserve_approval_marker(once, once) == once

    def test_no_marker_passthrough(self) -> None:
        from maestro.gates import preserve_approval_marker

        assert preserve_approval_marker("err", None) == "err"
        assert preserve_approval_marker("err", "plain failure") == "err"


# ------------------------------------------------ approver evidence (#137)


def test_verdict_record_accepts_not_run() -> None:
    """§6: observations use the semantically-neutral not_run, never waived."""
    rec = GateVerdictRecord(
        gate_id="agent.approver",
        enforcement="advisory",
        verdict="not_run",
        phase="ex_post",
        ts="2026-08-06T00:00:00+00:00",
        workstream_id="ws-1",
        note="disabled",
    )
    assert rec.verdict == "not_run"


def test_verdict_record_serializes_schema_discriminator() -> None:
    """§7.3: every line carries the internal-format discriminator so the
    file is never mistaken for steward's contracts/gate-verdicts/v1."""
    rec = GateVerdictRecord(
        gate_id="agent.approver",
        enforcement="advisory",
        verdict="not_run",
        phase="ex_post",
        ts="2026-08-06T00:00:00+00:00",
        workstream_id="ws-1",
    )
    dumped = rec.model_dump(by_alias=True, exclude_none=True)
    assert dumped["schema"] == "maestro.gate-verdict-record/v2"


async def test_append_records_writes_schema_field(tmp_path: Path) -> None:
    keeper = _make_keeper(tmp_path)
    rec = GateVerdictRecord(
        gate_id="agent.approver",
        enforcement="advisory",
        verdict="not_run",
        phase="ex_post",
        ts="2026-08-06T00:00:00+00:00",
        workstream_id="ws-1",
        note="stale_sha",
    )
    keeper.append_records([rec])
    jsonl = tmp_path / "logs" / "gate_verdicts.jsonl"
    lines = [json.loads(line) for line in jsonl.read_text().splitlines()]
    assert lines[-1]["schema"] == "maestro.gate-verdict-record/v2"
    assert lines[-1]["verdict"] == "not_run"
