"""Upstream-drift watch over ATP's score contract.

The comparison is pure and tested here; the network lives in a two-line wrapper.
That split is the point: what can go wrong in the comparison is subtle (a
fixture published after we vendored, a basename collision, a version bump), and
none of it should require a live producer to exercise.

Companion to `tests/test_benchmark_score_contract.py`, which guards the other
half — that our vendored bytes still match our own `PIN`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "check_atp_score_contract_drift.py"
)


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("atp_drift_watch", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging error
        raise RuntimeError(f"cannot load the drift watch from {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["atp_drift_watch"] = module
    spec.loader.exec_module(module)
    return module


WATCH = _load()

PINS = {
    "run_status_completion_only.json": "aaaa",
    "run_status_evaluated.json": "bbbb",
    "score_contract.py": "cccc",
}

FIXTURE_DIR = "tests/fixtures/benchmark_score_contract"
MODULE_PATH = "packages/atp-dashboard/atp/dashboard/benchmark/score_contract.py"


def _sidecar(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "contract_version": 1,
        "files": {
            f"{FIXTURE_DIR}/run_status_completion_only.json": "aaaa",
            f"{FIXTURE_DIR}/run_status_evaluated.json": "bbbb",
            MODULE_PATH: "cccc",
        },
    }
    base.update(overrides)
    return base


def test_agreement_is_silent() -> None:
    assert WATCH.compare(_sidecar(), PINS) == []


def test_changed_digest_is_reported_by_name() -> None:
    sidecar = _sidecar()
    sidecar["files"][f"{FIXTURE_DIR}/run_status_evaluated.json"] = "dddd"

    findings = WATCH.compare(sidecar, PINS)

    assert len(findings) == 1
    assert "run_status_evaluated.json" in findings[0]
    assert "re-vendor" in findings[0]


def test_new_upstream_fixture_is_reported() -> None:
    """The case a hardcoded list of known paths cannot catch.

    A fixture published after we vendored breaks nothing of ours — which is
    exactly why it goes unnoticed. It is also how `run_status_evaluated.json`
    shipped undeclared in ATP's own handoff document (their #298).
    """
    sidecar = _sidecar()
    sidecar["files"][f"{FIXTURE_DIR}/run_status_future.json"] = "eeee"

    findings = WATCH.compare(sidecar, PINS)

    assert len(findings) == 1
    assert "run_status_future.json" in findings[0]
    assert "absent from our PIN" in findings[0]


def test_pinned_file_gone_upstream_is_reported() -> None:
    sidecar = _sidecar()
    del sidecar["files"][MODULE_PATH]

    findings = WATCH.compare(sidecar, PINS)

    assert len(findings) == 1
    assert "score_contract.py" in findings[0]
    assert "not published upstream" in findings[0]


def test_contract_version_bump_is_reported() -> None:
    """A version we do not implement is an event, not noise."""
    findings = WATCH.compare(_sidecar(contract_version=2), PINS)

    assert any("contract_version" in f for f in findings)


def test_basename_collision_is_its_own_finding() -> None:
    """The join is on basename; ambiguity must be loud, not last-write-wins."""
    sidecar = _sidecar()
    sidecar["files"]["packages/elsewhere/score_contract.py"] = "ffff"

    findings = WATCH.compare(sidecar, PINS)

    assert any("ambiguous" in f for f in findings)


def test_missing_files_map_is_unusable_not_drift() -> None:
    """Nothing was compared, so this is "unusable", not "disagreed"."""
    with pytest.raises(WATCH.SidecarMalformed, match="files"):
        WATCH.compare({"contract_version": 1}, PINS)


def test_unreadable_sidecar_exits_two_not_zero(tmp_path: Path) -> None:
    """Unknown is not agreement — the whole reason this watch exists."""
    assert WATCH.main(["--sidecar-file", str(tmp_path / "nope.json")]) == 2


def test_agreeing_sidecar_exits_zero(tmp_path: Path) -> None:
    real_pins = WATCH.read_pin()
    sidecar = {
        "contract_version": 1,
        "files": {
            f"{FIXTURE_DIR}/{name}" if name.endswith(".json") else MODULE_PATH: digest
            for name, digest in real_pins.items()
        },
    }
    path = tmp_path / "DIGESTS.json"
    path.write_text(json.dumps(sidecar), encoding="utf-8")

    assert WATCH.main(["--sidecar-file", str(path)]) == 0


def test_drifted_sidecar_exits_one(tmp_path: Path) -> None:
    path = tmp_path / "DIGESTS.json"
    path.write_text(json.dumps(_sidecar()), encoding="utf-8")

    assert WATCH.main(["--sidecar-file", str(path)]) == 1


def test_pin_parser_agrees_with_the_vendored_pin() -> None:
    """The watch and the copy-integrity test must read one file the same way."""
    pins = WATCH.read_pin()

    assert set(pins) == {
        "run_status_completion_only.json",
        "run_status_evaluated.json",
        "run_status_forward_compat.json",
        "score_contract.py",
    }
    assert all(len(digest) == 64 for digest in pins.values())


@pytest.mark.parametrize("field", ["contract_version", "files"])
def test_missing_required_field_is_never_agreement(field: str) -> None:
    """A required field of their schema: without it, we cannot say what we read."""
    sidecar = _sidecar()
    del sidecar[field]

    with pytest.raises(WATCH.SidecarMalformed):
        WATCH.compare(sidecar, PINS)


# --- ревью PR #208: сломанный сайдкар не должен ронять скрипт ---------------


def test_non_string_digest_is_unusable_not_drift() -> None:
    """One bad entry makes the whole document untrustworthy.

    The sidecar comes out of a single generator, so a digest that is not a
    digest means the file is not what it claims to be. A traceback here would
    exit the same way drift does and send the operator re-vendoring for nothing.
    """
    sidecar = _sidecar()
    sidecar["files"][f"{FIXTURE_DIR}/run_status_evaluated.json"] = 12345

    with pytest.raises(WATCH.SidecarMalformed, match=r"run_status_evaluated\.json"):
        WATCH.compare(sidecar, PINS)


def test_null_digest_is_unusable_too() -> None:
    sidecar = _sidecar()
    sidecar["files"][MODULE_PATH] = None

    with pytest.raises(WATCH.SidecarMalformed):
        WATCH.compare(sidecar, PINS)


def test_malformed_digest_exits_two(tmp_path: Path) -> None:
    """Red either way, but they ask for different things: re-vendor, or look."""
    sidecar = _sidecar()
    sidecar["files"][MODULE_PATH] = []
    path = tmp_path / "DIGESTS.json"
    path.write_text(json.dumps(sidecar), encoding="utf-8")

    assert WATCH.main(["--sidecar-file", str(path)]) == 2


def test_ambiguous_name_is_reported_once_and_not_compared() -> None:
    """Comparing an arbitrarily chosen key would depend on dict order."""
    sidecar = _sidecar()
    sidecar["files"]["packages/elsewhere/score_contract.py"] = "ffff"

    findings = WATCH.compare(sidecar, PINS)

    naming_the_file = [f for f in findings if "score_contract.py" in f]
    assert len(naming_the_file) == 1
    assert "ambiguous" in naming_the_file[0]


@pytest.mark.parametrize(
    "root", [pytest.param([], id="list"), pytest.param("text", id="string")]
)
def test_non_object_root_is_inconclusive_not_drift(tmp_path: Path, root: Any) -> None:
    """Valid JSON, but not the sidecar: an error page, a redirect, a wrong URL.

    That is "could not read it" (2), never "upstream drifted" (1): exit 1 would
    send the operator to re-vendor bytes that were never compared.
    """
    path = tmp_path / "DIGESTS.json"
    path.write_text(json.dumps(root), encoding="utf-8")

    assert WATCH.main(["--sidecar-file", str(path)]) == 2


def test_version_bump_is_drift_not_malformed() -> None:
    """A version that is present but not ours is their event, not a broken file.

    The distinction is not pedantry: re-vendoring (after reading the handoff) is
    the right response here, so it is a finding (1), not unusable (2).
    """
    findings = WATCH.compare(_sidecar(contract_version=2), PINS)

    assert any("contract_version" in f for f in findings)
