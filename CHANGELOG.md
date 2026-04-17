# Changelog

## v0.2.0 (2026-04-17)

### Added
- **Arbiter MCP client integration (R-03)** — optional policy-engine routing.
  Declare an `arbiter:` section in the project YAML to spawn an arbiter
  subprocess, ask it to route every ready task (`advisory` or `authoritative`
  mode), and report back outcomes for the learning loop. See
  [`examples/with-arbiter.yaml`](examples/with-arbiter.yaml) for a full
  configuration reference. When the section is absent or `enabled: false`,
  Maestro stays on the zero-config `StaticRouting` path — **byte-identical
  to v0.1.0**; no subprocess, no routing overhead.
- `AgentType.AUTO` routing sentinel — let the arbiter pick the agent per task.
- New `maestro/coordination/` subpackage: `routing.py` (`StaticRouting`,
  `ArbiterRouting`, `make_routing_strategy` factory), `arbiter_client.py`
  (vendored MCP client), `arbiter_errors.py`.
- `Task` gains persisted arbiter routing fields (`routed_agent_type`,
  `arbiter_decision_id`, `arbiter_route_reason`, `arbiter_outcome_reported_at`)
  with automatic SQLite migration for pre-R-03 databases.
- Scheduler delivers outcomes on completion/failure, gates retries on
  arbiter mode (advisory retries regardless of delivery failure;
  authoritative waits for successful `report_outcome`), and runs a
  bounded re-attempt pass (5/tick) each loop iteration with an
  authoritative abandon timer (`abandon_outcome_after_s`, default 300s)
  as the escape hatch when the arbiter stays unreachable.
- Crash recovery closes dangling arbiter decisions on startup via
  `recover_arbiter_outcomes` (available standalone or through
  `StateRecovery.recover(routing=...)`).
- 10 new structured `EventType` members cover the route/outcome/recovery
  lifecycle; `HoldThrottle` helper collapses repeat HOLD events.
- Dependency bump: `authlib` 1.6.9 → 1.6.11 (transitive via `fastmcp`).

### Compatibility
- Zero-config projects (no `arbiter:` section) behave exactly as in v0.1.0.
  No subprocess is spawned, no routing overhead, and the scheduler's
  route-then-spawn path short-circuits through `StaticRouting`.
- SQLite migration is idempotent; upgrading an existing v0.1.0 database
  adds four nullable columns with no data changes.

### Docs
- [`docs/superpowers/specs/2026-04-16-r03-arbiter-mcp-client-design.md`](docs/superpowers/specs/2026-04-16-r03-arbiter-mcp-client-design.md) —
  architecture spec.
- [`docs/superpowers/plans/2026-04-16-r03-arbiter-mcp-client.md`](docs/superpowers/plans/2026-04-16-r03-arbiter-mcp-client.md) —
  32-step implementation plan (all complete).

### Tests
- +113 tests (1112 total), `pyrefly check` 0 errors, `ruff check .` clean,
  `ruff format --check .` clean.

## v0.1.0 (2026-04-06)

First public release.

### Features
- **Mode 1 (Task Scheduler):** DAG-based scheduling of AI coding agents
  (Claude Code, Codex, Aider) in a shared directory
- **Mode 2 (Multi-Process Orchestrator):** Decompose projects into independent
  zadachi, run each in isolated git worktrees via spec-runner, auto-create PRs
- Spawner registry with 4 built-in spawners (claude_code, codex, aider, announce)
- SQLite state persistence with crash recovery
- CLI: run, status, retry, stop, orchestrate, zadachi, workspaces
- Web dashboard with DAG visualization and SSE updates
- Desktop notifications (macOS/Linux)
- Auto-commit per task with git diff summary
- Dogfood-tested: Maestro builds itself (3 weeks of real usage)
