"""R-06b — Agent benchmarking via ATP.

M1 thin slice: data models + async runner driven by Protocols. M2 added
``SpawnerResponder`` (real Maestro spawners as the agent under test). M3
added ``MaestroATPAdapter`` (live ATP HTTP via ``atp-platform-sdk``).
"""

from maestro.benchmark.arbiter_report import (
    ReportBenchmarkPayload,
    WireTaskResult,
    report_benchmark_to_arbiter,
)
from maestro.benchmark.atp_client import MaestroATPAdapter
from maestro.benchmark.models import (
    AgentResponse,
    BenchmarkResult,
    BenchmarkTaskResult,
    FinalizedScore,
    ScoreSemantics,
)
from maestro.benchmark.runner import (
    AgentResponder,
    ATPClientLike,
    BenchmarkRun,
    BenchmarkRunner,
    BenchmarkTask,
)
from maestro.benchmark.score_contract import (
    PublicationDecision,
    ScoreContractError,
    parse_finalized_score,
    publication_decision,
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
    "FinalizedScore",
    "MaestroATPAdapter",
    "PublicationDecision",
    "ReportBenchmarkPayload",
    "ScoreContractError",
    "ScoreSemantics",
    "SpawnerResponder",
    "WireTaskResult",
    "parse_finalized_score",
    "publication_decision",
    "report_benchmark_to_arbiter",
]
