"""Tests for maestro/run_branch_gate.py — spec §4 start matrix."""

import logging
import subprocess
from pathlib import Path

import pytest

from maestro.run_branch_gate import (
    CheckoutSnapshot,
    RunBranchGateError,
    RunBranchRecord,
    StartAction,
    apply_start_gate,
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
    """Spec §8: run_branch_gate.{created,verified,refused} through the obs
    pipeline (module logger -> logging_bridge). Best-effort telemetry; the
    stderr text remains the contract — but the pilot's acceptance built on
    these traces (issue #216), so they are asserted, not assumed."""

    def _messages(self, caplog: pytest.LogCaptureFixture) -> list[str]:
        return [r.getMessage() for r in caplog.records]

    def test_create_emits_created(
        self, repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="maestro.run_branch_gate"):
            apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        assert any("run_branch_gate.created" in m for m in self._messages(caplog))

    def test_proceed_emits_verified(
        self, repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        # caplog captures for the WHOLE test once any earlier suite member
        # raised the root level (setup_logging leaves it at INFO), so the
        # setup call's `.created` record must be dropped before asserting
        # the second call emits only `.verified`.
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="maestro.run_branch_gate"):
            apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        messages = self._messages(caplog)
        assert any("run_branch_gate.verified" in m for m in messages)
        assert not any("run_branch_gate.created" in m for m in messages)

    def test_start_refusal_emits_refused_with_reason(
        self, repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (repo / "a.txt").write_text("edited")
        with (
            caplog.at_level(logging.INFO, logger="maestro.run_branch_gate"),
            pytest.raises(RunBranchGateError),
        ):
            apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        assert any(
            "run_branch_gate.refused" in m and "dirty_tree" in m
            for m in self._messages(caplog)
        )

    def test_continuation_success_emits_verified(
        self, repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        tip = apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        with caplog.at_level(logging.INFO, logger="maestro.run_branch_gate"):
            verify_continuation(
                repo, RunBranchRecord(branch="pilot/x", head=tip), accept_tip=False
            )
        assert any("run_branch_gate.verified" in m for m in self._messages(caplog))

    def test_continuation_refusal_emits_refused(
        self, repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        tip = apply_start_gate(repo, run_branch="pilot/x", base_branch="master")
        _git(repo, "switch", "master")
        with (
            caplog.at_level(logging.INFO, logger="maestro.run_branch_gate"),
            pytest.raises(RunBranchGateError),
        ):
            verify_continuation(
                repo, RunBranchRecord(branch="pilot/x", head=tip), accept_tip=False
            )
        assert any(
            "run_branch_gate.refused" in m and "resume_branch_mismatch" in m
            for m in self._messages(caplog)
        )
