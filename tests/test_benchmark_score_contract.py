"""Consumer contract test — ATP benchmark score contract v1.

The producer is atp-platform; its fixtures and the contract module itself are
vendored under ``tests/fixtures/atp-score-contract/v1/`` with a ``PIN``. Two
guarantees, deliberately separate:

* **copy-integrity** — the pinned hashes still describe the bytes on disk.
  Checked at import, BEFORE the fixtures are parametrized, so a truncated copy
  cannot quietly shrink into a smaller green suite.
* **upstream-drift** — upstream has not moved past the pinned commit. Checked
  only when a sibling checkout is present; skipped, never silently passed,
  when it is not.

Copy-integrity alone reads an upstream change as health. That is exactly how
atp-platform's own handoff doc went stale: two of its three pins were wrong by
the time this copy was taken.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from maestro.benchmark.models import (
    BenchmarkResult,
    BenchmarkTaskResult,
    ScoreSemantics,
)
from maestro.benchmark.score_contract import (
    UNKNOWN_KIND,
    ScoreContractError,
    parse_finalized_score,
    publication_decision,
    split_numeric_components,
)


VENDOR_DIR = Path(__file__).parent / "fixtures" / "atp-score-contract" / "v1"
PIN_PATH = VENDOR_DIR / "PIN"

#: atp-platform commit the bytes below were taken from (see PIN).
UPSTREAM_COMMIT = "05bd939"
UPSTREAM_PATHS = {
    "run_status_completion_only.json": (
        "tests/fixtures/benchmark_score_contract/run_status_completion_only.json"
    ),
    "run_status_evaluated.json": (
        "tests/fixtures/benchmark_score_contract/run_status_evaluated.json"
    ),
    "run_status_forward_compat.json": (
        "tests/fixtures/benchmark_score_contract/run_status_forward_compat.json"
    ),
    "score_contract.py": (
        "packages/atp-dashboard/atp/dashboard/benchmark/score_contract.py"
    ),
}


def _read_pin() -> dict[str, str]:
    """Parse ``PIN`` into {filename: sha256}, ignoring comments."""
    pins: dict[str, str] = {}
    for line in PIN_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, name = line.partition("  ")
        pins[name.strip()] = digest.strip()
    return pins


def _verify_copy_integrity() -> dict[str, str]:
    """Fail at import if the vendored copy no longer matches its pin."""
    pins = _read_pin()
    if not pins:
        raise AssertionError(f"{PIN_PATH} lists no files")
    for name, expected in pins.items():
        path = VENDOR_DIR / name
        if not path.exists():
            raise AssertionError(f"vendored file missing: {path}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise AssertionError(
                f"vendored {name} does not match PIN: {actual} != {expected}. "
                "Never hand-edit a vendored byte; re-vendor instead."
            )
    return pins


# Executed at import — before any parametrization reads the directory.
_PINS = _verify_copy_integrity()

#: Only the payload fixtures are parametrized; score_contract.py is reference.
PAYLOAD_FIXTURES = sorted(n for n in _PINS if n.endswith(".json"))


def _load(name: str) -> dict[str, Any]:
    return json.loads((VENDOR_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Guarantee 1 — copy-integrity
# ---------------------------------------------------------------------------


def test_pin_covers_every_vendored_file() -> None:
    """A file added to the directory without a pin is unpinned, not trusted."""
    on_disk = {p.name for p in VENDOR_DIR.iterdir() if p.name != "PIN"}
    assert on_disk == set(_PINS), (
        "vendored directory and PIN disagree; every file must be pinned"
    )


def test_three_payload_fixtures_are_parametrized() -> None:
    """Guards the count itself: a lost fixture must not shrink the suite."""
    assert len(PAYLOAD_FIXTURES) == 3


# ---------------------------------------------------------------------------
# Guarantee 2 — upstream-drift
# ---------------------------------------------------------------------------


def test_upstream_has_not_drifted_past_the_pin() -> None:
    """Compare against a sibling atp-platform checkout when one exists.

    Skipped (never silently passed) when the sibling is absent — installed
    users have no sibling, and for them the TODO.md trigger carries this.
    """
    sibling = Path(__file__).parents[2] / "atp-platform"
    if not (sibling / ".git").exists():
        pytest.skip("no sibling atp-platform checkout; drift covered by TODO trigger")

    for name, upstream_path in UPSTREAM_PATHS.items():
        proc = subprocess.run(
            ["git", "-C", str(sibling), "show", f"{UPSTREAM_COMMIT}:{upstream_path}"],
            capture_output=True,
        )
        if proc.returncode != 0:
            pytest.skip(f"pinned commit {UPSTREAM_COMMIT} not in sibling checkout")
        upstream_digest = hashlib.sha256(proc.stdout).hexdigest()
        assert upstream_digest == _PINS[name], (
            f"{name} differs from atp-platform@{UPSTREAM_COMMIT}"
        )


# ---------------------------------------------------------------------------
# Parsing the three vendored payloads
# ---------------------------------------------------------------------------


def test_completion_only_is_not_a_quality_signal() -> None:
    finalized = parse_finalized_score(_load("run_status_completion_only.json"))

    assert finalized.score == pytest.approx(66.66666666666667)
    assert finalized.finalized is True
    assert finalized.semantics.kind == "completion_rate"
    assert finalized.semantics.quality_signal is False
    assert dict(finalized.score_components) == {}
    assert "null_until_finalized" in " ".join(finalized.semantics.caveats)
    assert "zero_is_ambiguous" in " ".join(finalized.semantics.caveats)


def test_evaluated_carries_measured_components() -> None:
    finalized = parse_finalized_score(_load("run_status_evaluated.json"))

    assert finalized.semantics.kind == "aggregated_evaluation"
    assert finalized.semantics.quality_signal is True
    assert dict(finalized.score_components) == {"contains": 50.0}


def test_forward_compat_survives_structured_components_and_unknown_keys() -> None:
    """The fixture whose only job is to prove consumer tolerance.

    Object-valued components and unknown semantics keys must both survive
    parsing — that is what makes additions additive rather than breaking.
    """
    finalized = parse_finalized_score(_load("run_status_forward_compat.json"))

    assert finalized.semantics.kind == "weighted_quality"
    assert finalized.semantics.quality_signal is True
    assert finalized.score_components["correctness"] == {
        "normalized_value": 0.82,
        "weight": 0.6,
    }
    assert "some_future_axis_atp_has_not_invented_yet" in finalized.score_components
    assert finalized.semantics.raw["some_future_key"] == "consumers must ignore this"


# ---------------------------------------------------------------------------
# Legacy, malformed, unfinalized
# ---------------------------------------------------------------------------


def test_absent_semantics_is_legacy_unknown_never_quality() -> None:
    payload = _load("run_status_completion_only.json")
    del payload["score_semantics"]

    finalized = parse_finalized_score(payload)

    assert finalized.semantics.kind == UNKNOWN_KIND
    assert finalized.semantics.quality_signal is False
    assert dict(finalized.semantics.raw) == {}


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param({"score_semantics": []}, id="not-a-mapping"),
        pytest.param({"score_semantics": {"quality_signal": False}}, id="no-kind"),
        pytest.param({"score_semantics": {"kind": "completion_rate"}}, id="no-signal"),
        pytest.param(
            {"score_semantics": {"kind": "completion_rate", "quality_signal": "no"}},
            id="signal-not-bool",
        ),
        pytest.param(
            {
                "score_semantics": {
                    "kind": "completion_rate",
                    "quality_signal": False,
                    "caveats": "null_until_finalized",
                }
            },
            id="caveats-not-a-list",
        ),
    ],
)
def test_malformed_semantics_is_a_contract_error_not_legacy(
    mutation: dict[str, Any],
) -> None:
    """A corrupted new producer must not be able to pass as an old one."""
    payload = _load("run_status_completion_only.json")
    payload.update(mutation)

    with pytest.raises(ScoreContractError):
        parse_finalized_score(payload)


def test_null_total_score_forces_quality_signal_false() -> None:
    """`null_until_finalized`: the producer's claim does not survive a null score."""
    payload = _load("run_status_evaluated.json")
    payload["total_score"] = None

    finalized = parse_finalized_score(payload)

    assert finalized.score == 0.0
    assert finalized.finalized is False
    assert finalized.semantics.quality_signal is False
    # The original claim is not erased — it stays readable in raw.
    assert finalized.semantics.raw["quality_signal"] is True


