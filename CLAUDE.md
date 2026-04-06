# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Maestro is an AI Agent Orchestrator with two operation modes:

1. **Task Scheduler** (`maestro run`) — coordinates multiple AI coding agents (Claude Code, Codex, Aider) on tasks defined in a single YAML config. All tasks share one directory.

2. **Multi-Process Orchestrator** (`maestro orchestrate`) — decomposes a project into independent work units ("zadachi"), runs each in an isolated git worktree via spec-runner, and creates PRs on completion.

## Development Commands

```bash
# === Task Scheduler (original mode) ===
uv run maestro run <config.yaml>
uv run maestro run config.yaml --resume  # Resume after crash
uv run maestro status --db maestro.db
uv run maestro retry <task-id> --db maestro.db

# === Multi-Process Orchestrator (new mode) ===
uv run maestro orchestrate <project.yaml>   # Run orchestrator
uv run maestro zadachi --db maestro.db       # Show zadachi status
uv run maestro workspaces <project.yaml>     # List active worktrees

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
- **models.py**: Pydantic models (Task, TaskStatus, Zadacha, ZadachaStatus, OrchestratorConfig)
- **config.py**: YAML parsing with defaults merging, env var substitution, `load_orchestrator_config()`
- **database.py**: SQLite layer with async CRUD, WAL mode (tasks + zadachi tables)
- **dag.py**: DAG building, cycle detection, topological sort, scope overlap warnings
- **git.py**: Git operations (branch, rebase, push, worktree, merge)
- **cli.py**: Typer CLI (run, status, retry, stop, approve, orchestrate, zadachi, workspaces)
- **scheduler.py**: Main scheduler loop — polls DAG, spawns agents, monitors completion
- **validator.py**: Post-task validation (run validation_cmd)
- **retry.py**: Exponential backoff retry logic with jitter
- **recovery.py**: State recovery after crash
- **cost_tracker.py**: Token usage parsing and cost calculation
- **event_log.py**: Structured event logging for task lifecycle

**Multi-process orchestration (new):**
- **orchestrator.py**: Main async loop — decompose, spawn, monitor, PR creation
- **workspace.py**: Git worktree lifecycle (create, setup, cleanup)
- **decomposer.py**: Project decomposition via Claude CLI into zadachi + spec generation
- **pr_manager.py**: GitHub PR creation via `gh` CLI

**Subpackages:**
- **spawners/**: AgentSpawner ABC + implementations (claude_code, codex, aider, announce) + registry
- **coordination/**: MCP server (FastMCP) + REST API (FastAPI) with /zadachi endpoints
- **notifications/**: Desktop notifications (macOS/Linux)
- **dashboard/**: Web UI with DAG visualization (Mermaid.js) + SSE updates

### Task State Machine (scheduler mode)

```
PENDING -> READY -> RUNNING -> VALIDATING -> DONE
                      |            |
                   FAILED <- (validation failed)
                      |
               NEEDS_REVIEW -> (manual) -> READY
```

### Zadacha State Machine (orchestrator mode)

```
PENDING -> DECOMPOSING -> READY -> RUNNING -> MERGING -> PR_CREATED -> DONE
                                     |                       |
                                  FAILED                  FAILED
                                     |
                              NEEDS_REVIEW -> (manual) -> READY
```

### Key Design Decisions

- **Two modes**: Scheduler for single-process tasks, Orchestrator for multi-process isolation
- **Workspace isolation**: git worktree per zadacha (lightweight, shares .git)
- **Two-level hierarchy**: Orchestrator manages zadachi, spec-runner manages subtasks within each
- **Git strategy**: `feature/<zadacha-id>` branch per zadacha, subtask branches merge into it, then PR to main
- **Communication**: REST API callbacks from spec-runner (state file polling deprecated)
- **Conflict prevention**: Zadachi define `scope` (file/dir globs), decomposer validates non-overlap
- **Storage**: SQLite (single file, no external services)
- **Spec-runner**: External package (PyPI) handles subtask execution within a worktree

### Orchestrator Flow

```
1. Load project.yaml
2. Decompose project into zadachi (Claude CLI or manual config)
3. For each ready zadacha:
   a. Create git worktree + branch
   b. Generate spec/tasks.md via Claude CLI (read-only tools)
   c. Write spec-runner.config.yaml
   d. Commit spec in feature branch
   e. Spawn `spec-runner run --all` subprocess
4. Monitor processes (poll returncode + callbacks)
5. On success: auto-merge feature branch into base, create PR (if auto_pr), cleanup worktree
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
