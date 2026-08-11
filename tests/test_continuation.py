"""Continuation readiness — the pure half of #166 B (spec §4.2).

"Continue" means: re-dispatch spec-runner against the existing tasks.md, with
no regeneration and no author respawn. That is only safe when the world still
matches what the interrupted run left behind, so the decision is fail-closed on
every precondition and never falls back to a regeneration.

Kept pure — facts in, verdict out — so each refusal is a plain assertion rather
than a reconstruction through a worktree, a probe and a database.
"""

from maestro.continuation import (
    CONTINUATION_WARN_THRESHOLD,
    ContinuationVerdict,
    classify_continuation_readiness,
    describe_continuation_count,
)
from maestro.tasks_spec import DanglingDependency


def _verdict(
    *,
    worktree_exists: bool = True,
    live_execution: bool = False,
    dangling: list[DanglingDependency] | None = None,
    state_db_present: bool = True,
) -> ContinuationVerdict:
    """Typed wrapper: defaults are the happy path, each test names its fault."""
    return classify_continuation_readiness(
        worktree_exists=worktree_exists,
        live_execution=live_execution,
        dangling=dangling or [],
        state_db_present=state_db_present,
    )


class TestReadiness:
    def test_all_preconditions_met_is_ready(self) -> None:
        verdict = _verdict()

        assert verdict.ok
        assert verdict.reason == "ready"

    def test_missing_worktree_refuses(self) -> None:
        """The accepted result no longer exists, so there is nothing to
        continue — and regenerating would be a different run entirely."""
        verdict = _verdict(worktree_exists=False)

        assert not verdict.ok
        assert verdict.reason == "no_worktree"

    def test_live_execution_refuses(self) -> None:
        """Continuation must never race an orphan: two spec-runners over one
        worktree is worse than paying for a regeneration."""
        verdict = _verdict(live_execution=True)

        assert not verdict.ok
        assert verdict.reason == "live_execution"

    def test_dangling_dependencies_refuse(self) -> None:
        """spec-runner would reject this spec at run time (#165), so
        dispatching costs exactly the spawn this feature exists to save."""
        verdict = _verdict(dangling=[DanglingDependency(task_id="T-2", missing="T-1")])

        assert not verdict.ok
        assert verdict.reason == "invalid_tasks"
        assert "T-2 -> T-1" in verdict.message

    def test_missing_state_db_refuses(self) -> None:
        """Without executor state, "continue" is indistinguishable from
        "start", and calling it continuation would be a lie."""
        verdict = _verdict(state_db_present=False)

        assert not verdict.ok
        assert verdict.reason == "no_state"

    def test_a_live_execution_outranks_other_faults(self) -> None:
        """Ordering matters for the message: an operator told "invalid tasks"
        would edit the file while a process is still writing it."""
        verdict = _verdict(
            live_execution=True,
            dangling=[DanglingDependency(task_id="T-2", missing="T-1")],
            state_db_present=False,
        )

        assert verdict.reason == "live_execution"

    def test_missing_worktree_outranks_its_own_consequences(self) -> None:
        """No worktree implies no tasks.md and no state db; reporting those
        would describe symptoms instead of the cause."""
        verdict = _verdict(worktree_exists=False, state_db_present=False)

        assert verdict.reason == "no_worktree"

    def test_every_refusal_names_a_distinct_reason(self) -> None:
        """Fail-closed with a distinct reason, never a generic block: each of
        these needs a different action from the operator."""
        reasons = {
            _verdict(worktree_exists=False).reason,
            _verdict(live_execution=True).reason,
            _verdict(dangling=[DanglingDependency(task_id="a", missing="b")]).reason,
            _verdict(state_db_present=False).reason,
        }

        assert len(reasons) == 4

    def test_refusal_messages_never_suggest_regeneration(self) -> None:
        """A silent fallback to regeneration is the failure this whole feature
        removes; the message must not invite it either."""
        for verdict in (
            _verdict(worktree_exists=False),
            _verdict(live_execution=True),
            _verdict(state_db_present=False),
        ):
            assert "regenerat" not in verdict.message.lower()


class TestVerdictConstruction:
    def test_ok_verdict_carries_no_remedy(self) -> None:
        assert ContinuationVerdict.ready().ok
        assert ContinuationVerdict.ready().reason == "ready"


class TestContinuationCount:
    def test_low_count_is_not_warned_about(self) -> None:
        assert describe_continuation_count(1) is None

    def test_high_count_warns_without_forbidding(self) -> None:
        """No numeric cap (owner decision): count and warn, never refuse the
        N+1th without new knowledge."""
        message = describe_continuation_count(CONTINUATION_WARN_THRESHOLD)

        assert message is not None
        assert str(CONTINUATION_WARN_THRESHOLD) in message

    def test_zero_is_silent(self) -> None:
        assert describe_continuation_count(0) is None
