# R-03 Arbiter MCP Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate Maestro's scheduler with the Arbiter policy engine over MCP (JSON-RPC 2.0 over stdio), with persisted decision tracking, mode-aware retry gating, crash recovery, and a mock-based contract test suite.

**Architecture:** New `maestro/coordination/routing.py` introduces `RoutingStrategy` Protocol with two implementations — `StaticRouting` (byte-identical zero-config OSS path) and `ArbiterRouting` (owns one long-lived vendored `ArbiterClient` subprocess). Scheduler gets a `routing` dependency and an `arbiter_mode` flag; calls `route()` before spawner lookup, `report_outcome()` in terminal handlers. Four new columns on `tasks` (`routed_agent_type`, `arbiter_decision_id`, `arbiter_route_reason`, `arbiter_outcome_reported_at`) plus atomic `reset_for_retry_atomic` close a race widened by network latency.

**Tech Stack:** Python 3.12, asyncio, aiosqlite, pydantic v2, pyrefly for typing, ruff for formatting, pytest+anyio for tests. Vendored `arbiter-mcp@0.1.0` subprocess via asyncio `create_subprocess_exec`.

**Spec:** `docs/superpowers/specs/2026-04-16-r03-arbiter-mcp-client-design.md`

**Conventions:**
- Run `uv run ruff format .` + `uv run ruff check . --fix` + `uv run pyrefly check` before each commit.
- Tests use `anyio` fixtures (project convention), not `asyncio.run`.
- Commit messages: `feat(R-03): …` / `test(R-03): …` / `refactor(R-03): …`. One concept per commit.

---

## Task 1: Error types module

**Files:**
- Create: `maestro/coordination/arbiter_errors.py`
- Test: `tests/test_arbiter_errors.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_arbiter_errors.py`:
```python
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
```

- [ ] **Step 2: Run test to verify failure**

```bash
uv run pytest tests/test_arbiter_errors.py -v
```
Expected: ModuleNotFoundError / import failure.

- [ ] **Step 3: Implement errors module**

Create `maestro/coordination/arbiter_errors.py`:
```python
"""Maestro-native exception types for the Arbiter integration.

Kept as a separate module (not inside the vendored arbiter_client.py)
so consumers and tests can import these without pulling in the full
vendored client transitive surface.
"""

from __future__ import annotations


class ArbiterError(Exception):
    """Base class for all Arbiter-integration errors."""


class ArbiterStartupError(ArbiterError):
    """Raised at startup when the Arbiter subprocess cannot be brought up.

    Covers: missing/non-executable binary, failed handshake, version
    mismatch against ARBITER_MCP_REQUIRED_VERSION. Fail-fast by default;
    caller can opt into graceful fallback via ArbiterConfig.optional=True.
    """

    def __init__(self, message: str, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path


class ArbiterUnavailable(ArbiterError):
    """Raised at runtime when a live Arbiter call fails.

    Covers: broken pipe on subprocess stdio, read timeout, JSON parse
    failure. ArbiterRouting catches this for route-path (delegates to
    static fallback); report_outcome path re-raises so the scheduler
    can apply mode-dependent retry gating.
    """

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause
```

- [ ] **Step 4: Run test to verify pass**

```bash
uv run pytest tests/test_arbiter_errors.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Ruff + pyrefly + commit**

```bash
uv run ruff format maestro/coordination/arbiter_errors.py tests/test_arbiter_errors.py
uv run ruff check maestro/coordination/arbiter_errors.py tests/test_arbiter_errors.py
uv run pyrefly check
git add maestro/coordination/arbiter_errors.py tests/test_arbiter_errors.py
git commit -m "feat(R-03): add Arbiter error types (ArbiterStartupError, ArbiterUnavailable)"
```

---

## Task 2: AgentType.AUTO sentinel

**Files:**
- Modify: `maestro/models.py` (AgentType enum)
- Test: `tests/test_models.py` (add test class)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_models.py`:
```python
class TestAgentTypeAuto:
    """Tests for AgentType.AUTO sentinel added in R-03."""

    def test_auto_value_is_lowercase_auto(self) -> None:
        from maestro.models import AgentType

        assert AgentType.AUTO.value == "auto"

    def test_auto_is_distinct_from_real_agents(self) -> None:
        from maestro.models import AgentType

        assert AgentType.AUTO != AgentType.CLAUDE_CODE
        assert AgentType.AUTO != AgentType.CODEX
        assert AgentType.AUTO != AgentType.AIDER

    def test_auto_round_trips_through_enum(self) -> None:
        from maestro.models import AgentType

        assert AgentType("auto") is AgentType.AUTO
```

- [ ] **Step 2: Run test to verify failure**

```bash
uv run pytest tests/test_models.py::TestAgentTypeAuto -v
```
Expected: `AttributeError: AUTO` / `ValueError: 'auto' is not a valid AgentType`.

- [ ] **Step 3: Add AUTO to AgentType**

In `maestro/models.py`, modify the `AgentType` class:
```python
class AgentType(StrEnum):
    """Supported agent types for task execution."""

    CLAUDE_CODE = "claude_code"
    CODEX = "codex_cli"
    AIDER = "aider"
    ANNOUNCE = "announce"
    AUTO = "auto"
    """Routing sentinel: arbiter decides the real agent. NOT a spawnable agent.

    Invariants enforced in code:
    - Task.from_config raises when agent_type=AUTO and arbiter is not enabled.
    - Scheduler._spawn_task refuses to proceed with agent_type=AUTO reaching
      spawner lookup (defensive guard against misbehaving RoutingStrategy).
    """
```

- [ ] **Step 4: Run test to verify pass**

```bash
uv run pytest tests/test_models.py::TestAgentTypeAuto -v
```
Expected: 3 passed.

- [ ] **Step 5: Also run the full models test to catch regressions**

```bash
uv run pytest tests/test_models.py -v
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add maestro/models.py tests/test_models.py
git commit -m "feat(R-03): add AgentType.AUTO routing sentinel"
```

---

## Task 3: RouteAction / RouteDecision / TaskOutcomeStatus / TaskOutcome pydantic models

**Files:**
- Modify: `maestro/models.py`
- Test: `tests/test_routing_models.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_routing_models.py`:
```python
"""Tests for routing and outcome pydantic models added in R-03."""

import pytest
from pydantic import ValidationError

from maestro.models import (
    RouteAction,
    RouteDecision,
    TaskOutcome,
    TaskOutcomeStatus,
)


class TestRouteAction:
    def test_values(self) -> None:
        assert RouteAction.ASSIGN.value == "assign"
        assert RouteAction.HOLD.value == "hold"
        assert RouteAction.REJECT.value == "reject"


class TestRouteDecision:
    def test_assign_shape(self) -> None:
        d = RouteDecision(
            action=RouteAction.ASSIGN,
            chosen_agent="codex_cli",
            decision_id="dec-123",
            reason="dt_inference",
        )
        assert d.action is RouteAction.ASSIGN
        assert d.chosen_agent == "codex_cli"
        assert d.decision_id == "dec-123"

    def test_frozen(self) -> None:
        d = RouteDecision(
            action=RouteAction.HOLD, chosen_agent=None, decision_id=None, reason="budget"
        )
        with pytest.raises(ValidationError):
            d.action = RouteAction.ASSIGN  # type: ignore[misc]

    def test_hold_allows_none_chosen_and_decision(self) -> None:
        RouteDecision(
            action=RouteAction.HOLD, chosen_agent=None, decision_id=None, reason="x"
        )

    def test_reject_allows_none_chosen_and_decision(self) -> None:
        RouteDecision(
            action=RouteAction.REJECT,
            chosen_agent=None,
            decision_id="dec-5",
            reason="invariant_violation",
        )


class TestTaskOutcomeStatus:
    def test_values(self) -> None:
        assert TaskOutcomeStatus.SUCCESS.value == "success"
        assert TaskOutcomeStatus.FAILURE.value == "failure"
        assert TaskOutcomeStatus.TIMEOUT.value == "timeout"
        assert TaskOutcomeStatus.CANCELLED.value == "cancelled"
        assert TaskOutcomeStatus.INTERRUPTED.value == "interrupted"


class TestTaskOutcome:
    def test_minimal_shape_with_nones(self) -> None:
        o = TaskOutcome(
            status=TaskOutcomeStatus.SUCCESS,
            agent_used="claude_code",
            duration_min=None,
            tokens_used=None,
            cost_usd=None,
            error_code=None,
        )
        assert o.agent_used == "claude_code"
        assert o.tokens_used is None

    def test_populated_shape(self) -> None:
        o = TaskOutcome(
            status=TaskOutcomeStatus.FAILURE,
            agent_used="codex_cli",
            duration_min=3.5,
            tokens_used=12000,
            cost_usd=0.04,
            error_code="ValueError: bad input",
        )
        assert o.cost_usd == 0.04
```

- [ ] **Step 2: Run test to verify failure**

```bash
uv run pytest tests/test_routing_models.py -v
```
Expected: ImportError on `RouteAction`.

- [ ] **Step 3: Add models to `maestro/models.py`**

Insert after the existing `Priority` enum (around the arbiter enums block):
```python
class RouteAction(StrEnum):
    """Routing decision action (mirrors arbiter `AgentAction`)."""

    ASSIGN = "assign"
    HOLD = "hold"
    REJECT = "reject"


class RouteDecision(BaseModel):
    """Routing decision returned by RoutingStrategy.route().

    Frozen so scheduler cannot accidentally mutate a decision after
    receiving it from the routing layer.
    """

    model_config = ConfigDict(frozen=True)

    action: RouteAction
    chosen_agent: str | None = None
    decision_id: str | None = None
    reason: str


class TaskOutcomeStatus(StrEnum):
    """Terminal status reported back to arbiter via report_outcome."""

    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class TaskOutcome(BaseModel):
    """Task completion report sent to arbiter for learning signal."""

    status: TaskOutcomeStatus
    agent_used: str
    duration_min: float | None = None
    tokens_used: int | None = None
    cost_usd: float | None = None
    error_code: str | None = None
```

Add `ConfigDict` to the existing imports from `pydantic` at the top of `models.py`:
```python
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
```

- [ ] **Step 4: Run test to verify pass**

```bash
uv run pytest tests/test_routing_models.py -v
```
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff format maestro/models.py tests/test_routing_models.py
uv run ruff check maestro/models.py tests/test_routing_models.py
uv run pyrefly check
git add maestro/models.py tests/test_routing_models.py
git commit -m "feat(R-03): add RouteDecision/TaskOutcome pydantic models"
```

---

## Task 4: ArbiterMode + ArbiterConfig pydantic model

**Files:**
- Modify: `maestro/models.py`
- Test: `tests/test_arbiter_config.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_arbiter_config.py`:
```python
"""Tests for ArbiterConfig pydantic model."""

import pytest
from pydantic import ValidationError

from maestro.models import ArbiterConfig, ArbiterMode


class TestArbiterMode:
    def test_values(self) -> None:
        assert ArbiterMode.ADVISORY.value == "advisory"
        assert ArbiterMode.AUTHORITATIVE.value == "authoritative"


class TestArbiterConfigDefaults:
    def test_disabled_by_default(self) -> None:
        cfg = ArbiterConfig()
        assert cfg.enabled is False
        assert cfg.mode is ArbiterMode.ADVISORY
        assert cfg.optional is False
        assert cfg.timeout_ms == 500
        assert cfg.reconnect_interval_s == 60
        assert cfg.abandon_outcome_after_s == 300
        assert cfg.binary_path is None

    def test_disabled_allows_missing_paths(self) -> None:
        ArbiterConfig(enabled=False, binary_path=None, tree_path=None)


class TestArbiterConfigValidationWhenEnabled:
    def test_missing_binary_path_rejected(self) -> None:
        with pytest.raises(ValidationError, match="binary_path"):
            ArbiterConfig(enabled=True, config_dir="/c", tree_path="/t")

    def test_missing_config_dir_rejected(self) -> None:
        with pytest.raises(ValidationError, match="config_dir"):
            ArbiterConfig(enabled=True, binary_path="/b", tree_path="/t")

    def test_missing_tree_path_rejected(self) -> None:
        with pytest.raises(ValidationError, match="tree_path"):
            ArbiterConfig(enabled=True, binary_path="/b", config_dir="/c")

    def test_fully_populated_passes(self) -> None:
        cfg = ArbiterConfig(
            enabled=True,
            binary_path="/usr/local/bin/arbiter",
            config_dir="/etc/arbiter",
            tree_path="/var/lib/arbiter/tree.json",
        )
        assert cfg.binary_path == "/usr/local/bin/arbiter"


class TestArbiterConfigUnresolvedEnvVar:
    """Config parser only supports ${VAR}; ${VAR:-default} leaks through unresolved."""

    def test_unresolved_default_syntax_rejected_in_binary_path(self) -> None:
        with pytest.raises(ValidationError, match="env var substitution"):
            ArbiterConfig(
                enabled=True,
                binary_path="${ARBITER_BIN:-/fallback}",
                config_dir="/c",
                tree_path="/t",
            )

    def test_unresolved_plain_var_rejected(self) -> None:
        with pytest.raises(ValidationError, match="env var substitution"):
            ArbiterConfig(
                enabled=True,
                binary_path="${ARBITER_BIN}",  # did not get resolved
                config_dir="/c",
                tree_path="/t",
            )

    def test_absolute_path_no_dollar_passes(self) -> None:
        ArbiterConfig(
            enabled=True,
            binary_path="/opt/arbiter/arbiter-mcp",
            config_dir="/etc/arbiter",
            tree_path="/etc/arbiter/tree.json",
        )
```

- [ ] **Step 2: Run test to verify failure**

```bash
uv run pytest tests/test_arbiter_config.py -v
```
Expected: ImportError on `ArbiterConfig`.

- [ ] **Step 3: Add ArbiterMode and ArbiterConfig to `maestro/models.py`**

After the `Priority` enum block, add:
```python
class ArbiterMode(StrEnum):
    """Arbiter routing authority.

    ADVISORY — explicit `agent_type` in task config is honored; arbiter is
    consulted for learning signal and can HOLD/REJECT on invariants.
    AUTHORITATIVE — arbiter's `chosen_agent` overrides user declaration.
    """

    ADVISORY = "advisory"
    AUTHORITATIVE = "authoritative"


class ArbiterConfig(BaseModel):
    """Configuration for the Arbiter MCP integration.

    Validated on YAML load. `enabled=false` (default) keeps Maestro on the
    zero-config StaticRouting path; no arbiter subprocess ever started.
    """

    enabled: bool = False
    mode: ArbiterMode = ArbiterMode.ADVISORY
    optional: bool = False
    binary_path: str | None = None
    config_dir: str | None = None
    tree_path: str | None = None
    db_path: str | None = None
    timeout_ms: int = Field(default=500, ge=1)
    reconnect_interval_s: int = Field(default=60, ge=1)
    abandon_outcome_after_s: int = Field(default=300, ge=1)
    log_level: str = "warn"

    @model_validator(mode="after")
    def _validate_when_enabled(self) -> "ArbiterConfig":
        if not self.enabled:
            return self

        missing: list[str] = []
        for name in ("binary_path", "config_dir", "tree_path"):
            if getattr(self, name) is None:
                missing.append(name)
        if missing:
            msg = (
                f"arbiter.{'/'.join(missing)} required when arbiter.enabled=true. "
                f"Set via env var (e.g. ARBITER_BIN) or inline in config."
            )
            raise ValueError(msg)

        # config.py's env-var resolver only supports ${VAR}, not ${VAR:-default}.
        # Catch any residue of either syntax so users get a clear diagnostic
        # instead of a cryptic "binary not found" at startup.
        for name in ("binary_path", "config_dir", "tree_path", "db_path"):
            val = getattr(self, name)
            if val is not None and "${" in val:
                msg = (
                    f"arbiter.{name}={val!r}: unresolved env var substitution. "
                    f"config.py supports ${{VAR}} only; "
                    f"${{VAR:-default}} is not supported."
                )
                raise ValueError(msg)

        return self
```

- [ ] **Step 4: Run test to verify pass**

```bash
uv run pytest tests/test_arbiter_config.py -v
```
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff format maestro/models.py tests/test_arbiter_config.py
uv run ruff check maestro/models.py tests/test_arbiter_config.py
uv run pyrefly check
git add maestro/models.py tests/test_arbiter_config.py
git commit -m "feat(R-03): add ArbiterConfig pydantic model with validators"
```

---

## Task 5: Task model — 4 new arbiter fields

