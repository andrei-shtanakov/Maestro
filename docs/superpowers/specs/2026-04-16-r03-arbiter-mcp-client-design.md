# R-03 Design: Arbiter MCP Client in Maestro

**Status:** approved (brainstorm 2026-04-16, review round 1 applied)
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
- Scheduler wiring of `cost_tracker` so `TaskOutcome.tokens_used`/`cost_usd` carry real values — **R-NN** (effort M, independent; arbiter tolerates `None` per cross-repo contract below)
- Schema migrations journal (`schema_migrations` table, linear migration list) — separate mini-R, not R-03
- Eval-driven routing validation — R-07 (depends on R-06b)
- Global `~/.maestro/arbiter.yaml` override — post-v0.1.0
- Authoritative as default mode — stays advisory until explicit config flip
- Arbiter metrics in Maestro dashboard — observability iteration
- Outbox pattern for outcome delivery (`arbiter_outcomes_outbox` table + worker) — **R-03b refactor target**; inline delivery in R-03 is adequate for single-process, single-DB scope

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
    tokens_used: int | None        # None expected in R-03 until cost wiring (R-NN)
    cost_usd: float | None         # None expected in R-03 until cost wiring (R-NN)
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
- Tracks `_degraded_since: datetime | None` (first ArbiterUnavailable, drives `arbiter.reconnected(downtime_s)`) and `_last_reconnect_attempt: datetime | None`.
- `route(task)`:
  - If `_degraded_since is not None and now - _last_reconnect_attempt < reconnect_interval_s` → delegate to static.
  - Else: try `client.route_task_typed(task_id, payload, constraints)` with `asyncio.wait_for(timeout_ms)`.
    - On success: map `RouteDecision` from typed DTO, handle unknown `chosen_agent` (see Error handling).
    - On `ArbiterUnavailable` (broken pipe / timeout): log `arbiter.unavailable` event, set `_degraded_since`, delegate to static, schedule reconnect.
  - **AUTO-safe fallback:** when delegating to static AND `task.agent_type == AgentType.AUTO`, `route` returns `RouteDecision(action=HOLD, reason="arbiter_unavailable_no_default_for_auto")`. Scheduler cannot spawn an `auto` task without arbiter's decision; holding is the only correct option.
  - Advisory override happens inside `ArbiterRouting.route`, not in scheduler: if `cfg.mode == ADVISORY and task.agent_type != AgentType.AUTO` and `action == ASSIGN`, the returned `RouteDecision.chosen_agent` is rewritten to `task.agent_type.value`. `decision_id` and `reason` are preserved as-is. Scheduler code stays mode-agnostic.
- `report_outcome(task, outcome)`:
  - If `task.arbiter_decision_id is None` → noop (static-routed task).
  - Else: call `client.report_outcome(decision_id=..., agent_used=..., status=..., tokens_used=None-OK, cost_usd=None-OK, ...)`. Single attempt, no internal retries, bounded by `timeout_ms`. On failure **raises `ArbiterUnavailable`** so the caller decides what to do with it.
- `aclose()` closes the subprocess cleanly — drain stdin, SIGTERM, wait up to 5s. **Does not drain inflight calls**: any route/outcome in flight during shutdown will raise `ArbiterUnavailable` → static fallback. Documented for SIGINT handling.

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
      _maybe_log_hold(task.id, decision.reason)  # throttled, see Event log
      return False                               # next tick retries

  if decision.action == REJECT:
      log_event("arbiter.route.rejected", ...)
      await db.update_task_status(task_id, NEEDS_REVIEW,
                                  error_message=f"arbiter rejected: {decision.reason}")
      # REJECT self-closes: decision is terminal on arbiter side, no outcome needed
      await db.mark_outcome_reported(task_id, datetime.now(UTC), decision_id=decision.decision_id)
      return False

  # action == ASSIGN
  try:
      chosen = AgentType(decision.chosen_agent)
  except ValueError:
      # unknown agent → HOLD (config drift, not invariant violation)
      logger.warning("arbiter chose unknown agent %s, holding task %s",
                     decision.chosen_agent, task_id)
      _maybe_log_hold(task.id, "unknown_agent")
      return False

  if chosen == AgentType.AUTO:
      # defensive: StaticRouting on an AUTO task would return this; should not happen
      # because ArbiterRouting converts to HOLD, but guard against misbehaving strategies
      logger.error("routing returned AUTO for task %s — refusing to spawn", task_id)
      _maybe_log_hold(task.id, "auto_not_resolved")
      return False

  task = task.model_copy(update={
      "routed_agent_type": chosen.value,               # distinct from assigned_to (coordination layer)
      "arbiter_decision_id": decision.decision_id,
      "arbiter_route_reason": decision.reason,
  })
  await db.update_task_routing(task)   # persist BEFORE spawn — crash safety
  log_event("arbiter.route.decided", ...)

  spawner_key = task.routed_agent_type or task.agent_type.value
  spawner = self._spawners.get(spawner_key)
  # ...existing spawn flow...

