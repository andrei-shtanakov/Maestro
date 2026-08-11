"""Completeness verdict + its approval marker phase (#164, spec §4.2/§5).

The decision itself is pure: counters in, verdict out. Keeping it out of the
orchestrator is what lets the sharp cases — an unknown denominator, a stale
one, an all-no-op run — be stated as plain assertions instead of being
reconstructed through a git worktree and a database.
"""

from maestro.completeness import (
    COMPLETENESS_PHASE,
    CompletenessVerdict,
    build_completeness_block_reason,
    classify_completeness,
    completeness_approval_is_fresh,
)
from maestro.gate_approvals import build_approval_marker, parse_approval_marker


SHA = "a" * 40


class TestClassify:
    def test_done_equals_planned_passes(self) -> None:
        verdict = classify_completeness(done=9, planned=9, noop_done=0)

        assert verdict.ok
        assert verdict.reason == "complete"

    def test_fewer_done_than_planned_blocks(self) -> None:
        """The pilot's 1/9: spec-runner exited 0 with the work unfinished."""
        verdict = classify_completeness(done=1, planned=9, noop_done=0)

        assert not verdict.ok
        assert verdict.reason == "incomplete"
        assert "1 of 9" in verdict.message

    def test_unknown_planned_blocks_fail_closed(self) -> None:
        """No denominator means completeness is unknowable, not satisfied."""
        verdict = classify_completeness(done=3, planned=None, noop_done=0)

        assert not verdict.ok
        assert verdict.reason == "unknown_total"

    def test_more_done_than_planned_blocks_as_inconsistent(self) -> None:
        """A stale denominator describes a different revision of the plan.

        `subtask_total` is captured once after spec-gen, and a rework rewrites
        tasks.md — so `done > planned` means the two numbers are not about the
        same plan. Passing that on `>=` would call completeness proven by
        numbers that disagree.
        """
        verdict = classify_completeness(done=10, planned=9, noop_done=0)

        assert not verdict.ok
        assert verdict.reason == "inconsistent"

    def test_zero_planned_with_zero_done_passes(self) -> None:
        """A plan with no tasks is vacuously complete, not an error."""
        verdict = classify_completeness(done=0, planned=0, noop_done=0)

        assert verdict.ok

    def test_no_op_tasks_count_as_done(self) -> None:
        """Spec §4.3: the gate measures completeness, not productivity."""
        verdict = classify_completeness(done=5, planned=5, noop_done=2)

        assert verdict.ok
        assert verdict.all_no_op is False

    def test_all_no_op_passes_and_is_flagged(self) -> None:
        """Never a block — a diagnostic the caller emits as an event (§10.2)."""
        verdict = classify_completeness(done=9, planned=9, noop_done=9)

        assert verdict.ok
        assert verdict.all_no_op is True

    def test_all_no_op_is_false_for_an_empty_run(self) -> None:
        """0 done of 0 planned is not "everything was a no-op"."""
        verdict = classify_completeness(done=0, planned=0, noop_done=0)

        assert verdict.all_no_op is False

    def test_message_reports_the_no_op_count(self) -> None:
        """`completed 8 of 9 (3 no-op)` must not read as "3 were skipped"."""
        verdict = classify_completeness(done=8, planned=9, noop_done=3)

        assert "8 of 9" in verdict.message
        assert "3 no-op" in verdict.message

    def test_unreadable_is_its_own_verdict(self) -> None:
        verdict = CompletenessVerdict.unreadable("archive manifest missing")

        assert not verdict.ok
        assert verdict.reason == "unreadable"


