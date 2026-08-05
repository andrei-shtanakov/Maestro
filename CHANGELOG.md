# Changelog

## Unreleased

### Fixed
- **Preflight scope-overlap no longer flags DAG-ordered workstreams as a
  merge-conflict risk (#121).** When a `depends_on` path (direct or
  transitive) already orders two workstreams, they never run concurrently,
  so their scope overlap is reported at a new `info` severity — without the
  misleading "add a depends_on edge" advice — instead of `warning`. Info
  findings never fail `maestro validate --strict`. Genuinely parallel
  overlaps keep the warning unchanged.

### Added
- **`maestro workstream-approve <id>` (gates v1.1, H-5):** the sanctioned
  operator re-queue for gate-blocked workstreams — NEEDS_REVIEW → READY with
  `error_message` preserved (used to require a raw sqlite UPDATE).

### Changed
- **`validation_backend` default flipped `local` → `same` (validation_backend
  PR3).** Post-task validation now runs by default in the *same* backend the
  task ran in (environment parity), instead of always on the local host.
  **No-op for the common case:** a task with no `execution` config (or
  `default_backend: local`) and no per-task `backend` still validates on the
  bare local host — `same` resolves to the default `local` backend.
  **Behavior change only where a task actually runs on a Docker or SSH
  backend:** its validation now executes durably inside that backend (its own
  execution handle), so the validation command sees the task's real
  toolchain/filesystem rather than the host's. Set `validation_backend: local`
  explicitly to keep host-local validation. Existing persisted tasks keep their
  recorded value (no data-migration; migration 12 only changes the column
  default for new rows); only newly-created tasks pick up the new default.

### Fixed
- **Gates approval memory survives phase overwrites (H-3):** the single-slot
  `error_message` marker gets overwritten when a later phase blocks; the
  verdict store (`gate_verdicts.jsonl`) is now the durable approval memory —
  a recorded owner-approval block for the exact (workstream, phase, sha)
  counts as operator approval on re-evaluation (NEEDS_REVIEW → READY is a
  human-only transition). New commits still invalidate approvals (SHA-bound).
- **Orchestrator spec commits no longer trip the scope gate (H-4):**
  `spec/**` and `spec-runner.config.yaml` are Maestro's own infra commits and
  are excluded from ex-post classification and the declared-scope check.
