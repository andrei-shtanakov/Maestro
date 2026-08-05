# Maestro

> AI Agent Orchestrator — coordinate multiple coding agents with DAG-based scheduling and git worktree isolation

## Quick Start

```bash
uv add maestro
uv run maestro run examples/hello.yaml
```

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), git. Mode 2 also needs [gh CLI](https://cli.github.com/).

**A model source is required.** Maestro no longer bakes in a default model, so
one of the following must be true or the run fails loud: set `$ATP_CATALOG` to
a model catalog (or run `atp models init`), set `MAESTRO_CLAUDE_MODEL` /
`MAESTRO_CODEX_MODEL`, or let arbiter routing supply a model. With none of
these, `maestro run` halts with a clear `CatalogNotConfigured` error before
spawning any agent.

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

Decompose a project into isolated work units ("workstreams"), each running in its own git worktree.

- Auto-decompose a project description into workstreams, or define them manually
- Each workstream gets an isolated git worktree and feature branch
- Task specs are generated per workstream and executed via spec-runner
- Completed workstreams are pushed and PRs are auto-created via `gh`

See [`examples/project.yaml`](examples/project.yaml) for a fully annotated config.

```bash
uv run maestro orchestrate project.yaml  # Run orchestrator
uv run maestro workstreams                   # Check workstreams status
uv run maestro workspaces                # List active worktrees
```

### Config authoring: `init` and `validate`

- `uv run maestro init` — scaffold a commented `project.yaml` from the current
  directory (git-derived autofill for `project`/`repo`).
- `uv run maestro validate project.yaml` — preflight checks before you run:
  dependency cycles, scope overlap between workstreams, and repo sanity.
- `uv run maestro validate project.yaml --strict --no-fs` — CI mode: `--strict`
  treats warnings as errors (exit 1), `--no-fs` skips filesystem access for a
  deterministic check with no real repo required.

`maestro orchestrate` also runs this preflight automatically as a fail-fast
gate before spawning any workstream.

### Dual-mode repos: direct spec-runner runs + Maestro

Maestro owns `spec-runner.config.yaml` inside each worktree: it regenerates
the file from `project.yaml`'s `spec_runner` block on every workstream
launch. That makes `project.yaml` the single source of truth for worktree
configs — and it means the target repo must **not** track its own
`spec-runner.config.yaml`. A tracked copy gets overwritten inside the
worktree, and the overwrite then surfaces ex-post as a scope violation
(preflight warns about this: `spec-runner-config-tracked`).

For a repo that is *also* driven by spec-runner directly (outside Maestro),
the canonical setup is:

1. Untrack the file — `git rm --cached spec-runner.config.yaml` — and add it
   to `.gitignore`.
2. Keep a local, untracked copy of `spec-runner.config.yaml` for direct
   `spec-runner` runs.
3. Treat `project.yaml` as the SSOT for everything Maestro-driven.

This is the current interoperability contract between the two tools, not a
final design — a shared convention may replace it later.

### Where the state DB lives

All state-reading commands (`status`, `workstreams`, `retry`, `approve`,
`costs`, ...) default to `~/.maestro/maestro.db`, and `maestro run` /
`maestro orchestrate` write there by default too. Pass `--db <path>` on both
sides when you want a reproducible, per-project DB instead of the shared
default.

## Examples

| File | Description |
|------|-------------|
| [`hello.yaml`](examples/hello.yaml) | Minimal quick-start with the `announce` agent (no AI needed) |
| [`tasks.yaml`](examples/tasks.yaml) | Full task scheduler config with dependencies, validation, and git settings |
| [`parallel-refactor.yaml`](examples/parallel-refactor.yaml) | DAG-based parallel refactoring across multiple modules |
| [`project.yaml`](examples/project.yaml) | Multi-process orchestrator with manual workstreams definitions |
| [`maestro-builds-maestro.yaml`](examples/maestro-builds-maestro.yaml) | Meta-dogfooding — Maestro implements its own backlog |
| [`dogfood-maestro.yaml`](examples/dogfood-maestro.yaml) | Dogfooding config — Maestro runs quick wins from its own backlog in parallel |
| [`with-arbiter.yaml`](examples/with-arbiter.yaml) | Optional Arbiter-driven routing (advisory mode) — `agent_type: auto` lets the policy engine pick the best agent |
| [`with-atp-validation.yaml`](examples/with-atp-validation.yaml) | Post-task validation via the ATP Platform CLI through `validation_cmd` |

## Optional: Arbiter routing

Add an `arbiter:` section to your project YAML to delegate per-task agent selection to the [Arbiter](https://github.com/andrei-shtanakov/arbiter) policy engine. Advisory mode honors your explicit `agent_type` and feeds the learning loop; authoritative mode lets the arbiter override your choice and gates retries on outcome delivery. When the section is absent, Maestro runs the zero-config static-routing path — no subprocess, no routing overhead. See [`examples/with-arbiter.yaml`](examples/with-arbiter.yaml).

## Supported Agents

| Agent | Key | Notes |
|-------|-----|-------|
| Claude Code | `claude_code` | Default. Requires `claude` CLI |
| Codex | `codex_cli` | Requires `codex` CLI |
| OpenCode | `opencode` | Requires `opencode` CLI |
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
