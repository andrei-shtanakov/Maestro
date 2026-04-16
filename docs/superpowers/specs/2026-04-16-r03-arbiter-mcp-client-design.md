# R-03 Design: Arbiter MCP Client in Maestro

**Status:** approved (brainstorm 2026-04-16)
**Scope:** Mode 1 (scheduler) routing + persisted decision tracking + recovery outcome closing + mock-based contract test
**Effort:** L
**Depends on:** R-01 (done), R-02 (done)
**Blocks:** R-03b (Mode 2), R-05 (integration tests), eventually R-14 (shared type library)

---

## Goal

Integrate Maestro's scheduler with the Arbiter policy engine over MCP (JSON-RPC 2.0 over stdio). Scheduler routes each task through Arbiter before spawning, persists the decision, reports the outcome after terminal state, and closes dangling decisions on crash recovery. Arbiter is opt-in; the default OSS path is unchanged static routing.

## Non-goals (explicit out-of-scope, tracked as separate R-items)

- Mode 2 (`maestro orchestrate`) routing at zadacha level — **R-03b** (gate: R-03 merged + ≥1 week stable Mode-1 dogfood)
- Real arbiter subprocess in e2e tests — **R-05** (depends on **R-10** arbiter CI that produces `arbiter-mcp` binary artifact)
- Eval-driven routing validation — R-07 (depends on R-06b)
- Global `~/.maestro/arbiter.yaml` override — post-v0.1.0
- Authoritative as default mode — stays advisory until explicit config flip
- Arbiter metrics in Maestro dashboard — observability iteration

---

## Architecture

### New module layout

```
maestro/coordination/
├── arbiter_client.py      # vendored from arbiter@861534e, adapted (pydantic/logging/wrapping)
├── arbiter_errors.py      # ArbiterUnavailable, ArbiterStartupError
└── routing.py             # RoutingStrategy protocol + StaticRouting + ArbiterRouting + make_routing_strategy
```

`arbiter_errors.py` is separate so tests (and future consumers) can `import ArbiterUnavailable` without pulling the full vendored client.

### RoutingStrategy protocol

```python
from typing import Protocol
from enum import StrEnum
from pydantic import BaseModel
from maestro.models import Task

class RoutingStrategy(Protocol):
    async def route(self, task: Task) -> RouteDecision: ...
    async def report_outcome(self, task: Task, outcome: TaskOutcome) -> None: ...
    async def aclose(self) -> None: ...

class RouteAction(StrEnum):
    ASSIGN = "assign"
    HOLD = "hold"
    REJECT = "reject"

class RouteDecision(BaseModel, frozen=True):
    action: RouteAction
    chosen_agent: str | None       # None for HOLD / REJECT
    decision_id: str | None        # arbiter-provided correlation id for report_outcome
    reason: str                    # free-form, e.g. "budget_exceeded:daily"

class TaskOutcomeStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"    # emitted by recovery hook

class TaskOutcome(BaseModel):
    status: TaskOutcomeStatus
    agent_used: str                # may differ from chosen_agent if fallback kicked in
    duration_min: float | None
    tokens_used: int | None
    cost_usd: float | None
    error_code: str | None         # first line of error_message or None on success
```

### Implementations

**StaticRouting** — default, zero-config, OSS path.
- `route(task)` → `RouteDecision(action=ASSIGN, chosen_agent=task.agent_type, decision_id=None, reason="static")`
- `report_outcome` — noop
- `aclose` — noop
- Instantiated when `arbiter: None` or `arbiter.enabled: false`, and as the fallback target inside `ArbiterRouting`.

