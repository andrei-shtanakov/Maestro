"""Tests for maestro/run_branch_gate.py — spec §4 start matrix."""

import pytest

from maestro.run_branch_gate import (
    CheckoutSnapshot,
    RunBranchGateError,
    StartAction,
    decide_start,
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
