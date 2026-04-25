"""R-05 — real-subprocess e2e tests against a built `arbiter-mcp` binary.

Distinct from `test_scheduler_arbiter_integration.py` which uses
`FakeArbiterClient` for fast unit-level coverage. This module verifies
the actual JSON-RPC contract end-to-end against the real Rust subprocess.

Skipped automatically when the binary or bundled config aren't present —
this keeps the suite green on dev machines without a Rust toolchain.
Set `MAESTRO_ARBITER_BIN` to point at a binary in a non-default location.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from maestro.coordination.arbiter_client import ArbiterClient, ArbiterClientConfig
from maestro.coordination.routing import _extract_decision_id


# ---------------------------------------------------------------------------
# Locating the arbiter artifacts (sibling-repo layout)
# ---------------------------------------------------------------------------


def _arbiter_repo_root() -> Path:
    """Locate the `arbiter` sibling repo. Maestro lives at
    `<root>/Maestro/` and arbiter at `<root>/arbiter/` in this monorepo."""
    return Path(__file__).resolve().parent.parent.parent / "arbiter"


def _arbiter_binary() -> Path:
    env = os.environ.get("MAESTRO_ARBITER_BIN")
    if env:
        return Path(env).resolve()
    return _arbiter_repo_root() / "target" / "release" / "arbiter-mcp"


def _arbiter_artifacts_present() -> bool:
    root = _arbiter_repo_root()
    binary = _arbiter_binary()
    tree = root / "models" / "agent_policy_tree.json"
    cfg_dir = root / "config"
    return binary.exists() and tree.exists() and cfg_dir.exists()


real_arbiter_only = pytest.mark.skipif(
    not _arbiter_artifacts_present(),
    reason=(
        "real arbiter binary or config missing; build with "
        "`cargo build --release --bin arbiter-mcp` in the arbiter repo. "
        "Override binary location with MAESTRO_ARBITER_BIN."
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def real_arbiter_client(tmp_path):
    """Spawn an arbiter-mcp subprocess pointing at a per-test temp DB.

    Yields a started, handshaken ArbiterClient ready for tool calls.
    Tears the subprocess down on exit even if the test raises.
    """
    root = _arbiter_repo_root()
    cfg = ArbiterClientConfig(
        binary_path=_arbiter_binary(),
        tree_path=root / "models" / "agent_policy_tree.json",
        config_dir=root / "config",
        db_path=tmp_path / "arbiter-test.db",
        log_level="warn",
    )
    client = ArbiterClient(cfg)
    await client.start()
    try:
        yield client
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@real_arbiter_only
@pytest.mark.anyio
async def test_route_task_response_contains_decision_id_i64(real_arbiter_client):
    """arbiter#9 fix: real arbiter must surface SQLite rowid as i64
    in `metadata.decision_id` so Maestro can persist it for stale-guard."""
    raw = await real_arbiter_client.route_task(
        "r05-task-1",
        {
            "type": "bugfix",
            "language": "python",
            "complexity": "simple",
            "priority": "normal",
        },
    )
    metadata = raw.get("metadata") or {}
    assert "decision_id" in metadata, (
        f"metadata.decision_id missing — arbiter#9 regression. metadata={metadata!r}"
    )
    decision_id = metadata["decision_id"]
    assert isinstance(decision_id, int), (
        f"decision_id must be a JSON integer (SQLite rowid); "
        f"got {type(decision_id).__name__}: {decision_id!r}"
    )
    assert decision_id >= 1, "SQLite rowids start at 1"


@real_arbiter_only
@pytest.mark.anyio
async def test_maestro_extractor_coerces_real_decision_id_to_str(real_arbiter_client):
    """End-to-end contract: real arbiter int → Maestro `_extract_decision_id`
    → str ready for `arbiter_decision_id TEXT` column."""
    raw = await real_arbiter_client.route_task(
        "r05-task-2",
        {
            "type": "feature",
            "language": "rust",
            "complexity": "complex",
            "priority": "high",
        },
    )
    coerced = _extract_decision_id(raw)
    assert coerced is not None
    assert isinstance(coerced, str)
    # Round-trip: must parse back to the same int the metadata carried.
    assert int(coerced) == raw["metadata"]["decision_id"]


@real_arbiter_only
@pytest.mark.anyio
async def test_route_then_report_outcome_round_trip(real_arbiter_client):
    """The full happy-path loop: route_task surfaces decision_id, Maestro
    passes it back to report_outcome, arbiter records the outcome linked
    to the original decision row."""
    route_resp = await real_arbiter_client.route_task(
        "r05-task-3",
        {
            "type": "bugfix",
            "language": "python",
            "complexity": "simple",
            "priority": "normal",
        },
    )
    assert route_resp["action"] in {"assign", "fallback"}
    chosen_agent = route_resp["chosen_agent"]
    assert chosen_agent

    decision_id_int = route_resp["metadata"]["decision_id"]
    assert isinstance(decision_id_int, int)

    outcome_resp = await real_arbiter_client.report_outcome(
        task_id="r05-task-3",
        agent_id=chosen_agent,
        status="success",
        tokens_used=1234,
        cost_usd=0.01,
        duration_min=1.5,
        exit_code=0,
        tests_passed=True,
        validation_passed=True,
        decision_id=decision_id_int,
    )
    assert outcome_resp.get("recorded") is True, (
        f"outcome should be recorded against decision_id={decision_id_int}; "
        f"got {outcome_resp!r}"
    )


@real_arbiter_only
@pytest.mark.anyio
async def test_concurrent_routes_have_distinct_decision_ids(real_arbiter_client):
    """Each route_task call must mint a fresh SQLite rowid — otherwise
    Maestro's stale-guard collapses across retries."""
    ids: list[int] = []
    for i in range(3):
        raw = await real_arbiter_client.route_task(
            f"r05-distinct-{i}",
            {
                "type": "bugfix",
                "language": "python",
                "complexity": "simple",
                "priority": "normal",
            },
        )
        ids.append(raw["metadata"]["decision_id"])
    assert len(set(ids)) == len(ids), (
        f"decision_ids must be unique per route_task call; got {ids}"
    )