terminal handler (success path):
  # Transition RUNNING → DONE immediately
  await db.update_task_status(task_id, DONE, result_summary=...)
  outcome = _build_outcome(task, exit_code, log_file)
  try:
      await routing.report_outcome(task, outcome)
      await db.mark_outcome_reported(task.id, datetime.now(UTC), decision_id=task.arbiter_decision_id)
      log_event("arbiter.outcome.reported", ...)
  except ArbiterUnavailable:
      # reported_at stays NULL — re-attempt pass delivers later. Task already DONE.
      pass

terminal handler (failure path — mode-dependent):
  outcome = _build_outcome(task, exit_code, log_file)
  await db.update_task_status(task_id, FAILED, error_message=..., retry_count=...)
  try:
      await routing.report_outcome(task, outcome)
      await db.mark_outcome_reported(task.id, datetime.now(UTC), decision_id=task.arbiter_decision_id)
      log_event("arbiter.outcome.reported", ...)
      if should_retry:
          await db.reset_for_retry_atomic(task_id, decision_id=task.arbiter_decision_id)
  except ArbiterUnavailable:
      # Behavior depends on mode; see "Retry gating" below
      if cfg.mode == ADVISORY:
          # Best-effort: don't block queue on missed learning signal
          if should_retry:
              await db.reset_for_retry_atomic(task_id, decision_id=None)  # no arbiter-field clearing gate
      else:  # AUTHORITATIVE
          # Strict: task stays FAILED until outcome delivered OR abandon_outcome_after_s elapses
          pass  # re-attempt pass will handle it