**ArbiterRouting** — owns one long-lived `ArbiterClient` subprocess for the Maestro process lifetime.
- Holds an internal `StaticRouting` instance as the fallback delegate.
- Tracks `_last_reconnect_attempt: datetime | None` and a `_degraded: bool` flag.
- `route(task)`:
  - If `_degraded and now - _last_reconnect_attempt < reconnect_interval_s` → delegate to static.
  - Else: try `client.route_task_typed(task_id, payload, constraints)` with `asyncio.wait_for(timeout_ms)`.
    - On success: map `RouteDecision` from typed DTO, handle unknown `chosen_agent` (see below).
    - On `ArbiterUnavailable` (broken pipe / timeout): log `arbiter.unavailable` event, set `_degraded`, delegate to static, schedule reconnect.
  - Advisory override happens inside `ArbiterRouting.route`, not in scheduler: if `cfg.mode == ADVISORY and task.agent_type != AgentType.AUTO` and `action == ASSIGN`, the returned `RouteDecision.chosen_agent` is rewritten to `task.agent_type.value`. `decision_id` and `reason` are preserved as-is. Scheduler code stays mode-agnostic.
- `report_outcome(task, outcome)`:
  - If `task.arbiter_decision_id is None` → noop (static-routed task).
  - Else: call `client.report_outcome(decision_id=..., ...)` with a single attempt (no internal retries), bounded by `timeout_ms`. On failure **raises `ArbiterUnavailable`** so the scheduler can decide whether to mark `arbiter_outcome_reported_at` and whether to block the retry transition. No in-method fallback for outcome delivery — correctness of arbiter's training signal trumps resilience.
- `aclose()` closes the subprocess cleanly (drain stdin, SIGTERM, wait).

### make_routing_strategy factory

```python
async def make_routing_strategy(
    cfg: ArbiterConfig | None,
) -> RoutingStrategy:
    if cfg is None or not cfg.enabled:
        return StaticRouting()
    try:
        client = ArbiterClient(cfg.to_client_config())
        await client.start()  # handshake + version check
        return ArbiterRouting(client, cfg)
    except ArbiterStartupError:
        if cfg.optional:
            logger.warning("arbiter startup failed, falling back to static")
            return StaticRouting()
        raise
```

---

## Data flow (Mode 1)

```
_spawn_task(task_id):
  task = await db.get_task(task_id)
  # ...existing approval/retry/workdir checks...
  decision = await routing.route(task)

  if decision.action == HOLD:
      log_event("arbiter.route.hold", ...)
      return False                        # next tick retries

  if decision.action == REJECT:
      log_event("arbiter.route.rejected", ...)
      await db.update_task_status(task_id, NEEDS_REVIEW,
                                  error_message=f"arbiter rejected: {decision.reason}")
      return False

  # action == ASSIGN
  try:
      chosen = AgentType(decision.chosen_agent)
  except ValueError:
      # unknown agent → treat as hold (config drift, not invariant violation)
      logger.warning("arbiter chose unknown agent %s, holding task %s",
                     decision.chosen_agent, task_id)
      log_event("arbiter.route.hold", reason="unknown_agent")
      return False

  task = task.model_copy(update={
      "assigned_to": chosen.value,                    # effective agent for this run
      "arbiter_decision_id": decision.decision_id,
      "arbiter_route_reason": decision.reason,
  })
  await db.update_task_routing(task)   # persist BEFORE spawn — crash safety
  log_event("arbiter.route.decided", ...)

  # spawner lookup uses assigned_to, falling back to agent_type
  spawner_key = task.assigned_to or task.agent_type.value
  spawner = self._spawners.get(spawner_key)
  # ...existing spawn flow...

terminal handler (success path, no retry needed):
  outcome = _build_outcome(task, exit_code, log_file, retry_count)
  # Transition RUNNING → DONE immediately; outcome delivery is best-effort
  await db.update_task_status(task_id, DONE, result_summary=...)
  try:
      await routing.report_outcome(task, outcome)
      await db.mark_outcome_reported(task.id, datetime.now(UTC))
      log_event("arbiter.outcome.reported", ...)
  except ArbiterUnavailable:
      # reported_at stays NULL — re-attempt pass delivers later
      pass

terminal handler (failure path, retry candidate):
  outcome = _build_outcome(task, exit_code, log_file, retry_count)
  # Transition RUNNING → FAILED first
  await db.update_task_status(task_id, FAILED, error_message=..., retry_count=...)
  try:
      await routing.report_outcome(task, outcome)
      await db.mark_outcome_reported(task.id, datetime.now(UTC))
      log_event("arbiter.outcome.reported", ...)
      if should_retry:
          await _transition_to_ready_and_clear_arbiter_fields(task_id)
  except ArbiterUnavailable:
      # task stays FAILED. Re-attempt pass delivers outcome, then drives FAILED → READY.
      pass
```