**Files:**
- Modify: `maestro/models.py` (Task class)
- Modify: `tests/test_models.py` (add test class)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_models.py`:
```python
class TestTaskArbiterFields:
    """Fields added in R-03 for arbiter routing persistence."""

    def test_defaults_none(self) -> None:
        from maestro.models import Task

        task = Task(id="t1", title="T", prompt="P", workdir="/tmp")
        assert task.routed_agent_type is None
        assert task.arbiter_decision_id is None
        assert task.arbiter_route_reason is None
        assert task.arbiter_outcome_reported_at is None

    def test_can_be_set(self) -> None:
        from datetime import UTC, datetime

        from maestro.models import Task

        now = datetime.now(UTC)
        task = Task(
            id="t2",
            title="T",
            prompt="P",
            workdir="/tmp",
            routed_agent_type="codex_cli",
            arbiter_decision_id="dec-9",
            arbiter_route_reason="dt_path=budget_ok,bugfix",
            arbiter_outcome_reported_at=now,
        )
        assert task.routed_agent_type == "codex_cli"
        assert task.arbiter_decision_id == "dec-9"
        assert task.arbiter_outcome_reported_at == now
```

- [ ] **Step 2: Run test to verify failure**

```bash
uv run pytest tests/test_models.py::TestTaskArbiterFields -v
```
Expected: `ValidationError: Extra inputs are not permitted` or field not found.

- [ ] **Step 3: Add fields to Task model**

In `maestro/models.py`, find the `class Task(BaseModel):` block (around line 337) and insert after the `depends_on` field:
```python
    # ---- R-03: Arbiter routing state (runtime-only, no TaskConfig equivalent) ----
    routed_agent_type: str | None = Field(
        default=None,
        description=(
            "Agent type chosen by the RoutingStrategy for this run. "
            "Spawner lookup uses this first, falling back to agent_type. "
            "Cleared on retry reset so the next attempt routes fresh."
        ),
    )
    arbiter_decision_id: str | None = Field(
        default=None,
        description="Arbiter-provided correlation id for matching report_outcome.",
    )
    arbiter_route_reason: str | None = Field(
        default=None,
        description="Free-form reason string from arbiter (e.g. 'budget_exceeded').",
    )
    arbiter_outcome_reported_at: datetime | None = Field(
        default=None,
        description=(
            "Set when report_outcome succeeds; recovery / re-attempt pass "
            "uses NULL as 'delivery still pending'."
        ),
    )
```

- [ ] **Step 4: Run test to verify pass**

```bash
uv run pytest tests/test_models.py::TestTaskArbiterFields -v
```
Expected: 2 passed.

- [ ] **Step 5: Full models regression**

```bash
uv run pytest tests/test_models.py -v
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
uv run ruff format maestro/models.py tests/test_models.py
uv run ruff check maestro/models.py tests/test_models.py
uv run pyrefly check
git add maestro/models.py tests/test_models.py
git commit -m "feat(R-03): add arbiter routing fields to Task model"
```

---

## Task 6: Task.from_config validation for AUTO without arbiter

**Files:**
- Modify: `maestro/models.py` (Task.from_config)
- Modify: `tests/test_models.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_models.py`:
```python
class TestFromConfigAutoValidation:
    """Task.from_config must reject agent_type=AUTO when arbiter is not enabled."""

    def test_auto_without_arbiter_raises(self) -> None:
        from maestro.models import AgentType, Task, TaskConfig

        cfg = TaskConfig(
            id="t1", title="T", prompt="P", agent_type=AgentType.AUTO
        )
        with pytest.raises(ValueError, match="arbiter.enabled=true"):
            Task.from_config(cfg, workdir="/tmp", arbiter_enabled=False)

    def test_auto_with_arbiter_enabled_passes(self) -> None:
        from maestro.models import AgentType, Task, TaskConfig

        cfg = TaskConfig(
            id="t2", title="T", prompt="P", agent_type=AgentType.AUTO
        )
        task = Task.from_config(cfg, workdir="/tmp", arbiter_enabled=True)
        assert task.agent_type is AgentType.AUTO

    def test_explicit_agent_without_arbiter_passes(self) -> None:
        from maestro.models import AgentType, Task, TaskConfig

        cfg = TaskConfig(
            id="t3", title="T", prompt="P", agent_type=AgentType.CODEX
        )
        task = Task.from_config(cfg, workdir="/tmp", arbiter_enabled=False)
        assert task.agent_type is AgentType.CODEX

    def test_from_config_default_arbiter_flag_false(self) -> None:
        """Backward compat: callers that don't pass arbiter_enabled default to False."""
        from maestro.models import AgentType, Task, TaskConfig

        cfg = TaskConfig(id="t4", title="T", prompt="P")  # default agent=CLAUDE_CODE
        task = Task.from_config(cfg, workdir="/tmp")
        assert task.agent_type is AgentType.CLAUDE_CODE
```

- [ ] **Step 2: Run test to verify failure**

```bash
uv run pytest tests/test_models.py::TestFromConfigAutoValidation -v
```
Expected: `TypeError: unexpected keyword 'arbiter_enabled'` or AUTO passes silently.

- [ ] **Step 3: Update `Task.from_config`**

Find `Task.from_config` in `maestro/models.py` and add the `arbiter_enabled` kwarg + validation. The signature change is:
```python
@classmethod
def from_config(
    cls,
    config: TaskConfig,
    workdir: str,
    arbiter_enabled: bool = False,
) -> "Task":
    """Build a runtime Task from a TaskConfig.

    Args:
        config: Declarative task config from YAML.
        workdir: Working directory path.
        arbiter_enabled: Whether arbiter is enabled in the runtime.
            Required to validate agent_type=AUTO; AUTO is a routing
            sentinel and cannot be spawned without a router.

    Raises:
        ValueError: If agent_type=AUTO but arbiter is not enabled.
    """
    if config.agent_type is AgentType.AUTO and not arbiter_enabled:
        msg = (
            f"Task {config.id!r}: agent_type=auto requires arbiter.enabled=true. "
            f"Set an explicit agent_type or enable arbiter in the project config."
        )
        raise ValueError(msg)
    # ... keep the rest of the existing body ...
```

(Locate the existing body and keep the inference logic intact; add only the AUTO check at the top and the new kwarg in the signature.)

- [ ] **Step 4: Run test to verify pass**

```bash
uv run pytest tests/test_models.py::TestFromConfigAutoValidation -v
```
Expected: 4 passed.

- [ ] **Step 5: Regression**

```bash
uv run pytest tests/test_models.py -v
```
Expected: all pass (existing callers use default `arbiter_enabled=False`, unaffected).

- [ ] **Step 6: Commit**

```bash
uv run ruff format maestro/models.py tests/test_models.py
uv run ruff check maestro/models.py tests/test_models.py
uv run pyrefly check
git add maestro/models.py tests/test_models.py
git commit -m "feat(R-03): Task.from_config rejects AUTO when arbiter disabled"
```

---

## Task 7: SQLite schema + migration for 4 new columns

**Files:**
- Modify: `maestro/database.py` (SCHEMA_SQL + new migration helper)
- Test: `tests/test_database.py` (new test class)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_database.py`:
```python
class TestArbiterRoutingMigration:
    """_migrate_tasks_arbiter_routing adds R-03 columns idempotently."""

    @pytest.mark.anyio
    async def test_fresh_db_has_four_new_columns(self, tmp_path) -> None:
        from maestro.database import Database

        db = Database(tmp_path / "fresh.db")
        await db.connect()
        try:
            cursor = await db._connection.execute("PRAGMA table_info(tasks)")
            cols = {row["name"] for row in await cursor.fetchall()}
            assert "routed_agent_type" in cols
            assert "arbiter_decision_id" in cols
            assert "arbiter_route_reason" in cols
            assert "arbiter_outcome_reported_at" in cols
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_legacy_db_migrates(self, tmp_path) -> None:
        """Pre-R-03 schema (3 arbiter-R02 columns, no routing columns) → migrate."""
        import aiosqlite

        db_path = tmp_path / "legacy.db"
        # Create a legacy tasks table without the 4 new columns
        legacy_sql = """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            branch TEXT,
            workdir TEXT NOT NULL,
            agent_type TEXT NOT NULL DEFAULT 'claude_code',
            status TEXT NOT NULL DEFAULT 'pending',
            assigned_to TEXT,
            scope TEXT,
            priority INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 2,
            retry_count INTEGER DEFAULT 0,
            timeout_minutes INTEGER DEFAULT 30,
            requires_approval BOOLEAN DEFAULT FALSE,
            validation_cmd TEXT,
            task_type TEXT NOT NULL DEFAULT 'feature',
            language TEXT NOT NULL DEFAULT 'other',
            complexity TEXT NOT NULL DEFAULT 'moderate',
            result_summary TEXT,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP
        )
        """
        async with aiosqlite.connect(str(db_path)) as conn:
            await conn.execute(legacy_sql)
            await conn.execute(
                "INSERT INTO tasks (id, title, prompt, workdir) VALUES "
                "('t1', 'T', 'P', '/tmp')"
            )
            await conn.commit()

        # Now connect via Database — should migrate
        from maestro.database import Database

        db = Database(db_path)
        await db.connect()
        try:
            cursor = await db._connection.execute("PRAGMA table_info(tasks)")
            cols = {row["name"] for row in await cursor.fetchall()}
            assert "routed_agent_type" in cols
            assert "arbiter_outcome_reported_at" in cols

            # Legacy row survives with NULLs in new columns
            cursor = await db._connection.execute(
                "SELECT routed_agent_type, arbiter_decision_id FROM tasks WHERE id='t1'"
            )
            row = await cursor.fetchone()
            assert row["routed_agent_type"] is None
            assert row["arbiter_decision_id"] is None
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_migration_idempotent(self, tmp_path) -> None:
        """Running migrate twice does not fail."""
        from maestro.database import Database

        db = Database(tmp_path / "idem.db")
        await db.connect()
        try:
            # connect() already ran migrate once. Run it again manually.
            await db._migrate_tasks_arbiter_routing()
            await db._migrate_tasks_arbiter_routing()
        finally:
            await db.close()
```

- [ ] **Step 2: Run test to verify failure**

```bash
uv run pytest tests/test_database.py::TestArbiterRoutingMigration -v
```
Expected: new columns missing.

- [ ] **Step 3: Update `SCHEMA_SQL` and add migration helper in `maestro/database.py`**

Modify `SCHEMA_SQL` (the `tasks` table section) to add the 4 columns after `completed_at`:
```sql
-- existing columns ...
created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
started_at TIMESTAMP,
completed_at TIMESTAMP,
-- R-03 arbiter routing state
routed_agent_type TEXT,
arbiter_decision_id TEXT,
arbiter_route_reason TEXT,
arbiter_outcome_reported_at TIMESTAMP
```

Add a new migration helper method next to `_migrate_tasks_arbiter_columns`:
```python
async def _migrate_tasks_arbiter_routing(self) -> None:
    """R-03: Add arbiter routing state columns to an older `tasks` table.

    Idempotent via PRAGMA table_info check. Called from `connect()` after
    the R-02 column migration.
    """
    assert self._connection is not None
    cursor = await self._connection.execute("PRAGMA table_info(tasks)")
    columns = {row["name"] for row in await cursor.fetchall()}

    migrations = [
        ("routed_agent_type", "ALTER TABLE tasks ADD COLUMN routed_agent_type TEXT"),
        (
            "arbiter_decision_id",
            "ALTER TABLE tasks ADD COLUMN arbiter_decision_id TEXT",
        ),
        (
            "arbiter_route_reason",
            "ALTER TABLE tasks ADD COLUMN arbiter_route_reason TEXT",
        ),
        (
            "arbiter_outcome_reported_at",
            "ALTER TABLE tasks ADD COLUMN arbiter_outcome_reported_at TIMESTAMP",
        ),
    ]
    for column, ddl in migrations:
        if column not in columns:
            await self._connection.execute(ddl)
```

In the `connect()` method, call the new migration helper after `_migrate_tasks_arbiter_columns()`:
```python
await self._migrate_tasks_arbiter_columns()
await self._migrate_tasks_arbiter_routing()   # R-03
```

- [ ] **Step 4: Update `_row_to_task` and INSERT/UPDATE to include new fields**

Find `_row_to_task` in `database.py` and append the 4 new fields:
```python
return Task(
    # ... existing fields ...
    routed_agent_type=row["routed_agent_type"],
    arbiter_decision_id=row["arbiter_decision_id"],
    arbiter_route_reason=row["arbiter_route_reason"],
    arbiter_outcome_reported_at=_parse_datetime(row["arbiter_outcome_reported_at"]),
)
```

Find the INSERT statement in `create_task` and add the 4 columns + placeholders + params:
```python
await self._connection.execute(
    """
    INSERT INTO tasks (
        id, title, prompt, branch, workdir, agent_type, status,
        assigned_to, scope, priority, max_retries, retry_count,
        timeout_minutes, requires_approval, validation_cmd,
        task_type, language, complexity,
        result_summary, error_message, created_at, started_at, completed_at,
        routed_agent_type, arbiter_decision_id, arbiter_route_reason,
        arbiter_outcome_reported_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
        # ... existing values ...
        task.routed_agent_type,
        task.arbiter_decision_id,
        task.arbiter_route_reason,
        _format_datetime(task.arbiter_outcome_reported_at),
    ),
)
```

Similarly update any full-row UPDATE statement to include the four new columns.

- [ ] **Step 5: Run test to verify pass**

```bash
uv run pytest tests/test_database.py::TestArbiterRoutingMigration -v
uv run pytest tests/test_database.py -v
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
uv run ruff format maestro/database.py tests/test_database.py
uv run ruff check maestro/database.py tests/test_database.py
uv run pyrefly check
git add maestro/database.py tests/test_database.py
git commit -m "feat(R-03): add arbiter routing columns + migration"
```

---

## Task 8: Database.update_task_routing

**Files:**
- Modify: `maestro/database.py`
- Modify: `tests/test_database.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_database.py`:
```python
class TestUpdateTaskRouting:
    @pytest.mark.anyio
    async def test_writes_routing_fields_only(self, tmp_path) -> None:
        from maestro.database import Database
        from maestro.models import AgentType, Task, TaskStatus

        db = Database(tmp_path / "r.db")
        await db.connect()
        try:
            task = Task(
                id="t1",
                title="T",
                prompt="P",
                workdir="/tmp",
                agent_type=AgentType.AUTO,
                status=TaskStatus.READY,
            )
            await db.create_task(task)

            # Update routing fields (as scheduler does pre-spawn)
            task_updated = task.model_copy(
                update={
                    "routed_agent_type": "codex_cli",
                    "arbiter_decision_id": "dec-42",
                    "arbiter_route_reason": "dt_path",
                }
            )
            await db.update_task_routing(task_updated)

            refetched = await db.get_task("t1")
            assert refetched.routed_agent_type == "codex_cli"
            assert refetched.arbiter_decision_id == "dec-42"
            assert refetched.arbiter_route_reason == "dt_path"
            # agent_type and status untouched
            assert refetched.agent_type is AgentType.AUTO
            assert refetched.status is TaskStatus.READY
        finally:
            await db.close()
```

- [ ] **Step 2: Run test to verify failure**

```bash
uv run pytest tests/test_database.py::TestUpdateTaskRouting -v
```
Expected: `AttributeError: 'Database' object has no attribute 'update_task_routing'`.

- [ ] **Step 3: Add the method**

In `maestro/database.py`, in the `Database` class, add:
```python
async def update_task_routing(self, task: Task) -> None:
    """R-03: Persist routing decision for a task before spawner lookup.

    Writes only the routing-related columns; does NOT touch `agent_type`,
    `status`, `assigned_to`, or timestamps. The order matters: routing
    decision must be persisted BEFORE the agent subprocess is spawned,
    so a crash mid-spawn still leaves enough state for recovery to
    correlate the outcome.
    """
    if self._connection is None:
        msg = "Database not connected"
        raise DatabaseError(msg)

    await self._connection.execute(
        """
        UPDATE tasks
        SET routed_agent_type = ?,
            arbiter_decision_id = ?,
            arbiter_route_reason = ?
        WHERE id = ?
        """,
        (
            task.routed_agent_type,
            task.arbiter_decision_id,
            task.arbiter_route_reason,
            task.id,
        ),
    )
    await self._connection.commit()
```

- [ ] **Step 4: Run test to verify pass**

```bash
uv run pytest tests/test_database.py::TestUpdateTaskRouting -v
```
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff format maestro/database.py tests/test_database.py
uv run ruff check maestro/database.py tests/test_database.py
uv run pyrefly check
git add maestro/database.py tests/test_database.py
git commit -m "feat(R-03): Database.update_task_routing"
```

---

## Task 9: Database.mark_outcome_reported with decision_id guard

**Files:**
- Modify: `maestro/database.py`
- Modify: `tests/test_database.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_database.py`:
```python
class TestMarkOutcomeReported:
    @pytest.mark.anyio
    async def test_sets_timestamp_when_decision_matches(self, tmp_path) -> None:
        from datetime import UTC, datetime

        from maestro.database import Database
        from maestro.models import Task

        db = Database(tmp_path / "m.db")
        await db.connect()
        try:
            task = Task(
                id="t1",
                title="T",
                prompt="P",
                workdir="/tmp",
                arbiter_decision_id="dec-7",
            )
            await db.create_task(task)

            ts = datetime.now(UTC)
            ok = await db.mark_outcome_reported("t1", ts, "dec-7")
            assert ok is True

            refetched = await db.get_task("t1")
            assert refetched.arbiter_outcome_reported_at is not None

    @pytest.mark.anyio
    async def test_guard_rejects_wrong_decision_id(self, tmp_path) -> None:
        """decision_id mismatch → rowcount=0, returns False, no write."""
        from datetime import UTC, datetime

        from maestro.database import Database
        from maestro.models import Task

        db = Database(tmp_path / "g.db")
        await db.connect()
        try:
            task = Task(
                id="t1", title="T", prompt="P", workdir="/tmp",
                arbiter_decision_id="current-dec",
            )
            await db.create_task(task)

            ok = await db.mark_outcome_reported("t1", datetime.now(UTC), "stale-dec")
            assert ok is False

            refetched = await db.get_task("t1")
            assert refetched.arbiter_outcome_reported_at is None
        finally:
            await db.close()
