"""R-06b M2 — adapter from Maestro ``AgentSpawner`` to ``AgentResponder``.

Lets ``BenchmarkRunner`` drive the existing CLI subprocess machinery
(claude_code, codex_cli, aider) without touching the ``AgentSpawner`` ABC.

Each ``respond(prompt)`` call:

1. Synthesises a minimal ``Task`` (no scope, no validation, no DB) so the
   spawner's task-oriented contract is satisfied.
2. Spawns the subprocess via the wrapped spawner.
3. Awaits ``process.wait()`` on a worker thread, bounded by
   ``timeout_seconds``. On timeout the process is killed and the result
   is an empty response with ``error="timeout"``.
4. Parses tokens and cost from the captured log via
   ``maestro.cost_tracker``.
5. Returns the full log content as ``response.text`` — M2 punt; M3 will
   refine per-benchmark response extraction once the live ATP API shape
   is known.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from maestro.benchmark.models import AgentResponse
from maestro.cost_tracker import calculate_cost, parse_log
from maestro.models import AgentType, Task, TaskStatus


if TYPE_CHECKING:
    from pathlib import Path

    from maestro.spawners.base import AgentSpawner


logger = logging.getLogger(__name__)


class SpawnerResponder:
    """Adapts a ``maestro.spawners.AgentSpawner`` to the
    ``AgentResponder`` Protocol from ``maestro.benchmark.runner``."""

    def __init__(
        self,
        spawner: AgentSpawner,
        workdir: Path,
        log_dir: Path,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._spawner = spawner
        self._workdir = workdir
        self._log_dir = log_dir
        self._timeout = timeout_seconds
        self._counter = 0

    @property
    def agent_id(self) -> str:
        return self._spawner.agent_type

    async def respond(self, prompt: str) -> AgentResponse:
        self._counter += 1
        task_id = f"benchmark-{self._counter}"
        log_file = self._log_dir / f"{task_id}.log"

        try:
            agent_enum = AgentType(self._spawner.agent_type)
        except ValueError:
            return AgentResponse(
                text="",
                error=f"unknown agent_type: {self._spawner.agent_type!r}",
            )

        task = Task(
            id=task_id,
            title=f"benchmark task {self._counter}",
            prompt=prompt,
            workdir=str(self._workdir),
            agent_type=agent_enum,
            status=TaskStatus.RUNNING,
        )

        process = self._spawner.spawn(
            task=task,
            context="",
            workdir=self._workdir,
            log_file=log_file,
        )

        try:
            await asyncio.wait_for(
                asyncio.to_thread(process.wait), timeout=self._timeout
            )
        except TimeoutError:
            logger.warning(
                "benchmark task %s exceeded timeout of %.1fs — killing",
                task_id,
                self._timeout,
            )
            process.kill()
            await asyncio.to_thread(process.wait)
            return AgentResponse(text="", error="timeout")

        log_content = log_file.read_text() if log_file.exists() else ""
        usage = parse_log(log_content, agent_enum)
        total_tokens = usage.input_tokens + usage.output_tokens
        cost = calculate_cost(usage, agent_enum)

        if process.returncode != 0:
            return AgentResponse(
                text="",
                tokens_used=total_tokens or None,
                cost_usd=cost or None,
                error=f"exit code {process.returncode}",
            )

        return AgentResponse(
            text=log_content,
            tokens_used=total_tokens or None,
            cost_usd=cost or None,
        )