def test_mixed_task_scores_caveat_is_carried() -> None:
    """Locally authored — NOT vendored.

    atp-platform emits this caveat (`score_contract.py::_MIXED_CAVEAT`) but ships
    no fixture containing it, so this case is written here by hand. It must not
    be mistaken for pinned upstream bytes.
    """
    payload = _load("run_status_evaluated.json")
    payload["score_semantics"]["caveats"] = [
        *payload["score_semantics"]["caveats"],
        "mixed_task_scores: some tasks were scored by evaluation and others by "
        "completion; see coverage.tasks_evaluated and coverage.tasks_completion_only",
    ]

    finalized = parse_finalized_score(payload)

    assert any(c.startswith("mixed_task_scores") for c in finalized.semantics.caveats)


# ---------------------------------------------------------------------------
# Numeric narrowing at the arbiter edge
# ---------------------------------------------------------------------------


def test_split_numeric_components_names_what_it_drops() -> None:
    kept, dropped = split_numeric_components(
        {
            "contains": 50.0,
            "int_is_a_number": 3,
            "correctness": {"normalized_value": 0.82},
            "flag": True,
            "label": "text",
        }
    )

    assert kept == {"contains": 50.0, "int_is_a_number": 3.0}
    # Booleans are not numbers on the wire (JSON Schema `type: number`).
    assert dropped == ("correctness", "flag", "label")


