#!/usr/bin/env python3
"""Has ATP's score contract moved past the commit we vendored?

Two guarantees protect a vendored contract, and they fail differently.
**Copy-integrity** — our bytes still match our `PIN` — is a unit test
(`tests/test_benchmark_score_contract.py`). **Upstream-drift** — the producer has
not published something new — cannot be a unit test at all: it needs the
producer's current bytes, and for an installed user there is no sibling checkout
to read them from. That test therefore *skips*, and a skip reads as green.

ATP publishes a digest sidecar for exactly this (maestro#204, atp-platform#301),
so the check is now "download one file and compare" rather than "have a checkout
of their repo next to yours".

Run weekly, not per-PR: our pull requests do not move their contract, so a
per-PR frequency buys nothing but a flake whenever GitHub is briefly
unreachable.

    uv run python scripts/check_atp_score_contract_drift.py

Exit codes — every non-zero one means *act*, and none of them means "probably
fine":

* ``0`` — the sidecar agrees with our pin.
* ``1`` — drift: something upstream differs from what we vendored.
* ``2`` — the sidecar could not be read. **Not** treated as agreement: an
  unreachable producer is an unknown, and an unknown that renders as green is
  the failure this whole contract keeps re-teaching.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


#: Raw sidecar on the producer's default branch.
DEFAULT_SIDECAR_URL = (
    "https://raw.githubusercontent.com/andrei-shtanakov/atp-platform/"
    "main/tests/fixtures/benchmark_score_contract/DIGESTS.json"
)

PIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "atp-score-contract"
    / "v1"
    / "PIN"
)

#: The only `contract_version` this consumer implements (`ScoreSemantics`,
#: `publication_decision`). A bump upstream is a real event for us, not noise.
SUPPORTED_CONTRACT_VERSION = 1

FETCH_TIMEOUT_S = 30


def read_pin(path: Path = PIN_PATH) -> dict[str, str]:
    """Our vendored digests, keyed by file name."""
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, name = line.partition("  ")
        pins[name.strip()] = digest.strip()
    return pins


def compare(sidecar: dict[str, Any], pins: dict[str, str]) -> list[str]:
    """Findings, in reading order. Empty means the pin still describes upstream.

    Upstream keys are repo-relative paths; our `PIN` is keyed by file name, so
    the join is on the basename. That is unambiguous for this contract — and
    asserted to be, rather than assumed: two upstream files sharing a basename
    would make the comparison meaningless, so it is its own finding instead of
    a silent last-write-wins.
    """
    findings: list[str] = []

    version = sidecar.get("contract_version")
    if version != SUPPORTED_CONTRACT_VERSION:
        findings.append(
            f"contract_version is {version!r}, we implement "
            f"{SUPPORTED_CONTRACT_VERSION} — read the handoff before re-vendoring"
        )

    files = sidecar.get("files")
    if not isinstance(files, dict) or not files:
        findings.append("sidecar carries no `files` map — cannot compare anything")
        return findings

    by_name: dict[str, list[str]] = {}
    for key in files:
        by_name.setdefault(Path(key).name, []).append(key)

    ambiguous = {name for name, keys in by_name.items() if len(keys) > 1}
    for name in sorted(ambiguous):
        findings.append(
            f"upstream publishes {len(by_name[name])} files named {name!r} "
            f"({', '.join(sorted(by_name[name]))}) — the name-based join is ambiguous"
        )

    for name, keys in sorted(by_name.items()):
        if name in ambiguous:
            # Already reported. Comparing one arbitrarily chosen key would add
            # a second finding whose truth depends on dict order — noise on top
            # of a defect, and the kind that sends a reader after the wrong file.
            continue
        upstream = files[keys[0]]
        if not isinstance(upstream, str) or not upstream:
            # A malformed sidecar must read as a finding, not a traceback: an
            # exception here exits the same way drift does, so the operator
            # would go re-vendor bytes that are fine.
            findings.append(
                f"{name}: digest is {upstream!r}, not a string — sidecar is malformed"
            )
            continue
        ours = pins.get(name)
        if ours is None:
            # The case a hardcoded list of known paths cannot catch: a fixture
            # published after we vendored. Nothing of ours breaks, and that is
            # precisely why it goes unnoticed without this check.
            findings.append(
                f"{name}: published upstream ({keys[0]}) but absent from our PIN "
                f"— a new fixture we do not vendor"
            )
        elif ours != upstream:
            findings.append(
                f"{name}: upstream {upstream[:12]}… != pinned {ours[:12]}… "
                f"— re-vendor from the current bytes, never from prose"
            )

    for name in sorted(set(pins) - set(by_name)):
        findings.append(
            f"{name}: pinned by us but not published upstream — renamed, moved "
            f"or withdrawn"
        )

    return findings


def fetch(url: str) -> dict[str, Any]:
    """Read the sidecar. Any failure raises; the caller maps it to exit 2."""
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_SIDECAR_URL)
    parser.add_argument(
        "--sidecar-file",
        type=Path,
        help="read a local sidecar instead of fetching (for testing the wiring)",
    )
    args = parser.parse_args(argv)

    try:
        sidecar = (
            json.loads(args.sidecar_file.read_text(encoding="utf-8"))
            if args.sidecar_file
            else fetch(args.url)
        )
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"could not read the score-contract sidecar: {exc}", file=sys.stderr)
        print("unknown is not agreement — this run is inconclusive", file=sys.stderr)
        return 2

    findings = compare(sidecar, read_pin())
    if not findings:
        print("ATP score contract: pin still describes upstream")
        return 0

    print("ATP score contract has drifted from our pin:", file=sys.stderr)
    for finding in findings:
        print(f"  - {finding}", file=sys.stderr)
    print(
        "\nRe-vendor with the bytes on disk (never the prose pins), then update "
        "tests/fixtures/atp-score-contract/v1/PIN.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
