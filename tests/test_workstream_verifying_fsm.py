"""§4 topology: one new durable state, five outgoing edges, one READY edge."""

from maestro.models import WorkstreamStatus as WS


def test_running_can_enter_verifying() -> None:
    assert WS.RUNNING.can_transition_to(WS.VERIFYING)


def test_verifying_edges() -> None:
    assert WS.VERIFYING.can_transition_to(WS.MERGING)  # PASS + evidence commit
    assert WS.VERIFYING.can_transition_to(WS.READY)  # FAIL, rework left
    assert WS.VERIFYING.can_transition_to(WS.NEEDS_REVIEW)  # rework exhausted / orphan
    assert WS.VERIFYING.can_transition_to(WS.FAILED)  # ERROR exhausted


def test_ready_can_enter_verifying_for_reverify_resume() -> None:
    assert WS.READY.can_transition_to(WS.VERIFYING)


def test_verifying_not_terminal_and_not_reachable_from_merging() -> None:
    assert not WS.VERIFYING.is_terminal()
    assert not WS.MERGING.can_transition_to(WS.VERIFYING)


def test_legacy_edges_untouched() -> None:
    # Zero-change: the legacy path must remain exactly as before.
    assert WS.RUNNING.can_transition_to(WS.MERGING)
    assert WS.RUNNING.can_transition_to(WS.FAILED)
    assert WS.MERGING.can_transition_to(WS.PR_CREATED)
    assert WS.FAILED.can_transition_to(WS.READY)
    assert WS.FAILED.can_transition_to(WS.NEEDS_REVIEW)