def test_dropping_rank_score_is_observable() -> None:
    """`rank_score` is the one key with consequences on the arbiter side.

    arbiter reads exactly this key as the routing tiebreaker and falls back to
    the scalar score when it is absent or non-numeric (`db.rs::get_benchmark_score`).
    A structured drop must therefore name it, not report a bare count.
    """
    kept, dropped = split_numeric_components(
        {"rank_score": {"normalized_value": 0.63}, "contains": 50.0}
    )

    assert kept == {"contains": 50.0}
    assert "rank_score" in dropped


# ---------------------------------------------------------------------------
# Publication gate (fail-closed)
# ---------------------------------------------------------------------------


def _result_from(finalized: Any, **overrides: Any) -> BenchmarkResult:
    return BenchmarkResult(
        run_id="r1",
        benchmark_id="swe-mini",
        agent_id="claude_code@claude-sonnet-4-6",
        score=finalized.score,
        score_components=finalized.score_components,
        semantics=finalized.semantics,
        score_finalized=finalized.finalized,
        per_task=[
            BenchmarkTaskResult(
                task_index=0,
                prompt="p",
                response="r",
                duration_seconds=1.0,
            )
        ],
        duration_seconds=1.0,
        **overrides,
    )


def test_legacy_unknown_is_withheld() -> None:
    payload = _load("run_status_evaluated.json")
    del payload["score_semantics"]
    finalized = parse_finalized_score(payload)

    decision = publication_decision(_result_from(finalized))

    assert decision.allowed is False
    assert decision.reason == "semantics_unknown"


def test_unfinalized_run_is_withheld() -> None:
    payload = _load("run_status_evaluated.json")
    payload["total_score"] = None
    finalized = parse_finalized_score(payload)

    decision = publication_decision(_result_from(finalized))

    assert decision.allowed is False
    assert decision.reason == "score_not_finalized"


def test_semantics_is_required_on_the_result() -> None:
    """No default: a result that never read the contract cannot be built."""
    with pytest.raises(ValueError):
        BenchmarkResult(  # type: ignore[missing-argument]  # that is the point
            run_id="r1",
            benchmark_id="b",
            agent_id="a",
            score=1.0,
            per_task=[],
            duration_seconds=1.0,
        )


def test_unknown_semantics_helper_is_never_a_quality_signal() -> None:
    assert ScoreSemantics.unknown().quality_signal is False
    assert ScoreSemantics.unknown().kind == UNKNOWN_KIND


# ---------------------------------------------------------------------------
# The gate at the helper level: withheld means the client is never called
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Fails the test by recording any call it should never have received."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def report_benchmark_raw(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return {"status": "created"}


@pytest.mark.parametrize(
    ("fixture", "mutation", "reason"),
    [
        pytest.param(
            "run_status_evaluated.json",
            {"total_score": None},
            "score_not_finalized",
            id="unfinalized",
        ),
        pytest.param(
            "run_status_evaluated.json",
            {"score_semantics": None},
            "semantics_unknown",
            id="legacy",
        ),
    ],
)
async def test_non_quality_never_reaches_the_arbiter_tiebreaker(
    fixture: str, mutation: dict[str, Any], reason: str
) -> None:
    """What stays withheld after the softening, and the RPC must not happen.

    Not merely "reported as withheld": for a legacy run, sending would mean
    sending no `score_semantics` block, and an absent block is read as
    ELIGIBLE for routing on the arbiter side.
    """
    from maestro.benchmark.arbiter_report import report_benchmark_to_arbiter

    payload = _load(fixture)
    for key, value in mutation.items():
        if value is None and key == "score_semantics":
            del payload[key]
        else:
            payload[key] = value

    result = _result_from(parse_finalized_score(payload))
    client = _RecordingClient()

    returned = await report_benchmark_to_arbiter(result, client)

    assert client.payloads == [], "a withheld result must not be sent"
    assert returned.report_status == "withheld"
    assert returned.report_error == f"withheld: {reason}"


