"""Tests for maestro/run_branch_gate.py — spec §4 start matrix."""

import subprocess
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from maestro.run_branch_gate import (
    CheckoutSnapshot,
    RunBranchGateError,
    RunBranchRecord,
    StartAction,
    apply_start_gate,
    check_live,
    decide_start,
    verify_continuation,
)


B, BASE = "pilot/x", "master"


def snap(cur: str | None, exists: bool, dirty: list[str]) -> CheckoutSnapshot:
    return CheckoutSnapshot(current_branch=cur, target_exists=exists, dirty_paths=dirty)


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (snap(B, True, []), StartAction.PROCEED),
        (snap("other", True, []), StartAction.SWITCH),
        (snap(BASE, False, []), StartAction.CREATE),
    ],
)
def test_start_matrix_actions(
    snapshot: CheckoutSnapshot, expected: StartAction
) -> None:
    assert decide_start(snapshot, run_branch=B, base_branch=BASE) == expected


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (snap(B, True, ["f.txt"]), "dirty_tree"),
        (snap("other", True, ["f.txt"]), "dirty_tree"),
        (snap(BASE, False, ["f.txt"]), "dirty_tree"),
        (snap("other", False, []), "wrong_start_point"),  # B missing, cur != base
        (snap(None, True, []), "wrong_start_point"),  # detached HEAD
    ],
)
def test_start_matrix_refusals(snapshot: CheckoutSnapshot, reason: str) -> None:
    with pytest.raises(RunBranchGateError) as exc:
        decide_start(snapshot, run_branch=B, base_branch=BASE)
    assert exc.value.reason == reason


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "master")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("a")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-m", "init")
    return r


class TestApplyStartGate:
    def test_creates_from_base_and_switches(self, repo: Path) -> None:
        tip = apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "pilot/x"
        assert tip == _git(repo, "rev-parse", "HEAD")

    def test_switches_to_existing(self, repo: Path) -> None:
        _git(repo, "branch", "pilot/x")
        apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "pilot/x"

    def test_dirty_tree_refuses_and_does_not_switch(self, repo: Path) -> None:
        (repo / "a.txt").write_text("edited")
        with pytest.raises(RunBranchGateError) as exc:
            apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        assert exc.value.reason == "dirty_tree"
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "master"

    def test_non_git_directory_says_so(self, tmp_path: Path) -> None:
        """A `repo:` that is not a checkout at all used to read as an empty
        `git status` — a clean tree with no branch — and be refused as a
        detached HEAD, telling the operator to check out a branch somewhere
        with no branches to check out."""
        not_a_repo = tmp_path / "plain-dir"
        not_a_repo.mkdir()
        with pytest.raises(RunBranchGateError) as exc:
            apply_start_gate(not_a_repo, run_branch="pilot/x", base_branch="master")
        assert exc.value.reason == "wrong_start_point"
        assert "not a usable git checkout" in str(exc.value)


class TestVerifyContinuation:
    def test_matching_branch_and_tip(self, repo: Path) -> None:
        tip = apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        cur, dirty = verify_continuation(
            repo, RunBranchRecord(branch="pilot/x", head=tip), accept_tip=False
        )
        assert cur == tip
        assert dirty == []

    def test_wrong_branch_refuses(self, repo: Path) -> None:
        tip = apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        _git(repo, "switch", "master")
        with pytest.raises(RunBranchGateError) as exc:
            verify_continuation(
                repo, RunBranchRecord(branch="pilot/x", head=tip), accept_tip=False
            )
        assert exc.value.reason == "resume_branch_mismatch"

    def test_moved_tip_refuses_and_accept_tip_overrides(self, repo: Path) -> None:
        tip = apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        (repo / "b.txt").write_text("b")
        _git(repo, "add", "b.txt")
        _git(repo, "commit", "-m", "foreign")
        record = RunBranchRecord(branch="pilot/x", head=tip)
        with pytest.raises(RunBranchGateError) as exc:
            verify_continuation(repo, record, accept_tip=False)
        assert exc.value.reason == "resume_stale_checkout"
        cur, _ = verify_continuation(repo, record, accept_tip=True)
        assert cur == _git(repo, "rev-parse", "HEAD")

    def test_dirty_paths_returned_not_refused(self, repo: Path) -> None:
        tip = apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        (repo / "a.txt").write_text("edited")
        _, dirty = verify_continuation(
            repo, RunBranchRecord(branch="pilot/x", head=tip), accept_tip=False
        )
        assert dirty == ["a.txt"]

    def test_null_head_skips_stale_check(self, repo: Path) -> None:
        apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        verify_continuation(
            repo, RunBranchRecord(branch="pilot/x", head=None), accept_tip=False
        )


