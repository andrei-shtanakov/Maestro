"""Tests for resolve_verifier_model — isolated verifier-model resolution.

Precedence is deliberately narrow and ISOLATED from the main routing path:
    verifier.model -> MAESTRO_VERIFIER_MODEL -> fail loud

It must NEVER read MAESTRO_CLAUDE_MODEL and NEVER fall back to a catalog
default (`Catalog.default_model_for_harness`) — either could silently pick an
expensive main-harness model for a job that is supposed to be a cheap judge.
"""

import pytest

from maestro.catalog import Catalog, CatalogModel
from maestro.models import VerifierConfig
from maestro.verifier.config import VerifierModelError, resolve_verifier_model


@pytest.fixture
def fake_catalog() -> Catalog:
    """One healthy (active), one deprecated, one retired model. No agents/
    routable entries at all — proves resolve_verifier_model never touches
    `default_model_for_harness`.
    """
    return Catalog(
        models={
            "claude-haiku-4-5": CatalogModel(vendor="anthropic", status="active"),
            "claude-haiku-4-0": CatalogModel(vendor="anthropic", status="deprecated"),
            "claude-haiku-3-0": CatalogModel(vendor="anthropic", status="retired"),
        },
        agents=[],
    )


def test_backend_must_be_local() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        VerifierConfig(
            runner="claude",
            model="claude-haiku-4-5",
            backend="docker",  # type: ignore[bad-argument-type]
        )


def test_precedence_config_wins(monkeypatch, fake_catalog) -> None:
    monkeypatch.setenv("MAESTRO_VERIFIER_MODEL", "env-model")
    cfg = VerifierConfig(runner="claude", model="claude-haiku-4-5")
    assert resolve_verifier_model(cfg, fake_catalog) == "claude-haiku-4-5"


def test_env_fallback(monkeypatch, fake_catalog) -> None:
    monkeypatch.setenv("MAESTRO_VERIFIER_MODEL", "claude-haiku-4-5")
    cfg = VerifierConfig(runner="claude", model=None)
    assert resolve_verifier_model(cfg, fake_catalog) == "claude-haiku-4-5"


def test_never_uses_claude_model_env(monkeypatch, fake_catalog) -> None:
    monkeypatch.setenv("MAESTRO_CLAUDE_MODEL", "expensive-main")
    monkeypatch.delenv("MAESTRO_VERIFIER_MODEL", raising=False)
    cfg = VerifierConfig(runner="claude", model=None)
    with pytest.raises(VerifierModelError):
        resolve_verifier_model(cfg, fake_catalog)


def test_no_model_and_no_env_fails_loud(monkeypatch, fake_catalog) -> None:
    monkeypatch.delenv("MAESTRO_VERIFIER_MODEL", raising=False)
    monkeypatch.delenv("MAESTRO_CLAUDE_MODEL", raising=False)
    cfg = VerifierConfig(runner="claude", model=None)
    with pytest.raises(VerifierModelError):
        resolve_verifier_model(cfg, fake_catalog)


def test_unknown_or_retired_model_errors(fake_catalog) -> None:
    cfg = VerifierConfig(runner="claude", model="ghost-model")
    with pytest.raises(VerifierModelError):
        resolve_verifier_model(cfg, fake_catalog)


def test_retired_model_errors(fake_catalog) -> None:
    cfg = VerifierConfig(runner="claude", model="claude-haiku-3-0")
    with pytest.raises(VerifierModelError):
        resolve_verifier_model(cfg, fake_catalog)


def test_deprecated_model_warns_not_raises(fake_catalog) -> None:
    cfg = VerifierConfig(runner="claude", model="claude-haiku-4-0")
    # Must NOT raise — deprecated is a warning, not a failure.
    assert resolve_verifier_model(cfg, fake_catalog) == "claude-haiku-4-0"


def test_healthy_model_resolves_cleanly(fake_catalog) -> None:
    cfg = VerifierConfig(runner="claude", model="claude-haiku-4-5")
    assert resolve_verifier_model(cfg, fake_catalog) == "claude-haiku-4-5"