```

`_build_outcome` assembles `TaskOutcome` from persisted state:
- `duration_min` = `(completed_at - started_at).total_seconds() / 60` if both set, else `None`
- `tokens_used` / `cost_usd` = `SELECT SUM(input_tokens + output_tokens), SUM(estimated_cost_usd) FROM task_costs WHERE task_id=? AND attempt=?` (attempt = `retry_count + 1`). **In R-03, scheduler does not wire `cost_tracker.parse_and_create_cost` into task completion; these fields will typically be `None`. R-NN follow-up addresses this. Arbiter cross-repo contract tolerates `None`.**
- `error_code` = first line of `task.error_message` truncated to 200 chars, or `None` on success. Known limitation: for multi-line errors where the signal is in a later line (e.g., `Task timed out after 30 minutes\n...real cause...`), the first line may lose context. Documented as L1 follow-up.
- `agent_used` = `task.routed_agent_type or task.agent_type.value` — the spawner actually used; may differ from `chosen_agent` after runtime fallback in advisory.

---

## Retry gating (mode-aware)

Retry behavior when `report_outcome` fails with `ArbiterUnavailable`:

| `arbiter.mode` | outcome delivery | retry gating | abandon timer |
| -------------- | ---------------- | ------------ | ------------- |
| `advisory` | best-effort (logged on failure, not blocking) | no — `FAILED → READY` proceeds regardless | N/A |
| `authoritative` | must-succeed before retry | yes — task stays `FAILED` until `arbiter_outcome_reported_at` set | `arbiter.abandon_outcome_after_s` (default 300) — after this, task is force-unblocked, `arbiter_outcome_reported_at` set to sentinel timestamp, `outcome_abandoned` flag set in event log; retry proceeds |

**Rationale:** the two dimensions (outcome strictness, retry strictness) are not independent — they both express "how authoritative is arbiter on this task". A single `block_retries_on_unavailable` flag would allow nonsensical combinations (authoritative+best-effort breaks feedback loop; advisory+strict contradicts "advisory"). Tying behavior to `mode` keeps the semantic clean.

Advisory default preserves the acceptance criterion "byte-identical OSS path on first failure" — a user accidentally enabling arbiter then having it crash should not paralyze their queue.

Authoritative with the abandon timer gives an escape hatch for extended arbiter outages (e.g. crash loop from bad config) without silently dropping the learning signal in the common case.

---

## Data model changes

### Task model (`maestro/models.py`)

Four new optional fields added to `Task`:

```python
routed_agent_type: str | None = None       # RoutingStrategy's chosen agent (set pre-spawn)
arbiter_decision_id: str | None = None
arbiter_route_reason: str | None = None
arbiter_outcome_reported_at: datetime | None = None
```

**Why `routed_agent_type` and not `assigned_to`:** `assigned_to` is already used by `maestro/coordination/mcp_server.py` and `rest_api.py` for the MCP/REST task-claim flow (agent instance ID, e.g. `"agent-001"`). Its semantics ("the process that claimed this task") differ from routing's semantics ("the agent type the router chose"). Overloading would break coordination-mode consumers. `routed_agent_type` matches the `RoutingStrategy` abstraction — both `StaticRouting` and `ArbiterRouting` fill it; a future `RuleBasedRouting` would fit naturally.

`Task.agent_type` continues to reflect the user's declaration (`auto` / explicit), never mutated after `from_config`.

### SQLite migration

ALTER TABLE columns, idempotent via `PRAGMA table_info` check (pattern matches R-02's `_migrate_tasks_arbiter_columns`). New helper `_migrate_tasks_arbiter_routing` called from `Database.connect()`:

```sql
ALTER TABLE tasks ADD COLUMN routed_agent_type TEXT;
ALTER TABLE tasks ADD COLUMN arbiter_decision_id TEXT;
ALTER TABLE tasks ADD COLUMN arbiter_route_reason TEXT;
ALTER TABLE tasks ADD COLUMN arbiter_outcome_reported_at TIMESTAMP;
```

No new index in R-03. Recovery query runs once at startup; even at 10k tasks a full scan is <50ms.

### New Database methods

- `update_task_routing(task)` — single-row update writing `routed_agent_type`, `arbiter_decision_id`, `arbiter_route_reason`. Does **not** touch `agent_type`, `assigned_to`, or `status`.
- `mark_outcome_reported(task_id, ts, decision_id)` — atomic: sets `arbiter_outcome_reported_at=ts WHERE id=? AND arbiter_decision_id=?`. The `decision_id` guard prevents marking the wrong attempt if routing state was rewritten concurrently. `rowcount=0` is logged and returned as a boolean; callers decide whether to retry.
- `reset_for_retry_atomic(task_id, decision_id)` — single SQL transaction:
  ```sql
  UPDATE tasks
  SET status='ready',
      routed_agent_type=NULL,
      arbiter_decision_id=NULL,
      arbiter_route_reason=NULL,
      arbiter_outcome_reported_at=NULL
  WHERE id=? AND status='failed'
    AND (? IS NULL OR arbiter_decision_id=?)  -- decision_id guard optional in advisory path
  ```
  `rowcount=0` means external interference (dashboard `abandon`, manual SQL) — logged as `arbiter.retry_reset.skipped`, not re-attempted. Eliminates the FAILED/READY race window that `report_outcome`'s new latency would widen (H2).
- `get_tasks_with_pending_outcome()` — recovery helper: `WHERE arbiter_decision_id IS NOT NULL AND arbiter_outcome_reported_at IS NULL AND status IN (...)` (see status mapping below).

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
    timeout_ms: int = 500                # p95-friendly; 100 was overly optimistic (M3)
    reconnect_interval_s: int = 60
    abandon_outcome_after_s: int = 300   # authoritative-only escape hatch
    log_level: str = "warn"

    @model_validator(mode="after")
    def validate_required_when_enabled(self) -> Self:
        if self.enabled:
            missing = [n for n in ("binary_path", "config_dir", "tree_path")
                       if getattr(self, n) is None]
            if missing:
                raise ValueError(
                    f"arbiter.{'/'.join(missing)} required when arbiter.enabled=true. "
                    f"Set via env var (e.g. ARBITER_BIN) or inline in config."
                )
            # Catch accidental ${VAR:-default} usage (unsupported by config.py parser)
            for field in ("binary_path", "config_dir", "tree_path"):
                val = getattr(self, field)
                if val and "${" in val:
                    raise ValueError(
                        f"arbiter.{field}={val!r}: unresolved env var substitution. "
                        f"config.py supports ${{VAR}} only; ${{VAR:-default}} is not supported."
                    )
        return self
```

`OrchestratorConfig` / project YAML parser gains optional `arbiter: ArbiterConfig | None = None` at top level.