class TestGateEvents:
    """Spec §8: run_branch_gate.{created,verified,refused} as STRUCTURED obs
    events — `Attributes.event` carries the name and branch/reason/tip ride
    as attributes (codex round on PR #223: stdlib-bridge records all land as
    `log.stdlib`, so message-text logging was not queryable by event name).
    Best-effort telemetry; the stderr text remains the contract — but the
    pilot's acceptance built on these traces (issue #216)."""

    def test_create_emits_created(self, repo: Path) -> None:
        with capture_logs() as logs:
            apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        created = [e for e in logs if e["event"] == "run_branch_gate.created"]
        assert created and created[0]["branch"] == "pilot/x"
        assert created[0]["tip"]

    def test_proceed_emits_verified(self, repo: Path) -> None:
        apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        with capture_logs() as logs:
            apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        events = [e["event"] for e in logs]
        assert "run_branch_gate.verified" in events
        assert "run_branch_gate.created" not in events

    def test_start_refusal_emits_refused_with_reason(self, repo: Path) -> None:
        (repo / "a.txt").write_text("edited")
        with capture_logs() as logs, pytest.raises(RunBranchGateError):
            apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        refused = [e for e in logs if e["event"] == "run_branch_gate.refused"]
        assert refused and refused[0]["reason"] == "dirty_tree"

    def test_continuation_success_emits_verified(self, repo: Path) -> None:
        tip = apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        with capture_logs() as logs:
            verify_continuation(
                repo, RunBranchRecord(branch="pilot/x", head=tip), accept_tip=False
            )
        verified = [e for e in logs if e["event"] == "run_branch_gate.verified"]
        assert verified and verified[0]["branch"] == "pilot/x"

    def test_continuation_refusal_emits_refused(self, repo: Path) -> None:
        tip = apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        _git(repo, "switch", "master")
        with capture_logs() as logs, pytest.raises(RunBranchGateError):
            verify_continuation(
                repo, RunBranchRecord(branch="pilot/x", head=tip), accept_tip=False
            )
        refused = [e for e in logs if e["event"] == "run_branch_gate.refused"]
        assert refused and refused[0]["reason"] == "resume_branch_mismatch"


class TestCheckLive:
    """Spec §7: the per-seam live tripwire — name AND tip vs the record."""

    def test_passes_when_name_and_tip_match(self, repo: Path) -> None:
        _git(repo, "switch", "-c", "pilot/x")
        tip = _git(repo, "rev-parse", "refs/heads/pilot/x")
        check_live(repo, RunBranchRecord(branch="pilot/x", head=tip))  # no raise

    def test_branch_flip_trips_with_live_branch_mismatch(self, repo: Path) -> None:
        _git(repo, "switch", "-c", "pilot/x")
        tip = _git(repo, "rev-parse", "refs/heads/pilot/x")
        _git(repo, "switch", "master")
        with pytest.raises(RunBranchGateError) as exc:
            check_live(repo, RunBranchRecord(branch="pilot/x", head=tip))
        assert exc.value.reason == "live_branch_mismatch"

    def test_foreign_commit_same_branch_trips_stale(self, repo: Path) -> None:
        """Round-5 major 2: a commit on the SAME branch moves the state."""
        _git(repo, "switch", "-c", "pilot/x")
        recorded = _git(repo, "rev-parse", "refs/heads/pilot/x")
        (repo / "foreign.txt").write_text("x")
        _git(repo, "add", "foreign.txt")
        _git(repo, "commit", "-m", "foreign")
        with pytest.raises(RunBranchGateError) as exc:
            check_live(repo, RunBranchRecord(branch="pilot/x", head=recorded))
        assert exc.value.reason == "live_stale_checkout"

    def test_none_head_degrades_to_name_only(self, repo: Path) -> None:
        _git(repo, "switch", "-c", "pilot/x")
        (repo / "b.txt").write_text("b")
        _git(repo, "add", "b.txt")
        _git(repo, "commit", "-m", "moved")
        check_live(repo, RunBranchRecord(branch="pilot/x", head=None))  # no raise

    def test_detached_head_trips_mismatch(self, repo: Path) -> None:
        tip = _git(repo, "rev-parse", "HEAD")
        _git(repo, "switch", "-c", "pilot/x")
        _git(repo, "checkout", "--detach", tip)
        with pytest.raises(RunBranchGateError) as exc:
            check_live(repo, RunBranchRecord(branch="pilot/x", head=tip))
        assert exc.value.reason == "live_branch_mismatch"
