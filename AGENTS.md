# AGENTS.md

Guidance for AI agents (Codex, Claude Code, Aider, OpenCode, etc.) working in this repository.

## Project Overview

**Maestro** is an AI Agent Orchestrator (Python 3.12+, MIT) with two operation modes:

1. **Task Scheduler** (`maestro run`) — coordinates multiple AI coding agents (Claude Code, Codex, Aider, OpenCode) on tasks defined in a single YAML config inside a shared directory.
2. **Multi-Process Orchestrator** (`maestro orchestrate`) — decomposes a project into independent work units ("workstreams"), runs each in an isolated git worktree via spec-runner, and creates PRs on completion.

Key invariants:
- **Never commit to `master` directly.** All changes go through a branch → PR workflow.
- **Never use `pip`** to manage dependencies — use `uv add` / `uv add --dev`.
- **Shipped/runtime code must never read from `../_cowork_output/`** (a dev-only sibling workspace). Vendoring a pinned copy inside this repo is the correct pattern.
- **Never edit neighbour repos** (`../arbiter/`, `../spec-runner/`, etc.). Cross-repo changes belong in a handoff note.

---

## Development Commands

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest
uv run pytest tests/test_models.py -v       # Single file
uv run pytest -k "test_dag" -v              # By name pattern

# Type checking
uv run pyrefly check

# Lint and format
uv run ruff check .
uv run ruff check . --fix
uv run ruff format .

# Add dependencies
uv add <package>
uv add --dev <package>
```

### CLI reference

```bash
# Task Scheduler (Mode 1)
uv run maestro run <config.yaml>
uv run maestro run config.yaml --resume          # Resume after crash
uv run maestro status --db maestro.db
uv run maestro retry <task-id> --db maestro.db
uv run maestro stop
uv run maestro approve <task-id> --db maestro.db # Approve AWAITING_APPROVAL task

# Multi-Process Orchestrator (Mode 2)
uv run maestro orchestrate <project.yaml>
uv run maestro workstreams --db maestro.db
uv run maestro workstream-approve <workstream-id> --db maestro.db
uv run maestro check-scope <workstream-id> --base <base-branch> --db maestro.db
uv run maestro workspaces <project.yaml>

# Config authoring
uv run maestro init                              # Scaffold project.yaml from cwd
uv run maestro validate project.yaml             # Preflight checks
uv run maestro validate project.yaml --strict --no-fs  # CI mode

# Model catalog (ADR-ECO-003b)
uv run maestro models init --path ~/.config/atp/agents-catalog.toml
uv run maestro models list
uv run maestro models discover --observed observed.json
uv run maestro models update --observed observed.json --dry-run

# Benchmarking
uv run maestro benchmark swe-mini --agent claude_code
uv run maestro benchmark swe-mini --agent opencode --json