### AgentType.AUTO

Add `AUTO = "auto"` to `AgentType` enum. It is a **routing sentinel, not a real agent**. Two invariants enforced in code:

1. `Task.from_config` raises when `agent_type == AUTO and arbiter` is not enabled (fail-fast, clear message).
2. `_spawn_task` guards `if chosen == AgentType.AUTO: refuse` before spawner lookup — belt-and-suspenders against a misbehaving RoutingStrategy returning AUTO.

**Alternative considered:** separate `TaskConfig.agent_preference: AgentType | None = None` (None = auto) and runtime `Task.agent_type: AgentType` (always concrete). Cleaner type separation, but larger refactor touching config parsing, migration, and every test. Deferred to a follow-up refactor; the sentinel approach with invariant checks is acceptable for R-03.

---

## Configuration (YAML)

```yaml
# tasks.yaml (scheduler mode)
arbiter:
  enabled: true
  mode: advisory                         # advisory | authoritative
  optional: false                        # fail-fast by default
  binary_path: ${ARBITER_BIN}            # required; env-substituted via config.py
  config_dir: ${ARBITER_CONFIG}          # required
  tree_path: ${ARBITER_TREE}             # required
  db_path: ./arbiter.db                  # optional; omit → temp DB
  timeout_ms: 500                        # per route/outcome call
  reconnect_interval_s: 60
  abandon_outcome_after_s: 300           # authoritative-only escape hatch
  log_level: warn

tasks:
  - id: ...
    agent_type: auto                     # "auto" → chosen_agent from arbiter
    # or
    agent_type: codex_cli                # explicit → advisory (learning + HOLD respected)
```

Env-var substitution uses the `${VAR}` syntax already in `config.py`. **Default syntax `${VAR:-default}` is not supported** — config_dir/tree_path/binary_path must resolve to concrete paths. `ArbiterConfig.validate_required_when_enabled` catches unresolved `${` residue and surfaces an actionable error.

---

## Advisory semantics (precise)

