"""Tests for the model catalog loader (ADR-ECO-003b)."""

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

import maestro.catalog
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
    harness_kinds,
    load_catalog,
    model_statuses,
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


def test_empty_harness_plane_declares_zero_harnesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare `[harnesses]` header arms V1 and rejects every enrollment.

    This test used to assert the opposite ("bare header = schema scaffolding"),
    which is why it exists: the reading was pinned rather than left to an
    unexamined bool(), so when devtools#47 canonised the fail-closed reading the
    test failed and forced the change instead of letting the divergence sit.
    """
    catalog_file = tmp_path / "agents-catalog.toml"
    catalog_file.write_text(
        '[models."alpha-1"]\nvendor = "acme"\n\n'
        "[harnesses]\n\n"
        '[[agents]]\nharness = "alpha_cli"\nmodel = "alpha-1"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ATP_CATALOG", str(catalog_file))
    with pytest.raises(CatalogMalformed, match="V1"):
        load_catalog()


def test_empty_harness_plane_without_agents_stays_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only "nothing to resolve with + something to resolve" is rejected."""
    catalog_file = tmp_path / "agents-catalog.toml"
    catalog_file.write_text(
        '[models."alpha-1"]\nvendor = "acme"\n\n[harnesses]\n', encoding="utf-8"
    )
    monkeypatch.setenv("ATP_CATALOG", str(catalog_file))
    assert load_catalog() is not None


def test_unknown_harness_kind_warns_and_names_the_owner_and_the_fix() -> None:
    """V7 by `kind`: a warning that teaches the repair, never a rejection.

    A false positive is possible by construction — HARNESS_KINDS is a hand-made
    copy of a vocabulary published only as prose — so the message has to carry
    both who owns the vocabulary and what to do about it. A self-explaining
    false warning costs a minute; silence cost a session.
    """
    cat = Catalog(
        models={"alpha-1": CatalogModel(vendor="acme")},
        harnesses={"gamma_box": CatalogHarness(kind="container")},
        agents=[],
    )
    errors, warnings = check_catalog_references(cat)
    assert errors == []
    assert len(warnings) == 1
    assert "V7" in warnings[0] and "container" in warnings[0]
    assert "ADR-ECO-003" in warnings[0]
    assert "re-vendor" in warnings[0]


def test_vendored_vocabularies_cover_the_conformance_sets_valid_fixtures() -> None:
    """Every value the set's valid fixtures use is in the vendored vocabulary.

    Holds by construction now that both come from the same pinned file — which
    is the point of the test: it fails if the shipped copy and the conformance
    set ever stop being the same contract.
    """
    valid_dir = (
        Path(__file__).parent
        / "fixtures"
        / "catalog-conformance"
        / "v1"
        / "fixtures"
        / "valid"
    )
    fixtures = sorted(valid_dir.glob("*.toml"))
    assert fixtures, "vendored valid fixtures missing — copy truncated?"
    for fixture in fixtures:
        data = tomllib.loads(fixture.read_text(encoding="utf-8"))
        for name, model in data.get("models", {}).items():
            assert model.get("status", "active") in model_statuses(), (
                f"{fixture.name}: models.{name}.status outside the vocabulary"
            )
        for name, harness in data.get("harnesses", {}).items():
            assert harness.get("kind", "") in harness_kinds(), (
                f"{fixture.name}: harnesses.{name}.kind outside the vocabulary"
            )


def test_unarmed_reference_checks_are_announced_on_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_catalog(monkeypatch, "agents-catalog.toml")  # no [harnesses.*] plane
    with capture_logs() as logs:
        assert load_catalog() is not None
    events = [e for e in logs if e["event"] == "catalog.reference_checks_not_armed"]
    assert len(events) == 1
    assert events[0]["unarmed"] == ["V1", "V5"]


# --- The vocabulary is vendored, not declared (devtools#51) ---------------


def test_no_hand_written_vocabulary_remains_in_the_module() -> None:
    """The point of the change is DELETION, not a pinned constant.

    A copy that survives is a copy someone edits by hand on the next additive
    bump, which is exactly the maintenance this contract removes.
    """
    import maestro.catalog as catalog_module

    for name in ("MODEL_STATUSES", "HARNESS_KINDS", "ModelStatus"):
        assert not hasattr(catalog_module, name), (
            f"{name} is back — the vocabulary must come from the vendored file"
        )
    source = Path(catalog_module.__file__).read_text(encoding="utf-8")
    for value in ("api-baseline",):
        assert value not in source, f"vocabulary value {value!r} hard-coded again"


