"""Vendored Arbiter MCP client for Maestro.

VENDORED FROM: arbiter@861534e (repo: arbiter, path: orchestrator/arbiter_client.py + types.py).
TARGET ARBITER VERSION: 0.1.0 (enforced by _handshake against serverInfo.version).

Why vendored rather than imported: R-03 design treats the Arbiter subprocess
as an external service. Pinning the client to a specific commit isolates
Maestro from upstream churn and lets us adapt DTOs to pydantic-native for
internal use.

Do-list:
- Pydantic BaseModel DTOs with ConfigDict(frozen=True), not @dataclass.
- DTOs here are transport-only: suffix with `DTO`. Scheduler-facing models
  (`RouteDecision`, `TaskOutcome` etc.) live in `maestro/models.py`.
- Raise Maestro-native errors: `ArbiterStartupError` (path errors, version
  mismatch) and `ArbiterUnavailable` (connection/protocol/transport).
- Validate arbiter version in `_handshake` against
  `ARBITER_MCP_REQUIRED_VERSION`.

Don't-list:
- Do NOT modify: subprocess lifecycle, reconnect logic, stdio line framing,
  JSON-RPC id sequencing. These are the critical correctness surfaces and
  carry upstream test coverage.
- Do NOT import from `maestro.models` -- this module is pure transport and
  must stay decoupled. Mapping happens in `routing.py`.
- Do NOT use the upstream `ArbiterError` / `ArbiterConnectionError` /
  `ArbiterProtocolError` names -- translate to Maestro-native variants.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from maestro.coordination.arbiter_errors import ArbiterStartupError, ArbiterUnavailable


logger = logging.getLogger(__name__)

ARBITER_VENDOR_COMMIT = "861534e"
ARBITER_MCP_REQUIRED_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# DTOs (transport-only, pydantic-native, frozen)
# ---------------------------------------------------------------------------


class InvariantCheckDTO(BaseModel):
    """Single invariant rule check result."""

    model_config = ConfigDict(frozen=True)

    rule: str
    severity: str
    passed: bool
    detail: str


class RouteDecisionDTO(BaseModel):
    """Result of a route_task call."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    action: str
    chosen_agent: str
    confidence: float
    reasoning: str
    decision_path: list[str]
    invariant_checks: list[InvariantCheckDTO]
    metadata: dict[str, Any] = {}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RouteDecisionDTO:
        """Parse a raw route_task response dict."""
        checks = [
            InvariantCheckDTO(
                rule=c["rule"],
                severity=c["severity"],
                passed=c["passed"],
                detail=c.get("detail", ""),
            )
            for c in data.get("invariant_checks", [])
        ]
        return cls(
            task_id=data["task_id"],
            action=data["action"],
            chosen_agent=data.get("chosen_agent", ""),
            confidence=data.get("confidence", 0.0),
            reasoning=data.get("reasoning", ""),
            decision_path=data.get("decision_path", []),
            invariant_checks=checks,
            metadata=data.get("metadata", {}),
        )


class UpdatedStatsDTO(BaseModel):
    """Agent statistics returned after reporting an outcome."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    total_tasks: int
    success_rate: float
    avg_duration_min: float
    avg_cost_usd: float


class OutcomeResultDTO(BaseModel):
    """Result of a report_outcome call."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    recorded: bool
    updated_stats: UpdatedStatsDTO
    retrain_suggested: bool
    warnings: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutcomeResultDTO:
        """Parse a raw report_outcome response dict."""
        stats_raw = data.get("updated_stats", {})
        stats = UpdatedStatsDTO(
            agent_id=stats_raw.get("agent_id", ""),
            total_tasks=stats_raw.get("total_tasks", 0),
            success_rate=stats_raw.get("success_rate", 0.0),
            avg_duration_min=stats_raw.get("avg_duration_min", 0.0),
            avg_cost_usd=stats_raw.get("avg_cost_usd", 0.0),
        )
        return cls(
            task_id=data["task_id"],
            recorded=data.get("recorded", False),
            updated_stats=stats,
            retrain_suggested=data.get("retrain_suggested", False),
            warnings=data.get("warnings", []),
        )


