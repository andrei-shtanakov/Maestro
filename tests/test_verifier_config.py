"""Tests for VerifierConfig (models.py) and its wiring into config.py.

Verifier gate Mode-1 (idea #6), Task 2 of
`.superpowers/sdd/2026-07-25-verifier-gate-mode1.md`.
"""

import pytest
from pydantic import ValidationError

from maestro.config import load_config_from_string
from maestro.models import VerifierConfig


def test_defaults() -> None:
    cfg = VerifierConfig(runner="claude")
    assert cfg.model is None
    assert cfg.timeout_seconds == 120
    assert cfg.max_diff_bytes == 100_000
    assert cfg.backend == "local"


def test_backend_must_be_local() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        VerifierConfig(
            runner="claude",
            model="claude-haiku-4-5",
            backend="docker",  # type: ignore[bad-argument-type]
        )


def test_model_none_is_allowed_at_parse_time() -> None:
    """Resolution (not parsing) enforces that a model is available."""
    cfg = VerifierConfig(runner="claude", model=None)
    assert cfg.model is None


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        VerifierConfig(runner="claude", bogus="nope")  # type: ignore[unexpected-keyword]


def test_absent_verifier_block_is_none() -> None:
    """Mirrors `execution:` — an optional block absent from YAML stays None."""
    content = """
project: demo
repo: /tmp/demo
tasks:
  - id: t1
    title: Say hello
    prompt: hello
"""
    cfg = load_config_from_string(content)
    assert cfg.verifier is None


def test_verifier_block_parses_from_yaml() -> None:
    content = """
project: demo
repo: /tmp/demo
verifier:
  runner: claude
  model: claude-haiku-4-5
  timeout_seconds: 60
tasks:
  - id: t1
    title: Say hello
    prompt: hello
"""
    cfg = load_config_from_string(content)
    assert cfg.verifier is not None
    assert cfg.verifier.runner == "claude"
    assert cfg.verifier.model == "claude-haiku-4-5"
    assert cfg.verifier.timeout_seconds == 60
    assert cfg.verifier.backend == "local"


def test_verifier_block_rejects_non_local_backend_from_yaml() -> None:
    content = """
project: demo
repo: /tmp/demo
verifier:
  runner: claude
  backend: docker
tasks:
  - id: t1
    title: Say hello
    prompt: hello
"""
    with pytest.raises(Exception):  # noqa: B017 - maestro.config.ConfigError
        load_config_from_string(content)