class TestBlockReason:
    def test_carries_a_parseable_approval_marker(self) -> None:
        """Without the marker `workstream-approve` cannot record the approval."""
        verdict = classify_completeness(done=1, planned=9, noop_done=0)

        reason = build_completeness_block_reason(verdict, SHA)

        marker = parse_approval_marker(reason)
        assert marker is not None
        assert marker.phase == COMPLETENESS_PHASE
        assert marker.sha == SHA

    def test_states_the_counters_for_the_operator(self) -> None:
        verdict = classify_completeness(done=1, planned=9, noop_done=0)

        reason = build_completeness_block_reason(verdict, SHA)

        assert "completed 1 of 9" in reason

    def test_stop_reason_is_appended_as_context(self) -> None:
        """Diagnostic only — the counters decided (owner decision 2)."""
        verdict = classify_completeness(done=1, planned=9, noop_done=0)

        reason = build_completeness_block_reason(
            verdict, SHA, stop_reason="task_failed_stop"
        )

        assert "stop_reason=task_failed_stop" in reason

    def test_block_reason_binds_the_evidence_snapshot(self) -> None:
        """The marker a real block writes must satisfy the freshness rule.

        Without the evidence key the approval could never be honoured, so a
        block would be unapprovable in practice while looking approvable.
        """
        verdict = classify_completeness(done=1, planned=9, noop_done=0)

        reason = build_completeness_block_reason(verdict, SHA, evidence="exec-1")

        marker = parse_approval_marker(reason)
        assert marker is not None
        assert completeness_approval_is_fresh(marker, current_evidence="exec-1")

    def test_unknown_total_block_is_still_approvable(self) -> None:
        """Decision 1 demands an explicit manual path out of this state."""
        verdict = classify_completeness(done=3, planned=None, noop_done=0)

        reason = build_completeness_block_reason(verdict, SHA, evidence="exec-1")

        assert parse_approval_marker(reason) is not None


class TestMarkerPhase:
    def test_completeness_phase_round_trips(self) -> None:
        marker = build_approval_marker(COMPLETENESS_PHASE, SHA)

        parsed = parse_approval_marker(marker)

        assert parsed is not None
        assert parsed.phase == COMPLETENESS_PHASE

    def test_existing_phases_are_untouched(self) -> None:
        for phase in ("ex_ante", "ex_post"):
            parsed = parse_approval_marker(build_approval_marker(phase, SHA))
            assert parsed is not None
            assert parsed.phase == phase


class TestApprovalFreshness:
    """An approval accepts *one* partial result, not the next one.

    Binding to the worktree HEAD alone is not enough: a rework can leave the
    tree at the same sha while the executor state underneath is a different
    run — different tasks done, a different reason for stopping. The marker
    therefore names the evidence snapshot it was granted against, and the
    resume path refuses when that is no longer the current one.
    """

    def test_marker_carries_the_evidence_key(self) -> None:
        marker = parse_approval_marker(
            build_approval_marker(COMPLETENESS_PHASE, SHA, evidence="exec-1")
        )

        assert marker is not None
        assert marker.evidence == "exec-1"

    def test_legacy_marker_without_evidence_still_parses(self) -> None:
        """Markers written before #164 carry no evidence key."""
        marker = parse_approval_marker(build_approval_marker("ex_post", SHA))

        assert marker is not None
        assert marker.evidence is None

    def test_fresh_when_the_evidence_is_still_current(self) -> None:
        marker = parse_approval_marker(
            build_approval_marker(COMPLETENESS_PHASE, SHA, evidence="exec-1")
        )
        assert marker is not None

        assert completeness_approval_is_fresh(marker, current_evidence="exec-1")

    def test_stale_after_a_new_collect_produced_new_evidence(self) -> None:
        """A second run archives under a new execution id; the old approval
        must not accept the result it never saw."""
        marker = parse_approval_marker(
            build_approval_marker(COMPLETENESS_PHASE, SHA, evidence="exec-1")
        )
        assert marker is not None

        assert not completeness_approval_is_fresh(marker, current_evidence="exec-2")

    def test_stale_when_no_evidence_exists_at_all(self) -> None:
        """Nothing to compare against: refuse rather than assume (fail-closed)."""
        marker = parse_approval_marker(
            build_approval_marker(COMPLETENESS_PHASE, SHA, evidence="exec-1")
        )
        assert marker is not None

        assert not completeness_approval_is_fresh(marker, current_evidence=None)

    def test_marker_without_evidence_cannot_prove_freshness(self) -> None:
        """A completeness marker predating this rule proves nothing about the
        snapshot, so it does not unlock delivery."""
        marker = parse_approval_marker(build_approval_marker(COMPLETENESS_PHASE, SHA))
        assert marker is not None

        assert not completeness_approval_is_fresh(marker, current_evidence="exec-1")

    def test_a_non_completeness_marker_is_never_fresh_here(self) -> None:
        """The ex-post approval answers a different question entirely."""
        marker = parse_approval_marker(build_approval_marker("ex_post", SHA))
        assert marker is not None

        assert not completeness_approval_is_fresh(marker, current_evidence="exec-1")
