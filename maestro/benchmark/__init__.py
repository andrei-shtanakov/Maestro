"""R-06b — Agent benchmarking via ATP.

M1 thin slice: data models + async runner driven by Protocols. Mock-only
in tests; real ATP client and spawner adapters land in M2/M3.
"""

from maestro.benchmark.models import (
    AgentResponse,
    BenchmarkResult,
    BenchmarkTaskResult,
)
from maestro.benchmark.runner import (
    AgentResponder,
    ATPClientLike,
    BenchmarkRun,
    BenchmarkRunner,
    BenchmarkTask,
)
from maestro.benchmark.spawner_responder import SpawnerResponder


__all__ = [
    "ATPClientLike",
    "AgentResponder",
    "AgentResponse",
    "BenchmarkResult",
    "BenchmarkRun",
    "BenchmarkRunner",
    "BenchmarkTask",
    "BenchmarkTaskResult",
    "SpawnerResponder",
]