| `task.agent_type` | `arbiter.mode` | Effective chosen_agent | HOLD / REJECT respected |
| ----------------- | -------------- | ---------------------- | ----------------------- |
| `auto`            | advisory       | arbiter's `chosen_agent` | yes                   |
| `auto`            | authoritative  | arbiter's `chosen_agent` | yes                   |
| explicit (e.g. `codex_cli`) | advisory | explicit (arbiter's overridden inside ArbiterRouting) | yes (budget/invariant) |
| explicit          | authoritative  | arbiter's `chosen_agent` (overrides user) | yes         |

In all cases, `route_task` is called when `arbiter.enabled=true`, so arbiter gets the learning signal. `decision_id` / `reason` are persisted in all cases.

---

## Error handling

Three disjoint paths, each with its own identity.

### ArbiterStartupError (fail-fast at `maestro run` start)

Raised from `make_routing_strategy` if any of:
1. `binary_path` does not exist or is not executable
2. Subprocess spawn fails
3. Handshake does not complete within 5s
4. `serverInfo.version` in `initialize` response != `ARBITER_MCP_REQUIRED_VERSION` pinned in vendored client

Behavior: CLI prints message + actionable hint, exits non-zero. If `arbiter.optional: true`, log warning and fall back to `StaticRouting` instead of exiting.

**Pre-impl verification:** arbiter-side returns `serverInfo.version = CARGO_PKG_VERSION` (confirmed in `arbiter-mcp/src/server.rs:370`; workspace Cargo version = `0.1.0`). Shape is `{"protocolVersion": "2024-11-05", "capabilities": {...}, "serverInfo": {"name": "arbiter", "version": "0.1.0"}}`. No arbiter-side change needed.

### ArbiterUnavailable (runtime degraded mode)

Raised by `ArbiterClient` on broken pipe, read timeout, JSON parse failure after MCP is established. `ArbiterRouting` catches it for route-path:
- Delegates current call to static fallback (or HOLD for AUTO tasks)
- Sets `_degraded_since = now()` (first transition only — event also emits only on first transition), `_last_reconnect_attempt = now()`
- Logs `arbiter.unavailable(since=...)` event once per transition
- On next `route()` call after `reconnect_interval_s`, tries to re-`start()` the client. On success: logs `arbiter.reconnected(downtime_s = now - _degraded_since)`, clears both flags.

`report_outcome` path does **not** catch `ArbiterUnavailable` internally — it re-raises so the caller (scheduler terminal handler) can apply the mode-dependent retry gating policy.

### HOLD / REJECT (normal responses, not errors)

These are `RouteDecision` values, not exceptions.

- `action=REJECT` from arbiter → task → `NEEDS_REVIEW` with `reason` in `error_message`. REJECT **self-closes**: `mark_outcome_reported` is called immediately with arbiter-provided `decision_id`, which excludes the task from recovery pool (no follow-up outcome needed — the decision is final on arbiter's side).
- `action=HOLD` from arbiter → task stays `READY`, next scheduler tick retries the route call.
- `timeout_ms` exceeded → mapped to `HOLD` (not `FAILED`, not `ArbiterUnavailable` — assumption: arbiter is up but slow; we retry next tick).
- Unknown `chosen_agent` (not in `AgentType` enum) → mapped to `HOLD` with reason `unknown_agent`; logged as warning.
- AUTO task with arbiter degraded → `HOLD` with reason `arbiter_unavailable_no_default_for_auto`.

---

## Recovery

### recovery.py additions

On Maestro startup, after existing recovery (reset stale RUNNING → READY, etc.), call `recover_arbiter_outcomes(db, routing)`:

```python
async def recover_arbiter_outcomes(db: Database, routing: RoutingStrategy) -> int:
    """Close dangling arbiter decisions after a Maestro crash.

    Finds tasks with arbiter_decision_id set but arbiter_outcome_reported_at
    NULL. For RUNNING/VALIDATING tasks, emits INTERRUPTED outcome (mid-flight
    crash). For terminal tasks, reconstructs outcome from persisted state.

    Returns: count of outcomes re-delivered.
    """
    pending = await db.get_tasks_with_pending_outcome()
    for task in pending:
        outcome_status = _task_status_to_outcome_status(task.status)
        if outcome_status is None:
            logger.error("task %s has decision_id but unexpected status %s",
                         task.id, task.status)
            continue
        outcome = _build_outcome_from_task(task, outcome_status)
        try:
            await routing.report_outcome(task, outcome)
            await db.mark_outcome_reported(task.id, datetime.now(UTC), task.arbiter_decision_id)
        except ArbiterUnavailable:
            # arbiter down during recovery — scheduler's re-attempt pass will retry
            break
    return count
```

### TaskStatus → TaskOutcomeStatus mapping (M6)

Recovery's `_task_status_to_outcome_status` uses this explicit table:

| `TaskStatus`           | `TaskOutcomeStatus` | Notes |
| ---------------------- | ------------------- | ----- |
| `DONE`                 | `SUCCESS`           | happy path |
| `FAILED`               | `FAILURE`           | retry-eligible failure |
| `NEEDS_REVIEW`         | `FAILURE`           | max retries exhausted |
| `ABANDONED`            | `CANCELLED`         | user/system gave up |
| `RUNNING`              | `INTERRUPTED`       | crashed mid-run |
| `VALIDATING`           | `INTERRUPTED`       | crashed mid-validation |
| `PENDING` / `READY` / `AWAITING_APPROVAL` | `None` (invariant violation) | `decision_id IS NOT NULL` on these is a bug; log error + skip |

### Re-attempt pass in scheduler main loop

The scheduler main loop also runs a lightweight re-attempt pass once per tick for rows still flagged `arbiter_outcome_reported_at IS NULL`, **bounded to at most 5 rows per tick** so outcome delivery can never starve task scheduling.

In `authoritative` mode, this pass also unblocks retry-gated tasks: after `abandon_outcome_after_s` of unsuccessful delivery attempts, the task is force-unblocked (`arbiter_outcome_reported_at` set to abandon-sentinel, `arbiter.outcome.abandoned(task_id, age_s)` event fired, task transitions FAILED → READY as normal).

Event emitted on startup: `recovery.arbiter.decisions_closed(count=N)`.

---

## Contract to Arbiter (cross-repo requirement)

R-03 assumes the following on arbiter's side. Violations are blockers for R-05 (integration tests) and production usage; coordinate with arbiter team before R-03 ships.

1. **`report_outcome` MUST be idempotent by `decision_id`.** Maestro recovery may re-deliver the same outcome after a crash between `client.report_outcome(...)` and `db.mark_outcome_reported(...)`. Arbiter must deduplicate silently (not increment success/failure counters twice, not corrupt training stats).
2. **`report_outcome` MUST accept and use `agent_used` as the actual label.** In advisory mode, `agent_used != chosen_agent` is the common case and the primary learning signal. Arbiter must not fall back to `chosen_agent` for label attribution.
3. **`report_outcome` MUST tolerate `tokens_used=None` and `cost_usd=None`.** Maestro's scheduler does not wire `cost_tracker` in R-03; these fields will usually arrive unset. Arbiter should treat `None` as "unknown" (no training on cost dimension for that sample), not as `0`.
4. **`route_task` SHOULD honor `timeout_ms` server-side.** Maestro cancels on its side via `asyncio.wait_for`, but the arbiter subprocess continuing to churn uses CPU and memory. Nice-to-have, not blocker.
5. **`serverInfo.version` in MCP `initialize` response is the authoritative version string for `ARBITER_MCP_REQUIRED_VERSION` comparison.** Arbiter maintainer: coordinate bumps via re-vendoring Maestro's client.

---

## Event log types (new)

Added to `event_log.py`:

- `arbiter.route.decided` — fields: `task_id`, `decision_id`, `chosen_agent`, `reason`. Per-call (decisions are events).
- `arbiter.route.hold` — fields: `task_id`, `reason`. **Throttled**: first occurrence of `(task_id, reason)` logged; subsequent occurrences with the same reason within the same HOLD-streak are suppressed. In-memory `dict[task_id, (reason, first_seen, count)]`; on reason change or transition out of HOLD, the entry is flushed (single summary event with `count`) and cleared. Prevents megabytes of log from long-degraded arbiter.
- `arbiter.route.rejected` — fields: `task_id`, `reason`. Per-call.
- `arbiter.outcome.reported` — fields: `task_id`, `decision_id`, `status`. Per-call.
- `arbiter.outcome.abandoned` — fields: `task_id`, `decision_id`, `age_s`. Authoritative-mode escape hatch; per-call.
- `arbiter.unavailable` — fields: `error`, `since`. First transition only.
- `arbiter.reconnected` — fields: `downtime_s` (from `_degraded_since`). Per-transition.
- `arbiter.retry_reset.skipped` — fields: `task_id`, `expected_decision_id`, `observed_status`. When atomic retry-reset finds rowcount=0.
- `recovery.arbiter.decisions_closed` — fields: `count`. Once per startup.

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

Version pin policy: strict equals. Arbiter patch bumps (0.1.1) require
re-vendoring + explicit version-constant update. Document in arbiter's
release checklist.
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
- `test_arbiter_config.py` — YAML parsing, env-expansion, validator catches missing `binary_path` and unresolved `${` residue when `enabled=true`.
- `test_task_outcome.py` — shape, enum values, pydantic validation, `None` handling for tokens/cost.
- `test_route_decision.py` — frozen, action enum, serialization.
- `test_status_mapping.py` — `_task_status_to_outcome_status` covers all 9 TaskStatus values including the 3 invariant-violation cases.

### Contract tests (FakeArbiterClient — stdin/stdout stub)

A `FakeArbiterClient` test helper implements the same MCP surface but over an in-process `asyncio.Queue` pair. Lets tests inject scripted responses without a real binary.

- `test_arbiter_routing_assign.py` — route → `ASSIGN` → `RouteDecision` with `decision_id` from fake.
- `test_arbiter_routing_hold.py` — route → `HOLD` → scheduler skips tick, task stays READY.
- `test_arbiter_routing_reject.py` — route → `REJECT` → task → `NEEDS_REVIEW` + `arbiter_outcome_reported_at` set (self-closing).
- `test_arbiter_routing_advisory_override.py` — explicit `agent_type` + `advisory` mode: arbiter called, `chosen_agent` overridden to explicit inside ArbiterRouting, `decision_id` still persisted.
- `test_arbiter_routing_authoritative_override.py` — explicit `agent_type` + `authoritative` mode: arbiter's `chosen_agent` wins even against explicit.
- `test_arbiter_routing_authoritative_auto.py` — `auto` + `authoritative`: same path as auto+advisory (ensure mode doesn't confuse logic).
- `test_arbiter_routing_fallback.py` — `ArbiterUnavailable` → `StaticRouting` for non-AUTO tasks; `_degraded_since` set; next call within `reconnect_interval_s` skips arbiter; after interval, reconnect attempt logs `arbiter.reconnected(downtime_s)`.
- `test_arbiter_routing_auto_task_holds_when_arbiter_down.py` — AUTO task + ArbiterUnavailable → `HOLD` with `reason=arbiter_unavailable_no_default_for_auto`, no static-fallback misfire.
- `test_arbiter_routing_timeout.py` — stalled fake → mapped to `HOLD`.
- `test_routing_unknown_agent_is_hold.py` — fake returns `chosen_agent="new_agent_v2"`, AgentType enum doesn't know it → `HOLD` + warn log.
- `test_arbiter_routing_hold_event_throttle.py` — same `(task_id, reason)` HOLD across 10 ticks → 1 initial event + 1 summary on transition, 8 suppressed.

### Scheduler integration (fake arbiter)

- `test_scheduler_arbiter_integration.py` — full cycle: spawn → route → DB persists `routed_agent_type` + `arbiter_decision_id` → terminal → `report_outcome` called with matching `decision_id` → `arbiter_outcome_reported_at` set.
- `test_scheduler_arbiter_reject_to_review.py` — task ends in NEEDS_REVIEW without running; `arbiter_outcome_reported_at` set (self-close).
- `test_scheduler_arbiter_advisory_retry_not_blocked.py` — advisory mode, task fails, arbiter down during `report_outcome` → task transitions FAILED → READY anyway (best-effort).
- `test_scheduler_arbiter_authoritative_retry_blocked.py` — authoritative mode, task fails, arbiter down → task stays FAILED until re-attempt delivers, then transitions. Tests `abandon_outcome_after_s` timing with `freezegun`.
- `test_scheduler_arbiter_authoritative_abandon.py` — authoritative mode, arbiter down longer than `abandon_outcome_after_s` → `arbiter.outcome.abandoned` event fires, task force-unblocked, transitions READY.
- `test_scheduler_arbiter_reattempt_pass_bounded.py` — 20 dangling outcomes, single tick delivers at most 5, remaining caught on subsequent ticks.
- `test_scheduler_atomic_retry_reset_guards_against_external_transition.py` — during `report_outcome` in-flight, external `abandon` call changes status → atomic reset sees `rowcount=0`, logs `arbiter.retry_reset.skipped`, no double-transition.

### Migration

- `test_database_migration_arbiter_routing.py` — pre-R-03 schema → `connect()` → four columns added; running again idempotent; existing rows preserve NULL.

### Recovery

- `test_recovery_arbiter_outcome_interrupted.py` — RUNNING task with `decision_id`, no reported_at → startup closes with `INTERRUPTED`, `reported_at` set.
- `test_recovery_arbiter_outcome_replay.py` — DONE task with `decision_id`, no reported_at → startup reconstructs outcome (with None tokens/cost expected), reports, marks.
- `test_recovery_arbiter_unavailable.py` — arbiter down during recovery → rows remain flagged, scheduler lightweight pass retries next tick.
- `test_recovery_arbiter_invariant_violation_status.py` — pathological row with `decision_id IS NOT NULL` and `status=PENDING` → skipped with error log, other rows still processed.

### Startup

- `test_arbiter_startup_fail_fast.py` — missing `binary_path` → `ArbiterStartupError` → CLI exits non-zero.
- `test_arbiter_startup_optional_fallback.py` — `optional=true` + missing binary → warn + `StaticRouting`.
- `test_arbiter_startup_version_mismatch.py` — fake returns `version="9.9.9"` in handshake → `ArbiterStartupError`.
- `test_arbiter_startup_unresolved_env_var.py` — `binary_path="${ARBITER_BIN:-/fallback}"` (unresolved default syntax) → pydantic validator rejects with clear message.

Real arbiter subprocess tests — **deferred to R-05 (after R-10 ships arbiter CI artifact)**.

---

## File-by-file change summary

### New
- `maestro/coordination/arbiter_client.py` — vendored client
- `maestro/coordination/arbiter_errors.py` — exception types
- `maestro/coordination/routing.py` — `RoutingStrategy`, `StaticRouting`, `ArbiterRouting`, `make_routing_strategy`
- `tests/test_routing_static.py`, `tests/test_arbiter_*.py`, `tests/test_scheduler_arbiter_*.py`, `tests/test_recovery_arbiter_*.py` (see Testing)
- `tests/fakes/fake_arbiter_client.py` — test double
- `examples/with-arbiter.yaml` — usage example

### Modified
- `maestro/models.py` — add `AgentType.AUTO`, `RouteAction`, `RouteDecision`, `TaskOutcome`, `TaskOutcomeStatus`, `ArbiterMode`, `ArbiterConfig`; add 4 fields to `Task`.
- `maestro/database.py` — `SCHEMA_SQL` adds 4 columns; new `_migrate_tasks_arbiter_routing`; new `update_task_routing`, `mark_outcome_reported`, `reset_for_retry_atomic`, `get_tasks_with_pending_outcome`.
- `maestro/scheduler.py` — `Scheduler.__init__` accepts `routing: RoutingStrategy` and `arbiter_mode: ArbiterMode` (for retry-gating policy); `_spawn_task` routes before spawner lookup; terminal handlers call `report_outcome` with mode-dependent retry gating; `_maybe_log_hold` throttle helper; lightweight re-attempt pass + abandon-timer sweep in main loop.
- `maestro/recovery.py` — new `recover_arbiter_outcomes` called on startup; uses status mapping table.
- `maestro/config.py` — parse `arbiter` section into `ArbiterConfig`.
- `maestro/event_log.py` — 9 new event types (including abandoned and retry_reset.skipped).
- `maestro/cli.py` — `run` command calls `make_routing_strategy` and passes result + mode to scheduler.
- `pyproject.toml` — no new runtime deps (pydantic, aiosqlite already present); test dep for fake.

### TODO.md
- Mark R-03 `[x]` on merge with commit hash
- New items: R-03b (Mode 2 routing), R-10 (arbiter CI), R-NN (scheduler cost_tracker wiring), schema_migrations journal mini-R
- R-05 acceptance criteria: assumes R-03 landed, uses real subprocess
- Arbiter-team coordination: confirm idempotency on `decision_id`, `agent_used` as actual label, `None` cost/tokens tolerance

---

## Acceptance criteria

- [ ] `uv run pytest` passes; `uv run pyrefly check` clean; `uv run ruff check .` clean
- [ ] `maestro run` with `arbiter.enabled: false` (or no `arbiter` section) behaves byte-identical to pre-R-03 on existing example tasks.yaml configs
- [ ] `maestro run` with `arbiter.enabled: true` + valid `ARBITER_BIN`: scheduler routes every task, persists `routed_agent_type` + `arbiter_decision_id`, reports outcomes; SQLite shows `arbiter_outcome_reported_at` populated for terminal tasks
- [ ] `arbiter.enabled: true` with missing binary + `optional: false` → startup exits non-zero with hint
- [ ] `arbiter.enabled: true` with missing binary + `optional: true` → warning + static fallback
- [ ] `arbiter.enabled: true` with `${VAR:-default}` in a path → pydantic validator rejects with clear message on config load
- [ ] Kill arbiter subprocess mid-run in **advisory** mode → scheduler logs `arbiter.unavailable`, continues under static fallback, retries after `reconnect_interval_s`; **failed tasks transition FAILED → READY normally (not blocked on missed outcome)**
- [ ] Kill arbiter subprocess mid-run in **authoritative** mode → failed tasks stay FAILED until outcome delivered; after `abandon_outcome_after_s` they force-unblock with `arbiter.outcome.abandoned` event
- [ ] AUTO task + arbiter down → task held with `reason=arbiter_unavailable_no_default_for_auto`; never spawned with `auto` string
- [ ] Kill Maestro mid-task → restart → `recovery.arbiter.decisions_closed(count=N>0)` event fires, terminal `arbiter_outcome_reported_at` becomes non-NULL
- [ ] Atomic retry reset: concurrent external `abandon` during `report_outcome` in-flight → `arbiter.retry_reset.skipped` logged, no double-transition
- [ ] HOLD event throttle: same `(task_id, reason)` across 100 ticks → 1 initial + 1 summary event, not 100
- [ ] FakeArbiter-based contract tests in place (list in Testing section)
- [ ] Migration: existing Maestro DB (without 4 columns) opens cleanly, gains columns, preserves existing rows

### Known limitations (documented, not blockers)

- `TaskOutcome.tokens_used` and `cost_usd` are `None` for scheduler-run tasks in R-03. Cost wiring to scheduler terminal handlers is **R-NN** (effort M, independent follow-up). Arbiter cross-repo contract tolerates `None`.
- `error_code` is the first line of `error_message` truncated to 200 chars. For multi-line errors where the signal lives in a later line, context may be lost. Refine in L1 follow-up if real data shows it matters.
- `reconnect_interval_s` has no jitter. Single-Maestro deployments are unaffected; multi-instance dogfood may see synchronized reconnect storms. L2 follow-up if observed.