`_build_outcome` assembles `TaskOutcome` from persisted state:
- `duration_min` = `(completed_at - started_at).total_seconds() / 60` if both set, else `None`
- `tokens_used` / `cost_usd` = `SELECT SUM(input_tokens + output_tokens), SUM(estimated_cost_usd) FROM task_costs WHERE task_id=? AND attempt=?` (attempt = `retry_count + 1`)
- `error_code` = first line of `task.error_message` truncated to 200 chars, or `None` on success
- `agent_used` = `task.assigned_to or task.agent_type.value` — the spawner actually used. Falls back through `agent_type` for StaticRouting paths. May differ from `chosen_agent` if runtime fallback kicked in.

---

## Data model changes

### Task model (`maestro/models.py`)

Three new optional fields added to `Task`:

```python
arbiter_decision_id: str | None = None
arbiter_route_reason: str | None = None
arbiter_outcome_reported_at: datetime | None = None
```

No equivalent fields on `TaskConfig` — these are pure runtime state.

The existing `Task.assigned_to` column (already persisted, currently unused in scheduler mode) gains a concrete semantic: **the agent the scheduler actually used for this run**, set during route, cleared on retry-reset (so re-routing gets a fresh decision). `Task.agent_type` continues to reflect the user's declaration (`auto` / explicit), never mutated after `from_config`.

### SQLite migration