async def test_quality_signal_true_is_actually_sent() -> None:
    """The other half: the gate must not withhold everything."""
    from maestro.benchmark.arbiter_report import report_benchmark_to_arbiter

    result = _result_from(parse_finalized_score(_load("run_status_evaluated.json")))
    client = _RecordingClient()

    returned = await report_benchmark_to_arbiter(result, client)

    assert len(client.payloads) == 1
    assert client.payloads[0]["score_components"] == {"contains": 50.0}
    assert returned.report_status == "ok"


async def test_structured_components_are_narrowed_but_the_run_is_still_sent() -> None:
    """forward-compat: objects drop out of the wire, the report still goes."""
    from maestro.benchmark.arbiter_report import report_benchmark_to_arbiter

    result = _result_from(
        parse_finalized_score(_load("run_status_forward_compat.json"))
    )
    client = _RecordingClient()

    returned = await report_benchmark_to_arbiter(result, client)

    assert returned.report_status == "ok"
    assert client.payloads[0]["score_components"] == {}
    # The full breakdown survives locally even though it cannot go on the wire.
    assert "correctness" in returned.score_components


def test_semantics_raw_is_excluded_from_json_output() -> None:
    """`maestro benchmark --json` is documented output; `raw` is an unbounded
    upstream-shaped blob, so it stays in-process."""
    result = _result_from(parse_finalized_score(_load("run_status_evaluated.json")))

    dumped = json.loads(result.model_dump_json())

    assert dumped["semantics"]["kind"] == "aggregated_evaluation"
    assert dumped["semantics"]["quality_signal"] is True
    assert dumped["semantics"]["caveats"]
    assert "raw" not in dumped["semantics"]


def test_human_summary_marks_a_non_quality_score() -> None:
    """ATP ask #1: branch on quality_signal before showing the number."""
    from rich.console import Console

    from maestro.cli import _print_benchmark_summary

    result = _result_from(
        parse_finalized_score(_load("run_status_completion_only.json"))
    ).model_copy(
        update={
            "report_status": "withheld",
            "report_error": "withheld: quality_signal_false",
        }
    )
    console = Console(record=True, width=200)

    _print_benchmark_summary(result, Path(), console)
    out = console.export_text()

    assert "completion, not quality" in out
    assert "not reported to arbiter" in out


def test_human_summary_stays_quiet_on_a_real_quality_score() -> None:
    from rich.console import Console

    from maestro.cli import _print_benchmark_summary

    result = _result_from(parse_finalized_score(_load("run_status_evaluated.json")))
    console = Console(record=True, width=200)

    _print_benchmark_summary(result, Path(), console)
    out = console.export_text()

    assert "not quality" not in out
    assert "not reported to arbiter" not in out


@pytest.mark.parametrize(
    "total", [pytest.param("66.7", id="string"), pytest.param(True, id="bool")]
)
def test_non_numeric_total_score_is_a_contract_error(total: Any) -> None:
    """A broken producer is not an unfinalized run, and must not become one."""
    payload = _load("run_status_evaluated.json")
    payload["total_score"] = total

    with pytest.raises(ScoreContractError):
        parse_finalized_score(payload)


# ---------------------------------------------------------------------------
# Round 2 — arbiter reads `score_semantics` (arbiter#82) and the canonical
# wire unit is a fraction (arbiter#81). The gate softens, but not everywhere.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "mutation", "allowed", "reason"),
    [
        pytest.param(
            "run_status_evaluated.json",
            {},
            True,
            "quality_signal",
            id="evaluated-routes",
        ),
        pytest.param(
            "run_status_completion_only.json",
            {},
            True,
            "stored_not_routed",
            id="completion-stored-not-routed",
        ),
        pytest.param(
            "run_status_evaluated.json",
            {"schema_version": 2},
            True,
            "stored_not_routed",
            id="future-version-stored-not-routed",
        ),
        pytest.param(
            "run_status_evaluated.json",
            {"drop_semantics": True},
            False,
            "semantics_unknown",
            id="legacy-still-withheld",
        ),
        pytest.param(
            "run_status_evaluated.json",
            {"null_score": True},
            False,
            "score_not_finalized",
            id="unfinalized-still-withheld",
        ),
    ],
)
def test_publication_matrix(
    fixture: str, mutation: dict[str, Any], allowed: bool, reason: str
) -> None:
    """The two withheld rows are withheld *because* arbiter is permissive.

    `semantics_permit_routing` returns **true** for an absent block — a
    deliberate deviation on their side, so their 21 legacy rows keep feeding
    R-07. It means we must never send a run whose semantics we could not read:
    absence there is not "unknown", it is "eligible".
    """
    payload = _load(fixture)
    if mutation.pop("drop_semantics", False):
        del payload["score_semantics"]
    if mutation.pop("null_score", False):
        payload["total_score"] = None
    payload.get("score_semantics", {}).update(mutation)

    decision = publication_decision(_result_from(parse_finalized_score(payload)))

    assert decision.allowed is allowed
    assert decision.reason == reason


