# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Maestro is an AI Agent Orchestrator - a daemon/scheduler that coordinates multiple AI coding agents (Claude Code, Codex, Aider) working on different parts of the same project. It manages task dependencies as a DAG, automatically spawns agents, monitors execution, and provides coordination APIs (MCP + REST).

## Development Commands

```bash
# Run the application
uv run python main.py

# Run tests
uv run pytest

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

The planned structure (see `agent-orchestrator-spec.md` for full details):

- **Task Engine**: Parses YAML task definitions, builds/validates DAG, manages task state machine
- **Scheduler**: Main loop (resolve → spawn → monitor), handles parallelism limits
- **Agent Spawners**: Plugin architecture for different agents (Claude Code, Codex, Aider, announce-only)
- **Coordination API**: MCP server + FastAPI REST endpoints backed by SQLite
- **Notifications**: Desktop, Telegram, webhooks

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
- FastAPI + uvicorn for REST API
- FastMCP for MCP server
- SQLite for state persistence
- PyYAML + jsonschema for configuration
- Pydantic for data models
- AutoGen for agent orchestration patterns
