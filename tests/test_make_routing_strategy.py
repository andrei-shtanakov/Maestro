"""Tests for the make_routing_strategy factory."""

from __future__ import annotations

import pytest

from maestro.coordination.arbiter_errors import ArbiterStartupError
from maestro.coordination.routing import (
    ArbiterRouting,  # noqa: F401  (imported for public-API surface check)
    StaticRouting,
    make_routing_strategy,
)
from maestro.models import ArbiterConfig


@pytest.mark.anyio
async def test_none_config_returns_static() -> None:
    r = await make_routing_strategy(None)
    assert isinstance(r, StaticRouting)


@pytest.mark.anyio
async def test_disabled_returns_static() -> None:
    cfg = ArbiterConfig(enabled=False)
    r = await make_routing_strategy(cfg)
    assert isinstance(r, StaticRouting)


@pytest.mark.anyio
async def test_enabled_missing_binary_fails_fast_when_not_optional() -> None:
    cfg = ArbiterConfig(
        enabled=True,
        binary_path="/does/not/exist",
        config_dir="/tmp",
        tree_path="/tmp/t",
    )
    with pytest.raises(ArbiterStartupError):
        await make_routing_strategy(cfg)


@pytest.mark.anyio
async def test_enabled_missing_binary_falls_back_when_optional() -> None:
    cfg = ArbiterConfig(
        enabled=True,
        optional=True,
        binary_path="/does/not/exist",
        config_dir="/tmp",
        tree_path="/tmp/t",
    )
    r = await make_routing_strategy(cfg)
    assert isinstance(r, StaticRouting)
