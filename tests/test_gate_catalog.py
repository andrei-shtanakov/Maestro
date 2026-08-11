"""The vendored steward gate catalog and the gate_id namespace rule (#160).

Three separate guarantees live here, and keeping them separate is the point:

- **copy-integrity** — the vendored bytes are the bytes we recorded. Runs
  everywhere, including CI, where no sibling checkout exists.
- **upstream provenance** — those bytes really are steward's at the pinned
  commit, not a hand-written paraphrase. Needs the sibling; skipped without it.
- **upstream drift** — the catalog's *composition* has not moved past the
  version we vendored. Also needs the sibling.

A single "is the vendored file ok" test would answer none of these three
questions properly: a local edit, a fabricated provenance and a stale pin are
different defects with different fixes.
"""

import hashlib
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from maestro.gate_catalog import (
    CANONICAL_GATE_ID_PATTERN,
    PRODUCER_GATE_ID_PATTERN,
    RESERVED_OBLIGATION_TOKENS,
    STEWARD_VENDORED_FROM_SHA,
    VENDORED_FILE_SHA256,
    GateIdMalformed,
    UnknownCanonicalGate,
    canonical_gate_ids,
    catalog_version,
    classify_gate_id,
    vendored_file,
)


SIBLING = Path(__file__).resolve().parents[2] / "steward"
_needs_sibling = pytest.mark.skipif(
    not (SIBLING / ".git").exists(),
    reason="steward sibling checkout absent (CI); provenance unverifiable here",
)


class TestCopyIntegrity:
    """The copy has not been edited since it was vendored."""

    def test_every_vendored_file_matches_its_recorded_digest(self) -> None:
        for relative, digest in VENDORED_FILE_SHA256.items():
            actual = hashlib.sha256(vendored_file(relative).read_bytes()).hexdigest()
            assert actual == digest, f"{relative} was edited after vendoring"

    def test_the_pin_is_a_full_sha(self) -> None:
        assert re.fullmatch(r"[0-9a-f]{40}", STEWARD_VENDORED_FROM_SHA)


@_needs_sibling
class TestUpstreamProvenance:
    """The recorded pin is real: upstream at that commit has these bytes."""

    @pytest.mark.parametrize(
        "relative,source",
        [
            ("profiles/gate-catalog.yaml", "profiles/gate-catalog.yaml"),
            (
                "contracts/gate-verdicts/v1/README.md",
                "contracts/gate-verdicts/v1/README.md",
            ),
        ],
    )
    def test_vendored_bytes_equal_upstream_at_the_pin(
        self, relative: str, source: str
    ) -> None:
        upstream = subprocess.run(
            ["git", "show", f"{STEWARD_VENDORED_FROM_SHA}:{source}"],
            cwd=SIBLING,
            capture_output=True,
            check=True,
        ).stdout
        assert vendored_file(relative).read_bytes() == upstream


@_needs_sibling
class TestUpstreamDrift:
    """Composition changes upstream must reach us, unrelated edits need not.

    steward bumps `version` on any change to the gate composition and only
    then. Failing on every upstream byte change would make an unrelated
    wording fix in a 222-line file turn this repo red; failing on a version
    bump is the signal that our pinned copy now describes a different set of
    gates.
    """

    def test_upstream_catalog_version_still_matches_the_vendored_one(self) -> None:
        head = subprocess.run(
            ["git", "show", "origin/master:profiles/gate-catalog.yaml"],
            cwd=SIBLING,
            capture_output=True,
        )
        if head.returncode != 0:
            pytest.skip("steward origin/master unavailable in this checkout")
        match = re.search(r"^version:\s*(\d+)$", head.stdout.decode(), re.MULTILINE)
        assert match is not None
        assert int(match.group(1)) == catalog_version(), (
            "steward bumped the gate catalog version: re-vendor the pinned copy"
        )


class TestMirroredRule:
    """The patterns come from the vendored file, not from a second copy in code.

    steward calls the mirror "not a knob": the loader rejects any divergence
    from its own constants. Re-declaring the regexes in Python here would
    create exactly the divergence the mirror exists to prevent.
    """

    def test_patterns_are_read_from_the_vendored_catalog(self) -> None:
        """Guards against the one refactor that would void the mirror:
        replacing the load with a pair of regexes typed out in Python."""
        published = yaml.safe_load(
            vendored_file("profiles/gate-catalog.yaml").read_text(encoding="utf-8")
        )["gate_id_namespaces"]

        assert CANONICAL_GATE_ID_PATTERN.pattern == published["canonical_pattern"]
        assert PRODUCER_GATE_ID_PATTERN.pattern == published["producer_pattern"]

    def test_reserved_tokens_are_our_enforcement_vocabulary(self) -> None:
        """The collision steward permanently barred is exactly our axis."""
        assert frozenset({"mandatory", "advisory"}) == RESERVED_OBLIGATION_TOKENS

    def test_the_catalog_carries_the_canonical_gate_set(self) -> None:
        ids = canonical_gate_ids()

        assert "GC-APPROVAL-MISSING" in ids
        assert all(CANONICAL_GATE_ID_PATTERN.fullmatch(gid) for gid in ids)


class TestClassifyGateId:
    """Producer ids are validated by shape; only GC-* is resolved."""

    @pytest.mark.parametrize(
        "gate_id",
        [
            "steward.risk_classify_ex_ante",
            "steward.risk_classify_ex_post",
            "steward.gate_check",
            "human.owner_approval",
            "human.transition_approval",
            "maestro.validate_strict",
            "git.required_reviews",
            "agent.approver",
        ],
    )
    def test_every_id_maestro_emits_is_producer_conformant(self, gate_id: str) -> None:
        assert classify_gate_id(gate_id) == "producer"

    def test_a_producer_id_is_never_resolved_against_the_catalog(self) -> None:
        """Membership is decided by resolving the id, and producer ids are not
        resolved at all — an id steward has never heard of is still valid."""
        assert classify_gate_id("someone.a_gate_steward_never_saw") == "producer"

    def test_a_known_canonical_id_resolves(self) -> None:
        assert classify_gate_id("GC-APPROVAL-MISSING") == "canonical"

    def test_an_unknown_canonical_id_fails_closed(self) -> None:
        with pytest.raises(UnknownCanonicalGate) as excinfo:
            classify_gate_id("GC-INVENTED-GATE")

        assert "GC-INVENTED-GATE" in str(excinfo.value)

    @pytest.mark.parametrize(
        "gate_id",
        [
            "Steward.risk_classify",  # capitalised namespace
            "steward",  # no dot
            "steward.",  # empty name
            "steward.Risk_Classify",  # capitalised name
            "gc-approval-missing",  # canonical namespace lowercased
            "",
        ],
    )
    def test_an_id_in_neither_namespace_is_rejected(self, gate_id: str) -> None:
        with pytest.raises(GateIdMalformed):
            classify_gate_id(gate_id)