async def test_completion_rate_now_reaches_arbiter_carrying_its_block() -> None:
    """Softened: stored and inspectable, excluded from the tiebreaker by them."""
    from maestro.benchmark.arbiter_report import report_benchmark_to_arbiter

    result = _result_from(
        parse_finalized_score(_load("run_status_completion_only.json"))
    )
    client = _RecordingClient()

    returned = await report_benchmark_to_arbiter(result, client)

    assert returned.report_status == "ok"
    sent = client.payloads[0]
    assert sent["score_semantics"]["quality_signal"] is False
    assert sent["score_semantics"]["schema_version"] == 1


async def test_legacy_run_is_still_withheld_because_absence_means_eligible() -> None:
    from maestro.benchmark.arbiter_report import report_benchmark_to_arbiter

    payload = _load("run_status_evaluated.json")
    del payload["score_semantics"]
    result = _result_from(parse_finalized_score(payload))
    client = _RecordingClient()

    returned = await report_benchmark_to_arbiter(result, client)

    assert client.payloads == []
    assert returned.report_status == "withheld"
    assert returned.report_error == "withheld: semantics_unknown"


async def test_wire_score_is_a_fraction_not_a_percent() -> None:
    """ATP reports a percent; the wire's canonical unit is a fraction [0,1].

    Sending the percent is what made every run above 1% arrive as a perfect
    `1.0` after arbiter's clamp; arbiter now rejects out-of-range with -32602.
    """
    from maestro.benchmark.arbiter_report import report_benchmark_to_arbiter

    result = _result_from(parse_finalized_score(_load("run_status_evaluated.json")))
    assert result.score == 50.0  # domain value stays ATP's percent
    client = _RecordingClient()

    await report_benchmark_to_arbiter(result, client)

    assert client.payloads[0]["score"] == pytest.approx(0.5)


async def test_semantics_travels_verbatim_not_normalized() -> None:
    """Send ATP's claim, not our reading of it.

    Verbatim keeps the block *theirs*; the cases where our reading disagrees
    (an unfinalized run whose producer claimed quality) never ship at all.
    """
    from maestro.benchmark.arbiter_report import report_benchmark_to_arbiter

    result = _result_from(
        parse_finalized_score(_load("run_status_forward_compat.json"))
    )
    client = _RecordingClient()

    await report_benchmark_to_arbiter(result, client)

    sent = client.payloads[0]["score_semantics"]
    assert sent["some_future_key"] == "consumers must ignore this"
    assert sent["kind"] == "weighted_quality"


def test_wire_payload_refuses_to_ship_without_a_semantics_block() -> None:
    """Defence in depth behind the gate: a block-less payload is routable
    on their side, so building one must be impossible, not merely unreached."""
    from maestro.benchmark.arbiter_report import _build_wire_payload

    result = _result_from(parse_finalized_score(_load("run_status_evaluated.json")))
    blind = result.model_copy(update={"semantics": ScoreSemantics.unknown()})

    with pytest.raises(ValueError, match="semantics"):
        _build_wire_payload(blind, max_per_task=200)


def test_gate_and_wire_guard_agree_on_having_no_block() -> None:
    """The gate decides; the guard must never be the thing that decides.

    A hand-built `ScoreSemantics` with a named kind but an empty `raw` is
    unreachable from `parse_finalized_score` — but if the two predicates
    disagreed, such a result would pass the gate and then raise inside
    `_build_wire_payload`, surfacing as `report_status="failed"` instead of an
    honest `withheld`. (Found by review on PR #205.)
    """
    blind = ScoreSemantics(kind="completion_rate", quality_signal=False, raw={})
    result = _result_from(
        parse_finalized_score(_load("run_status_evaluated.json"))
    ).model_copy(update={"semantics": blind})

    decision = publication_decision(result)

    assert decision.allowed is False
    assert decision.reason == "semantics_unknown"
