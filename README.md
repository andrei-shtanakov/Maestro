# Maestro

AI Agent Orchestrator for parallel coding agent coordination.

Maestro coordinates multiple AI coding agents (Claude Code, Codex, Aider) working on different parts of the same project. It has two operation modes:

1. **Task Scheduler** — run tasks from a YAML config in a shared directory with DAG-based dependency resolution
2. **Multi-Process Orchestrator** — decompose a project into independent work units ("zadachi"), run each in an isolated git worktree via [spec-runner](https://github.com/user/spec-runner), and auto-create PRs

## Installation

```bash
# Clone and install
git clone https://github.com/user/maestro.git
cd maestro
uv sync

# Verify
uv run maestro --help
```

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), git, [gh CLI](https://cli.github.com/) (for PR creation).

## Quick Start

### Mode 1: Task Scheduler

Define tasks in a YAML file:

```yaml
# tasks.yaml
project: my-app
repo: ~/projects/my-app
max_concurrent: 3

tasks:
  - id: setup-models
    title: "Create data models"
    prompt: "Create SQLAlchemy models for User and Post"
    scope: ["src/models/**"]

  - id: setup-api
    title: "Create API endpoints"
    prompt: "Create FastAPI endpoints for CRUD operations"
    scope: ["src/api/**"]
    depends_on: [setup-models]

  - id: add-tests
    title: "Write tests"
    prompt: "Write pytest tests for models and API"
    scope: ["tests/**"]
    depends_on: [setup-models, setup-api]
```

Run:

```bash
uv run maestro run tasks.yaml
uv run maestro status                    # Check progress
uv run maestro retry setup-api           # Retry failed task
uv run maestro run tasks.yaml --resume   # Resume after crash
```

### Mode 2: Multi-Process Orchestrator

Define a project config:

```yaml
# project.yaml
project: my-app
description: |
  Build a REST API with authentication, user management,
  and admin dashboard using FastAPI and React.
repo_url: https://github.com/user/my-app
repo_path: ~/projects/my-app
workspace_base: /tmp/maestro-ws/my-app
max_concurrent: 3
auto_pr: true

spec_runner:
  test_command: "uv run pytest"
  lint_command: "uv run ruff check ."
```

Run:

```bash
uv run maestro orchestrate project.yaml
uv run maestro zadachi                   # Check zadachi status
uv run maestro workspaces                # List active worktrees
```

The orchestrator will:
1. Decompose the project into independent zadachi via Claude CLI
2. Create a git worktree + branch for each zadacha
3. Generate specs (requirements, design, tasks) for each
4. Run spec-runner in each worktree in parallel
5. Push branches and create PRs on completion
6. Clean up worktrees after merge

See [`examples/project.yaml`](examples/project.yaml) for a fully annotated config.

## CLI Reference

```
maestro run <config.yaml>           Run task scheduler
  --resume, -r                      Resume from existing state
  --db, -d PATH                     Database path (default: ~/.maestro/maestro.db)
  --log-dir, -l PATH                Log directory

maestro orchestrate <project.yaml>  Run multi-process orchestrator
  --resume, -r                      Resume from existing state
  --db, -d PATH                     Database path
  --log-dir, -l PATH                Log directory

maestro status                      Show task status table
  --db, -d PATH                     Database path

maestro zadachi                     Show zadachi status table
  --db, -d PATH                     Database path

maestro retry <task-id>             Retry a failed task
  --db, -d PATH                     Database path

maestro approve <task-id>           Approve a task awaiting approval
  --db, -d PATH                     Database path

maestro stop                        Stop the running scheduler

maestro workspaces                  List active worktree workspaces
  --path, -p PATH                   Workspace base directory
```

## Architecture

```
maestro/
├── models.py          # Pydantic models (Task, Zadacha, configs, state machines)
├── config.py          # YAML parsing, defaults merging, env var substitution
├── database.py        # SQLite with async CRUD, WAL mode (tasks + zadachi)
├── dag.py             # DAG building, cycle detection, topological sort
├── scheduler.py       # Task scheduler main loop (resolve → spawn → monitor)
├── orchestrator.py    # Multi-process orchestrator (decompose → spawn → PR)
├── workspace.py       # Git worktree lifecycle (create, setup, cleanup)
├── decomposer.py      # Project decomposition via Claude CLI
├── pr_manager.py      # GitHub PR creation via gh CLI
├── git.py             # Git operations (branch, rebase, push, worktree, merge)
├── cli.py             # Typer CLI with all commands
├── validator.py       # Post-task validation commands
├── retry.py           # Exponential backoff retry logic
├── recovery.py        # State recovery after crash
├── cost_tracker.py    # Token usage and cost calculation
├── spawners/          # Agent spawners (claude_code, codex, aider, announce)
├── coordination/      # MCP server + REST API with /zadachi endpoints
├── notifications/     # Desktop notifications (macOS/Linux)
└── dashboard/         # Web UI with DAG visualization + SSE updates
```

### Task State Machine (Scheduler Mode)

```
PENDING → READY → RUNNING → VALIDATING → DONE
                     │           │
                  FAILED ← (validation fail)
                     │
              NEEDS_REVIEW → READY (manual retry)
```

### Zadacha State Machine (Orchestrator Mode)

```
PENDING → DECOMPOSING → READY → RUNNING → MERGING → PR_CREATED → DONE
                                    │                      │
                                 FAILED                 FAILED
                                    │
                             NEEDS_REVIEW → READY
```

### Orchestrator Data Flow

```
1. Load project.yaml → OrchestratorConfig
2. Auto-decompose project into zadachi (Claude CLI)
   or use manually defined zadachi from config
3. Save zadachi to SQLite (status=PENDING)
4. Main loop:
   a. Resolve ready zadachi (DAG dependency check)
   b. For each ready (up to max_concurrent):
      - git worktree add → isolated directory
      - Generate spec/ (requirements.md, design.md, tasks.md)
      - Write executor.config.yaml for spec-runner
      - Spawn: spec-runner run --all
   c. Monitor processes:
      - Poll process returncode
      - Read spec/.executor-state.json for subtask progress
      - Handle REST callbacks from spec-runner
   d. On success:
      - git push -u origin feature/<zadacha-id>
      - gh pr create → PR URL
      - Cleanup worktree
   e. On failure:
      - Retry with backoff, or mark NEEDS_REVIEW
5. Report summary when all complete
```

## Configuration

### Task Scheduler Config (`tasks.yaml`)

```yaml
project: my-app                    # Project name
repo: ~/projects/my-app            # Repository path (absolute or ~)
max_concurrent: 3                  # Max parallel tasks (1-10)

defaults:                          # Optional defaults for all tasks
  timeout_minutes: 30
  max_retries: 2
  agent_type: claude_code

git:                               # Optional git settings
  base_branch: main
  auto_push: true
  branch_prefix: "agent/"

notifications:                     # Optional notifications
  desktop: true

tasks:
  - id: task-id                    # Unique ID (alphanumeric, hyphens, underscores)
    title: "Task title"            # Human-readable title
    prompt: "Task description"     # Prompt sent to the AI agent
    agent_type: claude_code        # claude_code | codex | aider | announce
    scope: ["src/**/*.py"]         # File globs this task can modify
    depends_on: [other-task]       # Task IDs that must complete first
    timeout_minutes: 30            # Timeout (1-1440)
    max_retries: 2                 # Retry attempts (0-10)
    validation_cmd: "pytest"       # Command to validate completion
    requires_approval: false       # Require manual approval before start
    priority: 0                    # Priority (-100 to 100)
```

### Orchestrator Config (`project.yaml`)

```yaml
project: my-app                    # Project name
description: |                     # Description for auto-decomposition
  Build a REST API with auth...
repo_url: https://github.com/...   # GitHub remote URL
repo_path: ~/projects/my-app       # Local repo path
workspace_base: /tmp/maestro-ws/my-app  # Worktree base directory
max_concurrent: 3                  # Max parallel zadachi (1-10)
base_branch: main                  # Base branch
branch_prefix: "feature/"          # Branch prefix for zadachi
auto_pr: true                      # Auto-create PRs

spec_runner:                       # Spec-runner settings
  max_retries: 3
  task_timeout_minutes: 30
  claude_command: claude
  auto_commit: true
  create_git_branch: true
  run_tests_on_done: true
  test_command: "uv run pytest"
  lint_command: "uv run ruff check ."
  run_lint_on_done: true

# Optional: define zadachi manually instead of auto-decompose
# zadachi:
#   - id: auth-system
#     title: "Authentication"
#     description: "JWT auth with login/register/refresh"
#     scope: ["src/auth/**"]
#     depends_on: []
#     priority: 10
```

## Development

```bash
uv run pytest                        # Run all tests
uv run pytest tests/test_models.py   # Single file
uv run pytest -k "test_dag"          # Pattern match
pyrefly check                        # Type checking
uv run ruff format .                 # Format code
uv run ruff check .                  # Lint
uv run ruff check . --fix            # Auto-fix lint issues
```

## License

MIT