class AgentCapabilitiesDTO(BaseModel):
    """Capabilities declared by an agent."""

    model_config = ConfigDict(frozen=True)

    languages: list[str]
    task_types: list[str]
    max_concurrent: int
    cost_per_hour: float


class AgentStatusInfoDTO(BaseModel):
    """Status information for a single agent."""

    model_config = ConfigDict(frozen=True)

    id: str
    display_name: str
    state: str
    capabilities: AgentCapabilitiesDTO
    active_tasks: int
    total_completed: int
    success_rate: float
    avg_duration_min: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentStatusInfoDTO:
        """Parse a raw agent status dict."""
        caps_raw = data.get("capabilities", {})
        caps = AgentCapabilitiesDTO(
            languages=caps_raw.get("languages", []),
            task_types=caps_raw.get("task_types", []),
            max_concurrent=caps_raw.get("max_concurrent", 0),
            cost_per_hour=caps_raw.get("cost_per_hour", 0.0),
        )
        return cls(
            id=data["id"],
            display_name=data.get("display_name", ""),
            state=data.get("state", "unknown"),
            capabilities=caps,
            active_tasks=data.get("active_tasks", 0),
            total_completed=data.get("total_completed", 0),
            success_rate=data.get("success_rate", 0.0),
            avg_duration_min=data.get("avg_duration_min", 0.0),
        )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class ArbiterClientConfig(BaseModel):
    """Configuration for ArbiterClient."""

    model_config = ConfigDict(frozen=True)

    binary_path: str | Path = "target/release/arbiter-mcp"
    tree_path: str | Path = "models/agent_policy_tree.json"
    config_dir: str | Path = "config/"
    db_path: str | Path | None = None
    log_level: str = "warn"
    reconnect_delay: float = 1.0
    max_reconnect_attempts: int = 3


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ArbiterClient:
    """MCP client that manages an Arbiter subprocess.

    Communicates over stdin/stdout using JSON-RPC 2.0. One JSON object
    per line. Supports automatic reconnection on broken pipe.

    Usage::

        client = ArbiterClient(ArbiterClientConfig(binary_path="..."))
        await client.start()
        decision = await client.route_task(
            "task-1",
            {
                "type": "bugfix",
                "language": "python",
                "complexity": "simple",
                "priority": "normal",
            },
        )
        await client.stop()
    """

    def __init__(self, config: ArbiterClientConfig | None = None) -> None:
        self._config = config or ArbiterClientConfig()
        self._process: asyncio.subprocess.Process | None = None
        self._request_id: int = 0
        self._started: bool = False
        self._db_path: Path | None = None
        self._temp_db: tempfile.NamedTemporaryFile | None = None  # type: ignore[type-arg]

    @property
    def is_running(self) -> bool:
        """Check if the subprocess is currently running."""
        return self._process is not None and self._process.returncode is None

    async def start(self) -> dict[str, Any]:
        """Start the Arbiter subprocess and perform MCP handshake.

        Returns:
            Server capabilities from the initialize response.

        Raises:
            ArbiterStartupError: If the subprocess fails to start or version mismatches.
            ArbiterUnavailable: If the handshake fails due to connection issues.
        """
        if self._started and self.is_running:
            raise ArbiterStartupError("Client already started")

        await self._spawn_process()
        result = await self._handshake()
        self._started = True
        return result

    async def stop(self) -> None:
        """Gracefully shut down the Arbiter subprocess.

        Closes stdin to signal EOF, waits for the process to exit.
        """
        if self._process is None:
            return

        proc = self._process
        self._process = None
        self._started = False

        try:
            if proc.stdin is not None:
                proc.stdin.close()
                await proc.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            logger.warning("Arbiter process did not exit, killing")
            proc.kill()
            await proc.wait()
        finally:
            if self._temp_db is not None:
                with contextlib.suppress(OSError):
                    self._temp_db.close()
                self._temp_db = None

    async def route_task(
        self,
        task_id: str,
        task: dict[str, Any],
        constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Route a coding task to the best agent.

        Args:
            task_id: Unique task identifier.
            task: Task description with type, language, complexity, priority.
            constraints: Optional routing constraints.

        Returns:
            Decision dict with chosen_agent, confidence, invariant_checks.

        Raises:
            ArbiterUnavailable: On broken pipe (after retry) or protocol error.
        """
        arguments: dict[str, Any] = {"task_id": task_id, "task": task}
        if constraints is not None:
            arguments["constraints"] = constraints
        return await self._call_tool("route_task", arguments)

    async def report_outcome(
        self,
        task_id: str,
        agent_id: str,
        status: str,
        tokens_used: int | None = None,
        cost_usd: float | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Report the outcome of a task execution.

        Args:
            task_id: Task identifier from route_task.
            agent_id: Agent that executed the task.
            status: One of success, failure, timeout, cancelled.
            tokens_used: Optional token count for the task.
            cost_usd: Optional cost in USD for the task.
            **kwargs: Optional additional fields (duration_min, etc.).

        Returns:
            Outcome result with updated_stats.
        """
        arguments: dict[str, Any] = {
            "task_id": task_id,
            "agent_id": agent_id,
            "status": status,
            **kwargs,
        }
        if tokens_used is not None:
            arguments["tokens_used"] = tokens_used
        if cost_usd is not None:
            arguments["cost_usd"] = cost_usd
        return await self._call_tool("report_outcome", arguments)

    async def get_agent_status(
        self,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Query agent capabilities, load, and performance.

        Args:
            agent_id: Specific agent to query, or None for all.

        Returns:
            Status dict with agents list.
        """
        arguments: dict[str, Any] = {}
        if agent_id is not None:
            arguments["agent_id"] = agent_id
        return await self._call_tool("get_agent_status", arguments)

    # ------------------------------------------------------------------
    # Typed convenience methods
    # ------------------------------------------------------------------

    async def route_task_typed(
        self,
        task_id: str,
        task: dict[str, Any],
        constraints: dict[str, Any] | None = None,
    ) -> RouteDecisionDTO:
        """Route a task and return a typed RouteDecisionDTO.

        Same as route_task but parses the response into a pydantic model.
        """
        raw = await self.route_task(task_id, task, constraints)
        return RouteDecisionDTO.from_dict(raw)

    async def report_outcome_typed(
        self,
        task_id: str,
        agent_id: str,
        status: str,
        tokens_used: int | None = None,
        cost_usd: float | None = None,
        **kwargs: Any,
    ) -> OutcomeResultDTO:
        """Report an outcome and return a typed OutcomeResultDTO.

        Same as report_outcome but parses the response into a pydantic model.
        """
        raw = await self.report_outcome(
            task_id,
            agent_id,
            status,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
            **kwargs,
        )
        return OutcomeResultDTO.from_dict(raw)

    async def get_agent_status_typed(
        self,
        agent_id: str | None = None,
    ) -> list[AgentStatusInfoDTO]:
        """Query agent status and return typed AgentStatusInfoDTO objects.

        Same as get_agent_status but parses the response into pydantic models.
        """
        raw = await self.get_agent_status(agent_id)
        agents = raw.get("agents", [])
        return [AgentStatusInfoDTO.from_dict(a) for a in agents]

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    async def _spawn_process(self) -> None:
        """Spawn the Arbiter subprocess with pipes."""
        binary = str(self._config.binary_path)
        if not Path(binary).is_absolute():
            binary = str(Path.cwd() / binary)

        if not Path(binary).exists():  # noqa: ASYNC240
            raise ArbiterStartupError(
                f"Binary not found: {binary}",
                path=binary,
            )

        # Use configured db_path or create a temp file
        if self._config.db_path is not None:
            db_path = str(self._config.db_path)
        else:
            self._temp_db = tempfile.NamedTemporaryFile(  # noqa: SIM115
                suffix=".db", delete=False
            )
            db_path = self._temp_db.name
        self._db_path = Path(db_path)

        cmd = [
            binary,
            "--tree",
            str(self._config.tree_path),
            "--config",
            str(self._config.config_dir),
            "--db",
            db_path,
            "--log-level",
            self._config.log_level,
        ]

        logger.debug("Spawning: %s", " ".join(cmd))

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError) as e:
            raise ArbiterUnavailable(f"Failed to start Arbiter: {e}", cause=e) from e

    async def _handshake(self) -> dict[str, Any]:
        """Perform MCP initialize + initialized handshake with version check."""
        result = await self._send_request("initialize", {})
        server_info = result.get("serverInfo", {}) or {}
        version = server_info.get("version", "")
        if version != ARBITER_MCP_REQUIRED_VERSION:
            raise ArbiterStartupError(
                f"arbiter version mismatch: expected "
                f"{ARBITER_MCP_REQUIRED_VERSION!r}, got {version!r}. "
                f"Re-vendor client or update ARBITER_MCP_REQUIRED_VERSION."
            )
        await self._send_notification("notifications/initialized")
        return result

    async def _call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Call an MCP tool with automatic reconnection on broken pipe.

        Returns the parsed tool result (inner JSON from content[0].text).
        """
        try:
            return await self._call_tool_once(name, arguments)
        except ArbiterUnavailable:
            logger.warning(
                "Broken pipe, reconnecting in %.1fs",
                self._config.reconnect_delay,
            )
            await self._reconnect()
            return await self._call_tool_once(name, arguments)

    async def _call_tool_once(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Single attempt to call an MCP tool."""
        raw = await self._send_request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )

        # Extract inner JSON from MCP content wrapper
        if "content" in raw and isinstance(raw["content"], list):
            text = raw["content"][0].get("text", "{}")
            return json.loads(text)  # type: ignore[no-any-return]
        return raw

    async def _send_request(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the response."""
        self._request_id += 1
        msg = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        return await self._send_and_receive(msg)

    async def _send_notification(self, method: str) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg = {
            "jsonrpc": "2.0",
            "method": method,
        }
        await self._write_message(msg)

    async def _send_and_receive(
        self,
        msg: dict[str, Any],
    ) -> dict[str, Any]:
        """Write a message and read the response."""
        await self._write_message(msg)
        response = await self._read_response()

        if "error" in response and response["error"] is not None:
            err = response["error"]
            raise ArbiterUnavailable(
                f"protocol error: JSON-RPC error {err.get('code', -32000)}: "
                f"{err.get('message', 'Unknown error')}"
            )

        return response.get("result", {})  # type: ignore[no-any-return]

    async def _write_message(self, msg: dict[str, Any]) -> None:
        """Write a JSON message as a single line to the subprocess stdin."""
        if self._process is None or self._process.stdin is None:
            raise ArbiterUnavailable("Not connected")

        line = json.dumps(msg, separators=(",", ":")) + "\n"
        try:
            self._process.stdin.write(line.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            raise ArbiterUnavailable(f"Write failed: {e}", cause=e) from e

    async def _read_response(self) -> dict[str, Any]:
        """Read a single JSON-RPC response line from stdout."""
        if self._process is None or self._process.stdout is None:
            raise ArbiterUnavailable("Not connected")

        try:
            line = await asyncio.wait_for(
                self._process.stdout.readline(),
                timeout=30.0,
            )
        except TimeoutError as e:
            raise ArbiterUnavailable("Read timeout", cause=e) from e

        if not line:
            raise ArbiterUnavailable("Process exited unexpectedly")

        try:
            return json.loads(line.decode())  # type: ignore[no-any-return]
        except json.JSONDecodeError as e:
            raise ArbiterUnavailable(
                f"protocol error: Invalid JSON response: {e}", cause=e
            ) from e

    async def _reconnect(self) -> None:
        """Reconnect to the Arbiter subprocess after a broken pipe."""
        attempts = 0
        while attempts < self._config.max_reconnect_attempts:
            attempts += 1
            await asyncio.sleep(self._config.reconnect_delay)
            logger.info(
                "Reconnect attempt %d/%d",
                attempts,
                self._config.max_reconnect_attempts,
            )
            try:
                # Kill old process if still alive
                if self._process is not None:
                    try:
                        self._process.kill()
                        await self._process.wait()
                    except (ProcessLookupError, OSError):
                        pass
                    self._process = None

                await self._spawn_process()
                await self._handshake()
                logger.info("Reconnected successfully")
                return
            except (ArbiterUnavailable, ArbiterStartupError) as e:
                logger.warning("Reconnect attempt %d failed: %s", attempts, e)

        raise ArbiterUnavailable(
            f"Failed to reconnect after {self._config.max_reconnect_attempts} attempts"
        )
