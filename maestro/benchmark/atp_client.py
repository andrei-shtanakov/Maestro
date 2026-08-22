"""R-06b M3 — adapt ``atp_sdk.AsyncATPClient`` to Maestro M1 Protocols.

The runtime gap: the M1 ``ATPClientLike`` / ``BenchmarkRun`` /
``BenchmarkTask`` Protocols (frozen in M1 against mocks) do not match
``atp_sdk`` one-for-one:

* SDK ``BenchmarkRun.run_id`` is ``int``; M1 expects ``str``.
* SDK iter yields raw ATPRequest dicts; M1 expects a typed ``BenchmarkTask``.
* SDK ``submit`` takes a dict ATPResponse and returns a score; M1 takes
  plain text and returns ``None``.
* SDK has no ``finalize``; the server auto-finalises on the last submit
  and the aggregate score lands on ``GET /api/v1/runs/{id}/status``.

This module bridges the two with a thin adapter. Auth, HTTP, retry, and
token refresh delegate to ``AsyncATPClient`` (token resolution order:
explicit ``token`` arg → ``ATP_TOKEN`` env → ``~/.atp/config.json``), so
M3 itself owns no auth UX.

ATP scoring note: the server reports the mean of per-submit scores as
``total_score``, and says what that mean *means* in ``score_semantics``
(contract v1). Whether a component breakdown arrives, and whether the number
is a quality signal at all, is read off the payload rather than assumed —
see ``maestro.benchmark.score_contract``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from atp_sdk.client import AsyncATPClient

from maestro.benchmark.score_contract import parse_finalized_score


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from types import TracebackType

    from atp_sdk.benchmark import BenchmarkRun as SDKBenchmarkRun

    from maestro.benchmark.models import FinalizedScore


@dataclass(frozen=True, slots=True)
class _Task:
    """Concrete row that satisfies ``maestro.benchmark.BenchmarkTask``."""

    task_index: int
    prompt: str
    task_type: str | None = None  # R-06b M4: pulled from ATP metadata when present


def _extract_task_index(raw: dict[str, Any]) -> int:
    """Pull ``task_index`` out of an ATPRequest dict.

    The benchmark API stamps ``task_index`` into ``metadata`` when serving
    tasks (see ``atp.dashboard.v2.routes.benchmark_api.next_task``).
    """
    metadata = raw.get("metadata") or {}
    return int(metadata.get("task_index", 0))


def _extract_prompt(raw: dict[str, Any]) -> str:
    """Pull the prompt text out of an ATPRequest dict.

    ATP's ``Task`` carries free-form ``description`` plus structured
    ``input_data``; the description is what an agent actually reads.
    """
    task = raw.get("task") or {}
    return str(task.get("description", ""))


def _extract_task_type(raw: dict[str, Any]) -> str | None:
    """Pull ``task_type`` out of an ATPRequest dict (R-06b M4).

    The benchmark API may stamp ``task_type`` into ``metadata`` for some
    benchmarks (e.g. swe-bench has bugfix/feature/refactor labels);
    others don't. Returns ``None`` when absent.
    """
    metadata = raw.get("metadata") or {}
    value = metadata.get("task_type")
    return str(value) if value is not None else None


def _extract_task_id(raw: dict[str, Any]) -> str:
    """Pull the original ATPRequest task_id (uuid)."""
    return str(raw.get("task_id", ""))


class _RunAdapter:
    """Wrap ``atp_sdk.BenchmarkRun`` to satisfy ``maestro.benchmark.BenchmarkRun``."""

    def __init__(self, sdk_run: SDKBenchmarkRun) -> None:
        self._sdk_run = sdk_run
        # task_index → original ATPRequest task_id, populated as we
        # iterate. submit() reuses it so the ATPResponse references the
        # same task_id the server issued. Falls back to a deterministic
        # synthetic if a caller submits a task it never iterated.
        self._task_ids: dict[int, str] = {}

    @property
    def run_id(self) -> str:
        return str(self._sdk_run.run_id)

    async def tasks(self) -> AsyncIterator[_Task]:
        async for raw in self._sdk_run:
            idx = _extract_task_index(raw)
            self._task_ids[idx] = _extract_task_id(raw)
            yield _Task(
                task_index=idx,
                prompt=_extract_prompt(raw),
                task_type=_extract_task_type(raw),
            )

    async def submit(self, task_index: int, response: str) -> None:
        task_id = self._task_ids.get(
            task_index, f"run-{self._sdk_run.run_id}-task-{task_index}"
        )
        atp_response: dict[str, Any] = {
            "version": "1.0",
            "task_id": task_id,
            "status": "completed" if response else "failed",
        }
        if response:
            atp_response["artifacts"] = [
                {
                    "type": "structured",
                    "name": "response",
                    "data": {"text": response},
                }
            ]
        await self._sdk_run.submit(atp_response, task_index)

    async def finalize(self) -> FinalizedScore:
        status = await self._sdk_run.status()
        return parse_finalized_score(status)


class MaestroATPAdapter:
    """Wrap ``atp_sdk.AsyncATPClient`` to satisfy ``ATPClientLike``.

    Prefer the classmethods (``from_env`` / ``from_token``) over manual
    construction so the underlying ``AsyncATPClient`` is created with
    Maestro-friendly defaults.

    Usage::

        async with MaestroATPAdapter.from_env(
            platform_url="https://atp.example.com"
        ) as client:
            runner = BenchmarkRunner(client, agent)
            result = await runner.run(benchmark_id="swe-mini")
    """

    def __init__(self, client: AsyncATPClient) -> None:
        self._client = client

    @classmethod
    def from_env(
        cls,
        platform_url: str = "http://localhost:8000",
        timeout: float = 30.0,
    ) -> MaestroATPAdapter:
        """Build an adapter using ``AsyncATPClient``'s default token resolution
        (explicit arg → ``ATP_TOKEN`` env → ``~/.atp/config.json``)."""
        return cls(AsyncATPClient(platform_url=platform_url, timeout=timeout))

    @classmethod
    def from_token(
        cls,
        token: str,
        platform_url: str = "http://localhost:8000",
        timeout: float = 30.0,
    ) -> MaestroATPAdapter:
        """Build an adapter with an explicit token (overrides env / saved config)."""
        return cls(
            AsyncATPClient(platform_url=platform_url, token=token, timeout=timeout)
        )

    async def start_run(self, benchmark_id: str, agent_name: str) -> _RunAdapter:
        sdk_run = await self._client.start_run(benchmark_id, agent_name=agent_name)
        return _RunAdapter(sdk_run)

    async def close(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> MaestroATPAdapter:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()
