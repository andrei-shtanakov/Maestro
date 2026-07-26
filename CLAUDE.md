# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## `../_cowork_output/` is dev-only — never a code/runtime resource

`../_cowork_output/` (the polyrepo **sibling** workspace — not to be confused with this repo's own local `./_cowork_output/` scratch directory) is the development-time coordination area (cross-team ADRs, status notes, contract drafts, PM/dev tooling). Users and teams installing or cloning this project do NOT have it. Rules:

- Shipped/runtime code must never read, import, or resolve paths under `../_cowork_output/`.
- Canonical shippable facts live inside the owning repo: the ecosystem agents-catalog SSOT is `atp-platform/method/agents-catalog.toml` (ADR-ECO-003, canon confirmed 2026-07-03); Maestro's spawner model defaults are (to be) generated from that catalog (ADR-ECO-003 action #4), never resolved from `../_cowork_output/` at runtime.
- Vendoring a pinned copy INTO a repo is the correct pattern; referencing OUT to `../_cowork_output/` from shipped code is the antipattern.
- Only workspace-local dev tooling (e.g. the conformance check in `../_cowork_output/devtools/`) and documentation may reference it.

## Project Overview

Maestro is an AI Agent Orchestrator with two operation modes:

1. **Task Scheduler** (`maestro run`) — coordinates multiple AI coding agents (Claude Code, Codex, Aider) on tasks defined in a single YAML config. All tasks share one directory.

2. **Multi-Process Orchestrator** (`maestro orchestrate`) — decomposes a project into independent work units ("workstreams"), runs each in an isolated git worktree via spec-runner, and creates PRs on completion.

## Development Commands

```bash
# === Task Scheduler (original mode) ===
uv run maestro run <config.yaml>
uv run maestro run config.yaml --resume  # Resume after crash
uv run maestro status --db maestro.db
uv run maestro retry <task-id> --db maestro.db
uv run maestro stop                          # Stop the running scheduler
uv run maestro approve <task-id> --db maestro.db  # Approve an AWAITING_APPROVAL task

# === Multi-Process Orchestrator (new mode) ===
uv run maestro orchestrate <project.yaml>   # Run orchestrator
uv run maestro workstreams --db maestro.db       # Show workstreams status
uv run maestro workstream-approve <workstream-id> --db maestro.db  # Approve a NEEDS_REVIEW workstream — records the durable gate approval (phase+sha) and re-queues
uv run maestro check-scope <workstream-id> --base <base-branch> --db maestro.db  # deterministic scope containment (exit 1 on escape)
uv run maestro workspaces <project.yaml>     # List active worktrees

# === Mode-2 config authoring ===
uv run maestro init                          # Scaffold project.yaml from cwd
uv run maestro validate project.yaml         # Preflight: cycles, scope overlap, repo sanity
uv run maestro validate project.yaml --strict --no-fs  # CI mode, no filesystem access

# === Model catalog management (ADR-ECO-003b D3) ===
uv run maestro models init --path ~/.config/atp/agents-catalog.toml   # Scaffold user catalog
uv run maestro models list                                            # Show resolved catalog
uv run maestro models discover --observed observed.json               # Propose additions (exit 2 = new found)
uv run maestro models update --observed observed.json --dry-run       # Apply proposals (Plane 1 only)

# === Agent benchmarking (R-06b M5) ===
uv run maestro benchmark swe-mini --agent claude_code            # Run one ATP benchmark
uv run maestro benchmark swe-mini --agent opencode --json        # Machine output (stdout = JSON)
# MAESTRO_ARBITER_BIN set -> result reported to arbiter (fire-and-forget)

# === Log utilities ===
uv run maestro merge-logs <pipeline-dir>     # Time-sort per-pid JSONL into merged.jsonl
uv run maestro costs --db maestro.db   # database-wide cost summary (read-only; TOTAL / by-harness / by-task; unpriced = UNKNOWN, not $0)

# === Tests ===
uv run pytest
uv run pytest tests/test_models.py -v       # Single file
uv run pytest -k "test_dag" -v              # By pattern

# === Type checking ===
uv run pyrefly check

# === Linting and formatting ===
uv run ruff format .
uv run ruff check .
uv run ruff check . --fix

# === Dependencies (NEVER use pip) ===
uv add <package>
uv add --dev <package>
```

## Architecture

### Core modules in `maestro/`

**Shared infrastructure:**
- **models.py**: Pydantic models (Task, TaskStatus, Workstream, WorkstreamStatus, OrchestratorConfig)
- **config.py**: YAML parsing with defaults merging, env var substitution, `load_orchestrator_config()`
- **catalog.py**: Model catalog loader (ADR-ECO-003b). `resolve_model()` applies the precedence `routed > MAESTRO_<H>_MODEL > catalog-default > fail-loud`; the catalog (loaded from `$ATP_CATALOG`, no baked default) supplies only the last-resort *default* layer, used when neither a routed model nor the env var provides one. Also emits a status-graded coherence warning. Fault taxonomy by blast radius: `CatalogError` (global — halts the run) vs `HarnessModelUnresolved` (per-task — sends that task to `NEEDS_REVIEW`)
- **catalog_discovery.py**: Pure diff logic for `maestro models` — observed-manifest contract (missing vendor key = not observed; empty list = observed-and-empty), alias-aware new-model detection, vendor-conflict reporting, TOML-escaped Plane-1 rendering
- **catalog_cli.py**: `maestro models init|list|discover|update` Typer sub-app (ADR-ECO-003b D3) — init from the shipped inert template (`maestro/resources/`), read-only discover (public exit contract 0/2/1), update via validate-then-atomic-replace
- **database.py**: SQLite layer with async CRUD, WAL mode (tasks + workstreams tables)
- **dag.py**: DAG building, cycle detection, topological sort, scope overlap warnings
- **git.py**: Git operations (branch, rebase, push, worktree, merge)
- **cli.py**: Typer CLI (run, status, retry, stop, approve, orchestrate, workstreams, workstream-approve, check-scope, workspaces, merge-logs, costs, models)
- **scheduler.py**: Main scheduler loop — polls DAG, spawns agents, monitors completion
- **validator.py**: Post-task validation (run validation_cmd)
- **retry.py**: Exponential backoff retry logic with jitter
- **recovery.py**: State recovery after crash
- **cost_tracker.py**: Token usage parsing and cost calculation
- **event_log.py**: Structured event logging for task lifecycle
- **merge_logs.py**: Standalone merge-logs CLI — time-sorts per-pid JSONL into merged.jsonl
- **preflight.py**: Mode-2 config validation — ValidationReport (errors/warnings), dangling-dep + cycle detection (shared dag.find_cycle), two-tier scope-overlap (static heuristic + exact file-set intersection), repo/glob filesystem checks; runs standalone (`maestro validate`) and as a fail-fast gate inside `maestro orchestrate`
- **scaffold.py**: `maestro init` generator — commented project.yaml template with git-derived autofill, self-checked against OrchestratorConfig before writing
- **spec_runner.py**: Integration boundary between Maestro and the external spec-runner
- **correlation.py**: WorkCorrelation v1 reference implementation (`contracts/work-correlation/`) — Maestro mints `work_item_id` (= task/workstream id), surjective status projection onto the common enum with `source_status` kept verbatim, spec↔DAG bridge (`<parent>/<TASK-nnn>` + `source_locator`)

**Multi-process orchestration (new):**
- **orchestrator.py**: Main async loop — decompose, spawn, monitor, PR creation. On resume it first reconciles workstreams stranded by a prior hard crash (DECOMPOSING/RUNNING/MERGING/PR_CREATED → READY; a live-orphan RUNNING (process_pid) or DECOMPOSING (generation_pid) → NEEDS_REVIEW; FAILED by the retry rule) so the main loop can advance them. The spawn→persist window (crash between spawning a subprocess and persisting its pid) is closed by a spawning sentinel written before the spawn: recovery reads it as a possible live orphan → NEEDS_REVIEW. A recovery NEEDS_REVIEW thus means either a confirmed live orphan or a spawn-in-progress (state uncertain).
- **workspace.py**: Git worktree lifecycle (create, setup, cleanup)
- **decomposer.py**: Project decomposition via Claude CLI into workstreams (`decompose`) + async spec generation delegated to `spec-runner plan --full` (`generate_spec` — spec-runner owns the tasks.md format; runs as a background task in the orchestrator, budget-capped via `SpecRunnerConfig.spec_gen_budget_usd`)
- **pr_manager.py**: GitHub PR creation via `gh` CLI

**Subpackages:**
- **spawners/**: AgentSpawner ABC + implementations (claude_code, codex_cli, aider, announce, opencode) + registry. opencode (`opencode run --format json -m opencode/<model>`, ADR-ECO-003c) is the first open-model agentic harness: open models (glm-5.1, qwen3.6, …) reach routing as `opencode@<model>`. Its cost comes from opencode's own per-step `part.cost` (persisted as `TaskCost.reported_cost_usd`); when the log reports none, the arbiter sees unknown (`cost_usd=None`), never 0.0
- **coordination/**: MCP server (FastMCP) + REST API (FastAPI) with /workstreams endpoints; Arbiter routing (`routing.py` strategies, vendored `arbiter_client.py` MCP client, `arbiter_errors.py`)
- **benchmark/**: R-06b/R-07 benchmark-aware routing — async runner, ATP client, spawner→responder adapter, and Arbiter feedback wiring (`arbiter_report.py`)
- **notifications/**: Desktop notifications (macOS/Linux)
- **dashboard/**: Web UI with DAG visualization (Mermaid.js) + SSE updates
- **schemas/**: JSON-schema generation for config/contract artifacts
- **_vendor/**: Vendored observability lib (`obs.py`) — structlog-based spans, trace propagation, and `child_env()` for cross-process trace continuity

### Task State Machine (scheduler mode)

```
PENDING -> READY -> RUNNING -> VALIDATING -> DONE
             |        |  |         |
             |        |  |         └-> FAILED -> READY (retry)
             |        |  |              |
             |        |  └──FAILED──────┴-> NEEDS_REVIEW -> READY
             |        |                      |
             |        └──NEEDS_REVIEW────────┘   (catalog default unresolved for harness)
             |                            |
             |                            └-> ABANDONED
             |
             └-> AWAITING_APPROVAL -> READY   (requires_approval; `maestro approve
                       |             <task-id>` sets READY, then scheduler runs it)
                       └-> ABANDONED
```

A verifier-enabled task (project-level `verifier:` block + the task has both
`validation_cmd` and a non-empty `scope`) inserts a third phase between
`VALIDATING` and `DONE`: `VALIDATING -> VERIFYING -> {DONE (PASS), FAILED/READY
(FAIL, retried), NEEDS_REVIEW (ERROR, fail-closed)}`. See the verifier gate
bullet under Key Design Decisions below. No `verifier:` block keeps
`VALIDATING -> DONE` byte-identical to today.

### Workstream State Machine (orchestrator mode)

```
PENDING -> DECOMPOSING -> READY -> RUNNING -> MERGING -> PR_CREATED -> DONE
                            |        |  └-> FAILED -> READY (retry)
                            |        |              └-> NEEDS_REVIEW
                            |        └-> VERIFYING   (domain verification)
                            |               |-> PASS + evidence commit  -> MERGING
                            |               |-> FAIL, rework left       -> READY
                            |               |-> FAIL, budget exhausted  -> NEEDS_REVIEW
                            |               |-> ERROR retries exhausted -> FAILED
                            |               └-> live orphan/ambiguity   -> NEEDS_REVIEW
                            |        (READY + reverify marker -> VERIFYING;
                            |         finalization happens INSIDE VERIFYING)
                            └-> ABANDONED
```

An ex-post gate block that the operator approved resumes at the ex-post edge (H-6): READY + approval marker + unchanged worktree HEAD -> straight to MERGING tail, no regen/respawn; the marker in error_message clears only at DONE.

### Key Design Decisions

- **Two modes**: Scheduler for single-process tasks, Orchestrator for multi-process isolation
- **Workspace isolation**: git worktree per workstream (lightweight, shares .git)
- **Two-level hierarchy**: Orchestrator manages workstreams, spec-runner manages subtasks within each
- **Git strategy**: `feature/<workstream-id>` branch per workstream, subtask branches merge into it, then PR to main
- **Communication**: REST API callbacks from spec-runner (state file polling deprecated) for local/Docker backends. Phase 2a SSH backend (remote/NAT'd executors, unreachable for inbound callbacks) deliberately reintroduces polling in a different shape: a WAL-safe `sqlite3.backup()` snapshot of the remote spec-runner DB mirrored back over SSH each tick (`maestro/execution/ssh_mirror.py`), not the old raw state-file poll. Docker Isolation Phase 1 adds a container-backed execution path (`backend: docker`, local Docker isolation); recovery for docker-backed executions keys off the execution-handle label, not a pid. Phase 2b enables **Mode-1 remote** (`maestro run` on an ssh backend): a static per-workdir arming gate + a `(workdir, scope)` reservation lock (conservative path-anchor overlap) + scope-bounded collect. Validation still runs locally after collect (`validation_backend` deferred); collect is `scope_paths` only (patch-collect deferred); local Docker Mode-1 is unchanged. Phase 2c enables **SSH + Docker isolation** (`transport: ssh` + `isolation: {type: docker}`): the center rewrites the launch argv into a remote `docker run` (supervisor unchanged), drives container lifecycle/recovery via `DockerCli` run over SSH, defaults the container user to the remote uid:gid (root only via explicit `user: "0:0"`), and recovery probes BOTH the remote process-group and the container (fail-closed → NEEDS_REVIEW).
- **Validation backend (PR1 + PR2 + PR3)**: post-task validation runs through the execution layer as a second `ExecutionRequest` (`validation_backend: local | same | <name>`, default **`same`** since PR3). Non-local (docker/named-local/SSH) validation is durable — its own `execution_id` + `execution_phase='validation'` handle. PR2 adds SSH validation: a fresh remote layout, a real `CollectPolicy(none)` no-op in `SshTaskHandle` (no collect for validation), and dual-probe recovery routed through `ExecutionBackend.probe()` as the single isolation-aware boundary (bare + docker, fail-closed → NEEDS_REVIEW) shared by Mode-1 `StateRecovery` and the Mode-2 orchestrator; GC is transport-aware (never docker-GCs an SSH terminal handle). `check_validation_backends` no longer fails loud on an SSH target; `maestro validate` remains Mode-2 only. **PR3 flipped the default `validation_backend` `local -> same`** (release-noted in CHANGELOG): validation runs in the task's own backend by default — a no-op when the task runs on bare `local` (`same` -> `default_backend` -> `local`), and a durable in-backend validation when the task runs on docker/SSH. Existing persisted tasks keep their recorded value (no data-migration; migration 12 rebuilds `tasks` only to change the column default, under `foreign_keys=OFF` so CASCADE children survive).
- **Verifier gate (Mode-1, opt-in)**: distinct from "Domain verification (Stage B)" below (that one is Mode-2/workstream-level; this is Mode-1/task-level). A third task phase, `VERIFYING`, gated by an adversarial LLM judge over the task's scope-bounded diff — inserted only when `Scheduler._verifier_enabled` holds: a project-level `verifier:` block is present AND the task has both a `validation_cmd` and a non-empty `scope` (nothing to gate, or no bounded diff to judge, otherwise). Config (`maestro/models.py::VerifierConfig`): `runner: claude` is the only supported value this slice; `model` is required, via `verifier.model` or `$MAESTRO_VERIFIER_MODEL` (`maestro/verifier/config.py::resolve_verifier_model` — a precedence isolated from the main harness's `resolve_model`: never reads `MAESTRO_<HARNESS>_MODEL`, never falls back to a catalog default, so the judge can't silently end up on an expensive main model); `timeout_seconds`, `max_diff_bytes`; `backend: local` only — "policy isolation" (scratch cwd, no collect, envelope on stdin, `inherit_env=False`), not OS isolation. Reuses the shared execution layer with `execution_phase="verification"` (same recovery/probe machinery as task/validation executions) — migrations 16/17 add `tasks.verifier_baseline_sha` (nullable, the gate's diff baseline) and `task_costs.execution_phase`/`task_costs.model`. Fail-closed routing: PASS -> `DONE` (+ auto-commit); FAIL -> the existing retry path (judge findings folded into the retry context, same NEEDS_REVIEW-on-exhaustion as any validation failure); ERROR (bad envelope, judge-process crash, unresolvable model, oversize/binary diff) -> `NEEDS_REVIEW` straight from VALIDATING/VERIFYING, never softened to FAIL. Once verifier-enabled, a task's `(workdir, scope)` reservation is held for its WHOLE lifecycle — first dispatch through every retry and NEEDS_REVIEW, releasing only at DONE/ABANDONED (unlike the post-collect release for non-verifier tasks) — for unambiguous diff attribution; a restart reconstructs this from any non-terminal task with a recorded baseline, not just an open execution handle. Durable + recoverable: a crash mid-`VERIFYING` is recovered fail-closed straight to `NEEDS_REVIEW` (never auto-re-run), reconciling the `execution_phase='verification'` handle once the judge process is provably dead; `maestro retry`'s `NEEDS_REVIEW -> READY` requeue fences on that handle still being open, succeeding once reconciled. `maestro costs` gains `by_phase`/`by_model` breakdowns so verifier judge spend is visible separately from task/validation cost (an envelope with no usage leaves the row UNKNOWN, never $0). See `examples/with-verifier.yaml`.
- **Conflict prevention**: Workstreams define `scope` (file/dir globs), decomposer validates non-overlap
- **Storage**: SQLite (single file, no external services)
- **Spec-runner**: External package (PyPI) handles subtask execution within a worktree
- **Domain verification (Stage B)**: activation is by-presence — `domain.verification.verifier` in a workstream's `project.yaml` turns the VERIFYING phase on; an absent `domain:` (or no `verifier`) takes the pre-Stage-B path byte-identically (zero-change guarantee, proven by the unchanged full test suite). Verdict contract v2 is run-keyed with a strict handshake (echoed run_id/attempt/sha); malformed or mismatched echoes are protocol ERROR, never softened to FAIL. The verifier subprocess never receives `workstream_id`/`rework_attempt` via argv (the argv placeholder set is a small template shared across an entire profile) — all five echo-checked fields (`profile_sha256`, `verified_source_commit`, `verified_source_tree`, `workstream_id`, `rework_attempt`) are conveyed via `MAESTRO_*` env vars instead. The evidence ledger lives at `<db_dir>/evidence/`, outside the worktree, durably recording each attempt and unreachable by the author until delivery. Exactly one evidence commit lands on the branch at delivery, tagged `Maestro-Verification-Run: <run_id>`; finalization checks that trailer first, so re-running it is idempotent across retries/crashes. Rework (author respawn) and reverify (operator-approved resume) are distinct `resume_reason` paths — author respawn fires ONLY on a genuine FAIL, never on ERROR or an ambiguous recovery outcome. `criteria_visibility: verifier_only` is capability-gated in preflight and refused unless the author backend is docker-isolated. `CommandVerifier` runs through the shared execution layer (`execution_phase="verification"`), inheriting the same recovery/probe machinery — including live-orphan re-poll — as task and validation executions.

### Orchestrator Flow

```
1. Load project.yaml
2. Decompose project into workstreams (Claude CLI or manual config)
3. For each ready workstream:
   a. Create git worktree + branch (+ ensure repo-local harness excludes)
   b. Write spec-runner.config.yaml (spec_prefix: maestro-)
   c. Generate spec/maestro-tasks.md via `spec-runner plan --full --spec-prefix maestro-`
   d. Spawn `spec-runner run --all --spec-prefix maestro-` subprocess
      (harness artifacts stay untracked — never committed, never in the PR)
4. Monitor processes (poll returncode + callbacks)
5. On success: create PR (if auto_pr), then merge feature branch into base BEFORE marking DONE (DONE is gated on the merge — a conflict routes the workstream to NEEDS_REVIEW with the worktree left intact; a crash mid-merge is recoverable via startup recovery), then cleanup worktree
6. On failure: retry or mark NEEDS_REVIEW
```

## Tech Stack

- Python 3.12+, uv for package management
- FastAPI + uvicorn for REST API and dashboard
- FastMCP for MCP server
- SQLite (aiosqlite) for state persistence
- PyYAML for configuration
- Pydantic for data models
- Typer + Rich for CLI
- git worktree for workspace isolation
- gh CLI for PR creation
- spec-runner (external) for subtask execution

## Ideas and Docs

- Направление/идеи (авторинг workstream'ов, scaffold/SDK): см. `docs/idea-workstream-framework.md`

## Repo scope & boundaries

- **Этот репо:** `maestro` — git-корень `all_ai_orchestrators/maestro/`, remote `git@github.com:andrei-shtanakov/maestro.git`.
- **Соседи (READ-ONLY reference):** `../arbiter/`, `../atp-platform/`, `../deployer/`, `../dispatcher/`, `../libretto/`, `../proctor/`, `../prograph/`, `../prograph-vault/`, `../robin-runtime/`, `../robin-toolkit/`, `../spec-runner/`, `../spec-runner-vscode/`, `../steward/` — их код не редактировать.
- Нужна правка у соседа → **стоп**: запиши handoff в `../prograph-vault/authored/notes/`
  (кросс-проектное) или `../_cowork_output/` (черновик), не трогай его файлы.
- Кросс-репные контракты — **вендорить пиненой копией внутрь**, не ссылаться наружу.
- Полное правило (SSOT): `../prograph-vault/authored/rules/repo-boundaries.md`.

## Git workflow (у репо есть remote)

- Ветка `<type>/<slug>` → push → `gh pr create`. **Прямые коммиты в `master` запрещены.**
- После открытия PR — прочитать ревью **GitHub Copilot**: валидные замечания исправлять
  новыми коммитами в ту же ветку; невалидные — ответить с обоснованием, **не применять
  вслепую**; итерировать, пока не останется открытых замечаний.
- **Не мержить.** Мерж делает пользователь.
- После мержа пользователем: `git switch master && git pull --ff-only`, затем удалить
  влитую ветку (`git branch -d <branch>`) и `git fetch --prune`; убрать прочие влитые ветки.
- Никогда не делать force-push в общие ветки; не трогать другие репо (см. scope выше).
- Полное правило (SSOT): `../prograph-vault/authored/rules/git-workflow.md`.