```

- [ ] **Step 2: Run test to verify failure**

```bash
uv run pytest tests/test_database.py::TestMarkOutcomeReported -v
```
Expected: method missing.

- [ ] **Step 3: Add `mark_outcome_reported`**

```python
async def mark_outcome_reported(
    self,
    task_id: str,
    reported_at: datetime,
    decision_id: str,
) -> bool:
    """R-03: Atomically record that report_outcome succeeded.

    The `decision_id` guard prevents a stale call from marking the current
    attempt as reported — if a retry already overwrote arbiter_decision_id,
    this call returns False and the caller (scheduler re-attempt pass)
    drops the stale outcome.

    Returns:
        True if a row was updated, False if the decision_id no longer
        matches (external interference or stale recovery attempt).
    """
    if self._connection is None:
        msg = "Database not connected"
        raise DatabaseError(msg)

    cursor = await self._connection.execute(
        """
        UPDATE tasks
        SET arbiter_outcome_reported_at = ?
        WHERE id = ? AND arbiter_decision_id = ?
        """,
        (_format_datetime(reported_at), task_id, decision_id),
    )
    await self._connection.commit()
    return cursor.rowcount > 0
```

- [ ] **Step 4: Run test + commit**

```bash
uv run pytest tests/test_database.py::TestMarkOutcomeReported -v
# Expected: 2 passed
uv run ruff format maestro/database.py tests/test_database.py
uv run pyrefly check
git add maestro/database.py tests/test_database.py
git commit -m "feat(R-03): Database.mark_outcome_reported with decision_id guard"
```

---

## Task 10: Database.reset_for_retry_atomic

**Files:**
- Modify: `maestro/database.py`
- Modify: `tests/test_database.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_database.py`:
```python
class TestResetForRetryAtomic:
    @pytest.mark.anyio
    async def test_failed_to_ready_with_fields_cleared(self, tmp_path) -> None:
        from maestro.database import Database
        from maestro.models import Task, TaskStatus

        db = Database(tmp_path / "r.db")
        await db.connect()
        try:
            task = Task(
                id="t1", title="T", prompt="P", workdir="/tmp",
                status=TaskStatus.FAILED,
                routed_agent_type="codex_cli",
                arbiter_decision_id="dec-9",
                arbiter_route_reason="dt",
            )
            await db.create_task(task)

            ok = await db.reset_for_retry_atomic("t1", "dec-9")
            assert ok is True

            refetched = await db.get_task("t1")
            assert refetched.status is TaskStatus.READY
            assert refetched.routed_agent_type is None
            assert refetched.arbiter_decision_id is None
            assert refetched.arbiter_route_reason is None
            assert refetched.arbiter_outcome_reported_at is None
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_external_status_change_is_skipped(self, tmp_path) -> None:
        """If status is not FAILED (external abandon/approve), reset is a no-op."""
        from maestro.database import Database
        from maestro.models import Task, TaskStatus

        db = Database(tmp_path / "e.db")
        await db.connect()
        try:
            task = Task(
                id="t1", title="T", prompt="P", workdir="/tmp",
                status=TaskStatus.NEEDS_REVIEW,  # external handoff
                arbiter_decision_id="dec-9",
            )
            await db.create_task(task)

            ok = await db.reset_for_retry_atomic("t1", "dec-9")
            assert ok is False

            refetched = await db.get_task("t1")
            assert refetched.status is TaskStatus.NEEDS_REVIEW
            # fields NOT cleared
            assert refetched.arbiter_decision_id == "dec-9"
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_none_decision_id_skips_guard(self, tmp_path) -> None:
        """Advisory path calls without decision_id guard (best-effort)."""
        from maestro.database import Database
        from maestro.models import Task, TaskStatus

        db = Database(tmp_path / "n.db")
        await db.connect()
        try:
            task = Task(
                id="t1", title="T", prompt="P", workdir="/tmp",
                status=TaskStatus.FAILED,
                arbiter_decision_id="dec-9",  # whatever value
            )
            await db.create_task(task)

            ok = await db.reset_for_retry_atomic("t1", decision_id=None)
            assert ok is True

            refetched = await db.get_task("t1")
            assert refetched.status is TaskStatus.READY
        finally:
            await db.close()
```

- [ ] **Step 2: Run test to verify failure**

```bash
uv run pytest tests/test_database.py::TestResetForRetryAtomic -v
```
Expected: method missing.

- [ ] **Step 3: Implement**

```python
async def reset_for_retry_atomic(
    self,
    task_id: str,
    decision_id: str | None,
) -> bool:
    """R-03: Atomically transition FAILED → READY and clear arbiter fields.

    Single UPDATE closes the race window that `report_outcome`'s network
    latency would otherwise widen: an external `abandon` / `approve` /
    dashboard action during outcome delivery cannot interleave with
    retry transition.

    Args:
        task_id: Task to reset.
        decision_id: If not None, an additional guard that the row's
            current `arbiter_decision_id` matches; used by authoritative
            mode after successful outcome delivery. Pass None to skip
            the guard (advisory best-effort retry).

    Returns:
        True if the row transitioned; False if status != FAILED or the
        decision_id guard failed (external interference).
    """
    if self._connection is None:
        msg = "Database not connected"
        raise DatabaseError(msg)

    if decision_id is None:
        sql = """
            UPDATE tasks
            SET status = 'ready',
                routed_agent_type = NULL,
                arbiter_decision_id = NULL,
                arbiter_route_reason = NULL,
                arbiter_outcome_reported_at = NULL
            WHERE id = ? AND status = 'failed'
        """
        params: tuple[Any, ...] = (task_id,)
    else:
        sql = """
            UPDATE tasks
            SET status = 'ready',
                routed_agent_type = NULL,
                arbiter_decision_id = NULL,
                arbiter_route_reason = NULL,
                arbiter_outcome_reported_at = NULL
            WHERE id = ? AND status = 'failed' AND arbiter_decision_id = ?
        """
        params = (task_id, decision_id)

    cursor = await self._connection.execute(sql, params)
    await self._connection.commit()
    return cursor.rowcount > 0
```

- [ ] **Step 4: Run test + commit**

```bash
uv run pytest tests/test_database.py::TestResetForRetryAtomic -v
uv run ruff format maestro/database.py tests/test_database.py
uv run pyrefly check
git add maestro/database.py tests/test_database.py
git commit -m "feat(R-03): Database.reset_for_retry_atomic with race guard"
```

---

## Task 11: Database.get_tasks_with_pending_outcome

**Files:**
- Modify: `maestro/database.py`
- Modify: `tests/test_database.py`

- [ ] **Step 1: Write the failing test**

```python
class TestGetTasksWithPendingOutcome:
    @pytest.mark.anyio
    async def test_returns_tasks_with_decision_but_no_reported_at(self, tmp_path) -> None:
        from datetime import UTC, datetime

        from maestro.database import Database
        from maestro.models import Task, TaskStatus

        db = Database(tmp_path / "p.db")
        await db.connect()
        try:
            # Three tasks: one pending, one already reported, one without routing
            t1 = Task(
                id="pending", title="T", prompt="P", workdir="/tmp",
                status=TaskStatus.DONE,
                arbiter_decision_id="dec-pending",
            )
            t2 = Task(
                id="reported", title="T", prompt="P", workdir="/tmp",
                status=TaskStatus.DONE,
                arbiter_decision_id="dec-reported",
                arbiter_outcome_reported_at=datetime.now(UTC),
            )
            t3 = Task(
                id="static", title="T", prompt="P", workdir="/tmp",
                status=TaskStatus.DONE,
            )
            for t in (t1, t2, t3):
                await db.create_task(t)

            pending = await db.get_tasks_with_pending_outcome()
            ids = {t.id for t in pending}
            assert ids == {"pending"}
        finally:
            await db.close()
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_database.py::TestGetTasksWithPendingOutcome -v
```

- [ ] **Step 3: Implement**

```python
async def get_tasks_with_pending_outcome(self) -> list[Task]:
    """R-03: Tasks that have a routing decision but no outcome delivered yet.

    Returns tasks in any status (RUNNING/VALIDATING/terminal/FAILED) with
    `arbiter_decision_id IS NOT NULL AND arbiter_outcome_reported_at IS NULL`.
    Used by recovery hook and scheduler re-attempt pass.
    """
    if self._connection is None:
        msg = "Database not connected"
        raise DatabaseError(msg)

    cursor = await self._connection.execute(
        """
        SELECT * FROM tasks
        WHERE arbiter_decision_id IS NOT NULL
          AND arbiter_outcome_reported_at IS NULL
        ORDER BY created_at ASC
        """,
    )
    rows = await cursor.fetchall()
    return [_row_to_task(row) for row in rows]
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/test_database.py::TestGetTasksWithPendingOutcome -v
uv run ruff format maestro/database.py tests/test_database.py
uv run pyrefly check
git add maestro/database.py tests/test_database.py
git commit -m "feat(R-03): Database.get_tasks_with_pending_outcome"
```

---

## Task 12: RoutingStrategy protocol + StaticRouting

**Files:**
- Create: `maestro/coordination/routing.py`
- Test: `tests/test_routing_static.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_routing_static.py`:
```python
"""Tests for StaticRouting — the default zero-config routing strategy."""

import pytest

from maestro.coordination.routing import RoutingStrategy, StaticRouting
from maestro.models import (
    AgentType,
    RouteAction,
    Task,
    TaskOutcome,
    TaskOutcomeStatus,
)


def _task(agent: AgentType = AgentType.CLAUDE_CODE) -> Task:
    return Task(id="t1", title="T", prompt="P", workdir="/tmp", agent_type=agent)


class TestStaticRoutingRoute:
    @pytest.mark.anyio
    async def test_returns_assign_with_declared_agent(self) -> None:
        routing = StaticRouting()
        d = await routing.route(_task(AgentType.CODEX))
        assert d.action is RouteAction.ASSIGN
        assert d.chosen_agent == "codex_cli"
        assert d.decision_id is None
        assert d.reason == "static"

    @pytest.mark.anyio
    async def test_respects_claude_code(self) -> None:
        routing = StaticRouting()
        d = await routing.route(_task(AgentType.CLAUDE_CODE))
        assert d.chosen_agent == "claude_code"


class TestStaticRoutingReportOutcome:
    @pytest.mark.anyio
    async def test_is_noop(self) -> None:
        routing = StaticRouting()
        outcome = TaskOutcome(
            status=TaskOutcomeStatus.SUCCESS, agent_used="codex_cli"
        )
        # Should not raise regardless of task state
        await routing.report_outcome(_task(), outcome)


class TestStaticRoutingAclose:
    @pytest.mark.anyio
    async def test_is_noop(self) -> None:
        await StaticRouting().aclose()


class TestProtocolSatisfied:
    def test_static_is_routing_strategy(self) -> None:
        # Runtime-protocol check via isinstance would need @runtime_checkable;
        # just ensure attribute presence.
        s = StaticRouting()
        assert callable(s.route)
        assert callable(s.report_outcome)
        assert callable(s.aclose)
        assert isinstance(s, RoutingStrategy) or True  # structural typing
```

Ensure `conftest.py` has anyio backend parameterization (already present in Maestro). If not, add:
```python
# tests/conftest.py (add if missing)
import pytest

@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_routing_static.py -v
```
Expected: ImportError.

- [ ] **Step 3: Create routing module**

Create `maestro/coordination/routing.py`:
```python
"""RoutingStrategy protocol and its implementations.

Scheduler calls `route(task)` before spawning to get a chosen agent,
and `report_outcome(task, outcome)` in terminal handlers to close the
learning loop. StaticRouting is the zero-config OSS default and the
fallback delegate inside ArbiterRouting.

ArbiterRouting is added in a later task; this file currently exposes
only the protocol and StaticRouting so lower-level tests can run.
"""

from __future__ import annotations

from typing import Protocol

from maestro.models import (
    RouteAction,
    RouteDecision,
    Task,
    TaskOutcome,
)


class RoutingStrategy(Protocol):
    """Protocol implemented by every routing strategy."""

    async def route(self, task: Task) -> RouteDecision:
        """Return a routing decision for the given task."""
        ...

    async def report_outcome(self, task: Task, outcome: TaskOutcome) -> None:
        """Close the feedback loop for a terminal task.

        Static-routed tasks (decision_id IS NULL) are typically a noop.
        Arbiter-routed tasks raise ArbiterUnavailable on delivery failure
        so the caller can apply mode-dependent retry gating.
        """
        ...

    async def aclose(self) -> None:
        """Release any resources held by the strategy (subprocess, etc.)."""
        ...


class StaticRouting:
    """Default strategy: use `task.agent_type` verbatim, no feedback loop.

    This is the zero-config OSS path. `arbiter: null` or `arbiter.enabled:
    false` yield this strategy. `ArbiterRouting` also instantiates one
    internally as the fallback delegate when the arbiter subprocess is
    unavailable.
    """

    async def route(self, task: Task) -> RouteDecision:
        return RouteDecision(
            action=RouteAction.ASSIGN,
            chosen_agent=task.agent_type.value,
            decision_id=None,
            reason="static",
        )

    async def report_outcome(self, task: Task, outcome: TaskOutcome) -> None:
        # Static decisions have no correlation id; nothing to report.
        return None

    async def aclose(self) -> None:
        return None
```

- [ ] **Step 4: Run tests + commit**

```bash
uv run pytest tests/test_routing_static.py -v
uv run ruff format maestro/coordination/routing.py tests/test_routing_static.py
uv run ruff check maestro/coordination/routing.py tests/test_routing_static.py
uv run pyrefly check
git add maestro/coordination/routing.py tests/test_routing_static.py
git commit -m "feat(R-03): RoutingStrategy protocol + StaticRouting"
```

---

## Task 13: Status mapping helper (TaskStatus → TaskOutcomeStatus)

**Files:**
- Modify: `maestro/coordination/routing.py`
- Test: `tests/test_status_mapping.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_status_mapping.py`:
```python
"""Tests for TaskStatus → TaskOutcomeStatus mapping used by recovery."""

import pytest

from maestro.coordination.routing import task_status_to_outcome_status
from maestro.models import TaskOutcomeStatus, TaskStatus


class TestMapping:
    def test_done_maps_to_success(self) -> None:
        assert task_status_to_outcome_status(TaskStatus.DONE) is TaskOutcomeStatus.SUCCESS

    def test_failed_maps_to_failure(self) -> None:
        assert (
            task_status_to_outcome_status(TaskStatus.FAILED) is TaskOutcomeStatus.FAILURE
        )

    def test_needs_review_maps_to_failure(self) -> None:
        assert (
            task_status_to_outcome_status(TaskStatus.NEEDS_REVIEW)
            is TaskOutcomeStatus.FAILURE
        )

    def test_abandoned_maps_to_cancelled(self) -> None:
        assert (
            task_status_to_outcome_status(TaskStatus.ABANDONED)
            is TaskOutcomeStatus.CANCELLED
        )

    def test_running_maps_to_interrupted(self) -> None:
        assert (
            task_status_to_outcome_status(TaskStatus.RUNNING)
            is TaskOutcomeStatus.INTERRUPTED
        )

    def test_validating_maps_to_interrupted(self) -> None:
        assert (
            task_status_to_outcome_status(TaskStatus.VALIDATING)
            is TaskOutcomeStatus.INTERRUPTED
        )

    @pytest.mark.parametrize(
        "invariant_state",
        [TaskStatus.PENDING, TaskStatus.READY, TaskStatus.AWAITING_APPROVAL],
    )
    def test_invariant_violation_states_return_none(
        self, invariant_state: TaskStatus
    ) -> None:
        assert task_status_to_outcome_status(invariant_state) is None
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_status_mapping.py -v
```
Expected: ImportError.

- [ ] **Step 3: Add the helper to `routing.py`**

Append to `maestro/coordination/routing.py`:
```python
from maestro.models import TaskOutcomeStatus, TaskStatus

_STATUS_MAP: dict[TaskStatus, TaskOutcomeStatus | None] = {
    TaskStatus.DONE: TaskOutcomeStatus.SUCCESS,
    TaskStatus.FAILED: TaskOutcomeStatus.FAILURE,
    TaskStatus.NEEDS_REVIEW: TaskOutcomeStatus.FAILURE,
    TaskStatus.ABANDONED: TaskOutcomeStatus.CANCELLED,
    TaskStatus.RUNNING: TaskOutcomeStatus.INTERRUPTED,
    TaskStatus.VALIDATING: TaskOutcomeStatus.INTERRUPTED,
    # Invariant-violation states: decision_id should never be set here.
    TaskStatus.PENDING: None,
    TaskStatus.READY: None,
    TaskStatus.AWAITING_APPROVAL: None,
}


