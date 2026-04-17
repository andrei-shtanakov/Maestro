"""Tests for maestro.coordination.arbiter_errors."""

import pytest

from maestro.coordination.arbiter_errors import (
    ArbiterError,
    ArbiterStartupError,
    ArbiterUnavailable,
)


def test_hierarchy() -> None:
    """Both specific errors inherit from ArbiterError."""
    assert issubclass(ArbiterStartupError, ArbiterError)
    assert issubclass(ArbiterUnavailable, ArbiterError)


def test_startup_error_carries_path_and_reason() -> None:
    err = ArbiterStartupError("binary missing", path="/nope")
    assert err.path == "/nope"
    assert "binary missing" in str(err)


def test_unavailable_carries_cause() -> None:
    original = BrokenPipeError("pipe closed")
    err = ArbiterUnavailable("arbiter subprocess died", cause=original)
    assert err.cause is original
    assert "arbiter subprocess died" in str(err)


def test_errors_can_be_raised_and_caught() -> None:
    with pytest.raises(ArbiterError):
        raise ArbiterStartupError("x")
    with pytest.raises(ArbiterError):
        raise ArbiterUnavailable("y")
