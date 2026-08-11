"""`maestro.gate-verdict-record/v2`: the axis split and its consequences (#160).

v1 called the consumer-owned enforcement axis `obligation` — steward's name
for the *intent* axis, with tokens (`mandatory`/`advisory`) the catalog has now
permanently barred from that vocabulary. Keeping the name while claiming the
axes are separate would have made the separation true only in prose.

Renaming a required field is a breaking change, so the discriminator moves to
`/v2`. These tests pin both halves: the new shape, and the absence of the old
one — a record that still accepts `obligation=` would silently re-merge the
axes for any writer that had not been updated.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from maestro.gates import GateVerdictRecord


def _record(**overrides: object) -> GateVerdictRecord:
    fields: dict[str, object] = {
        "gate_id": "agent.approver",
        "enforcement": "advisory",
        "verdict": "not_run",
        "phase": "ex_post",
        "ts": "2026-08-12T00:00:00+00:00",
        "workstream_id": "ws-1",
    }
    fields.update(overrides)
    return GateVerdictRecord(**fields)  # type: ignore[arg-type]


class TestAxisSeparation:
    def test_the_enforcement_axis_keeps_its_vocabulary(self) -> None:
        assert _record(enforcement="mandatory").enforcement == "mandatory"
        assert _record(enforcement="advisory").enforcement == "advisory"

    def test_the_old_field_name_is_gone(self) -> None:
        """Not renamed-with-an-alias: the whole point is that this name now
        belongs to steward's catalog-owned intent axis."""
        legacy: dict[str, object] = {
            "gate_id": "agent.approver",
            "obligation": "advisory",  # the v1 spelling
            "verdict": "not_run",
            "phase": "ex_post",
            "ts": "2026-08-12T00:00:00+00:00",
            "workstream_id": "ws-1",
        }

        with pytest.raises(ValidationError):
            GateVerdictRecord(**legacy)  # type: ignore[arg-type]

    def test_the_intent_axis_is_absent_not_null(self) -> None:
        """Owner decision (2026-08-12): classifying our own producer ids as
        quality|approval waits for a consumer. Absent, never a null that would
        read as 'unclassified'."""
        dumped = _record().model_dump(by_alias=True)

        assert "obligation" not in dumped

    def test_enforcement_tokens_are_the_ones_the_catalog_reserved(self) -> None:
        from maestro.gate_catalog import RESERVED_OBLIGATION_TOKENS

        assert {"mandatory", "advisory"} == RESERVED_OBLIGATION_TOKENS


class TestDiscriminator:
    def test_records_declare_v2(self) -> None:
        dumped = _record().model_dump(by_alias=True, exclude_none=True)

        assert dumped["schema"] == "maestro.gate-verdict-record/v2"

    def test_the_written_line_declares_v2(self, tmp_path: Path) -> None:
        from tests.test_gates import _make_keeper

        keeper = _make_keeper(tmp_path)
        keeper.append_records([_record(note="stale_sha")])

        jsonl = tmp_path / "logs" / "gate_verdicts.jsonl"
        line = json.loads(jsonl.read_text().splitlines()[-1])
        assert line["schema"] == "maestro.gate-verdict-record/v2"
        assert line["enforcement"] == "advisory"
        assert "obligation" not in line


class TestGateIdNamespace:
    """Every id we write is namespace-conformant, checked at construction."""

    def test_a_producer_id_is_accepted(self) -> None:
        assert _record(gate_id="maestro.validate_strict").gate_id == (
            "maestro.validate_strict"
        )

    def test_a_known_canonical_id_is_accepted(self) -> None:
        assert _record(gate_id="GC-APPROVAL-MISSING").gate_id == "GC-APPROVAL-MISSING"

    def test_an_unknown_canonical_id_cannot_be_written(self) -> None:
        with pytest.raises(ValidationError, match="GC-"):
            _record(gate_id="GC-NOT-IN-OUR-COPY")

    def test_a_malformed_id_cannot_be_written(self) -> None:
        with pytest.raises(ValidationError):
            _record(gate_id="Maestro.Validate_Strict")


class TestCanonicalGatesFromTheClassifier:
    """`mandatory_gates` is where a `GC-*` id can reach us.

    steward's `tier_gates` carries only producer ids today, but it is an
    operator-editable profile. Before this change, any id we had no branch for
    was dropped without a record — for a `GC-*` that is exactly the "degrade to
    a pass" the ruling forbids, and the audit log would not even show that
    something was dropped.
    """

    def _decide(self, tmp_path: Path, gates: list[str]):  # type: ignore[no-untyped-def]
        from tests.test_gates import _make_keeper

        return _make_keeper(tmp_path)._decide(
            "ex_post",
            "ws-1",
            "0" * 40,
            {"tier": "medium", "mandatory_gates": gates, "flags": []},
            approvals=set(),
        )

    def test_an_unknown_canonical_gate_blocks(self, tmp_path: Path) -> None:
        decision = self._decide(tmp_path, ["GC-NOT-IN-OUR-COPY"])

        assert decision.allow is False
        assert "GC-NOT-IN-OUR-COPY" in (decision.reason or "")

    def test_the_refusal_is_recorded_under_our_own_id(self, tmp_path: Path) -> None:
        """We refuse to mint a record under an id we do not recognise, so the
        refusal is attributed to the check that made it."""
        decision = self._decide(tmp_path, ["GC-NOT-IN-OUR-COPY"])

        refusals = [r for r in decision.records if r.verdict == "error"]
        assert [r.gate_id for r in refusals] == ["maestro.gate_id_namespace"]
        assert refusals[0].enforcement == "mandatory"
        assert "GC-NOT-IN-OUR-COPY" in (refusals[0].note or "")

    def test_a_known_canonical_gate_is_annotated_not_blocked(
        self, tmp_path: Path
    ) -> None:
        """A catalog gate is real but enforced in the target repo's CI, not at
        this edge — the existing advisory-annotation case (M-2)."""
        decision = self._decide(tmp_path, ["GC-APPROVAL-MISSING"])

        assert decision.allow is True
        annotations = [
            r for r in decision.records if r.gate_id == "GC-APPROVAL-MISSING"
        ]
        assert annotations and annotations[0].enforcement == "advisory"
        assert annotations[0].verdict == "missing"

    def test_producer_gates_keep_their_existing_handling(self, tmp_path: Path) -> None:
        decision = self._decide(
            tmp_path, ["git.required_reviews", "maestro.validate_strict"]
        )

        by_id = {r.gate_id: r for r in decision.records}
        assert by_id["git.required_reviews"].enforcement == "advisory"
        assert by_id["maestro.validate_strict"].enforcement == "mandatory"
        assert decision.allow is True

    def test_an_unrecognised_producer_gate_is_still_dropped(
        self, tmp_path: Path
    ) -> None:
        """Unchanged on purpose: outside `GC-*` steward defines nothing, so an
        id we have no enforcement point for is not ours to adjudicate."""
        decision = self._decide(tmp_path, ["someone.a_gate_we_do_not_enforce"])

        assert decision.allow is True
        assert not [
            r
            for r in decision.records
            if r.gate_id == "someone.a_gate_we_do_not_enforce"
        ]

    def test_a_malformed_gate_id_from_the_classifier_blocks(
        self, tmp_path: Path
    ) -> None:
        decision = self._decide(tmp_path, ["Not A Gate Id"])

        assert decision.allow is False
