# Maestro

> AI Agent Orchestrator — coordinate multiple coding agents with DAG-based scheduling and git worktree isolation

## Quick Start

```bash
uv add maestro
uv run maestro run examples/hello.yaml
```

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), git. Mode 2 also needs [gh CLI](https://cli.github.com/).

## What It Does

Maestro coordinates AI coding agents (Claude Code, Codex, Aider) on complex multi-part tasks. It resolves dependencies between tasks as a DAG, runs independent tasks in parallel, and handles retries, validation, and crash recovery. Two modes cover different workflows:

- **Task Scheduler** — run tasks from a YAML config in a shared directory
- **Multi-Process Orchestrator** — decompose a project into independent units, run each in an isolated git worktree, and auto-create PRs

## Mode 1: Task Scheduler

Run multiple AI agent tasks with dependency ordering in a single repository.

- Define tasks, dependencies, and file scopes in a YAML config
- DAG-based scheduling runs independent tasks in parallel
- Post-task validation commands catch broken builds early
- Crash recovery with `--resume` picks up where you left off

```yaml
# examples/hello.yaml
project: hello-maestro
repo: ~/projects/hello

defaults:
  agent_type: announce       # No real agent — just logs the task

tasks:
  - id: greet
    title: "Say hello"
    prompt: "Hello from Maestro!"

  - id: compute
    title: "Crunch numbers"
    prompt: "Computing 2 + 2 = 4"

  - id: summarize
    title: "Summarize results"
    prompt: "All tasks completed successfully."
    depends_on: [greet, compute]
```

```bash
uv run maestro run config.yaml           # Run tasks
uv run maestro run config.yaml --resume  # Resume after crash
uv run maestro status                    # Check progress
uv run maestro retry <task-id>           # Retry a failed task
```

## Mode 2: Multi-Process Orchestrator

Decompose a project into isolated work units ("zadachi"), each running in its own git worktree.

- Auto-decompose a project description into zadachi, or define them manually
- Each zadacha gets an isolated git worktree and feature branch
- Task specs are generated per zadacha and executed via spec-runner
- Completed zadachi are pushed and PRs are auto-created via `gh`

See [`examples/project.yaml`](examples/project.yaml) for a fully annotated config.

```bash
uv run maestro orchestrate project.yaml  # Run orchestrator
uv run maestro zadachi                   # Check zadachi status
uv run maestro workspaces                # List active worktrees
```

## Examples

| File | Description |
|------|-------------|
| [`hello.yaml`](examples/hello.yaml) | Minimal quick-start with the `announce` agent (no AI needed) |
| [`tasks.yaml`](examples/tasks.yaml) | Full task scheduler config with dependencies, validation, and git settings |
| [`parallel-refactor.yaml`](examples/parallel-refactor.yaml) | DAG-based parallel refactoring across multiple modules |
| [`project.yaml`](examples/project.yaml) | Multi-process orchestrator with manual zadachi definitions |
| [`maestro-builds-maestro.yaml`](examples/maestro-builds-maestro.yaml) | Meta-dogfooding — Maestro implements its own backlog |

## Supported Agents

| Agent | Key | Notes |
|-------|-----|-------|
| Claude Code | `claude_code` | Default. Requires `claude` CLI |
| Codex | `codex` | Requires `codex` CLI |
| Aider | `aider` | Requires `aider` CLI |
| Announce | `announce` | Dry-run mode — logs tasks without running an agent |

## Development

```bash
git clone https://github.com/andrei-shtanakov/maestro.git
cd maestro
uv sync
uv run pytest
uv run ruff check .
uv run pyrefly check
```

## License

MIT — see [LICENSE](LICENSE).