ALTER TABLE columns, idempotent via `PRAGMA table_info` check (pattern matches R-02's `_migrate_tasks_arbiter_columns`). New helper `_migrate_tasks_arbiter_routing` called from `Database.connect()`:

```sql
ALTER TABLE tasks ADD COLUMN arbiter_decision_id TEXT;
ALTER TABLE tasks ADD COLUMN arbiter_route_reason TEXT;
ALTER TABLE tasks ADD COLUMN arbiter_outcome_reported_at TIMESTAMP;
```

No new index in R-03. Recovery query runs once at startup; even at 10k tasks a full scan is <50ms.

### New Database methods

- `update_task_routing(task)` — single-row update writing `assigned_to`, `arbiter_decision_id`, `arbiter_route_reason`. Does **not** touch `agent_type` or `status`.
- Retry reset logic: terminal handlers (`_handle_task_failure`/`_handle_validation_failure`) must **deliver `report_outcome` successfully before transitioning FAILED → READY**. If `report_outcome` fails (`ArbiterUnavailable`), the task stays in FAILED with `arbiter_outcome_reported_at IS NULL`; the scheduler's bounded re-attempt pass (≤5 rows/tick) delivers on a later tick and then does the FAILED → READY transition. This prevents a concurrent retry from overwriting `arbiter_decision_id` while the previous attempt's outcome is still undelivered. Once transition to READY happens, `assigned_to`, `arbiter_decision_id`, `arbiter_route_reason` are cleared (new route gets fresh state); `arbiter_outcome_reported_at` is also cleared so the field is reused for the next attempt's tracking.

Future work (post-R-03): move per-attempt outcome tracking to a dedicated `task_attempts` table so outcomes for all historical attempts are permanently queryable. For R-03, only the latest attempt is tracked on the `tasks` row; successfully reported outcomes are flushed to arbiter's side and not queryable from Maestro DB.
- `mark_outcome_reported(task_id, ts)` — sets `arbiter_outcome_reported_at`.
- `get_tasks_with_pending_outcome()` — recovery helper.

### TaskOutcome / RouteDecision / RouteAction / TaskOutcomeStatus

Pydantic v2 models live in `maestro/models.py` next to the existing arbiter enums (`TaskType`, `Language`, `Complexity`, `Priority`). Keeps models.py as the single canon for pydantic.

### ArbiterConfig model

```python
class ArbiterMode(StrEnum):
    ADVISORY = "advisory"
    AUTHORITATIVE = "authoritative"

class ArbiterConfig(BaseModel):
    enabled: bool = False
    mode: ArbiterMode = ArbiterMode.ADVISORY
    optional: bool = False
    binary_path: str | None = None       # no monorepo default
    config_dir: str | None = None
    tree_path: str | None = None
    db_path: str | None = None
    timeout_ms: int = 100
    reconnect_interval_s: int = 60
    log_level: str = "warn"

    @model_validator(mode="after")
    def validate_required_when_enabled(self) -> Self:
        if self.enabled and not self.binary_path:
            raise ValueError(
                "arbiter.binary_path is required when arbiter.enabled=true. "
                "Set ARBITER_BIN env var or arbiter.binary_path in config."
            )
        # Same for config_dir, tree_path
        return self
```

`OrchestratorConfig` / project YAML parser gains optional `arbiter: ArbiterConfig | None = None` at top level.

---

## Configuration (YAML)

```yaml
# tasks.yaml (scheduler mode)
arbiter:
  enabled: true
  mode: advisory              # advisory | authoritative
  optional: false             # fail-fast by default
  binary_path: ${ARBITER_BIN}
  config_dir: ${ARBITER_CONFIG}
  tree_path: ${ARBITER_TREE}
  db_path: ./arbiter.db
  timeout_ms: 100
  reconnect_interval_s: 60
  log_level: warn

tasks:
  - id: ...
    agent_type: auto          # "auto" → chosen_agent authoritative
    # or
    agent_type: codex_cli     # explicit → advisory (learning + hold respected)
```

Env-var substitution uses the existing `${VAR:-default}` mechanism in `config.py`. No CLI flags.

`AgentType` gains a new sentinel value `AUTO = "auto"`. When `AgentType.AUTO` is present and `arbiter` is not enabled, `Task.from_config` should raise a validation error (fail-fast: "agent_type=auto requires arbiter.enabled=true").

---

## Advisory semantics (precise)

| `task.agent_type` | `arbiter.mode` | Effective chosen_agent | HOLD / REJECT respected |
| ----------------- | -------------- | ---------------------- | ----------------------- |
| `auto`            | advisory       | arbiter's `chosen_agent` | yes                   |
| `auto`            | authoritative  | arbiter's `chosen_agent` | yes                   |
| explicit (e.g. `codex_cli`) | advisory | explicit (arbiter's ignored) | yes (budget/invariant) |
| explicit          | authoritative  | arbiter's `chosen_agent` (overrides) | yes         |

In all cases, `route_task` is called when `arbiter.enabled=true`, so arbiter gets the learning signal. `decision_id` / `reason` are persisted in all cases.

---

## Error handling

Three disjoint error paths, each with its own exception type.

### ArbiterStartupError (fail-fast at `maestro run` start)

Raised from `make_routing_strategy` if any of:
1. `binary_path` does not exist or is not executable
2. Subprocess spawn fails
3. Handshake does not complete within 5s
4. `serverInfo.version` in `initialize` response != `ARBITER_MCP_REQUIRED_VERSION` pinned in vendored client

Behavior: CLI prints message + actionable hint, exits non-zero. If `arbiter.optional: true`, log warning and fall back to `StaticRouting` instead of exiting.

### ArbiterUnavailable (runtime degraded mode)

Raised by `ArbiterClient` on broken pipe, read timeout, JSON parse failure after MCP is established. `ArbiterRouting` catches this and:
- Delegates current call to static fallback
- Sets `_degraded = True`, `_last_reconnect_attempt = now`
- Logs `arbiter.unavailable` event once per transition
- On next `route()` call after `reconnect_interval_s`, tries to re-`start()` the client. On success: logs `arbiter.reconnected`, clears flag.

### HOLD / REJECT (normal responses, not errors)

These are `RouteDecision` values, not exceptions.

- `action=REJECT` from arbiter → task moves to `NEEDS_REVIEW` with `reason` in `error_message`.
- `action=HOLD` from arbiter → task stays `READY`, next scheduler tick retries the route call.
- `timeout_ms` exceeded → mapped to `HOLD` (not `FAILED`, not `ArbiterUnavailable` — assumption: arbiter is up but slow; we retry next tick).
- Unknown `chosen_agent` (not in `AgentType` enum) → mapped to `HOLD` with reason `unknown_agent`; logged as warning; `arbiter.route.hold` event fired.

---

## Recovery

### recovery.py additions

On Maestro startup, after existing recovery (reset stale RUNNING → READY, etc.), call `recover_arbiter_outcomes(db, routing)`:

```python
async def recover_arbiter_outcomes(db: Database, routing: RoutingStrategy) -> int:
    """Close dangling arbiter decisions after a Maestro crash.

    Finds tasks with arbiter_decision_id set but arbiter_outcome_reported_at
    NULL. For RUNNING tasks, emits INTERRUPTED outcome (mid-flight crash).
    For terminal tasks, reconstructs outcome from persisted state (we crashed
    between task completion and outcome delivery).

    Returns: count of outcomes re-delivered.
    """
    pending = await db.get_tasks_with_pending_outcome()
    for task in pending:
        if task.status == TaskStatus.RUNNING:
            outcome = TaskOutcome(status=INTERRUPTED, ...)
        else:
            outcome = _reconstruct_outcome(task)
        try:
            await routing.report_outcome(task, outcome)
            await db.mark_outcome_reported(task.id, datetime.now(UTC))
        except ArbiterUnavailable:
            # arbiter down during recovery — next scheduler tick will retry
            break
    return count
```

The scheduler main loop also runs a lightweight re-attempt pass once per tick for rows still flagged `arbiter_outcome_reported_at IS NULL`, bounded to **at most 5 rows per tick** so outcome delivery can never starve task scheduling. Rows remain flagged until successfully reported. This handles the "crash during recovery while arbiter is also down" case the user called out.

Event emitted on startup: `recovery.arbiter.decisions_closed(count=N)`.

---

## Event log types (new)

Added to `event_log.py`:

- `arbiter.route.decided` — fields: `task_id`, `decision_id`, `chosen_agent`, `reason`
- `arbiter.route.hold` — fields: `task_id`, `reason` (e.g. `"budget_exceeded"`, `"unknown_agent"`, `"timeout"`)
- `arbiter.route.rejected` — fields: `task_id`, `reason`
- `arbiter.outcome.reported` — fields: `task_id`, `decision_id`, `status`
- `arbiter.unavailable` — fields: `error`, `since` (first time only, not per-call)
- `arbiter.reconnected` — fields: `downtime_s`
- `recovery.arbiter.decisions_closed` — fields: `count`

---

## Vendoring discipline (`arbiter_client.py`)

File opens with:

```python
"""Arbiter MCP client (JSON-RPC 2.0 over stdio).

Vendored from arbiter@861534e (typed DTOs + E2E smoke test commit).
Maintainer: when re-vendoring, update ARBITER_VENDOR_COMMIT and confirm
ARBITER_MCP_REQUIRED_VERSION still matches arbiter's Cargo workspace
version. DO NOT modify DTO shapes, MCP method names, or subprocess
lifecycle logic; adapt only pydantic/logging/error-wrapping surface.
Extraction target: R-14 (arbiter-py PyPI package).
"""

ARBITER_VENDOR_COMMIT: str = "861534e"
ARBITER_MCP_REQUIRED_VERSION: str = "0.1.0"
```

**Adapt scope (allowed):**
- `@dataclass(frozen=True)` → `BaseModel(model_config=ConfigDict(frozen=True))` for DTOs
- `print` / `logging.getLogger(...)` → `maestro.event_log` + `structlog`-style structured events
- Bare exceptions wrapped: `BrokenPipeError` / `asyncio.TimeoutError` / subprocess errors → `ArbiterUnavailable`; `FileNotFoundError` / `PermissionError` / version-mismatch → `ArbiterStartupError`; MCP `action=reject` response does not raise — it's a normal return
- `pyrefly check` green

**Do not adapt:**
- DTO field names / shapes (wire format is the contract)
- MCP method names (`route_task`, `report_outcome`, `get_agent_status`, `initialize`)
- subprocess lifecycle (reconnect, stdio line framing, JSON-RPC id sequencing)

---

## Testing (R-03 scope — mock only)

### Unit tests

- `test_routing_static.py` — `StaticRouting.route` returns `ASSIGN` with input agent; `report_outcome` noop; `aclose` noop.
- `test_arbiter_config.py` — YAML parsing, env-expansion, validator catches missing `binary_path` when `enabled=true`.
- `test_task_outcome.py` — shape, enum values, pydantic validation, `None` handling.
- `test_route_decision.py` — frozen, action enum, serialization.

### Contract tests (FakeArbiterClient — stdin/stdout stub)

A `FakeArbiterClient` test helper implements the same MCP surface but over an in-process `asyncio.Queue` pair. Lets tests inject scripted responses without a real binary.

- `test_arbiter_routing_assign.py` — route → `ASSIGN` → `RouteDecision` with `decision_id` from fake.
- `test_arbiter_routing_hold.py` — route → `HOLD` → scheduler skips tick, task stays READY.
- `test_arbiter_routing_reject.py` — route → `REJECT` → task → `NEEDS_REVIEW` with reason.
- `test_arbiter_routing_advisory_override.py` — explicit `agent_type` + `advisory` mode: arbiter called, `chosen_agent` overridden to explicit, `decision_id` still persisted.
- `test_arbiter_routing_fallback.py` — `ArbiterUnavailable` → `StaticRouting` result; `_degraded` flag set; next call within `reconnect_interval_s` skips arbiter; after interval, reconnect attempt.
- `test_arbiter_routing_timeout.py` — stalled fake → mapped to `HOLD`.
- `test_routing_unknown_agent_is_hold.py` — fake returns `chosen_agent="new_agent_v2"`, AgentType enum doesn't know it → `HOLD` + warn log + `arbiter.route.hold(reason=unknown_agent)`.

### Scheduler integration (fake arbiter)

- `test_scheduler_arbiter_integration.py` — full cycle: spawn → route → DB persists `arbiter_decision_id` → terminal → `report_outcome` called with matching `decision_id` → `arbiter_outcome_reported_at` set.
- `test_scheduler_arbiter_reject_to_review.py` — task ends in NEEDS_REVIEW without running.
- `test_scheduler_arbiter_retry_blocked_on_unavailable.py` — task fails, arbiter down during `report_outcome` → task stays `FAILED` (no FAILED → READY transition) until re-attempt pass delivers outcome, then proceeds to READY with cleared arbiter fields.
- `test_scheduler_arbiter_reattempt_pass_bounded.py` — with 20 dangling outcomes, single tick delivers at most 5, remaining are caught on subsequent ticks.

### Migration

- `test_database_migration_arbiter_routing.py` — pre-R-03 schema → `connect()` → three columns added; running again is idempotent; existing rows preserve NULL.

### Recovery

- `test_recovery_arbiter_outcome_interrupted.py` — RUNNING task with `decision_id`, no reported_at → startup closes with `INTERRUPTED`, `reported_at` set.
- `test_recovery_arbiter_outcome_replay.py` — DONE task with `decision_id`, no reported_at → startup reconstructs outcome from `task_costs` join, reports, marks.
- `test_recovery_arbiter_unavailable.py` — arbiter down during recovery → rows remain flagged, scheduler lightweight pass retries next tick.

### Startup

- `test_arbiter_startup_fail_fast.py` — missing `binary_path` → `ArbiterStartupError` → CLI exits non-zero.
- `test_arbiter_startup_optional_fallback.py` — `optional=true` + missing binary → warn + `StaticRouting`.
- `test_arbiter_startup_version_mismatch.py` — fake returns `version="9.9.9"` in handshake → `ArbiterStartupError`.

Real arbiter subprocess tests — **deferred to R-05 (after R-10 ships arbiter CI artifact)**.

---

## File-by-file change summary

### New
- `maestro/coordination/arbiter_client.py` — vendored client
- `maestro/coordination/arbiter_errors.py` — exception types
- `maestro/coordination/routing.py` — `RoutingStrategy`, `StaticRouting`, `ArbiterRouting`, `make_routing_strategy`
- `tests/test_routing_static.py`, `tests/test_arbiter_*.py` (list above)
- `tests/fakes/fake_arbiter_client.py` — test double
- `examples/with-arbiter.yaml` — usage example

### Modified
- `maestro/models.py` — add `AgentType.AUTO`, `RouteAction`, `RouteDecision`, `TaskOutcome`, `TaskOutcomeStatus`, `ArbiterMode`, `ArbiterConfig`; add 3 fields to `Task`.
- `maestro/database.py` — `SCHEMA_SQL` adds 3 columns; new `_migrate_tasks_arbiter_routing`; new `update_task_routing`, `mark_outcome_reported`, `get_tasks_with_pending_outcome`.
- `maestro/scheduler.py` — `Scheduler.__init__` accepts `routing: RoutingStrategy`; `_spawn_task` routes before spawner lookup; terminal handlers call `report_outcome`; lightweight re-attempt pass in main loop.
- `maestro/recovery.py` — new `recover_arbiter_outcomes` called on startup.
- `maestro/config.py` — parse `arbiter` section into `ArbiterConfig`.
- `maestro/event_log.py` — 7 new event types.
- `maestro/cli.py` — `run` command calls `make_routing_strategy` and passes result to scheduler.
- `pyproject.toml` — no new runtime deps (pydantic, aiosqlite already present); test dep for fake.

### TODO.md
- Mark R-03 `[x]` on merge with commit hash
- New items R-03b (Mode 2 routing), R-10 (arbiter CI), explicit dependency notes
- R-05 acceptance criteria updated: assumes R-03 landed, uses real subprocess

---

## Acceptance criteria

- [ ] `uv run pytest` passes; `uv run pyrefly check` clean; `uv run ruff check .` clean
- [ ] `maestro run` with `arbiter.enabled: false` (or no `arbiter` section) behaves byte-identical to pre-R-03 on existing example tasks.yaml configs
- [ ] `maestro run` with `arbiter.enabled: true` + valid `ARBITER_BIN`: scheduler routes every task, persists `arbiter_decision_id`, reports outcomes; SQLite shows `arbiter_outcome_reported_at` populated for terminal tasks
- [ ] `arbiter.enabled: true` with missing binary + `optional: false` → startup exits non-zero with hint
- [ ] `arbiter.enabled: true` with missing binary + `optional: true` → warning + static fallback
- [ ] Kill arbiter subprocess mid-run → scheduler logs `arbiter.unavailable`, continues under static fallback, next route after `reconnect_interval_s` attempts reconnect
- [ ] Kill Maestro mid-task → restart → `recovery.arbiter.decisions_closed(count=N>0)` event fires, terminal `arbiter_outcome_reported_at` becomes non-NULL
- [ ] Retry gating: with arbiter down at terminal handler time, task stays FAILED until outcome delivered, then transitions to READY; `arbiter_decision_id` is never overwritten while prior attempt's outcome is undelivered
- [ ] FakeArbiter-based contract tests in place (list in Testing section)
- [ ] Migration: existing Maestro DB (without 3 columns) opens cleanly, gains columns, preserves existing rows
