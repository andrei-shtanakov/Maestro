# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Maestro is an AI Agent Orchestrator - a daemon/scheduler that coordinates multiple AI coding agents (Claude Code, Codex, Aider) working on different parts of the same project. It manages task dependencies as a DAG, automatically spawns agents, monitors execution, and provides coordination APIs (MCP + REST).

## Development Commands

```bash
# Run the orchestrator
uv run maestro run <config.yaml>
uv run maestro run config.yaml --resume  # Resume after crash

# Check status
uv run maestro status --db maestro.db

# Retry failed task
uv run maestro retry <task-id> --db maestro.db

# Run tests
uv run pytest
uv run pytest tests/test_models.py -v  # Single file
uv run pytest -k "test_dag" -v         # By pattern

# Type checking
pyrefly check

# Linting and formatting
uv run ruff format .
uv run ruff check .
uv run ruff check . --fix

# Add dependencies (NEVER use pip)
uv add <package>
uv add --dev <package>
```

## Architecture

Core modules in `maestro/`:

- **models.py**: Pydantic models (Task, TaskStatus, TaskConfig, ProjectConfig)
- **config.py**: YAML parsing with defaults merging and env var substitution
- **database.py**: SQLite layer with async CRUD, WAL mode
- **dag.py**: DAG building, cycle detection, topological sort, scope overlap warnings
- **scheduler.py**: Main asyncio loop (resolve → spawn → monitor)
- **cli.py**: Typer CLI (run, status, retry, stop commands)
- **git.py**: Git operations (branch creation, rebase, push)
- **validator.py**: Post-task validation (run validation_cmd)
- **retry.py**: Exponential backoff retry logic
- **recovery.py**: State recovery after crash
- **cost_tracker.py**: Token usage parsing and cost calculation

Subpackages:
- **spawners/**: AgentSpawner ABC + implementations (claude_code, codex, aider, announce) + registry
- **coordination/**: MCP server (FastMCP) + REST API (FastAPI)
- **notifications/**: Desktop notifications (macOS/Linux)
- **dashboard/**: Web UI with DAG visualization (Mermaid.js) + SSE updates

### Task State Machine

```
PENDING → READY → RUNNING → VALIDATING → DONE
                     ↓           ↓
                  FAILED ← (validation failed)
                     ↓
              NEEDS_REVIEW → (manual) → READY
```

### Key Design Decisions

- **Git strategy**: Separate branch per task (`agent/<task-id>`), integration branches for related tasks
- **Conflict prevention**: Tasks define `scope` (file/dir globs), orchestrator warns on overlaps
- **Storage**: SQLite (single file, no external services), all components communicate via API
- **Cost tracking**: Parse token usage from agent logs, store in `task_costs` table

## Tech Stack

- Python 3.12+, uv for package management
- FastAPI + uvicorn for REST API and dashboard
- FastMCP for MCP server
- SQLite (aiosqlite) for state persistence
- PyYAML for configuration
- Pydantic for data models
- Typer + Rich for CLI