# Log utilities
uv run maestro merge-logs <pipeline-dir>
```

---

## Architecture

### Core modules (`maestro/`)

**Shared infrastructure:**

| Module | Purpose |
|---|---|
| `models.py` | Pydantic models: `Task`, `TaskStatus`, `Workstream`, `WorkstreamStatus`, `OrchestratorConfig` |
| `config.py` | YAML parsing, defaults merging, env var substitution, `load_orchestrator_config()` |
| `catalog.py` | Model catalog loader. `resolve_model()` precedence: `routed > MAESTRO_<H>_MODEL > catalog-default > fail-loud`. No baked default — `$ATP_CATALOG` required. Faults: `CatalogError` (global) vs `HarnessModelUnresolved` (per-task) |
| `catalog_cli.py` | `maestro models init|list|discover|update` Typer sub-app |
| `catalog_discovery.py` | Pure diff logic for `maestro models` — observed-manifest contract, alias-aware detection |
| `database.py` | Async SQLite CRUD (aiosqlite), WAL mode |
| `dag.py` | DAG building, cycle detection, topological sort, scope overlap warnings |
| `git.py` | Git operations: branch, rebase, push, worktree, merge |
| `cli.py` | Typer CLI entry point |
| `scheduler.py` | Main scheduler loop: poll DAG, spawn agents, monitor completion |
| `validator.py` | Post-task validation via `validation_cmd` |
| `retry.py` | Exponential backoff with jitter |
| `recovery.py` | State recovery after crash |
| `cost_tracker.py` | Token usage parsing and cost calculation |
| `event_log.py` | Structured event logging for task lifecycle |
| `preflight.py` | Mode-2 config validation (`maestro validate`), also runs as fail-fast gate inside `maestro orchestrate` |
| `scaffold.py` | `maestro init` — generates commented `project.yaml` with git-derived autofill |
| `spec_runner.py` | Integration boundary with external spec-runner subprocess |
| `correlation.py` | WorkCorrelation v1 reference implementation |
| `logging_bridge.py` | Routes stdlib `logging` records into the obs JSONL pipeline (`setup_logging()` — use this, not raw `obs.init_logging`, at entry points) |
| `transitions.py` | Declarative status-transition → side-effect mapping and dispatcher |
| `gates.py` | Gates-in-DAG runtime: guard hooks on workstream transition edges (ex-ante / ex-post) |
| `gate_approvals.py` | Approval-marker primitives (durable approval memory, H-6) |
| `scope_gate.py` | Pure scope-containment matcher (`find_escapes`) — no git/FS/DB |
| `changed_paths.py` | Git changed-paths source for the scope gate (workstream's own commits since branch point) |
| `merge_logs.py` | `maestro merge-logs` — time-sorts per-pid JSONL into `merged.jsonl`; tolerates partial runs |

**Multi-process orchestration:**

| Module | Purpose |
|---|---|
| `orchestrator.py` | Main async loop: decompose, spawn, monitor, PR creation, crash recovery |
| `workspace.py` | Git worktree lifecycle: create, setup, cleanup |
| `decomposer.py` | Workstream decomposition via Claude CLI; spec generation via `spec-runner plan --full` |
| `pr_manager.py` | GitHub PR creation via `gh` CLI |

**Subpackages:**

| Package | Purpose |
|---|---|
| `spawners/` | `AgentSpawner` ABC + implementations (`claude_code`, `codex_cli`, `aider`, `announce`, `opencode`) + registry |
| `coordination/` | MCP server (FastMCP) + REST API (FastAPI) with `/workstreams` endpoints; Arbiter routing |
| `benchmark/` | Benchmark-aware routing, ATP client, Arbiter feedback wiring |
| `notifications/` | Desktop notifications (macOS/Linux) |
| `dashboard/` | Web UI with DAG visualization (Mermaid.js) + SSE updates |
| `schemas/` | JSON-schema generation for config/contract artifacts |
| `execution/` | Transport-agnostic run contract + backends (local, Docker, SSH) with recovery/reservations |
| `resources/` | Packaged data files (TOML), shipped via `[tool.setuptools.package-data]` |
| `_vendor/` | Vendored observability lib (`obs.py`) — structlog spans, trace propagation |

### State machines

**Task (scheduler mode):**
```
PENDING -> READY -> RUNNING -> VALIDATING -> DONE
             |        |  |         |
             |        |  |         └-> FAILED -> READY (retry)
             |        |  └──FAILED──────┴-> NEEDS_REVIEW -> READY
             |        └──NEEDS_REVIEW────────────────────┘
             |                            └-> ABANDONED
             └-> AWAITING_APPROVAL -> READY
                       └-> ABANDONED
```

**Workstream (orchestrator mode):**
```
PENDING -> DECOMPOSING -> READY -> RUNNING -> MERGING -> PR_CREATED -> DONE
                            |         |
                            |         └-> FAILED -> READY (retry)
                            |               └-> NEEDS_REVIEW -> READY
                            └-> ABANDONED
```

---

## Coding Conventions

- **Python 3.12+** syntax and type hints throughout.
- **Pydantic v2** for all data models.
- **`async`/`await`** for I/O-bound work (database, subprocesses, HTTP).
- **Ruff** for linting and formatting (`line-length = 88`, `quote-style = "double"`).
- **Pyrefly** for type checking.
- Tests live in `tests/`; use `pytest` with `asyncio_mode = "auto"`.
- Coverage floor is **80%** (`fail_under = 80` in `pyproject.toml`).
- Do not remove or weaken existing tests.

### Adding a new agent spawner

1. Create `maestro/spawners/<name>.py` implementing `AgentSpawner` (see `base.py`).
2. Register it in `pyproject.toml` under `[project.entry-points."maestro.spawners"]`.
3. Add tests in `tests/test_spawners.py`.

### Model resolution

Models are resolved at runtime, never baked in. The precedence is:

1. Arbiter-routed model (if `arbiter:` section present)
2. `MAESTRO_<HARNESS>_MODEL` env var
3. Catalog default (from `$ATP_CATALOG`)
4. Fail loud with `CatalogNotConfigured`

---

## Git Workflow

- Branch naming: `<type>/<slug>` (e.g., `feat/new-spawner`, `fix/recovery-bug`).
- Open a PR; never push directly to `master`.
- After CI and code review pass, the user performs the merge — agents must not merge.
- Force-push to shared branches is forbidden.

---

## Repo Boundaries

This repo is `maestro`. Neighbour repos (`../arbiter/`, `../spec-runner/`, etc.) are **read-only references** — never edit their files. Cross-repo contracts must be vendored as pinned copies inside `maestro/`.

---

## Tech Stack

| Component | Library/Tool |
|---|---|
| Runtime | Python 3.12+, uv |
| Web / API | FastAPI + uvicorn |
| MCP server | FastMCP |
| State | SQLite via aiosqlite |
| Config | PyYAML |
| Data models | Pydantic v2 |
| CLI | Typer + Rich |
| Workspace isolation | git worktree |
| PR creation | gh CLI |
| Subtask execution | spec-runner — external subprocess from the sibling repo, not an import; expected version pinned in `maestro/spec_runner.py::SPEC_RUNNER_REQUIRED_VERSION` |
| Observability | structlog (vendored `_vendor/obs.py`) |
