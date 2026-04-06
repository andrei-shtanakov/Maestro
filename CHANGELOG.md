# Changelog

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
