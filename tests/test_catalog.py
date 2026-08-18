"""Tests for the model catalog loader (ADR-ECO-003b)."""

from pathlib import Path

import pytest
from structlog.testing import capture_logs

from maestro.catalog import (
    Catalog,
    CatalogAgent,
    CatalogError,
    CatalogHarness,
    CatalogMalformed,
    CatalogModel,
    CatalogNotConfigured,
    HarnessModelUnresolved,
    check_catalog_references,
    load_catalog,
    resolve_catalog_path,
    resolve_model,
    warn_on_model_status,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _use_catalog(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("ATP_CATALOG", str(FIXTURES / name))


def test_path_unset_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATP_CATALOG", raising=False)
    assert resolve_catalog_path() is None
    assert load_catalog() is None


def test_path_absent_file_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATP_CATALOG", str(FIXTURES / "does-not-exist.toml"))
    # A path typo must not crash — it is "no catalog", not a fatal error.
    assert load_catalog() is None


def test_malformed_raises_catalog_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_catalog(monkeypatch, "agents-catalog-malformed.toml")
    with pytest.raises(CatalogMalformed):
        load_catalog()


def test_default_model_for_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_catalog(monkeypatch, "agents-catalog.toml")
    cat = load_catalog()
    assert cat is not None
    assert cat.default_model_for_harness("claude_code") == "claude-sonnet-4-6"
    assert cat.default_model_for_harness("codex_cli") == "gpt-5.5"


def test_default_no_routable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_catalog(monkeypatch, "agents-catalog.toml")
    cat = load_catalog()
    assert cat is not None
    with pytest.raises(HarnessModelUnresolved):
        cat.default_model_for_harness("aider")


def test_default_ambiguous_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_catalog(monkeypatch, "agents-catalog-ambiguous.toml")
    cat = load_catalog()
    assert cat is not None
    with pytest.raises(HarnessModelUnresolved):
        cat.default_model_for_harness("claude_code")


def test_per_task_error_is_not_a_catalog_error() -> None:
    # Guards the blast-radius split: per-task must never be caught by the
    # scheduler's `except CatalogError` halt arm.
    assert not issubclass(HarnessModelUnresolved, CatalogError)


def test_status_of(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_catalog(monkeypatch, "agents-catalog.toml")
    cat = load_catalog()
    assert cat is not None
    assert cat.status_of("claude-sonnet-4-6") == "active"
    assert cat.status_of("legacy-mini") == "deprecated"
    assert cat.status_of("ancient-1") == "retired"
    assert cat.status_of("claude-sonnet-latest") == "active"  # alias resolves
    assert cat.status_of("never-heard-of-it") is None  # unknown


def test_nearest_models(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_catalog(monkeypatch, "agents-catalog.toml")
    cat = load_catalog()
    assert cat is not None
    near = cat.nearest_models("claude-sonnet-4-7")
    assert "claude-sonnet-4-6" in near


def test_fixture_matches_sibling_ssot() -> None:
    """When the sibling dev/ops SSOT exists, the fixture's routable defaults must
    still match it. Skipped in isolation (CI without the sibling repo). Seed of
    the ADR-003b cross-reader conformance test (shape only, not behavior)."""
    ssot = Path(__file__).parents[2] / "atp-platform" / "method" / "agents-catalog.toml"
    if not ssot.is_file():
        pytest.skip("sibling atp-platform SSOT not present")
    import tomllib

    data = tomllib.loads(ssot.read_text(encoding="utf-8"))
    cat = Catalog.model_validate(data)
    assert cat.default_model_for_harness("claude_code") == "claude-sonnet-4-6"
    assert cat.default_model_for_harness("codex_cli") == "gpt-5.5"


def _catalog(monkeypatch: pytest.MonkeyPatch, name: str = "agents-catalog.toml"):
    _use_catalog(monkeypatch, name)
    return load_catalog()


def test_resolve_precedence_routed_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _catalog(monkeypatch)
    monkeypatch.setenv("MAESTRO_CLAUDE_MODEL", "env-x")
    assert resolve_model("routed-x", "MAESTRO_CLAUDE_MODEL", "claude_code", cat) == (
        "routed-x",
        "routed",
    )


def test_resolve_env_then_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _catalog(monkeypatch)
    monkeypatch.setenv("MAESTRO_CLAUDE_MODEL", "env-x")
    assert resolve_model(None, "MAESTRO_CLAUDE_MODEL", "claude_code", cat) == (
        "env-x",
        "env",
    )
    monkeypatch.delenv("MAESTRO_CLAUDE_MODEL", raising=False)
    assert resolve_model(None, "MAESTRO_CLAUDE_MODEL", "claude_code", cat) == (
        "claude-sonnet-4-6",
        "catalog",
    )


def test_resolve_empty_routed_treated_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cat = _catalog(monkeypatch)
    monkeypatch.delenv("MAESTRO_CLAUDE_MODEL", raising=False)
    assert resolve_model("", "MAESTRO_CLAUDE_MODEL", "claude_code", cat) == (
        "claude-sonnet-4-6",
        "catalog",
    )


def test_resolve_no_catalog_default_path_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAESTRO_CLAUDE_MODEL", raising=False)
    with pytest.raises(CatalogNotConfigured):
        resolve_model(None, "MAESTRO_CLAUDE_MODEL", "claude_code", None)


def test_resolve_routed_selfsufficient_without_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Routed does not need a catalog — no raise even when catalog is None.
    assert resolve_model("routed-x", "MAESTRO_CLAUDE_MODEL", "claude_code", None) == (
        "routed-x",
        "routed",
    )


def test_warn_retired_fires_even_for_catalog_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cat = _catalog(monkeypatch)
    with capture_logs() as logs:
        warn_on_model_status("ancient-1", "catalog", cat)
    assert any(e["event"] == "agent.model_retired" for e in logs)


def test_warn_deprecated_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = _catalog(monkeypatch)
    with capture_logs() as logs:
        warn_on_model_status("legacy-mini", "routed", cat)
    assert any(e["event"] == "agent.model_deprecated" for e in logs)


def test_warn_unknown_soft_for_routed_but_skipped_for_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cat = _catalog(monkeypatch)
    with capture_logs() as logs:
        warn_on_model_status("mystery", "routed", cat)
    assert any(e["event"] == "agent.model_unknown" for e in logs)

    with capture_logs() as logs:
        warn_on_model_status("mystery", "catalog", cat)  # tautological → skip
    assert not any(e["event"] == "agent.model_unknown" for e in logs)


def test_warn_active_silent_and_no_catalog_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cat = _catalog(monkeypatch)
    with capture_logs() as logs:
        warn_on_model_status("claude-sonnet-4-6", "routed", cat)
    assert not [e for e in logs if e["event"].startswith("agent.model_")]

    with capture_logs() as logs:
        warn_on_model_status("anything", "routed", None)  # no catalog → no-op
    assert not [e for e in logs if e["event"].startswith("agent.model_")]


# --- Referential checks V1..V6 (shared conformance vocabulary) -------------


def _agent(harness: str, model: str, routable: bool = False) -> CatalogAgent:
    return CatalogAgent(harness=harness, model=model, tested=True, routable=routable)


def test_reference_checks_resolve_aliases_before_declaring_v2() -> None:
    """An alias is a declared model id — V2 must not fire on it."""
    cat = Catalog(
        models={"alpha-1": CatalogModel(vendor="acme", aliases=["alpha-latest"])},
        agents=[_agent("alpha_cli", "alpha-latest")],
    )
    errors, warnings = check_catalog_references(cat)
    assert errors == []
    assert warnings == []


def test_reference_checks_flag_deprecated_but_reject_retired() -> None:
    cat = Catalog(
        models={
            "old-1": CatalogModel(vendor="acme", status="deprecated"),
            "dead-1": CatalogModel(vendor="acme", status="retired"),
        },
        agents=[_agent("alpha_cli", "old-1"), _agent("alpha_cli", "dead-1")],
    )
    errors, warnings = check_catalog_references(cat)
    assert [e for e in errors if e.startswith("V3")]
    assert not [e for e in errors if e.startswith("V6")]
    assert [w for w in warnings if w.startswith("V6")]


def test_v1_and_v5_are_not_armed_without_the_harness_plane() -> None:
    """No [harnesses.*] means unverifiable, not valid — and it is announced."""
    cat = Catalog(
        models={"alpha-1": CatalogModel(vendor="acme")},
        agents=[_agent("ghost_cli", "alpha-1", routable=True)],
    )
    assert check_catalog_references(cat) == ([], [])


def test_v1_and_v5_fire_once_the_harness_plane_is_present() -> None:
    cat = Catalog(
        models={"alpha-1": CatalogModel(vendor="acme")},
        harnesses={"alpha_cli": CatalogHarness(kind="cli", routable=False)},
        agents=[
            _agent("ghost_cli", "alpha-1"),
            _agent("alpha_cli", "alpha-1", routable=True),
        ],
    )
    errors, _ = check_catalog_references(cat)
    assert [e for e in errors if e.startswith("V1")]
    assert [e for e in errors if e.startswith("V5")]


def test_empty_harness_plane_reads_as_scaffolding_not_as_zero_harnesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare `[harnesses]` header must not reject the whole catalog.

    Pydantic CAN tell "header present but empty" from "header absent"
    (model_fields_set), so treating them alike is a decision: the shipped
    template teaches users to keep an empty table header as schema scaffolding,
    and reading that header as "zero harnesses exist" would turn every
    [[agents]] row into a V1 violation. Recorded here so the reading cannot be
    flipped by accident.
    """
    catalog_file = tmp_path / "agents-catalog.toml"
    catalog_file.write_text(
        '[models."alpha-1"]\nvendor = "acme"\n\n'
        "[harnesses]\n\n"
        '[[agents]]\nharness = "alpha_cli"\nmodel = "alpha-1"\nroutable = true\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ATP_CATALOG", str(catalog_file))
    with capture_logs() as logs:
        catalog = load_catalog()
    assert catalog is not None
    assert catalog.harnesses == {}
    assert [e for e in logs if e["event"] == "catalog.reference_checks_not_armed"]


def test_unarmed_reference_checks_are_announced_on_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_catalog(monkeypatch, "agents-catalog.toml")  # no [harnesses.*] plane
    with capture_logs() as logs:
        assert load_catalog() is not None
    events = [e for e in logs if e["event"] == "catalog.reference_checks_not_armed"]
    assert len(events) == 1
    assert events[0]["unarmed"] == ["V1", "V5"]
