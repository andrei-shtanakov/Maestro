"""Tests for `maestro review-pr` pure helpers and its DB layer (#post-pr).

Spec: docs/superpowers/specs/2026-08-06-post-pr-review-command-design.md
(approved revision 3). Upstream contract: spec-runner >= 2.21.0 —
`review-pr --json` emits exactly one JSON document on every exit path,
payload carries `exit_code` (spec-runner #116/#117).
"""

import json
from pathlib import Path

import pytest

from maestro.database import Database
from maestro.review_pr import (
    MIN_SPEC_RUNNER_VERSION,
    PrRef,
    ReviewReport,
    classify_precondition,
    outcome_for_exit,
    parse_pr_url,
    repo_key,
    validate_report,
)


# =============================================================================
# repo_key — collision-free (§3)
# =============================================================================


def test_repo_key_is_stable_and_readable() -> None:
    key = repo_key("andrei-shtanakov/maestro")
    assert key.startswith("andrei-shtanakov-maestro-")
    assert repo_key("andrei-shtanakov/maestro") == key  # deterministic


def test_repo_key_avoids_sanitization_collisions() -> None:
    # `a-b/c` and `a/b-c` both sanitize to "a-b-c" — the hash separates them.
    assert repo_key("a-b/c") != repo_key("a/b-c")


def test_repo_key_is_filesystem_safe() -> None:
    key = repo_key("Owner.Name/repo with spaces")
    assert "/" not in key
    assert " " not in key


# =============================================================================
# parse_pr_url (§3.1 — canonical URL is the positional argument)
# =============================================================================


def test_parse_pr_url_canonical() -> None:
    ref = parse_pr_url("https://github.com/andrei-shtanakov/maestro/pull/145")
    assert ref == PrRef(
        owner="andrei-shtanakov",
        repo="maestro",
        number=145,
        canonical_url="https://github.com/andrei-shtanakov/maestro/pull/145",
    )
    assert ref.owner_repo == "andrei-shtanakov/maestro"


def test_parse_pr_url_tolerates_trailing_path_and_slash() -> None:
    for url in [
        "https://github.com/o/r/pull/7/files",
        "https://github.com/o/r/pull/7/",
        "https://github.com/o/r/pull/7#issuecomment-1",
    ]:
        ref = parse_pr_url(url)
        assert ref.number == 7
        assert ref.canonical_url == "https://github.com/o/r/pull/7"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not a url",
        "https://github.com/o/r/issues/7",
        "https://github.com/o/r/pull/abc",
        "https://gitlab.com/o/r/pull/7",
        "https://github.com/o/pull/7",
    ],
)
def test_parse_pr_url_rejects_non_pr_urls(bad: str) -> None:
    with pytest.raises(ValueError, match="PR URL"):
        parse_pr_url(bad)


# =============================================================================
# validate_report — upstream JSON contract (spec-runner >= 2.21.0)
# =============================================================================