- **`constraints.authority_context` on route_task calls (RD-006 M4):** the
  scheduler now sends the authority execution context `{role, phase}` to
  arbiter — `role` from the task's function (`review` tasks act as reviewers,
  everything else the scheduler executes is `implement`), `phase: execution`.
  Rides in constraints only, never in the task payload (arbiter structurally
  keeps it out of the 22-dim feature vector; Maestro keeps it out of
  capability features). Enables arbiter's role/phase-scoped allowlist
  enforcement (arbiter #50/#51) once `config/authority.toml` is vendored.
- **Gates-in-DAG runtime (WS-006 handoff M-1..M-3):** opt-in `gates:` section in
  `project.yaml` — the orchestrator evaluates risk gates at two transition
  edges by shelling out to `steward risk-classify` (single source of truth for
  tiers): **ex-ante** before READY→RUNNING over the declared workstream scope,
  **ex-post** before RUNNING→MERGING over the actual diff (scope violations
  escalate). Fail-closed: a missing/errored verdict on a mandatory gate blocks
  the transition; blocked workstreams route to NEEDS_REVIEW and an operator
  re-queue approves the gate for that exact SHA (a new commit invalidates the
  approval). Every evaluation appends verdict-records to
  `logs/<ULID>/gate_verdicts.jsonl` (addressable via EvidenceRef
  `kind=gate-verdict`); gates enforced beyond these edges (branch protection,
  PR reviews) are recorded as advisory annotations. New: `maestro/gates.py`,
  `GatesConfig`, preflight checks `gates-steward-missing` /
  `gates-risk-model-missing`, and a legal READY→NEEDS_REVIEW workstream
  transition. No behavior change when `gates:` is absent.
- **EvidenceRef `kind: gate-verdict` (WS-006 handoff M-4):** typed pointer to
  one gate verdict-record in `logs/<ULID>/gate_verdicts.jsonl`, addressed by
  `pipeline_id` + `gate_id` + full 40-hex `sha` (verdicts are SHA-bound).
  Pre-adoption additive change to `contracts/observability/evidence-ref.schema.json`
  with the WorkCorrelation inline copy kept in sync; new builder
  `gate_verdict_evidence()` in `maestro/correlation.py`.
- **`maestro validate <project.yaml>` (preflight, Mode 2):** static and
  filesystem checks over an orchestrator config before a run — dependency
  cycles (`dag-cycle`, error) via the shared `dag.find_cycle`, scope overlap
  between workstreams (`scope-overlap`, warning; two-tier: a static heuristic
  plus an exact file-set intersection when `--no-fs` is not set), empty scope
  (`scope-empty`, warning), missing/non-git repo (`repo-missing` /
  `repo-not-git`, errors), scope globs matching nothing on disk
  (`scope-no-match`, warning), and scope globs that are unsafe to expand —
  absolute, empty, containing a `..` segment, or otherwise rejected by the
  glob engine (`scope-invalid-pattern`, warning; contributes no files and
  never raises). `--strict` treats warnings as errors (exit 1);
  `--no-fs` skips filesystem checks for deterministic, repo-less runs. The
  same checks now run as a fail-fast gate inside `maestro orchestrate`
  (`maestro/preflight.py`).
- **`maestro init [PATH] [--force] [--project NAME]`:** scaffolds a commented
  `project.yaml` template with git-derived autofill (project name, repo path)
  and self-checks the generated config against `OrchestratorConfig` before
  writing (`maestro/scaffold.py`).

### Changed
- **Routing (D2):** the arbiter can now route to any harness that has a
  registered spawner; the closed `AgentType` enum no longer gates spawns
  (`scheduler.py`). Under **arbiter routing**, an unknown harness → retryable
  HOLD (`unknown_agent`); `auto` → refuse (`auto_not_resolved`) — semantics
  unchanged. Under **static/scheduler routing** (no arbiter to re-route), an
  unregistered harness fails **terminally** (`SchedulerError`, task FAILED) —
  a HOLD there would leave the task READY forever and hang the run.
- **Model execution (D1):** the arbiter-routed model (`<harness>@<model>`) is now
  passed into `spawn()` and executed. Each spawn emits an `agent.model_resolved
  {harness, model, source}` log for observability.
- **Catalog-driven model defaults (AI#4, ADR-ECO-003b):** the baked
  `DEFAULT_<H>_MODEL` constants have been removed. The model is now resolved
  from a user-config catalog loaded from `$ATP_CATALOG` (see `maestro/catalog.py`).
  New precedence is **`routed` (arbiter) > `MAESTRO_<H>_MODEL` env > catalog
  default > fail-loud** — env is now a fallback used only when routing
  supplies no model, and the catalog default is used only when neither routing
  nor env supply one. A status-graded coherence warning (`retired` /
  `deprecated` / `unknown`) is logged when the routed or env-supplied model
  doesn't cleanly match an `active` catalog entry.
  Fault taxonomy is split by blast radius: a malformed or unconfigured
  catalog raises a global `CatalogError` (`CatalogNotConfigured` /
  `CatalogMalformed`) that halts the whole run, while a harness with no (or
  an ambiguous) routable default raises a per-task `HarnessModelUnresolved`
  that sends only that task to `NEEDS_REVIEW`.
  **Breaking change:** a run with no routed model, no `MAESTRO_<H>_MODEL`,
  and no `$ATP_CATALOG` now fails loud (`CatalogNotConfigured`: "model
  catalog not configured: set $ATP_CATALOG (or run 'atp models init')")
  instead of silently falling back to a built-in default model.

---

## v0.4.0 — Rename Zadacha → Workstream (2026-05-23)

**Breaking changes** (no backward compatibility):
- CLI: `maestro zadachi` → `maestro workstreams`
- REST API: `/zadachi` endpoints → `/workstreams`; `/zadachi/{zadacha_id}` → `/workstreams/{workstream_id}`
- `project.yaml`: top-level key `zadachi:` → `workstreams:`
- DB schema: `zadachi` table → `workstreams`; `zadacha_dependencies` → `workstream_dependencies`; `zadacha_id` columns → `workstream_id`. Migration auto-applied on first run.
- Python API: `Zadacha`, `ZadachaStatus`, `ZadachaConfig`, `ZadachaNotFoundError`, `ZadachaAlreadyExistsError` → `Workstream`, `WorkstreamStatus`, `WorkstreamConfig`, `WorkstreamNotFoundError`, `WorkstreamAlreadyExistsError`. Code that imports these symbols must update.

**Motivation:** transliterated Russian word ("zadacha" / "zadachi") in identifiers was confusing for English-speaking users and code review. `Workstream` is the natural English term for the concept — a parallel independent track of work that owns its own git worktree, spec-runner subprocess, and final PR.

**Scheduler-mode `Task` concept is UNAFFECTED** — only the orchestrator-mode concept was renamed.

---

## v0.3.0 (2026-05-23)

### Added
- `maestro/benchmark/arbiter_report.py` — `report_benchmark_to_arbiter(result, client)` helper; never raises (except `CancelledError`); returns a copy with `report_status` / `report_error` set.
- `BenchmarkResult.report_status` (`Literal["ok","failed","skipped"]`) + `.report_error` (`str | None`) on the M1 model.
- `BenchmarkTaskResult.task_type` and `.score` (additive; populated from ATP `metadata.task_type` when present).
- `BenchmarkRunner.run(..., run_id: str | None = None)` — caller-provided `run_id` overrides ATP's for CI-retry idempotency.
- `ArbiterClient.report_benchmark_raw(payload)` — low-level MCP method.
- `ArbiterContractError(code, message, data)` — distinguishes JSON-RPC contract breaks (`-32600`/`-32602`/`-32603`) from transient `ArbiterUnavailable`.
- Vendored client: `ARBITER_PROTOCOL_VERSION = "1.1.0"`, `MIN_ARBITER_PROTOCOL = (1, 1)`, `ARBITER_VENDORED_FROM_SHA = "7aeb6b1..."`; `start()` validates server-advertised `protocolVersion` (major-mismatch → `ArbiterContractError`, minor-low → WARNING).
- `_cowork_output/benchmark-contract/report_benchmark-v1.schema.json` — single source of truth (schema-first).
- `scripts/smoke_benchmark_report.py` — CI smoke against a real arbiter subprocess.
- 5 distinct observability events: `benchmark.report.{skipped,succeeded,duplicate,failed,contract_break}` (contract_break gets ERROR severity).

### Configuration
- `MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK` env override (default 200) for per_task truncation.

### Changed
- `ARBITER_MCP_REQUIRED_VERSION` bumped `"0.1.0" → "0.2.0"` to match arbiter Phase 1 binary.
- `_send_and_receive` now raises `ArbiterContractError` (not `ArbiterUnavailable`) on JSON-RPC error codes -32600/-32602/-32603.

### Tests
- New: `tests/test_benchmark_arbiter_report.py` (~33 tests — projection, classification, helper paths, obs emit), `tests/test_benchmark_contract.py` (~9 tests — JSONSchema validation + forward-compat), `tests/test_arbiter_real_subprocess_benchmark.py` (3 e2e cases: created + duplicate + contract_break), `tests/test_arbiter_client_version.py` (5 version-sync tests), `tests/test_arbiter_errors.py` (4 contract-error tests), `tests/test_benchmark_models.py` (4 additive-field tests).
- Extended: `tests/test_arbiter_client.py` (+5 method/error-classification tests), `tests/test_benchmark_runner.py` (+4 run_id/task_type tests), `tests/test_benchmark_atp_client.py` (+3 task_type extraction tests).

### Cross-repo
- Requires `arbiter-mcp` at SHA `151004be4f0cf7ed20d3e734de8aaecf6b67c0ed` (PR #13 merge — latest behavioural change for `report_benchmark`: validation/runtime error classification, non-empty IDs, RFC3339 `ts`) or later. Earliest compatible SHA is `7aeb6b1a987a2610c9f2cddb38d90f42d849da42` (initial M4 baseline) — `151004be` is the recommended pin because it includes the input-validation hardening from PR #13. Advertises `protocolVersion="1.1.0"`, new `report_benchmark` MCP tool, `benchmark_runs` table migration.

Design: `docs/superpowers/specs/2026-05-23-r06b-m4-arbiter-wiring-design.md`.
Plan: `docs/superpowers/plans/2026-05-23-r06b-m4-arbiter-wiring.md`.

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
  workstreams, run each in isolated git worktrees via spec-runner, auto-create PRs
- Spawner registry with 4 built-in spawners (claude_code, codex, aider, announce)
- SQLite state persistence with crash recovery
- CLI: run, status, retry, stop, orchestrate, workstreams, workspaces
- Web dashboard with DAG visualization and SSE updates
- Desktop notifications (macOS/Linux)
- Auto-commit per task with git diff summary
- Dogfood-tested: Maestro builds itself (3 weeks of real usage)