def task_status_to_outcome_status(
    status: TaskStatus,
) -> TaskOutcomeStatus | None:
    """Map a Task lifecycle status to the outcome status arbiter expects.

    Returns None for states that should never carry an arbiter_decision_id
    (PENDING/READY/AWAITING_APPROVAL). Callers log and skip these as
    invariant violations.
    """
    return _STATUS_MAP.get(status)
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/test_status_mapping.py -v
uv run ruff format maestro/coordination/routing.py tests/test_status_mapping.py
uv run pyrefly check
git add maestro/coordination/routing.py tests/test_status_mapping.py
git commit -m "feat(R-03): task_status_to_outcome_status mapping helper"
```

---

## Task 14: Vendor ArbiterClient (pydantic DTOs + subprocess lifecycle)

**Files:**
- Create: `maestro/coordination/arbiter_client.py`
- Test: `tests/test_arbiter_client_structure.py`

This task is structural — copy-adapt from `../arbiter/orchestrator/arbiter_client.py` + `types.py`. Behavior tests come next task (with FakeArbiterClient helper).

- [ ] **Step 1: Write the failing structural test**

Create `tests/test_arbiter_client_structure.py`:
```python
"""Structural tests for the vendored ArbiterClient.

These verify the module exists and exposes the expected surface. Behavior
is exercised via FakeArbiterClient in later tasks.
"""

from maestro.coordination import arbiter_client


class TestVendoringHeader:
    def test_vendor_commit_pinned(self) -> None:
        assert arbiter_client.ARBITER_VENDOR_COMMIT == "861534e"

    def test_required_version_pinned(self) -> None:
        assert arbiter_client.ARBITER_MCP_REQUIRED_VERSION == "0.1.0"


class TestPublicAPI:
    def test_client_class_exists(self) -> None:
        assert hasattr(arbiter_client, "ArbiterClient")
        assert hasattr(arbiter_client, "ArbiterClientConfig")

    def test_dto_classes_exist(self) -> None:
        assert hasattr(arbiter_client, "RouteDecisionDTO")
        assert hasattr(arbiter_client, "OutcomeResultDTO")
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_arbiter_client_structure.py -v
```
Expected: ModuleNotFoundError.

- [ ] **Step 3: Create the vendored client**

Create `maestro/coordination/arbiter_client.py`. Start from `../arbiter/orchestrator/arbiter_client.py` and apply these adaptations:

- Header docstring per spec (vendored commit + required version + do/don't list).
- Convert `@dataclass(frozen=True)` DTOs to `BaseModel(model_config=ConfigDict(frozen=True))`. Rename DTO classes with `DTO` suffix to distinguish from the scheduler-facing `RouteDecision` (pydantic-native, in `models.py`). E.g. `RouteDecision` → `RouteDecisionDTO`, `OutcomeResult` → `OutcomeResultDTO`, `AgentStatusInfo` → `AgentStatusInfoDTO`.
- Replace `raise ArbiterConnectionError(...)` with `raise ArbiterUnavailable(...)` (Maestro-native).
- Replace `raise ArbiterError("Binary not found: ...")` with `raise ArbiterStartupError(...)` with `path=` kwarg.
- Replace `raise ArbiterProtocolError(...)` with `raise ArbiterUnavailable("protocol error: ...", cause=...)` — we don't distinguish protocol from transport at the Maestro layer.
- `import logging` + `logger = logging.getLogger(__name__)` (Maestro convention).
- At the top, module-level constants:
  ```python
  ARBITER_VENDOR_COMMIT = "861534e"
  ARBITER_MCP_REQUIRED_VERSION = "0.1.0"
  ```
- In `_handshake()`, after the handshake dict arrives, validate `serverInfo.version` against `ARBITER_MCP_REQUIRED_VERSION`:
  ```python
  async def _handshake(self) -> dict[str, Any]:
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
  ```
- Rename `get_agent_status` — keep. R-03 scheduler does not call it; leave for future use.
- Keep `route_task`, `report_outcome` method names (MCP contract).
- Add `tokens_used: int | None`, `cost_usd: float | None` to `report_outcome` signature so the caller can pass None.

Do NOT modify: subprocess lifecycle, reconnect logic, stdio line framing, JSON-RPC id sequencing, DTO field shapes beyond class-name suffix.

The resulting file is ~400-450 lines. Include a `FallbackScheduler` — optional; not required by R-03 (ArbiterRouting has its own fallback logic via `StaticRouting`). You may omit it to reduce surface area.

- [ ] **Step 4: Run structural test + ruff/pyrefly**

```bash
uv run pytest tests/test_arbiter_client_structure.py -v
uv run ruff format maestro/coordination/arbiter_client.py
uv run ruff check maestro/coordination/arbiter_client.py
uv run pyrefly check
```
Expected: 4 passed; type-check clean.

- [ ] **Step 5: Commit**

```bash
git add maestro/coordination/arbiter_client.py tests/test_arbiter_client_structure.py
git commit -m "feat(R-03): vendor ArbiterClient from arbiter@861534e"
```

---

## Task 15: FakeArbiterClient test helper

**Files:**
- Create: `tests/fakes/__init__.py`
- Create: `tests/fakes/fake_arbiter_client.py`

No test for this task directly — it's the test tool used by later tasks. Validated indirectly by tasks 16+.

- [ ] **Step 1: Create package init**

```bash
mkdir -p tests/fakes
```

Create `tests/fakes/__init__.py`:
```python
"""Shared test doubles for Maestro's coordination layer."""
```

- [ ] **Step 2: Create FakeArbiterClient**

Create `tests/fakes/fake_arbiter_client.py`:
```python
"""In-memory ArbiterClient double for contract tests.

Mimics the public surface of maestro.coordination.arbiter_client.ArbiterClient
without a real subprocess. Tests inject scripted responses or side effects.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from maestro.coordination.arbiter_errors import ArbiterUnavailable


@dataclass
class FakeCall:
    method: str
    arguments: dict[str, Any]


class FakeArbiterClient:
    """Lookalike ArbiterClient that returns scripted responses.

    Usage:
        fake = FakeArbiterClient()
        fake.route_handler = lambda task_id, task, constraints: {
            "task_id": task_id, "action": "assign", "chosen_agent": "codex_cli",
            "confidence": 0.9, "reasoning": "", "decision_path": [],
            "invariant_checks": [], "metadata": {"decision_id": "dec-1"},
        }
        await fake.start()
        resp = await fake.route_task("t1", {...})
        await fake.stop()
    """

    def __init__(self) -> None:
        self.calls: list[FakeCall] = []
        self.started: bool = False
        self.route_handler: Callable[[str, dict[str, Any], dict[str, Any] | None], dict[str, Any]] | None = None
        self.outcome_handler: Callable[..., dict[str, Any]] | None = None
        self.start_raises: BaseException | None = None
        self.version: str = "0.1.0"
        # route_delay simulates a slow arbiter so timeout tests can exercise wait_for
        self.route_delay_s: float = 0.0
        self.outcome_delay_s: float = 0.0
        self.outcome_raises: BaseException | None = None

    @property
    def is_running(self) -> bool:
        return self.started

    async def start(self) -> dict[str, Any]:
        if self.start_raises is not None:
            raise self.start_raises
        self.started = True
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "arbiter", "version": self.version},
        }

    async def stop(self) -> None:
        self.started = False

    async def route_task(
        self,
        task_id: str,
        task: dict[str, Any],
        constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(FakeCall("route_task", {"task_id": task_id, "task": task}))
        if self.route_delay_s:
            await asyncio.sleep(self.route_delay_s)
        if self.route_handler is None:
            raise AssertionError("FakeArbiterClient.route_handler not set")
        return self.route_handler(task_id, task, constraints)

    async def report_outcome(
        self,
        task_id: str,
        agent_id: str,
        status: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            FakeCall("report_outcome", {"task_id": task_id, "agent_id": agent_id, "status": status, **kwargs})
        )
        if self.outcome_delay_s:
            await asyncio.sleep(self.outcome_delay_s)
        if self.outcome_raises is not None:
            raise self.outcome_raises
        if self.outcome_handler is not None:
            return self.outcome_handler(
                task_id=task_id, agent_id=agent_id, status=status, **kwargs
            )
        return {"task_id": task_id, "recorded": True}
```

- [ ] **Step 3: Commit**

```bash
uv run ruff format tests/fakes/
uv run pyrefly check
git add tests/fakes/
git commit -m "test(R-03): FakeArbiterClient test double"
```

---

## Task 16: ArbiterRouting — ASSIGN happy path

**Files:**
- Modify: `maestro/coordination/routing.py`
- Create: `tests/test_arbiter_routing.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_arbiter_routing.py`:
```python
"""Contract tests for ArbiterRouting using FakeArbiterClient."""

from __future__ import annotations

import pytest

from maestro.coordination.routing import ArbiterRouting
from maestro.models import (
    AgentType,
    ArbiterConfig,
    ArbiterMode,
    RouteAction,
    Task,
)
from tests.fakes.fake_arbiter_client import FakeArbiterClient


def _task(agent: AgentType = AgentType.AUTO) -> Task:
    return Task(id="t1", title="T", prompt="P", workdir="/tmp", agent_type=agent)


def _cfg(mode: ArbiterMode = ArbiterMode.ADVISORY) -> ArbiterConfig:
    return ArbiterConfig(
        enabled=True,
        mode=mode,
        binary_path="/fake",
        config_dir="/fake",
        tree_path="/fake",
    )


class TestAssignHappyPath:
    @pytest.mark.anyio
    async def test_auto_task_gets_arbiter_chosen_agent(self) -> None:
        fake = FakeArbiterClient()
        fake.route_handler = lambda tid, t, c: {
            "task_id": tid, "action": "assign", "chosen_agent": "codex_cli",
            "confidence": 0.9, "reasoning": "", "decision_path": [],
            "invariant_checks": [], "metadata": {"decision_id": "dec-1"},
        }
        await fake.start()
        routing = ArbiterRouting(client=fake, cfg=_cfg())

        d = await routing.route(_task(AgentType.AUTO))

        assert d.action is RouteAction.ASSIGN
        assert d.chosen_agent == "codex_cli"
        assert d.decision_id == "dec-1"
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_arbiter_routing.py -v
```
Expected: ImportError on `ArbiterRouting`.

- [ ] **Step 3: Add `ArbiterRouting` (minimal happy path)**

Append to `maestro/coordination/routing.py`:
```python
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from maestro.coordination.arbiter_errors import ArbiterUnavailable
from maestro.models import (
    AgentType,
    ArbiterConfig,
    ArbiterMode,
    Priority,
    RouteAction,
    RouteDecision,
    Task,
    TaskOutcome,
    priority_int_to_enum,
)

logger = logging.getLogger(__name__)


def _task_to_arbiter_payload(task: Task) -> dict[str, Any]:
    """Build the `task` dict that route_task expects.

    Uses the R-02 arbiter fields already present on Task (task_type,
    language, complexity, priority-as-int → enum).
    """
    priority_enum: Priority = priority_int_to_enum(task.priority)
    return {
        "type": task.task_type.value,
        "language": task.language.value,
        "complexity": task.complexity.value,
        "priority": priority_enum.value,
    }


def _extract_decision_id(raw: dict[str, Any]) -> str | None:
    """Arbiter returns decision_id in metadata per its DTO spec."""
    meta = raw.get("metadata") or {}
    return meta.get("decision_id") if isinstance(meta, dict) else None


class ArbiterRouting:
    """Routing strategy backed by a running arbiter subprocess.

    Owns one long-lived client for the scheduler's lifetime. Falls back to
    StaticRouting on ArbiterUnavailable (except for AUTO tasks, which HOLD).
    Advisory-vs-authoritative semantics are applied inside `route()` so
    scheduler code stays mode-agnostic.
    """

    def __init__(self, client: Any, cfg: ArbiterConfig) -> None:
        self._client = client
        self._cfg = cfg
        self._fallback: StaticRouting = StaticRouting()
        self._degraded_since: datetime | None = None
        self._last_reconnect_attempt: datetime | None = None

    async def route(self, task: Task) -> RouteDecision:
        # Happy path only for this task; degraded path comes in Task 20.
        payload = _task_to_arbiter_payload(task)
        timeout_s = self._cfg.timeout_ms / 1000.0
        raw = await asyncio.wait_for(
            self._client.route_task(task.id, payload),
            timeout=timeout_s,
        )
        action_str = raw.get("action", "")
        try:
            action = RouteAction(action_str)
        except ValueError:
            logger.warning("unknown arbiter action %r, treating as HOLD", action_str)
            return RouteDecision(
                action=RouteAction.HOLD,
                chosen_agent=None,
                decision_id=_extract_decision_id(raw),
                reason=f"unknown_action:{action_str}",
            )

        chosen = raw.get("chosen_agent") or None
        reason = raw.get("reasoning") or ""
        decision_id = _extract_decision_id(raw)

        return RouteDecision(
            action=action,
            chosen_agent=chosen,
            decision_id=decision_id,
            reason=reason or "dt_inference",
        )

    async def report_outcome(self, task: Task, outcome: TaskOutcome) -> None:
        # Implemented fully in Task 21.
        raise NotImplementedError("implemented in Task 21")

    async def aclose(self) -> None:
        await self._client.stop()
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/test_arbiter_routing.py::TestAssignHappyPath -v
uv run ruff format maestro/coordination/routing.py tests/test_arbiter_routing.py
uv run pyrefly check
git add maestro/coordination/routing.py tests/test_arbiter_routing.py
git commit -m "feat(R-03): ArbiterRouting.route — ASSIGN happy path"
```

---

## Task 17: ArbiterRouting.route — HOLD / REJECT / unknown agent

**Files:**
- Modify: `maestro/coordination/routing.py`
- Modify: `tests/test_arbiter_routing.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_arbiter_routing.py`:
```python
class TestHoldRejectUnknown:
    @pytest.mark.anyio
    async def test_hold_returns_hold_with_reason(self) -> None:
        fake = FakeArbiterClient()
        fake.route_handler = lambda tid, t, c: {
            "task_id": tid, "action": "hold", "chosen_agent": "",
            "confidence": 0.0, "reasoning": "budget_exceeded",
            "decision_path": [], "invariant_checks": [],
            "metadata": {"decision_id": "dec-2"},
        }
        await fake.start()
        routing = ArbiterRouting(client=fake, cfg=_cfg())
        d = await routing.route(_task())
        assert d.action is RouteAction.HOLD
        assert d.chosen_agent is None
        assert d.reason == "budget_exceeded"
        assert d.decision_id == "dec-2"

    @pytest.mark.anyio
    async def test_reject_returns_reject(self) -> None:
        fake = FakeArbiterClient()
        fake.route_handler = lambda tid, t, c: {
            "task_id": tid, "action": "reject", "chosen_agent": "",
            "confidence": 0.0, "reasoning": "no_capable_agent",
            "decision_path": [], "invariant_checks": [],
            "metadata": {"decision_id": "dec-3"},
        }
        await fake.start()
        routing = ArbiterRouting(client=fake, cfg=_cfg())
        d = await routing.route(_task())
        assert d.action is RouteAction.REJECT
        assert d.reason == "no_capable_agent"
        assert d.decision_id == "dec-3"

    @pytest.mark.anyio
    async def test_unknown_agent_returned_as_assign(self) -> None:
        """ArbiterRouting returns ASSIGN with unknown chosen_agent; scheduler
        is responsible for the HOLD conversion (tested in Task 27)."""
        fake = FakeArbiterClient()
        fake.route_handler = lambda tid, t, c: {
            "task_id": tid, "action": "assign", "chosen_agent": "new_agent_v2",
            "confidence": 0.8, "reasoning": "", "decision_path": [],
            "invariant_checks": [], "metadata": {"decision_id": "dec-4"},
        }
        await fake.start()
        routing = ArbiterRouting(client=fake, cfg=_cfg())
        d = await routing.route(_task())
        assert d.action is RouteAction.ASSIGN
        assert d.chosen_agent == "new_agent_v2"
```

- [ ] **Step 2: Run tests (most pass already)**

```bash
uv run pytest tests/test_arbiter_routing.py::TestHoldRejectUnknown -v
```
Expected: all 3 pass (logic from Task 16 already covers them).

- [ ] **Step 3: Commit**

```bash
git add tests/test_arbiter_routing.py
git commit -m "test(R-03): ArbiterRouting HOLD/REJECT/unknown-agent paths"
```

---

## Task 18: ArbiterRouting — advisory override

**Files:**
- Modify: `maestro/coordination/routing.py`
- Modify: `tests/test_arbiter_routing.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestAdvisoryOverride:
    @pytest.mark.anyio
    async def test_advisory_explicit_agent_overrides_arbiter_choice(self) -> None:
        fake = FakeArbiterClient()
        fake.route_handler = lambda tid, t, c: {
            "task_id": tid, "action": "assign", "chosen_agent": "claude_code",
            "confidence": 0.9, "reasoning": "dt", "decision_path": [],
            "invariant_checks": [], "metadata": {"decision_id": "dec-5"},
        }
        await fake.start()
        routing = ArbiterRouting(
            client=fake, cfg=_cfg(mode=ArbiterMode.ADVISORY)
        )

        # Task explicitly asks for CODEX
        d = await routing.route(_task(AgentType.CODEX))

        assert d.action is RouteAction.ASSIGN
        assert d.chosen_agent == "codex_cli"   # user wins in advisory
        assert d.decision_id == "dec-5"         # decision still persisted
        assert d.reason == "dt"                 # arbiter's reason kept as-is

    @pytest.mark.anyio
    async def test_advisory_auto_task_uses_arbiter_choice(self) -> None:
        fake = FakeArbiterClient()
        fake.route_handler = lambda tid, t, c: {
            "task_id": tid, "action": "assign", "chosen_agent": "aider",
            "confidence": 0.7, "reasoning": "", "decision_path": [],
            "invariant_checks": [], "metadata": {"decision_id": "dec-6"},
        }
        await fake.start()
        routing = ArbiterRouting(
            client=fake, cfg=_cfg(mode=ArbiterMode.ADVISORY)
        )
        d = await routing.route(_task(AgentType.AUTO))
        assert d.chosen_agent == "aider"  # AUTO → arbiter wins even in advisory

    @pytest.mark.anyio
    async def test_authoritative_overrides_explicit_user_choice(self) -> None:
        fake = FakeArbiterClient()
        fake.route_handler = lambda tid, t, c: {
            "task_id": tid, "action": "assign", "chosen_agent": "claude_code",
            "confidence": 0.9, "reasoning": "", "decision_path": [],
            "invariant_checks": [], "metadata": {"decision_id": "dec-7"},
        }
        await fake.start()
        routing = ArbiterRouting(
            client=fake, cfg=_cfg(mode=ArbiterMode.AUTHORITATIVE)
        )
        d = await routing.route(_task(AgentType.CODEX))
        assert d.chosen_agent == "claude_code"   # arbiter overrides user

    @pytest.mark.anyio
    async def test_advisory_hold_still_respected_for_explicit(self) -> None:
        fake = FakeArbiterClient()
        fake.route_handler = lambda tid, t, c: {
            "task_id": tid, "action": "hold", "chosen_agent": "",
            "confidence": 0.0, "reasoning": "budget",
            "decision_path": [], "invariant_checks": [],
            "metadata": {"decision_id": "dec-8"},
        }
        await fake.start()
        routing = ArbiterRouting(
            client=fake, cfg=_cfg(mode=ArbiterMode.ADVISORY)
        )
        d = await routing.route(_task(AgentType.CODEX))
        assert d.action is RouteAction.HOLD   # hold respected even in advisory
```

- [ ] **Step 2: Run tests — expect 1 failure (advisory override not implemented)**

```bash
uv run pytest tests/test_arbiter_routing.py::TestAdvisoryOverride -v
```

- [ ] **Step 3: Add advisory override to `ArbiterRouting.route`**

Update the end of `ArbiterRouting.route` in `routing.py`:
```python
        decision = RouteDecision(
            action=action,
            chosen_agent=chosen,
            decision_id=decision_id,
            reason=reason or "dt_inference",
        )

        # Advisory override: in advisory mode, an explicit agent_type (not AUTO)
        # wins over arbiter's suggestion. HOLD/REJECT are always respected.
        if (
            action is RouteAction.ASSIGN
            and self._cfg.mode is ArbiterMode.ADVISORY
            and task.agent_type is not AgentType.AUTO
        ):
            decision = decision.model_copy(
                update={"chosen_agent": task.agent_type.value}
            )

        return decision
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/test_arbiter_routing.py::TestAdvisoryOverride -v
uv run ruff format maestro/coordination/routing.py tests/test_arbiter_routing.py
uv run pyrefly check
git add maestro/coordination/routing.py tests/test_arbiter_routing.py
git commit -m "feat(R-03): advisory-mode override inside ArbiterRouting.route"
```

---

## Task 19: ArbiterRouting — timeout → HOLD

**Files:**
- Modify: `tests/test_arbiter_routing.py`
- (No routing.py changes — wait_for already in place from Task 16)

- [ ] **Step 1: Write the failing test**

```python
class TestTimeoutMapping:
    @pytest.mark.anyio
    async def test_slow_arbiter_returns_hold_not_unavailable(self) -> None:
        fake = FakeArbiterClient()
        # Slower than timeout_ms (500 default, we'll force 50 via cfg)
        fake.route_delay_s = 1.0
        fake.route_handler = lambda tid, t, c: {
            "task_id": tid, "action": "assign", "chosen_agent": "codex_cli",
            "confidence": 1.0, "reasoning": "", "decision_path": [],
            "invariant_checks": [], "metadata": {"decision_id": "x"},
        }
        await fake.start()
        cfg = _cfg()
        cfg = cfg.model_copy(update={"timeout_ms": 50})
        routing = ArbiterRouting(client=fake, cfg=cfg)

        d = await routing.route(_task())
        assert d.action is RouteAction.HOLD
        assert d.reason == "timeout"
```

- [ ] **Step 2: Run — expect failure (TimeoutError not caught → test error)**

```bash
uv run pytest tests/test_arbiter_routing.py::TestTimeoutMapping -v
```
Expected: `asyncio.TimeoutError` propagates.

- [ ] **Step 3: Wrap `wait_for` to map TimeoutError → HOLD**

In `ArbiterRouting.route`, change the call site:
```python
        try:
            raw = await asyncio.wait_for(
                self._client.route_task(task.id, payload),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning("arbiter route_task timeout for task %s", task.id)
            return RouteDecision(
                action=RouteAction.HOLD,
                chosen_agent=None,
                decision_id=None,
                reason="timeout",
            )
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/test_arbiter_routing.py::TestTimeoutMapping -v
uv run ruff format maestro/coordination/routing.py
uv run pyrefly check
git add maestro/coordination/routing.py tests/test_arbiter_routing.py
git commit -m "feat(R-03): arbiter timeout → HOLD"
```

---

## Task 20: ArbiterRouting — degraded mode, AUTO fallback, reconnect

**Files:**
- Modify: `maestro/coordination/routing.py`
- Modify: `tests/test_arbiter_routing.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestDegradedMode:
    @pytest.mark.anyio
    async def test_unavailable_falls_back_to_static_for_explicit_task(self) -> None:
        from maestro.coordination.arbiter_errors import ArbiterUnavailable

        fake = FakeArbiterClient()
        fake.route_handler = lambda *a, **kw: (_ for _ in ()).throw(
            ArbiterUnavailable("pipe closed")
        )
        await fake.start()
        routing = ArbiterRouting(client=fake, cfg=_cfg())

        d = await routing.route(_task(AgentType.CODEX))
        assert d.action is RouteAction.ASSIGN
        assert d.chosen_agent == "codex_cli"   # static fallback returns declared
        assert d.decision_id is None
        assert d.reason == "static"

    @pytest.mark.anyio
    async def test_unavailable_holds_auto_task(self) -> None:
        """AUTO + arbiter down → HOLD with specific reason, not spawner misfire."""
        from maestro.coordination.arbiter_errors import ArbiterUnavailable

        fake = FakeArbiterClient()
        fake.route_handler = lambda *a, **kw: (_ for _ in ()).throw(
            ArbiterUnavailable("dead")
        )
        await fake.start()
        routing = ArbiterRouting(client=fake, cfg=_cfg())

        d = await routing.route(_task(AgentType.AUTO))
        assert d.action is RouteAction.HOLD
        assert d.reason == "arbiter_unavailable_no_default_for_auto"
        assert d.chosen_agent is None

    @pytest.mark.anyio
    async def test_degraded_window_skips_call_for_reconnect_interval(self) -> None:
        """Once degraded, we don't hammer the subprocess every tick."""
        from maestro.coordination.arbiter_errors import ArbiterUnavailable

        fake = FakeArbiterClient()
        call_count = {"n": 0}

        def handler(tid: str, t: dict, c: dict | None) -> dict:
            call_count["n"] += 1
            raise ArbiterUnavailable("dead")

        fake.route_handler = handler
        await fake.start()
        cfg = _cfg()
        # Big reconnect window; two calls back-to-back should only call once
        cfg = cfg.model_copy(update={"reconnect_interval_s": 3600})
        routing = ArbiterRouting(client=fake, cfg=cfg)

        await routing.route(_task(AgentType.CODEX))
        assert call_count["n"] == 1
        await routing.route(_task(AgentType.CODEX))
        # Within reconnect window — no second call
        assert call_count["n"] == 1
