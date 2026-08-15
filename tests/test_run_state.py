from maestro.run_state import RunRow, classify_run


STARTED = "2026-08-15T10:00:00+00:00"


def _row(**kw) -> RunRow:
    base = {
        "run_id": "A",
        "repo_key": "github.com/acme/app",
        "started_at": STARTED,
        "outcome": None,
        "ended_at": None,
        "reason": None,
        "suspended_at": None,
        "suspend_reason": None,
    }
    base.update(kw)
    return RunRow(**base)


def test_no_row_is_legacy():
    assert classify_run(None, lock_holder_run_id=None) == "legacy"


def test_terminal_outcome_wins_over_everything():
    row = _row(outcome="completed", ended_at="t")
    assert classify_run(row, lock_holder_run_id="A") == "completed"


def test_lock_held_by_this_run_is_running():
    assert classify_run(_row(), lock_holder_run_id="A") == "running"


def test_lock_held_by_another_run_does_not_make_this_one_running():
    # The case the lock alone gets wrong: A is dead, B holds the repo-level lock.
    assert classify_run(_row(run_id="A"), lock_holder_run_id="B") == "interrupted"


def test_free_lock_and_no_outcome_is_interrupted():
    assert classify_run(_row(), lock_holder_run_id=None) == "interrupted"


def test_suspended_without_a_live_lock_is_suspended_not_interrupted():
    row = _row(suspended_at="t", suspend_reason="QG-5")
    assert classify_run(row, lock_holder_run_id=None) == "suspended"


def test_a_suspended_run_that_is_running_again_reports_running():
    row = _row(suspended_at="t", suspend_reason="QG-5")
    assert classify_run(row, lock_holder_run_id="A") == "running"
