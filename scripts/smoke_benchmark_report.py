#!/usr/bin/env python3
"""R-06b M4 smoke — happy-path end-to-end with a real arbiter-mcp.

Run as the final step of the arbiter-e2e CI job (after pytest).
Exit 0 on green smoke, 1 + diagnostic on failure.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

from maestro.benchmark import (
    BenchmarkResult,
    BenchmarkTaskResult,
    ScoreSemantics,
    report_benchmark_to_arbiter,
)
from maestro.coordination.arbiter_client import ArbiterClient, ArbiterClientConfig


async def _run() -> int:
    binary = os.environ.get("MAESTRO_ARBITER_BIN")
    if not binary:
        print(
            "smoke FAIL: MAESTRO_ARBITER_BIN missing or not found (None)",
            file=sys.stderr,
        )
        return 1

    binary_path = Path(binary)
    if not binary_path.exists():  # noqa: ASYNC240
        print(
            f"smoke FAIL: MAESTRO_ARBITER_BIN missing or not found ({binary!r})",
            file=sys.stderr,
        )
        return 1

    arbiter_repo = binary_path.parent.parent.parent  # Release binary to repo root
    config_dir = arbiter_repo / "config"
    tree_path = arbiter_repo / "models" / "agent_policy_tree.json"

    if not config_dir.is_dir():
        print(
            f"smoke FAIL: arbiter config_dir not found ({config_dir})",
            file=sys.stderr,
        )
        return 1

    if not tree_path.is_file():
        print(
            f"smoke FAIL: arbiter tree_path not found ({tree_path})",
            file=sys.stderr,
        )
        return 1

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "arbiter.db"
        config = ArbiterClientConfig(
            binary_path=binary,
            config_dir=str(config_dir),
            tree_path=str(tree_path),
            db_path=str(db_path),
        )
        client = ArbiterClient(config)
        await client.start()
        try:
            run_id = f"smoke-{uuid.uuid4()}"
            result = BenchmarkResult(
                run_id=run_id,
                benchmark_id="smoke-bench",
                agent_id="claude_code",
                score=0.99,
                score_components={"smoke": 1.0},
                semantics=ScoreSemantics(
                    kind="aggregated_evaluation",
                    quality_signal=True,
                    raw={"schema_version": 1},
                ),
                per_task=[
                    BenchmarkTaskResult(
                        task_index=0,
                        prompt="p",
                        response="r",
                        duration_seconds=0.1,
                        task_type="smoke",
                        score=1.0,
                    )
                ],
                duration_seconds=0.5,
            )
            returned = await report_benchmark_to_arbiter(result, client)
        finally:
            await client.stop()

        if returned.report_status != "ok":
            print(
                f"smoke FAIL: report_status={returned.report_status} "
                f"error={returned.report_error}",
                file=sys.stderr,
            )
            return 1

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT benchmark_id, agent_id, score FROM benchmark_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            print(
                f"smoke FAIL: no row in benchmark_runs for run_id={run_id}",
                file=sys.stderr,
            )
            return 1

        print(
            f"smoke OK: run_id={run_id} benchmark_id={row[0]} "
            f"agent={row[1]} score={row[2]}"
        )
        return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
