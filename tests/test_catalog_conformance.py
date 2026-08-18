"""Conformance of Maestro's catalog loader against the SSOT fixture set.

The fixtures under ``tests/fixtures/catalog-conformance/v1/`` are a **pinned
vendored copy** of a contract owned by ``devtools`` (ADR-ECO-003 /
ADR-ECO-003b; see that directory's ``PIN``). Nothing here may be edited to
make a test pass — a red case is either a Maestro loader defect or a
deliberate, recorded divergence.

Three properties this module is responsible for, in order:

1. **Integrity first.** The vendored copy is verified against the upstream
   ``manifest.json`` by a test that does not depend on parametrization. A
   truncated or drifted copy must fail loudly, never quietly shrink the
   parametrized case list into a smaller green suite.
2. **The product entry point.** Every case goes through ``load_catalog()`` —
   the same function the scheduler and the spawners call. A tripwire test
   asserts those call sites still exist, so a refactor cannot move production
   onto a different path while this suite keeps testing the old one.
3. **One test per upstream expectation.** Cases are parametrized from
   ``expectations.toml``; adding a case upstream adds a test here on the next
   pin bump.

Known divergence, recorded rather than disputed: ``$ATP_CATALOG`` pointing at
a missing file returns ``None`` instead of surfacing an error. The expectation
is accepted as correct (ADR-ECO-003b D2, mirrored by arbiter); fixing it is a
breaking change against Maestro's 2026-07-02 decision and belongs in its own
PR. The ``xfail(strict=True)`` below is the tripwire: fixing the loader
silently is impossible, the test turns XPASS-failed.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from maestro.catalog import (
    CatalogError,
    CatalogNotConfigured,
    load_catalog,
    resolve_model,
)


CONTRACT_DIR = Path(__file__).parent / "fixtures" / "catalog-conformance" / "v1"
MANIFEST_PATH = CONTRACT_DIR / "manifest.json"

#: Excluded from the integrity surface: the manifest cannot hash itself, and
#: PIN is this repo's own file, not part of the upstream contract.
SURFACE_EXCLUDED = frozenset({"manifest.json", "PIN"})

#: Path-resolution scenarios the vendored set is known to carry. A subset
#: assertion, so an additive upstream bump does not fail on arrival — but a
#: truncated copy does.
KNOWN_PATHRES_IDS = frozenset(
    {"env-set-file-exists", "env-unset", "env-set-file-missing"}
)

#: `load_catalog()` call sites in product code. The suite is only meaningful
#: while production actually goes through this function.
PRODUCT_CALL_SITES = (
    "maestro/scheduler.py",
    "maestro/spawners/claude_code.py",
    "maestro/spawners/codex.py",
    "maestro/spawners/opencode.py",
)

MANIFEST: dict[str, Any] = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
EXPECTATIONS: dict[str, Any] = tomllib.loads(
    (CONTRACT_DIR / "expectations.toml").read_text(encoding="utf-8")
)
CASES: list[dict[str, Any]] = EXPECTATIONS["case"]
PATHRES: list[dict[str, Any]] = EXPECTATIONS["pathres"]


def _surface_files() -> list[Path]:
    """Every vendored file in the pin surface, sorted by relative POSIX path."""
    return sorted(
        (
            p
            for p in CONTRACT_DIR.rglob("*")
            if p.is_file() and p.name not in SURFACE_EXCLUDED
        ),
        key=lambda p: p.relative_to(CONTRACT_DIR).as_posix(),
    )


def _case_id(case: dict[str, Any]) -> str:
    return f"{case.get('code', case['expect'])}-{Path(case['file']).stem}"


# --------------------------------------------------------------------------
# 1. Integrity — runs independently of the parametrized cases below.
# --------------------------------------------------------------------------


def test_vendored_copy_matches_upstream_manifest() -> None:
    """Per-file sha256 + tree_sha256, exactly as the contract defines them."""
    entries = [
        {
            "path": p.relative_to(CONTRACT_DIR).as_posix(),
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in _surface_files()
    ]
    assert entries == MANIFEST["files"], "vendored copy drifted from its PIN"

    tree = "\n".join(f"{e['path']} {e['sha256']}" for e in entries) + "\n"
    assert hashlib.sha256(tree.encode("utf-8")).hexdigest() == MANIFEST["tree_sha256"]


def test_pin_file_records_the_vendored_source() -> None:
    pin = (CONTRACT_DIR / "PIN").read_text(encoding="utf-8")
    assert "devtools@2533ff7b8c3afd74110b3838325bf76ba46ba186" in pin
    assert "contracts/catalog-conformance-fixtures/v1" in pin


def test_every_manifest_fixture_has_an_expectation() -> None:
    """The parametrized case list may not silently shrink below the manifest."""
    manifest_fixtures = {
        e["path"] for e in MANIFEST["files"] if e["path"].startswith("fixtures/")
    }
    assert {c["file"] for c in CASES} == manifest_fixtures
    assert {p["id"] for p in PATHRES} >= KNOWN_PATHRES_IDS


def test_product_entry_point_is_load_catalog() -> None:
    """Tripwire: production must keep reaching the loader this suite exercises."""
    repo_root = Path(__file__).resolve().parents[1]
    for rel in PRODUCT_CALL_SITES:
        source = (repo_root / rel).read_text(encoding="utf-8")
        assert "load_catalog()" in source, f"{rel} no longer calls load_catalog()"


# --------------------------------------------------------------------------
# 2. One test per upstream [[case]].
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[_case_id(c) for c in CASES])
def test_catalog_case(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATP_CATALOG", str(CONTRACT_DIR / case["file"]))
    expect = case["expect"]

    if expect == "valid":
        with capture_logs() as logs:
            catalog = load_catalog()
        assert catalog is not None
        assert not [e for e in logs if e.get("log_level") == "warning"]
        return

    if expect == "parse-error":
        with pytest.raises(CatalogError) as exc_info:
            load_catalog()
        assert isinstance(exc_info.value.__cause__, tomllib.TOMLDecodeError)
        return

    if expect == "error":
        # Rejecting the catalog: the loader must not hand back a usable object.
        with pytest.raises(CatalogError):
            load_catalog()
        return

    if expect == "flag":
        # At least warned about; outright rejection also conforms. Silently
        # accepting it as a healthy catalog is the one nonconformant outcome.
        with capture_logs() as logs:
            try:
                load_catalog()
            except CatalogError:
                return
        assert [e for e in logs if e.get("log_level") == "warning"], (
            f"{case['file']} ({case.get('code')}) was accepted with no warning"
        )
        return

    pytest.fail(f"unknown expectation class {expect!r} — pin bumped?")


# --------------------------------------------------------------------------
# 3. One test per upstream [[pathres]] ($ATP_CATALOG layer only).
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", PATHRES, ids=[p["id"] for p in PATHRES])
def test_path_resolution_case(
    scenario: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: Any
) -> None:
    if scenario["expect"] == "missing-file-error":
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=(
                    "Recorded divergence, not a disputed expectation: "
                    "$ATP_CATALOG pointing at a missing file returns None + an "
                    "info log (Maestro decision 2026-07-02). The contract "
                    "(ADR-ECO-003b D2) is accepted as correct; the fix is a "
                    "breaking change and gets its own PR "
                    "(@id:catalog-missing-file-fail-loud)."
                ),
            )
        )

    env = scenario["env"]
    if env == "unset":
        monkeypatch.delenv("ATP_CATALOG", raising=False)
    elif env == "set":
        monkeypatch.setenv("ATP_CATALOG", str(CONTRACT_DIR / scenario["target"]))
    elif env == "set-missing":
        monkeypatch.setenv("ATP_CATALOG", str(CONTRACT_DIR / "no-such-catalog.toml"))
    else:
        pytest.fail(f"unknown pathres env {env!r} — pin bumped?")

    expect = scenario["expect"]

    if expect == "loaded":
        assert load_catalog() is not None
        return

    if expect == "not-configured":
        # Fail-loud lives one layer up: no catalog is only fatal when a default
        # is actually needed. That is resolve_model()'s CatalogNotConfigured —
        # asserted here, because "returns None" alone would also be satisfied
        # by a hidden default further down.
        catalog = load_catalog()
        assert catalog is None
        monkeypatch.delenv("MAESTRO_ALPHA_CLI_MODEL", raising=False)
        with pytest.raises(CatalogNotConfigured):
            resolve_model(None, "MAESTRO_ALPHA_CLI_MODEL", "alpha_cli", catalog)
        return

    if expect == "missing-file-error":
        with pytest.raises(CatalogError):
            load_catalog()
        return

    pytest.fail(f"unknown pathres expectation {expect!r} — pin bumped?")