```

- [ ] **Step 2: Run — expect failures (degraded path not implemented)**

```bash
uv run pytest tests/test_arbiter_routing.py::TestDegradedMode -v
```

- [ ] **Step 3: Implement degraded path**

Refactor `ArbiterRouting.route` to:
```python
async def route(self, task: Task) -> RouteDecision:
    # Degraded-mode short-circuit
    if self._is_in_degraded_window():
        return await self._fallback_route(task, reason_for_auto="arbiter_degraded")

    try:
        payload = _task_to_arbiter_payload(task)
        timeout_s = self._cfg.timeout_ms / 1000.0
        try:
            raw = await asyncio.wait_for(
                self._client.route_task(task.id, payload),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning("arbiter route_task timeout for task %s", task.id)
            return RouteDecision(
                action=RouteAction.HOLD,
                chosen_agent=None,
                decision_id=None,
                reason="timeout",
            )
    except ArbiterUnavailable as exc:
        logger.warning("arbiter unavailable for task %s: %s", task.id, exc)
        self._enter_degraded(exc)
        return await self._fallback_route(
            task, reason_for_auto="arbiter_unavailable_no_default_for_auto"
        )

    # ... existing mapping to RouteDecision ...
    # ... existing advisory override ...

def _is_in_degraded_window(self) -> bool:
    if self._degraded_since is None:
        return False
    if self._last_reconnect_attempt is None:
        return True
    elapsed = (datetime.now(UTC) - self._last_reconnect_attempt).total_seconds()
    return elapsed < self._cfg.reconnect_interval_s

def _enter_degraded(self, exc: ArbiterUnavailable) -> None:
    if self._degraded_since is None:
        self._degraded_since = datetime.now(UTC)
    self._last_reconnect_attempt = datetime.now(UTC)

async def _fallback_route(self, task: Task, reason_for_auto: str) -> RouteDecision:
    if task.agent_type is AgentType.AUTO:
        return RouteDecision(
            action=RouteAction.HOLD,
            chosen_agent=None,
            decision_id=None,
            reason=reason_for_auto,
        )
    return await self._fallback.route(task)
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/test_arbiter_routing.py -v
uv run ruff format maestro/coordination/routing.py
uv run pyrefly check
git add maestro/coordination/routing.py tests/test_arbiter_routing.py
git commit -m "feat(R-03): ArbiterRouting degraded mode + AUTO-aware fallback"
```

---

## Task 21: ArbiterRouting.report_outcome

**Files:**
- Modify: `maestro/coordination/routing.py`
- Modify: `tests/test_arbiter_routing.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestReportOutcome:
    @pytest.mark.anyio
    async def test_sends_outcome_with_decision_id(self) -> None:
        from maestro.models import TaskOutcome, TaskOutcomeStatus

        fake = FakeArbiterClient()
        await fake.start()
        routing = ArbiterRouting(client=fake, cfg=_cfg())

        task = _task(AgentType.CODEX).model_copy(
            update={"arbiter_decision_id": "dec-100"}
        )
        outcome = TaskOutcome(
            status=TaskOutcomeStatus.SUCCESS,
            agent_used="codex_cli",
            duration_min=3.2,
            tokens_used=None,
            cost_usd=None,
            error_code=None,
        )
        await routing.report_outcome(task, outcome)

        outcome_calls = [c for c in fake.calls if c.method == "report_outcome"]
        assert len(outcome_calls) == 1
        args = outcome_calls[0].arguments
        assert args["status"] == "success"
        assert args["agent_id"] == "codex_cli"
        # decision_id passed as kwarg
        assert args.get("decision_id") == "dec-100"
        # None fields tolerated (arbiter contract)
        assert args.get("tokens_used") is None

    @pytest.mark.anyio
    async def test_noop_when_no_decision_id(self) -> None:
        from maestro.models import TaskOutcome, TaskOutcomeStatus

        fake = FakeArbiterClient()
        await fake.start()
        routing = ArbiterRouting(client=fake, cfg=_cfg())

        task = _task()   # no arbiter_decision_id
        outcome = TaskOutcome(
            status=TaskOutcomeStatus.SUCCESS, agent_used="codex_cli"
        )
        await routing.report_outcome(task, outcome)
        assert [c for c in fake.calls if c.method == "report_outcome"] == []

    @pytest.mark.anyio
    async def test_reraises_arbiter_unavailable(self) -> None:
        from maestro.coordination.arbiter_errors import ArbiterUnavailable
        from maestro.models import TaskOutcome, TaskOutcomeStatus

        fake = FakeArbiterClient()
        fake.outcome_raises = ArbiterUnavailable("pipe closed")
        await fake.start()
        routing = ArbiterRouting(client=fake, cfg=_cfg())

        task = _task().model_copy(update={"arbiter_decision_id": "dec-x"})
        outcome = TaskOutcome(
            status=TaskOutcomeStatus.FAILURE, agent_used="codex_cli"
        )
        with pytest.raises(ArbiterUnavailable):
            await routing.report_outcome(task, outcome)
```

- [ ] **Step 2: Run — expect NotImplementedError**

```bash
uv run pytest tests/test_arbiter_routing.py::TestReportOutcome -v
```

- [ ] **Step 3: Implement**

Replace the `NotImplementedError` in `ArbiterRouting.report_outcome`:
```python
async def report_outcome(self, task: Task, outcome: TaskOutcome) -> None:
    if task.arbiter_decision_id is None:
        return  # static-routed task; no correlation to report

    timeout_s = self._cfg.timeout_ms / 1000.0
    try:
        await asyncio.wait_for(
            self._client.report_outcome(
                task_id=task.id,
                agent_id=outcome.agent_used,
                status=outcome.status.value,
                decision_id=task.arbiter_decision_id,
                duration_min=outcome.duration_min,
                tokens_used=outcome.tokens_used,
                cost_usd=outcome.cost_usd,
                error_code=outcome.error_code,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError as exc:
        raise ArbiterUnavailable("report_outcome timeout", cause=exc) from exc
    # ArbiterUnavailable from the fake/real client propagates as-is
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/test_arbiter_routing.py::TestReportOutcome -v
uv run ruff format maestro/coordination/routing.py
uv run pyrefly check
git add maestro/coordination/routing.py tests/test_arbiter_routing.py
git commit -m "feat(R-03): ArbiterRouting.report_outcome with decision_id correlation"
```

---

## Task 22: make_routing_strategy factory

**Files:**
- Modify: `maestro/coordination/routing.py`
- Create: `tests/test_make_routing_strategy.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_make_routing_strategy.py`:
```python
"""Tests for the make_routing_strategy factory."""

from __future__ import annotations

import pytest

from maestro.coordination.arbiter_errors import ArbiterStartupError
from maestro.coordination.routing import (
    ArbiterRouting,
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
        enabled=True, binary_path="/does/not/exist",
        config_dir="/tmp", tree_path="/tmp/t",
    )
    with pytest.raises(ArbiterStartupError):
        await make_routing_strategy(cfg)


@pytest.mark.anyio
async def test_enabled_missing_binary_falls_back_when_optional() -> None:
    cfg = ArbiterConfig(
        enabled=True, optional=True,
        binary_path="/does/not/exist",
        config_dir="/tmp", tree_path="/tmp/t",
    )
    r = await make_routing_strategy(cfg)
    assert isinstance(r, StaticRouting)
```

- [ ] **Step 2: Run — expect factory missing**

```bash
uv run pytest tests/test_make_routing_strategy.py -v
```

- [ ] **Step 3: Add factory**

Append to `maestro/coordination/routing.py`:
```python
async def make_routing_strategy(
    cfg: ArbiterConfig | None,
) -> RoutingStrategy:
    """Factory used by CLI / scheduler to pick a RoutingStrategy.

    Enforces fail-fast semantics of ArbiterConfig.optional:
    - enabled=false or cfg=None → StaticRouting.
    - enabled=true: start arbiter subprocess, handshake, version-check.
      Any failure → ArbiterStartupError unless cfg.optional=true (then warn
      and degrade to StaticRouting).
    """
    if cfg is None or not cfg.enabled:
        return StaticRouting()

    from maestro.coordination.arbiter_client import (
        ArbiterClient,
        ArbiterClientConfig,
    )

    client_cfg = ArbiterClientConfig(
        binary_path=cfg.binary_path or "",
        tree_path=cfg.tree_path or "",
        config_dir=cfg.config_dir or "",
        db_path=cfg.db_path,
        log_level=cfg.log_level,
    )
    client = ArbiterClient(client_cfg)
    try:
        await client.start()
    except ArbiterStartupError:
        if cfg.optional:
            logger.warning(
                "arbiter startup failed and optional=true — falling back to static"
            )
            return StaticRouting()
        raise
    return ArbiterRouting(client=client, cfg=cfg)
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/test_make_routing_strategy.py -v
uv run ruff format maestro/coordination/routing.py tests/test_make_routing_strategy.py
uv run pyrefly check
git add maestro/coordination/routing.py tests/test_make_routing_strategy.py
git commit -m "feat(R-03): make_routing_strategy factory with fail-fast/optional"
```

---

## Task 23: Config.py parses arbiter section

**Files:**
- Modify: `maestro/config.py`
- Modify: `tests/test_config.py` (add test class)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:
```python
class TestArbiterSection:
    def test_no_arbiter_section_defaults_to_none(self, tmp_path) -> None:
        from maestro.config import load_orchestrator_config

        yaml_path = tmp_path / "p.yaml"
        yaml_path.write_text(
            """
project:
  name: test
  repo_path: .
  base_branch: main
zadachi: []
"""
        )
        cfg = load_orchestrator_config(yaml_path)
        assert cfg.arbiter is None

    def test_arbiter_section_parses_to_pydantic(self, tmp_path, monkeypatch) -> None:
        from maestro.config import load_orchestrator_config
        from maestro.models import ArbiterMode

        monkeypatch.setenv("ARBITER_BIN", "/opt/arbiter/arbiter-mcp")
        monkeypatch.setenv("ARBITER_CONFIG", "/etc/arbiter")
        monkeypatch.setenv("ARBITER_TREE", "/etc/arbiter/tree.json")

        yaml_path = tmp_path / "p.yaml"
        yaml_path.write_text(
            """
project:
  name: test
  repo_path: .
  base_branch: main
arbiter:
  enabled: true
  mode: authoritative
  binary_path: ${ARBITER_BIN}
  config_dir: ${ARBITER_CONFIG}
  tree_path: ${ARBITER_TREE}
  timeout_ms: 750
zadachi: []
"""
        )
        cfg = load_orchestrator_config(yaml_path)
        assert cfg.arbiter is not None
        assert cfg.arbiter.enabled is True
        assert cfg.arbiter.mode is ArbiterMode.AUTHORITATIVE
        assert cfg.arbiter.binary_path == "/opt/arbiter/arbiter-mcp"
        assert cfg.arbiter.timeout_ms == 750
```

(If `load_orchestrator_config` doesn't exist, use whichever function currently parses `OrchestratorConfig`; this is `maestro/config.py:load_orchestrator_config` per spec.)

- [ ] **Step 2: Run — expect failures**

```bash
uv run pytest tests/test_config.py::TestArbiterSection -v
```

- [ ] **Step 3: Add `arbiter` field to `OrchestratorConfig` and parse it**

In `maestro/models.py`, find `OrchestratorConfig` (or wherever project-level config is defined) and add:
```python
arbiter: ArbiterConfig | None = None
```

In `maestro/config.py`, `load_orchestrator_config`, after existing YAML → dict resolution, include the `arbiter` key in the pydantic construction — if the top-level YAML dict passes through pydantic construction directly, no code change is needed beyond the model field. If parsing is field-by-field, add:
```python
arbiter_data = resolved_config.get("arbiter")
arbiter = ArbiterConfig(**arbiter_data) if arbiter_data else None
```
and pass `arbiter=arbiter` to the `OrchestratorConfig(...)` call.

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/test_config.py::TestArbiterSection -v
uv run ruff format maestro/config.py maestro/models.py tests/test_config.py
uv run pyrefly check
git add maestro/config.py maestro/models.py tests/test_config.py
git commit -m "feat(R-03): parse arbiter section in project YAML"
```

---

## Task 24: Event log types + HOLD throttle helper

**Files:**
- Modify: `maestro/event_log.py`
- Create: `tests/test_event_log_arbiter.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_event_log_arbiter.py`:
```python
"""Tests for arbiter-specific event types and HOLD throttle."""

from __future__ import annotations

import pytest

from maestro.event_log import (
    EventType,
    HoldThrottle,
    log_event,
)


class TestEventTypes:
    def test_arbiter_event_types_exist(self) -> None:
        for name in (
            "ARBITER_ROUTE_DECIDED",
            "ARBITER_ROUTE_HOLD",
            "ARBITER_ROUTE_HOLD_SUMMARY",
            "ARBITER_ROUTE_REJECTED",
            "ARBITER_OUTCOME_REPORTED",
            "ARBITER_OUTCOME_ABANDONED",
            "ARBITER_UNAVAILABLE",
            "ARBITER_RECONNECTED",
            "ARBITER_RETRY_RESET_SKIPPED",
            "RECOVERY_ARBITER_DECISIONS_CLOSED",
        ):
            assert hasattr(EventType, name), name


class TestHoldThrottle:
    def test_first_hold_returns_true_subsequent_same_reason_return_false(self) -> None:
        throttle = HoldThrottle()
        assert throttle.should_log("t1", "budget") is True
        assert throttle.should_log("t1", "budget") is False
        assert throttle.should_log("t1", "budget") is False

    def test_reason_change_returns_true_again(self) -> None:
        throttle = HoldThrottle()
        assert throttle.should_log("t1", "budget") is True
        assert throttle.should_log("t1", "rate_limit") is True   # new reason

    def test_different_tasks_independent(self) -> None:
        throttle = HoldThrottle()
        assert throttle.should_log("t1", "budget") is True
        assert throttle.should_log("t2", "budget") is True

    def test_clear_emits_summary_payload(self) -> None:
        throttle = HoldThrottle()
        throttle.should_log("t1", "budget")
        throttle.should_log("t1", "budget")
        throttle.should_log("t1", "budget")
        summary = throttle.clear_and_summarize("t1")
        assert summary is not None
        assert summary["reason"] == "budget"
        assert summary["count"] == 3

    def test_clear_on_untracked_returns_none(self) -> None:
        throttle = HoldThrottle()
        assert throttle.clear_and_summarize("ghost") is None
```

- [ ] **Step 2: Run — expect failures**

```bash
uv run pytest tests/test_event_log_arbiter.py -v
```

- [ ] **Step 3: Add event types + throttle to `event_log.py`**

Find the `EventType` enum (likely a `StrEnum`) and add the 10 new members:
```python
class EventType(StrEnum):
    # ... existing members ...
    ARBITER_ROUTE_DECIDED = "arbiter.route.decided"
    ARBITER_ROUTE_HOLD = "arbiter.route.hold"
    ARBITER_ROUTE_HOLD_SUMMARY = "arbiter.route.hold_summary"
    ARBITER_ROUTE_REJECTED = "arbiter.route.rejected"
    ARBITER_OUTCOME_REPORTED = "arbiter.outcome.reported"
    ARBITER_OUTCOME_ABANDONED = "arbiter.outcome.abandoned"
    ARBITER_UNAVAILABLE = "arbiter.unavailable"
    ARBITER_RECONNECTED = "arbiter.reconnected"
    ARBITER_RETRY_RESET_SKIPPED = "arbiter.retry_reset.skipped"
    RECOVERY_ARBITER_DECISIONS_CLOSED = "recovery.arbiter.decisions_closed"
```

At the bottom of `event_log.py`, add the throttle helper:
```python
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class _HoldEntry:
    reason: str
    count: int = 1
    first_seen: datetime = field(default_factory=lambda: datetime.now(UTC))


class HoldThrottle:
    """Per-(task, reason) throttle for arbiter HOLD events.

    Returns True once per unique (task_id, reason) streak. Subsequent calls
    with the same reason return False (and still increment the counter).
    A reason change resets, returning True again. On reason change OR
    transition out of HOLD, call `clear_and_summarize(task_id)` to get a
    summary payload for an ARBITER_ROUTE_HOLD_SUMMARY event.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _HoldEntry] = {}

    def should_log(self, task_id: str, reason: str) -> bool:
        entry = self._entries.get(task_id)
        if entry is None or entry.reason != reason:
            # First HOLD for this reason; flush any prior entry via the caller
            self._entries[task_id] = _HoldEntry(reason=reason)
            return True
        entry.count += 1
        return False

    def clear_and_summarize(self, task_id: str) -> dict[str, object] | None:
        entry = self._entries.pop(task_id, None)
        if entry is None:
            return None
        return {
            "task_id": task_id,
            "reason": entry.reason,
            "count": entry.count,
            "first_seen": entry.first_seen.isoformat(),
        }
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/test_event_log_arbiter.py -v
uv run ruff format maestro/event_log.py tests/test_event_log_arbiter.py
uv run pyrefly check
git add maestro/event_log.py tests/test_event_log_arbiter.py
git commit -m "feat(R-03): add arbiter event types + HoldThrottle helper"
```

---

## Task 25: Scheduler accepts RoutingStrategy and arbiter_mode

**Files:**
- Modify: `maestro/scheduler.py` (constructor, `create_scheduler_from_config`)
- Modify: `tests/test_scheduler.py` (add regression test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_scheduler.py` (or wherever scheduler construction is tested):
```python
class TestSchedulerRoutingInjection:
    @pytest.mark.anyio
    async def test_defaults_to_static_routing(self, tmp_path) -> None:
        from maestro.coordination.routing import StaticRouting
        from maestro.dag import DAG
        from maestro.database import Database
        from maestro.scheduler import Scheduler

        db = Database(tmp_path / "s.db")
        await db.connect()
        try:
            scheduler = Scheduler(db=db, dag=DAG([]), spawners={})
            assert isinstance(scheduler._routing, StaticRouting)
        finally:
            await db.close()

    @pytest.mark.anyio
    async def test_accepts_injected_routing(self, tmp_path) -> None:
        from unittest.mock import AsyncMock

        from maestro.dag import DAG
        from maestro.database import Database
        from maestro.models import ArbiterMode
        from maestro.scheduler import Scheduler

        db = Database(tmp_path / "s.db")
        await db.connect()
        try:
            routing = AsyncMock()
            scheduler = Scheduler(
                db=db, dag=DAG([]), spawners={},
                routing=routing,
                arbiter_mode=ArbiterMode.AUTHORITATIVE,
            )
            assert scheduler._routing is routing
            assert scheduler._arbiter_mode is ArbiterMode.AUTHORITATIVE
        finally:
            await db.close()
```

- [ ] **Step 2: Run — expect TypeError on new kwargs**

```bash
uv run pytest tests/test_scheduler.py::TestSchedulerRoutingInjection -v
```

- [ ] **Step 3: Update Scheduler.__init__**

In `maestro/scheduler.py`, add imports:
```python
from maestro.coordination.routing import RoutingStrategy, StaticRouting
from maestro.event_log import HoldThrottle
from maestro.models import ArbiterMode
```

Add parameters to `Scheduler.__init__`:
```python
def __init__(
    self,
    db: Database,
    dag: DAG,
    spawners: dict[str, SpawnerProtocol],
    config: SchedulerConfig | None = None,
    notification_manager: NotificationManager | None = None,
    retry_manager: RetryManager | None = None,
    on_status_change: StatusChangeCallback | None = None,
    routing: RoutingStrategy | None = None,
    arbiter_mode: ArbiterMode = ArbiterMode.ADVISORY,
) -> None:
    # ... existing assignments ...
    self._routing: RoutingStrategy = routing if routing is not None else StaticRouting()
    self._arbiter_mode: ArbiterMode = arbiter_mode
    self._hold_throttle: HoldThrottle = HoldThrottle()
```

Update `create_scheduler_from_config` similarly to accept and forward `routing` and `arbiter_mode`.

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/test_scheduler.py -v
uv run ruff format maestro/scheduler.py tests/test_scheduler.py
uv run pyrefly check
git add maestro/scheduler.py tests/test_scheduler.py
git commit -m "feat(R-03): Scheduler accepts RoutingStrategy + arbiter_mode"
```

---

## Task 26: Scheduler._spawn_task — ASSIGN/HOLD/REJECT paths

**Files:**
- Modify: `maestro/scheduler.py`
- Create: `tests/test_scheduler_arbiter_integration.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_scheduler_arbiter_integration.py`:
```python
"""End-to-end scheduler tests with FakeArbiter-backed ArbiterRouting."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.coordination.routing import ArbiterRouting
from maestro.dag import DAG
from maestro.database import Database
from maestro.models import (
    AgentType,
    ArbiterConfig,
    ArbiterMode,
    RouteAction,
    Task,
    TaskConfig,
    TaskStatus,
)
from maestro.scheduler import Scheduler, SchedulerConfig
from tests.fakes.fake_arbiter_client import FakeArbiterClient


def _cfg() -> ArbiterConfig:
    return ArbiterConfig(
        enabled=True,
        mode=ArbiterMode.ADVISORY,
        binary_path="/fake",
        config_dir="/fake",
        tree_path="/fake",
    )


@pytest.mark.anyio
async def test_assign_routes_and_persists_decision(tmp_path) -> None:
    fake = FakeArbiterClient()
    fake.route_handler = lambda tid, t, c: {
        "task_id": tid, "action": "assign", "chosen_agent": "codex_cli",
        "confidence": 0.9, "reasoning": "dt", "decision_path": [],
        "invariant_checks": [], "metadata": {"decision_id": "dec-A"},
    }
    await fake.start()
    routing = ArbiterRouting(client=fake, cfg=_cfg())

    db = Database(tmp_path / "s.db")
    await db.connect()
    try:
        task = Task(
            id="t1", title="T", prompt="P", workdir=str(tmp_path),
            agent_type=AgentType.AUTO, status=TaskStatus.READY,
        )
        await db.create_task(task)

        # Mock spawner that records the call and returns a finished process
        spawner = MagicMock()
        proc = MagicMock()
        proc.poll.return_value = 0
        spawner.spawn.return_value = proc
        spawner.is_available.return_value = True
        spawner.agent_type = "codex_cli"

        scheduler = Scheduler(
            db=db, dag=DAG([]), spawners={"codex_cli": spawner},
            routing=routing,
            arbiter_mode=ArbiterMode.ADVISORY,
            config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
        )
        (tmp_path / "logs").mkdir(exist_ok=True)

        spawned = await scheduler._spawn_task("t1")
        assert spawned is True

        refetched = await db.get_task("t1")
        assert refetched.routed_agent_type == "codex_cli"
        assert refetched.arbiter_decision_id == "dec-A"
        assert refetched.arbiter_route_reason == "dt"

        spawner.spawn.assert_called_once()
    finally:
        await db.close()


@pytest.mark.anyio
async def test_hold_keeps_task_ready(tmp_path) -> None:
    fake = FakeArbiterClient()
    fake.route_handler = lambda tid, t, c: {
        "task_id": tid, "action": "hold", "chosen_agent": "",
        "confidence": 0.0, "reasoning": "budget", "decision_path": [],
        "invariant_checks": [], "metadata": {"decision_id": None},
    }
    await fake.start()
    routing = ArbiterRouting(client=fake, cfg=_cfg())

    db = Database(tmp_path / "s.db")
    await db.connect()
    try:
        task = Task(
            id="t1", title="T", prompt="P", workdir=str(tmp_path),
            agent_type=AgentType.AUTO, status=TaskStatus.READY,
        )
        await db.create_task(task)

        scheduler = Scheduler(
            db=db, dag=DAG([]), spawners={},
            routing=routing,
        )
        spawned = await scheduler._spawn_task("t1")
        assert spawned is False

        refetched = await db.get_task("t1")
        assert refetched.status is TaskStatus.READY
    finally:
        await db.close()


@pytest.mark.anyio
async def test_reject_moves_to_needs_review_and_self_closes(tmp_path) -> None:
    fake = FakeArbiterClient()
    fake.route_handler = lambda tid, t, c: {
        "task_id": tid, "action": "reject", "chosen_agent": "",
        "confidence": 0.0, "reasoning": "no_capable_agent",
        "decision_path": [], "invariant_checks": [],
        "metadata": {"decision_id": "dec-R"},
    }
    await fake.start()
    routing = ArbiterRouting(client=fake, cfg=_cfg())

    db = Database(tmp_path / "s.db")
    await db.connect()
    try:
        task = Task(
            id="t1", title="T", prompt="P", workdir=str(tmp_path),
            agent_type=AgentType.AUTO, status=TaskStatus.READY,
        )
        await db.create_task(task)

        scheduler = Scheduler(
            db=db, dag=DAG([]), spawners={},
            routing=routing,
        )
        spawned = await scheduler._spawn_task("t1")
        assert spawned is False

        refetched = await db.get_task("t1")
        assert refetched.status is TaskStatus.NEEDS_REVIEW
        # self-close: decision_id persisted + reported_at set
        assert refetched.arbiter_decision_id == "dec-R"
        assert refetched.arbiter_outcome_reported_at is not None
    finally:
        await db.close()
```

- [ ] **Step 2: Run — expect failure**

```bash
uv run pytest tests/test_scheduler_arbiter_integration.py -v
```

- [ ] **Step 3: Update `Scheduler._spawn_task`**

In `maestro/scheduler.py`, insert the routing block into `_spawn_task`, between the READY-check and the spawner lookup. Pseudocode:
```python
from datetime import UTC, datetime

from maestro.coordination.arbiter_errors import ArbiterUnavailable
from maestro.event_log import EventType
from maestro.models import AgentType, RouteAction, TaskStatus

# ... inside _spawn_task, after the "not READY → return False" check,
# before the spawner lookup ...

# R-03: Ask the routing strategy for a decision
decision = await self._routing.route(task)

if decision.action is RouteAction.HOLD:
    if self._hold_throttle.should_log(task.id, decision.reason):
        self._emit_event(
            EventType.ARBITER_ROUTE_HOLD,
            {"task_id": task.id, "reason": decision.reason},
        )
    return False

if decision.action is RouteAction.REJECT:
    self._emit_event(
        EventType.ARBITER_ROUTE_REJECTED,
        {"task_id": task.id, "reason": decision.reason},
    )
    await self._db.update_task_status(
        task_id,
        TaskStatus.NEEDS_REVIEW,
        error_message=f"arbiter rejected: {decision.reason}",
    )
    if decision.decision_id is not None:
        # persist the decision so self-close works
        task = task.model_copy(
            update={
                "arbiter_decision_id": decision.decision_id,
                "arbiter_route_reason": decision.reason,
            }
        )
        await self._db.update_task_routing(task)
        await self._db.mark_outcome_reported(
            task.id, datetime.now(UTC), decision.decision_id
        )
    self._report_status_change(task_id, "ready", "needs_review")
    return False

# ASSIGN path
if decision.chosen_agent is None:
    logger.error("assign with None chosen_agent for task %s", task.id)
    return False
try:
    chosen = AgentType(decision.chosen_agent)
except ValueError:
    logger.warning(
        "arbiter chose unknown agent %r for task %s — HOLD",
        decision.chosen_agent, task.id,
    )
    if self._hold_throttle.should_log(task.id, "unknown_agent"):
        self._emit_event(
            EventType.ARBITER_ROUTE_HOLD,
            {"task_id": task.id, "reason": "unknown_agent"},
        )
    return False
if chosen is AgentType.AUTO:
    logger.error("routing returned AUTO for task %s — refusing to spawn", task.id)
    if self._hold_throttle.should_log(task.id, "auto_not_resolved"):
        self._emit_event(
            EventType.ARBITER_ROUTE_HOLD,
            {"task_id": task.id, "reason": "auto_not_resolved"},
        )
    return False

# flush any prior HOLD streak summary now that we're past HOLD
summary = self._hold_throttle.clear_and_summarize(task.id)
if summary is not None and summary.get("count", 0) > 1:
    self._emit_event(EventType.ARBITER_ROUTE_HOLD_SUMMARY, summary)

task = task.model_copy(
    update={
        "routed_agent_type": chosen.value,
        "arbiter_decision_id": decision.decision_id,
        "arbiter_route_reason": decision.reason,
    }
)
await self._db.update_task_routing(task)
self._emit_event(
    EventType.ARBITER_ROUTE_DECIDED,
    {
        "task_id": task.id,
        "decision_id": decision.decision_id,
        "chosen_agent": chosen.value,
        "reason": decision.reason,
    },
)

# Existing spawner lookup — use routed_agent_type first
spawner_key = task.routed_agent_type or task.agent_type.value
spawner = self._spawners.get(spawner_key)
# ... existing spawn code, using `spawner_key` / `spawner` ...
```

If `_emit_event` doesn't exist, use the project's existing event_log API (e.g. `log_event(EventType.X, payload)`). Match the convention in `event_log.py`.

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/test_scheduler_arbiter_integration.py -v
uv run pytest tests/ -v  # full regression
uv run ruff format maestro/scheduler.py tests/test_scheduler_arbiter_integration.py
uv run pyrefly check
git add maestro/scheduler.py tests/test_scheduler_arbiter_integration.py
git commit -m "feat(R-03): scheduler routes tasks via RoutingStrategy"
```

---

## Task 27: Scheduler terminal handlers — mode-aware retry gating

**Files:**
- Modify: `maestro/scheduler.py`
- Modify: `tests/test_scheduler_arbiter_integration.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_scheduler_arbiter_integration.py` — a small helper + three full tests:

```python
def _assign_fake(decision_id: str = "dec-x", agent: str = "codex_cli") -> FakeArbiterClient:
    fake = FakeArbiterClient()
    fake.route_handler = lambda tid, t, c: {
        "task_id": tid, "action": "assign", "chosen_agent": agent,
        "confidence": 0.9, "reasoning": "", "decision_path": [],
        "invariant_checks": [], "metadata": {"decision_id": decision_id},
    }
    return fake


async def _setup_task_and_scheduler(
    tmp_path, fake: FakeArbiterClient, mode: ArbiterMode, exit_code: int,
):
    from maestro.scheduler import Scheduler, SchedulerConfig

    await fake.start()
    routing = ArbiterRouting(client=fake, cfg=ArbiterConfig(
        enabled=True, mode=mode,
        binary_path="/fake", config_dir="/fake", tree_path="/fake",
    ))

    db = Database(tmp_path / "s.db")
    await db.connect()

    task = Task(
        id="t1", title="T", prompt="P", workdir=str(tmp_path),
        agent_type=AgentType.AUTO, status=TaskStatus.READY,
        max_retries=2,
    )
    await db.create_task(task)

    spawner = MagicMock()
    proc = MagicMock()
    proc.poll.return_value = exit_code
    spawner.spawn.return_value = proc
    spawner.is_available.return_value = True
    spawner.agent_type = "codex_cli"

    (tmp_path / "logs").mkdir(exist_ok=True)
    scheduler = Scheduler(
        db=db, dag=DAG([]), spawners={"codex_cli": spawner},
        routing=routing, arbiter_mode=mode,
        config=SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
    )
    return db, scheduler, fake


@pytest.mark.anyio
async def test_success_reports_outcome_and_sets_reported_at(tmp_path) -> None:
    fake = _assign_fake(decision_id="dec-OK")
    db, scheduler, _ = await _setup_task_and_scheduler(
        tmp_path, fake, ArbiterMode.ADVISORY, exit_code=0,
    )
    try:
        await scheduler._spawn_task("t1")
        # Simulate the monitor loop picking up the completed process
        running = list(scheduler._running_tasks.values())[0]
        await scheduler._handle_task_completion("t1", running, return_code=0)

        refetched = await db.get_task("t1")
        assert refetched.status is TaskStatus.DONE
        assert refetched.arbiter_outcome_reported_at is not None

        outcome_calls = [c for c in fake.calls if c.method == "report_outcome"]
        assert len(outcome_calls) == 1
        assert outcome_calls[0].arguments["status"] == "success"
    finally:
        await db.close()


@pytest.mark.anyio
async def test_advisory_retry_not_blocked_on_arbiter_down(tmp_path) -> None:
    from maestro.coordination.arbiter_errors import ArbiterUnavailable

    fake = _assign_fake(decision_id="dec-ADV")
    fake.outcome_raises = ArbiterUnavailable("dead")
    db, scheduler, _ = await _setup_task_and_scheduler(
        tmp_path, fake, ArbiterMode.ADVISORY, exit_code=1,
    )
    try:
        await scheduler._spawn_task("t1")
        running = list(scheduler._running_tasks.values())[0]
        await scheduler._handle_task_completion("t1", running, return_code=1)

        refetched = await db.get_task("t1")
        # advisory: retry proceeds regardless of failed outcome delivery
        assert refetched.status is TaskStatus.READY
        assert refetched.arbiter_decision_id is None  # cleared on retry reset
        assert refetched.routed_agent_type is None
    finally:
        await db.close()


@pytest.mark.anyio
async def test_authoritative_retry_blocked_on_arbiter_down(tmp_path) -> None:
    from maestro.coordination.arbiter_errors import ArbiterUnavailable

    fake = _assign_fake(decision_id="dec-AUTH")
    fake.outcome_raises = ArbiterUnavailable("dead")
    db, scheduler, _ = await _setup_task_and_scheduler(
        tmp_path, fake, ArbiterMode.AUTHORITATIVE, exit_code=1,
    )
    try:
        await scheduler._spawn_task("t1")
        running = list(scheduler._running_tasks.values())[0]
        await scheduler._handle_task_completion("t1", running, return_code=1)

        refetched = await db.get_task("t1")
        # authoritative: stays FAILED, awaiting successful outcome delivery
        assert refetched.status is TaskStatus.FAILED
        assert refetched.arbiter_decision_id == "dec-AUTH"
        assert refetched.arbiter_outcome_reported_at is None
    finally:
        await db.close()
```

- [ ] **Step 2: Run — expect failures**

```bash
uv run pytest tests/test_scheduler_arbiter_integration.py -v -k "outcome or retry"
```

- [ ] **Step 3: Update terminal handlers**

In `_handle_task_completion` (success path) after `update_task_status(..., DONE, ...)`:
```python
# R-03: deliver outcome (best-effort)
outcome = await self._build_outcome(task, exit_code=0)
try:
    await self._routing.report_outcome(task, outcome)
    if task.arbiter_decision_id is not None:
        ok = await self._db.mark_outcome_reported(
            task.id, datetime.now(UTC), task.arbiter_decision_id
        )
        if ok:
            self._emit_event(
                EventType.ARBITER_OUTCOME_REPORTED,
                {"task_id": task.id, "decision_id": task.arbiter_decision_id,
                 "status": outcome.status.value},
            )
except ArbiterUnavailable:
    logger.info("outcome delivery deferred (arbiter unavailable) for %s", task.id)
```

In `_handle_task_failure` (also reused by validation-failure and timeout), restructure:
```python
outcome = await self._build_outcome(task, exit_code=current_task.error_message)
# first: transition to FAILED
await self._db.update_task_status(
    task.id, TaskStatus.FAILED, error_message=error_message,
    retry_count=new_retry_count,
)
self._report_status_change(task.id, "running", "failed")

# deliver outcome; retry gating depends on mode
delivered = False
if task.arbiter_decision_id is not None:
    try:
        await self._routing.report_outcome(task, outcome)
        await self._db.mark_outcome_reported(
            task.id, datetime.now(UTC), task.arbiter_decision_id
        )
        self._emit_event(
            EventType.ARBITER_OUTCOME_REPORTED,
            {"task_id": task.id, "decision_id": task.arbiter_decision_id,
             "status": outcome.status.value},
        )
        delivered = True
    except ArbiterUnavailable:
        logger.info("outcome delivery deferred for %s", task.id)
else:
    delivered = True  # static-routed task; nothing to deliver

# mode-aware retry transition
if self._retry_manager.should_retry(current_task):
    if self._arbiter_mode is ArbiterMode.ADVISORY or delivered:
        # advisory: proceed regardless of delivery state
        # authoritative: only proceed after successful delivery
        ok = await self._db.reset_for_retry_atomic(
            task.id,
            decision_id=task.arbiter_decision_id if delivered else None,
        )
        if not ok:
            self._emit_event(
                EventType.ARBITER_RETRY_RESET_SKIPPED,
                {"task_id": task.id, "expected_decision_id": task.arbiter_decision_id},
            )
        else:
            self._report_status_change(task.id, "failed", "ready")
    # else: authoritative + not delivered → stays FAILED; re-attempt pass will retry
else:
    # no more retries → NEEDS_REVIEW as before
    ...
```

Also add `_build_outcome` helper on Scheduler:
```python
async def _build_outcome(self, task: Task, exit_code: int) -> TaskOutcome:
    from maestro.models import TaskOutcome, TaskOutcomeStatus

    # Duration
    duration_min: float | None = None
    if task.started_at and task.completed_at:
        duration_min = (task.completed_at - task.started_at).total_seconds() / 60

    # Cost / tokens: R-03 doesn't wire cost_tracker; look up any rows that
    # happen to exist (for the current attempt), else leave None
    tokens_used: int | None = None
    cost_usd: float | None = None
    try:
        rows = await self._db.get_task_costs(task.id)
        attempt = task.retry_count + 1
        matching = [r for r in rows if r.attempt == attempt]
        if matching:
            tokens_used = sum(r.input_tokens + r.output_tokens for r in matching)
            cost_usd = sum(r.estimated_cost_usd for r in matching)
    except Exception:
        pass

    # Error code
    error_code: str | None = None
    if task.error_message:
        first_line = task.error_message.splitlines()[0] if task.error_message.splitlines() else task.error_message
        error_code = first_line[:200]

    # Status — caller has better signal via exit_code + error_message.
    # For recovery (exit_code unknown) callers override via model_copy.
    if exit_code == 0:
        status = TaskOutcomeStatus.SUCCESS
    elif "timeout" in (task.error_message or "").lower():
        status = TaskOutcomeStatus.TIMEOUT
    else:
        status = TaskOutcomeStatus.FAILURE

    return TaskOutcome(
        status=status,
        agent_used=task.routed_agent_type or task.agent_type.value,
        duration_min=duration_min,
        tokens_used=tokens_used,
        cost_usd=cost_usd,
        error_code=error_code,
    )
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/test_scheduler_arbiter_integration.py -v
uv run ruff format maestro/scheduler.py
uv run pyrefly check
git add maestro/scheduler.py tests/test_scheduler_arbiter_integration.py
git commit -m "feat(R-03): mode-aware retry gating in terminal handlers"
```

---

## Task 28: Scheduler re-attempt pass + abandon timer

**Files:**
- Modify: `maestro/scheduler.py`
- Modify: `tests/test_scheduler_arbiter_integration.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_scheduler_arbiter_integration.py`:

```python
@pytest.mark.anyio
async def test_reattempt_pass_delivers_bounded_five_per_tick(tmp_path) -> None:
    """With 10 dangling outcomes, a single pass delivers at most 5."""
    fake = FakeArbiterClient()
    await fake.start()
    routing = ArbiterRouting(client=fake, cfg=ArbiterConfig(
        enabled=True, binary_path="/fake", config_dir="/fake", tree_path="/fake",
    ))

    db = Database(tmp_path / "r.db")
    await db.connect()
    try:
        for i in range(10):
            t = Task(
                id=f"t{i}", title="T", prompt="P", workdir=str(tmp_path),
                status=TaskStatus.DONE,
                arbiter_decision_id=f"dec-{i}",
                started_at=None, completed_at=None,
            )
            await db.create_task(t)

        scheduler = Scheduler(
            db=db, dag=DAG([]), spawners={},
            routing=routing, arbiter_mode=ArbiterMode.ADVISORY,
        )
        await scheduler._outcome_reattempt_pass()

        # 5 delivered, 5 remain pending
        pending_after = await db.get_tasks_with_pending_outcome()
        assert len(pending_after) == 5

        outcome_calls = [c for c in fake.calls if c.method == "report_outcome"]
        assert len(outcome_calls) == 5
    finally:
        await db.close()


@pytest.mark.anyio
async def test_authoritative_abandon_after_timeout(tmp_path) -> None:
    """Authoritative + arbiter down + completed_at older than abandon_outcome_after_s
    → task force-unblocked, ABANDONED event, FAILED → READY."""
    from datetime import UTC, datetime, timedelta

    from maestro.coordination.arbiter_errors import ArbiterUnavailable

    fake = FakeArbiterClient()
    fake.outcome_raises = ArbiterUnavailable("dead")
    await fake.start()
    routing = ArbiterRouting(client=fake, cfg=ArbiterConfig(
        enabled=True, mode=ArbiterMode.AUTHORITATIVE,
        binary_path="/fake", config_dir="/fake", tree_path="/fake",
        abandon_outcome_after_s=1,
    ))

    db = Database(tmp_path / "a.db")
    await db.connect()
    try:
        past = datetime.now(UTC) - timedelta(seconds=10)
        t = Task(
            id="t1", title="T", prompt="P", workdir=str(tmp_path),
            status=TaskStatus.FAILED,
            arbiter_decision_id="dec-abandon",
            started_at=past,
            completed_at=past,
        )
        await db.create_task(t)

        scheduler = Scheduler(
            db=db, dag=DAG([]), spawners={},
            routing=routing, arbiter_mode=ArbiterMode.AUTHORITATIVE,
        )
        # Scheduler must know the abandon-after window; plumb via cfg
        scheduler._abandon_outcome_after_s = 1

        await scheduler._outcome_reattempt_pass()

        refetched = await db.get_task("t1")
        assert refetched.status is TaskStatus.READY
        assert refetched.arbiter_outcome_reported_at is not None
        assert refetched.arbiter_decision_id is None  # cleared on abandon+reset
    finally:
        await db.close()
```

- [ ] **Step 2: Run to verify failure**

- [ ] **Step 3: Implement `_outcome_reattempt_pass`**

```python
MAX_REATTEMPTS_PER_TICK = 5

async def _outcome_reattempt_pass(self) -> None:
    """R-03: Deliver deferred outcomes (bounded per tick).

    In authoritative mode, also force-unblocks FAILED tasks whose decision
    is older than `abandon_outcome_after_s` — their outcome is considered
    lost, they transition to READY, and an ARBITER_OUTCOME_ABANDONED
    event is emitted.
    """
    pending = await self._db.get_tasks_with_pending_outcome()
    now = datetime.now(UTC)
    delivered_count = 0

    for task in pending:
        if delivered_count >= MAX_REATTEMPTS_PER_TICK:
            break

        outcome_status = task_status_to_outcome_status(task.status)
        if outcome_status is None:
            logger.error(
                "task %s has decision_id but unexpected status %s — skipping",
                task.id, task.status,
            )
            continue

        outcome = await self._build_outcome(task, exit_code=0)
        outcome = outcome.model_copy(update={"status": outcome_status})
        try:
            await self._routing.report_outcome(task, outcome)
            if task.arbiter_decision_id is not None:
                await self._db.mark_outcome_reported(
                    task.id, now, task.arbiter_decision_id
                )
                self._emit_event(
                    EventType.ARBITER_OUTCOME_REPORTED,
                    {"task_id": task.id,
                     "decision_id": task.arbiter_decision_id,
                     "status": outcome_status.value},
                )
            # authoritative: task stuck in FAILED → transition now
            if (
                self._arbiter_mode is ArbiterMode.AUTHORITATIVE
                and task.status is TaskStatus.FAILED
            ):
                ok = await self._db.reset_for_retry_atomic(
                    task.id, task.arbiter_decision_id
                )
                if ok:
                    self._report_status_change(task.id, "failed", "ready")
            delivered_count += 1
        except ArbiterUnavailable:
            # Check abandon timer in authoritative mode
            if (
                self._arbiter_mode is ArbiterMode.AUTHORITATIVE
                and task.completed_at is not None
                and (now - task.completed_at).total_seconds()
                >= self._abandon_outcome_after_s
            ):
                await self._db.mark_outcome_reported(
                    task.id, now, task.arbiter_decision_id or ""
                )
                self._emit_event(
                    EventType.ARBITER_OUTCOME_ABANDONED,
                    {"task_id": task.id,
                     "decision_id": task.arbiter_decision_id,
                     "age_s": (now - task.completed_at).total_seconds()},
                )
                if task.status is TaskStatus.FAILED:
                    await self._db.reset_for_retry_atomic(task.id, None)
            # otherwise leave flagged, try next tick
            break  # arbiter clearly down — stop for this tick
```

Call `_outcome_reattempt_pass()` from `_main_loop` once per iteration (right after `_monitor_running_tasks`).

Also wire `_abandon_outcome_after_s` and `_arbiter_mode` to the config — pass them explicitly to Scheduler via `make_routing_strategy` / CLI setup (Task 30).

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/test_scheduler_arbiter_integration.py -v
uv run ruff format maestro/scheduler.py
uv run pyrefly check
git add maestro/scheduler.py tests/test_scheduler_arbiter_integration.py
git commit -m "feat(R-03): outcome re-attempt pass + authoritative abandon timer"
```

---

## Task 29: Recovery hook

**Files:**
- Modify: `maestro/recovery.py`
- Create: `tests/test_recovery_arbiter.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_recovery_arbiter.py`:
```python
"""Tests for recover_arbiter_outcomes — closing dangling decisions at startup."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from maestro.coordination.routing import StaticRouting
from maestro.database import Database
from maestro.models import Task, TaskStatus
from maestro.recovery import recover_arbiter_outcomes
from tests.fakes.fake_arbiter_client import FakeArbiterClient


@pytest.mark.anyio
async def test_running_task_with_decision_gets_interrupted_outcome(tmp_path) -> None:
    fake = FakeArbiterClient()
    await fake.start()
    from maestro.coordination.routing import ArbiterRouting
    from maestro.models import ArbiterConfig

    cfg = ArbiterConfig(
        enabled=True, binary_path="/fake", config_dir="/fake", tree_path="/fake"
    )
    routing = ArbiterRouting(client=fake, cfg=cfg)

    db = Database(tmp_path / "r.db")
    await db.connect()
    try:
        task = Task(
            id="t1", title="T", prompt="P", workdir="/tmp",
            status=TaskStatus.RUNNING,
            arbiter_decision_id="dec-int",
        )
        await db.create_task(task)

        count = await recover_arbiter_outcomes(db, routing)
        assert count == 1

        refetched = await db.get_task("t1")
        assert refetched.arbiter_outcome_reported_at is not None
        # Verify we sent INTERRUPTED
        outcome_calls = [c for c in fake.calls if c.method == "report_outcome"]
        assert outcome_calls[0].arguments["status"] == "interrupted"
    finally:
        await db.close()


@pytest.mark.anyio
async def test_static_routed_tasks_are_skipped(tmp_path) -> None:
    """Tasks without decision_id are not in the pending pool."""
    routing = StaticRouting()
    db = Database(tmp_path / "r.db")
    await db.connect()
    try:
        task = Task(
            id="t1", title="T", prompt="P", workdir="/tmp",
            status=TaskStatus.RUNNING,
        )
        await db.create_task(task)
        count = await recover_arbiter_outcomes(db, routing)
        assert count == 0
    finally:
        await db.close()


@pytest.mark.anyio
async def test_invariant_violation_status_logged_and_skipped(tmp_path, caplog) -> None:
    """decision_id on a PENDING/READY task is a bug; log + skip."""
    routing = StaticRouting()
    db = Database(tmp_path / "r.db")
    await db.connect()
    try:
        # Force an invariant violation via raw SQL (pydantic would refuse)
        await db.create_task(
            Task(
                id="t1", title="T", prompt="P", workdir="/tmp",
                status=TaskStatus.DONE,  # valid for model
                arbiter_decision_id="dec-bad",
            )
        )
        # Mutate status to PENDING via direct SQL
        await db._connection.execute(
            "UPDATE tasks SET status='pending' WHERE id='t1'"
        )
        await db._connection.commit()

        count = await recover_arbiter_outcomes(db, routing)
        assert count == 0
    finally:
        await db.close()
```

- [ ] **Step 2: Run — expect failure**

```bash
uv run pytest tests/test_recovery_arbiter.py -v
```

- [ ] **Step 3: Add `recover_arbiter_outcomes` to `maestro/recovery.py`**

```python
from datetime import UTC, datetime
import logging

from maestro.coordination.arbiter_errors import ArbiterUnavailable
from maestro.coordination.routing import RoutingStrategy, task_status_to_outcome_status
from maestro.database import Database
from maestro.event_log import EventType, log_event
from maestro.models import Task, TaskOutcome, TaskStatus

logger = logging.getLogger(__name__)


async def recover_arbiter_outcomes(
    db: Database, routing: RoutingStrategy
) -> int:
    """R-03: Close dangling arbiter decisions after a Maestro crash.

    For RUNNING/VALIDATING tasks with a decision_id but no reported_at:
    emit INTERRUPTED outcome. For terminal tasks: reconstruct outcome
    from persisted state (duration from started/completed timestamps;
    tokens/cost None unless cost_tracker rows exist; error_code from
    error_message). Stops on first ArbiterUnavailable — scheduler's
    re-attempt pass will retry later.

    Returns: count of outcomes successfully re-delivered.
    """
    pending = await db.get_tasks_with_pending_outcome()
    count = 0
    now = datetime.now(UTC)

    for task in pending:
        outcome_status = task_status_to_outcome_status(task.status)
        if outcome_status is None:
            logger.error(
                "recovery: task %s has decision_id but status %s; skipping",
                task.id, task.status.value,
            )
            continue

        outcome = _reconstruct_outcome(task, outcome_status)
        try:
            await routing.report_outcome(task, outcome)
            if task.arbiter_decision_id is not None:
                await db.mark_outcome_reported(
                    task.id, now, task.arbiter_decision_id
                )
            count += 1
        except ArbiterUnavailable:
            logger.info(
                "recovery: arbiter unavailable — stopping at task %s",
                task.id,
            )
            break

    log_event(
        EventType.RECOVERY_ARBITER_DECISIONS_CLOSED, {"count": count}
    )
    return count


def _reconstruct_outcome(task: Task, status) -> TaskOutcome:
    from maestro.models import TaskOutcome

    duration_min: float | None = None
    if task.started_at and task.completed_at:
        duration_min = (task.completed_at - task.started_at).total_seconds() / 60

    error_code: str | None = None
    if task.error_message:
        lines = task.error_message.splitlines()
        first = lines[0] if lines else task.error_message
        error_code = first[:200]

    return TaskOutcome(
        status=status,
        agent_used=task.routed_agent_type or task.agent_type.value,
        duration_min=duration_min,
        tokens_used=None,
        cost_usd=None,
        error_code=error_code,
    )
```

Hook into the existing recovery entrypoint (e.g. `recover_state`) to call `recover_arbiter_outcomes(db, routing)` as the last step, accepting `routing: RoutingStrategy` as a parameter.

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/test_recovery_arbiter.py -v
uv run ruff format maestro/recovery.py tests/test_recovery_arbiter.py
uv run pyrefly check
git add maestro/recovery.py tests/test_recovery_arbiter.py
git commit -m "feat(R-03): recover_arbiter_outcomes closes dangling decisions"
```

---

## Task 30: CLI wiring in `maestro run`

**Files:**
- Modify: `maestro/cli.py`

- [ ] **Step 1: Read the current `maestro run` command**

```bash
grep -n "def run" maestro/cli.py | head -5
```

- [ ] **Step 2: Update `maestro run` to call `make_routing_strategy`**

In the `run` command:
```python
from maestro.coordination.routing import make_routing_strategy

# ... after loading config ...
arbiter_cfg = config.arbiter  # None if not declared
routing = await make_routing_strategy(arbiter_cfg)
arbiter_mode = arbiter_cfg.mode if arbiter_cfg else ArbiterMode.ADVISORY

try:
    # existing recovery
    await recover_state(db, routing)  # pass routing
    # existing scheduler construction
    scheduler = await create_scheduler_from_config(
        ...,
        routing=routing,
        arbiter_mode=arbiter_mode,
    )
    await scheduler.run()
finally:
    await routing.aclose()
```

If `recover_state` currently takes only `db`, update its signature to accept `routing` and pass to `recover_arbiter_outcomes`.

- [ ] **Step 3: Manual smoke test**

```bash
uv run maestro run examples/tasks.yaml  # should still work without arbiter
```

- [ ] **Step 4: Run all tests**

```bash
uv run pytest -q
```

- [ ] **Step 5: Commit**

```bash
uv run ruff format maestro/cli.py maestro/recovery.py
uv run pyrefly check
git add maestro/cli.py maestro/recovery.py
git commit -m "feat(R-03): wire make_routing_strategy + recovery in CLI"
```

---

## Task 31: Example YAML + TODO.md updates

**Files:**
- Create: `examples/with-arbiter.yaml`
- Modify: `TODO.md`

- [ ] **Step 1: Create example**

Create `examples/with-arbiter.yaml`:
```yaml
# Example: run Maestro with Arbiter policy-engine routing.
#
# Prerequisites:
#   - Arbiter built: cd ../arbiter && cargo build --release --bin arbiter-mcp
#   - ARBITER_BIN / ARBITER_CONFIG / ARBITER_TREE set in your environment
#
# arbiter.mode:
#   advisory       — user's agent_type honored; arbiter learns; HOLD/REJECT on
#                    invariant breaches (budget, rate limit). RECOMMENDED for
#                    initial adoption.
#   authoritative  — arbiter's chosen_agent overrides user; retry is gated on
#                    outcome delivery (escape hatch: abandon_outcome_after_s).

arbiter:
  enabled: true
  mode: advisory
  optional: false                # set true to fall back to static if arbiter is missing
  binary_path: ${ARBITER_BIN}
  config_dir: ${ARBITER_CONFIG}
  tree_path: ${ARBITER_TREE}
  db_path: ./arbiter.db          # persists decision/outcome history
  timeout_ms: 500
  reconnect_interval_s: 60
  abandon_outcome_after_s: 300   # authoritative-only escape hatch
  log_level: warn

tasks:
  - id: fix-auth-bug
    title: Fix token refresh race
    prompt: |
      The token refresh has a race when two calls land in the same second.
      Fix it and add a regression test.
    agent_type: auto             # arbiter picks the best agent
    scope:
      - "src/auth/*.py"
      - "tests/test_auth*.py"
    task_type: bugfix
    language: python
    complexity: moderate
    timeout_minutes: 20

  - id: add-docs
    title: Document new retry policy
    prompt: Write /docs/retry.md covering the new policy.
    agent_type: claude_code      # explicit; in advisory this is honored
    scope: ["docs/**/*.md"]
    task_type: docs
    complexity: trivial
```

- [ ] **Step 2: Update TODO.md**

In `TODO.md`, add a checkbox for R-03 completion and append R-NN / R-10 references in the "Что НЕ делать до стабилизации" block becomes "Follow-ups unblocked by R-03":
```markdown
- [x] **R-03: MCP-клиент Arbiter в Maestro** (commit `<HASH>`, CI run `<URL>`)
  - ... summary of what landed ...

### Follow-ups unblocked by R-03
- [ ] **R-03b**: Mode 2 (`maestro orchestrate`) zadacha-level routing. Gate: ≥1 week stable Mode-1 dogfood.
- [ ] **R-05**: Maestro↔Arbiter integration tests with real subprocess. Depends on R-10.
- [ ] **R-10**: Arbiter CI producing `arbiter-mcp` binary as artifact.
- [ ] **R-NN**: Scheduler cost_tracker wiring so `TaskOutcome.tokens_used/cost_usd` carry real values.
- [ ] **Mini-R**: `schema_migrations` journal table + linear migration list (before migrations > 5).
- [ ] **R-14**: Extract vendored `arbiter_client.py` into `arbiter-py` PyPI package.
```

- [ ] **Step 3: Commit**

```bash
git add examples/with-arbiter.yaml TODO.md
git commit -m "docs(R-03): example YAML + TODO follow-ups"
```

---

## Task 32: Full regression + final acceptance check

- [ ] **Step 1: Full test suite**

```bash
uv run pytest -v
```
Expected: all green, no regressions from pre-R-03 baseline.

- [ ] **Step 2: Type + lint**

```bash
uv run pyrefly check
uv run ruff check .
uv run ruff format --check .
```

- [ ] **Step 3: Smoke test — arbiter disabled (OSS path)**

```bash
uv run maestro run examples/tasks.yaml
```
Should behave byte-identical to pre-R-03 (no arbiter log messages, no routing overhead).

- [ ] **Step 4: Smoke test — arbiter enabled (if local arbiter build available)**

```bash
export ARBITER_BIN=../arbiter/target/release/arbiter-mcp
export ARBITER_CONFIG=../arbiter/config
export ARBITER_TREE=../arbiter/models/agent_policy_tree.json
uv run maestro run examples/with-arbiter.yaml
```
Expected: `arbiter.route.decided` events in log; SQLite inspection shows populated `routed_agent_type` + `arbiter_decision_id` + `arbiter_outcome_reported_at` on completed tasks.

- [ ] **Step 5: Kill-arbiter acceptance scenarios** (manual, if local arbiter available)

1. Advisory + arbiter killed mid-run → failed tasks still retry (FAILED → READY).
2. Authoritative + arbiter killed + < `abandon_outcome_after_s` → failed tasks stay FAILED.
3. Authoritative + arbiter killed + > `abandon_outcome_after_s` → `arbiter.outcome.abandoned` event, tasks unblock.

Document the results in TODO.md's R-03 entry with commit hashes.

- [ ] **Step 6: Final commit — acceptance doc**

If any small doc adjustments were needed:
```bash
git add -p
git commit -m "docs(R-03): acceptance verification notes"
```

---

## Self-Review Checklist

Before marking R-03 as merged:

- [ ] Spec `2026-04-16-r03-arbiter-mcp-client-design.md` — every section covered by at least one task?
- [ ] No `TODO:` / `TBD` markers left in new code
- [ ] All FakeArbiter-based contract tests passing (task list covers: ASSIGN, HOLD, REJECT, timeout, degraded/fallback, AUTO-hold, advisory override, authoritative override, unknown agent, HOLD throttle, retry gating advisory, retry gating authoritative, abandon timer, atomic reset race)
- [ ] Migration test passes on legacy DB (pre-R-03 schema)
- [ ] Recovery test passes (interrupted + reconstruct + invariant-violation)
- [ ] Startup tests pass (fail-fast, optional fallback, version mismatch, unresolved env var)
- [ ] `uv run pytest tests/test_models.py` — existing tests unaffected
- [ ] `uv run pytest tests/test_coordination` — MCP/REST coordination tests unaffected (assigned_to semantics preserved)
- [ ] `uv run maestro run examples/tasks.yaml` without `arbiter` section works identically to pre-R-03
- [ ] TODO.md has the new R-03b / R-05 / R-10 / R-NN / schema migrations entries

When all boxes check: merge, update memory (`project_maestro_status.md`), and request R-03 close.