def _ok_report(**overrides: object) -> str:
    payload: dict = {
        "repo": "o/r",
        "pr_number": 7,
        "head_sha": "a" * 40,
        "new_comments": 2,
        "comments": [],
        "counts": {"valid": 1, "refuted": 1},
        "needs_human": False,
        "exit_code": 0,
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_validate_report_exit_0() -> None:
    report = validate_report(_ok_report(), process_exit=0)
    assert isinstance(report, ReviewReport)
    assert report.exit_code == 0
    assert report.needs_human is False
    assert report.head_sha == "a" * 40


def test_validate_report_exit_2_needs_human() -> None:
    raw = _ok_report(needs_human=True, exit_code=2)
    report = validate_report(raw, process_exit=2)
    assert isinstance(report, ReviewReport)
    assert report.needs_human is True


def test_validate_report_exit_1_shape_with_nulls() -> None:
    # Upstream: {repo, pr_number, error, exit_code}; repo/pr_number may be null.
    raw = json.dumps(
        {"repo": None, "pr_number": None, "error": "dirty tree", "exit_code": 1}
    )
    report = validate_report(raw, process_exit=1)
    assert isinstance(report, ReviewReport)
    assert report.error == "dirty tree"
    assert report.repo is None


def test_validate_report_rejects_garbage_and_mixed_output() -> None:
    for raw in ["", "not json", "diagnostic line\n" + _ok_report()]:
        assert isinstance(validate_report(raw, process_exit=0), str)


def test_validate_report_rejects_exit_code_mismatch() -> None:
    # A stored report must be self-describing AND agree with the process.
    result = validate_report(_ok_report(exit_code=2), process_exit=0)
    assert isinstance(result, str)
    assert "exit_code" in result


def test_validate_report_rejects_unknown_exit_code() -> None:
    assert isinstance(validate_report(_ok_report(exit_code=9), process_exit=9), str)


# =============================================================================
# Preconditions and exit mapping (§3.1, §5.1)
# =============================================================================


@pytest.mark.parametrize(
    ("local", "remote", "ancestor", "dirty", "expected"),
    [
        ("a" * 40, "a" * 40, True, False, "ready"),
        ("b" * 40, "a" * 40, True, False, "continuation"),  # local ahead
        ("a" * 40, "a" * 40, True, True, "dirty"),
        ("b" * 40, "a" * 40, False, False, "diverged"),  # remote force-pushed
        ("b" * 40, "a" * 40, True, True, "dirty"),  # dirty wins over continuation
    ],
)
def test_classify_precondition(
    local: str, remote: str, ancestor: bool, dirty: bool, expected: str
) -> None:
    assert (
        classify_precondition(
            local_head=local, remote_head=remote, ancestor=ancestor, dirty=dirty
        )
        == expected
    )


def test_outcome_for_exit() -> None:
    assert outcome_for_exit(0) == "complete"
    assert outcome_for_exit(2) == "needs_human"
    assert outcome_for_exit(1) == "infra_error"
    assert outcome_for_exit(42) == "infra_error"


def test_min_spec_runner_version_pinned_to_json_purity_release() -> None:
    assert MIN_SPEC_RUNNER_VERSION == "2.21.0"


# =============================================================================
# DB layer: migration 21 (§5)
# =============================================================================


@pytest.fixture
async def db(tmp_path: Path):
    d = Database(tmp_path / "review.db")
    await d.connect()
    yield d
    await d.close()


async def test_sentinel_then_cas_finalize(db: Database) -> None:
    await db.insert_review_run(
        "01RUN",
        workstream_id="ws-1",
        pr_url="https://github.com/o/r/pull/7",
        repo="o/r",
        pr_number=7,
        input_head_sha="a" * 40,
        workspace_path="/tmp/wt",
        spec_runner_version="2.21.0",
    )
    unfinished = await db.list_unfinished_review_runs()
    assert [r["review_run_id"] for r in unfinished] == ["01RUN"]

    assert await db.finalize_review_run(
        "01RUN",
        exit_code=0,
        outcome="complete",
        reason=None,
        report_json="{}",
        output_head_sha="b" * 40,
    )
    assert await db.list_unfinished_review_runs() == []


async def test_second_finalize_is_refused(db: Database) -> None:
    """Immutable after finalization: two recovery passes can't rewrite."""
    await db.insert_review_run(
        "01RUN",
        workstream_id="ws-1",
        pr_url="u",
        repo="o/r",
        pr_number=7,
        input_head_sha=None,
        workspace_path=None,
        spec_runner_version=None,
    )
    assert await db.finalize_review_run(
        "01RUN",
        exit_code=2,
        outcome="needs_human",
        reason=None,
        report_json=None,
        output_head_sha=None,
    )
    assert not await db.finalize_review_run(
        "01RUN",
        exit_code=1,
        outcome="infra_error",
        reason="interrupted",
        report_json=None,
        output_head_sha=None,
    )
    rows = await db.list_review_runs("ws-1")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "needs_human"  # first finalize wins


async def test_history_is_kept_per_head(db: Database) -> None:
    for i, sha in enumerate(["a" * 40, "b" * 40]):
        await db.insert_review_run(
            f"01RUN{i}",
            workstream_id="ws-1",
            pr_url="u",
            repo="o/r",
            pr_number=7,
            input_head_sha=sha,
            workspace_path=None,
            spec_runner_version="2.21.0",
        )
        await db.finalize_review_run(
            f"01RUN{i}",
            exit_code=0,
            outcome="complete",
            reason=None,
            report_json=None,
            output_head_sha=sha,
        )
    rows = await db.list_review_runs("ws-1")
    assert [r["input_head_sha"] for r in rows] == ["a" * 40, "b" * 40]