def test_shipped_vocabulary_matches_the_conformance_sets_copy() -> None:
    """Runtime reads the package copy; the pin covers the conformance set.

    Byte-identity is what makes one pin cover both — `tests/fixtures/` is not
    in the wheel, so runtime cannot read the set itself.
    """
    shipped = (
        Path(maestro.catalog.__file__).parent
        / "resources"
        / "catalog_conformance"
        / "vocabulary.toml"
    )
    vendored = (
        Path(__file__).parent
        / "fixtures"
        / "catalog-conformance"
        / "v1"
        / "vocabulary.toml"
    )
    assert shipped.read_bytes() == vendored.read_bytes()


def test_vocabulary_is_read_from_package_data_not_a_checkout_path() -> None:
    """importlib.resources, so an installed wheel resolves it too."""
    from importlib import resources

    handle = resources.files("maestro.resources.catalog_conformance").joinpath(
        "vocabulary.toml"
    )
    assert handle.is_file()
    assert "harness_kind" in handle.read_text(encoding="utf-8")


def test_an_additive_bump_needs_no_python_edit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A value added upstream is accepted by re-vendoring alone.

    This is the property that makes the vocabulary a vendored contract rather
    than a maintained constant: no `if` in Python names any value.
    """
    from importlib import resources

    newer = tmp_path / "vocabulary.toml"
    newer.write_text(
        "version = 1\n"
        'model_status = ["active", "deprecated", "retired", "preview"]\n'
        'harness_kind = ["cli", "api-baseline", "local", "container"]\n',
        encoding="utf-8",
    )

    class _Files:
        def joinpath(self, _name: str) -> Path:
            return newer

    monkeypatch.setattr(resources, "files", lambda _pkg: _Files())
    maestro.catalog._vocabulary.cache_clear()
    try:
        assert "preview" in model_statuses()
        assert "container" in harness_kinds()
        # And the loader honours it: a status that was invalid a moment ago now
        # validates, with no code change of any kind.
        assert CatalogModel(vendor="acme", status="preview").status == "preview"
    finally:
        maestro.catalog._vocabulary.cache_clear()


def test_unreadable_vocabulary_fails_loud_not_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty set would reject every catalog AND stop flagging every kind."""
    from importlib import resources

    def _boom(_pkg: str) -> object:
        raise ModuleNotFoundError("package data missing")

    monkeypatch.setattr(resources, "files", _boom)
    maestro.catalog._vocabulary.cache_clear()
    try:
        with pytest.raises(maestro.catalog.CatalogVocabularyUnavailable):
            model_statuses()
    finally:
        maestro.catalog._vocabulary.cache_clear()


def test_unknown_status_is_still_rejected_at_validation_time() -> None:
    """Dropping the Literal moved the source of truth, not the strictness."""
    with pytest.raises(ValidationError, match="unknown model status"):
        CatalogModel(vendor="acme", status="preview")


def test_roundtrip_fixture_exercises_every_vocabulary_value() -> None:
    """The set's own guard against a loader whose known-set lost a value."""
    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "catalog-conformance"
        / "v1"
        / "fixtures"
        / "valid"
        / "vocabulary-roundtrip.toml"
    )
    data = tomllib.loads(fixture.read_text(encoding="utf-8"))
    used_statuses = {m.get("status", "active") for m in data["models"].values()}
    used_kinds = {h.get("kind", "") for h in data["harnesses"].values()}
    assert used_statuses == set(model_statuses())
    assert used_kinds == set(harness_kinds())


def test_scalar_enum_in_the_vocabulary_fails_loud_not_as_characters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`model_status = "active"` is valid TOML and a corrupt vocabulary.

    frozenset() over a string yields a set of CHARACTERS — non-empty, so it
    survives every emptiness check, and then rejects every catalog for a reason
    no message explains. Corruption has to fail as corruption.
    """
    from importlib import resources

    broken = tmp_path / "vocabulary.toml"
    broken.write_text(
        'version = 1\nmodel_status = "active"\nharness_kind = ["cli"]\n',
        encoding="utf-8",
    )

    class _Files:
        def joinpath(self, _name: str) -> Path:
            return broken

    monkeypatch.setattr(resources, "files", lambda _pkg: _Files())
    maestro.catalog._vocabulary.cache_clear()
    try:
        with pytest.raises(
            maestro.catalog.CatalogVocabularyUnavailable, match="list of strings"
        ):
            model_statuses()
    finally:
        maestro.catalog._vocabulary.cache_clear()
