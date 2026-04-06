# Maestro Mode 2 — First Real Run

**Date:** 2026-04-06
**Goal:** First end-to-end test of `maestro orchestrate` on a real project
**Target project:** proctor-a (Phase 2: SchedulerTrigger)
**Approach:** Run as-is, fix what breaks (dogfood-first)

---

## Context

Mode 2 (Multi-Process Orchestrator) is fully implemented but never tested on a real project. Week 3 of the dogfood roadmap requires a real launch. We chose proctor-a Phase 2 as the target — it's Python, well-tested, and has clear extension points.

spec-runner v1.1.0 is installed globally via `uv tool install`.

## Scope

**Single zadacha:** SchedulerTrigger for proctor-a.

- Inherits from existing `Trigger` ABC in `src/proctor/triggers/`
- Supports cron expressions and fixed intervals
- Integrates with proctor's async event bus
- Tests in `tests/test_triggers/test_scheduler.py`

**Why one zadacha:** Validate the full pipeline first (worktree -> spec generation -> spec-runner -> exit code -> cleanup), then scale to multiple.

## Design Decisions

### Monitoring: callbacks-only (no file polling)

spec-runner v1.1.0 switched state from `.executor-state.json` to SQLite (`.executor-state.db`). Maestro's `_update_progress()` reads JSON. Rather than adapting, we accept no intermediate progress for now:

- `_update_progress()` returns early when JSON file doesn't exist (line 414-415 in orchestrator.py) — no crash
- Final status determined by process exit code (0 = success, non-zero = failure)
- Callback URL available but not required for first run

### No auto-PR

`auto_pr: false` — first run, we inspect the branch manually before creating PRs.

### No auto-decompose

Zadacha defined manually in project.yaml, not via Claude CLI decomposition. Simpler, more predictable for first test.

## project.yaml

```yaml
project: proctor-a-scheduler
description: "Add SchedulerTrigger to proctor-a"
repo_path: /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/proctor-a
workspace_base: /tmp/maestro-ws/proctor-a
max_concurrent: 1
base_branch: main
branch_prefix: "feature/"
auto_pr: false

spec_runner:
  max_retries: 2
  task_timeout_minutes: 30
  auto_commit: true
  run_tests_on_done: true
  test_command: "uv run pytest tests/ -v"
  run_lint_on_done: true
  lint_command: "uv run ruff check ."

zadachi:
  - id: scheduler-trigger
    title: "Implement SchedulerTrigger (cron/interval)"
    description: |
      Add SchedulerTrigger to src/proctor/triggers/.
      - Inherits from Trigger ABC (see src/proctor/triggers/base.py)
      - Supports cron expressions (via croniter or similar) and fixed intervals
      - Integrates with EventBus to emit TaskSubmitted events
      - Config via YAML (schedules section in proctor config)
      - Tests in tests/test_triggers/test_scheduler.py
      - Follow existing patterns from TerminalTrigger
    scope:
      - "src/proctor/triggers/**"
      - "tests/test_triggers/**"
    priority: 10
```

## Expected Flow

1. `maestro orchestrate proctor-a-scheduler.yaml`
2. Maestro creates worktree at `/tmp/maestro-ws/proctor-a/scheduler-trigger`
3. Decomposer generates spec (requirements.md, design.md, tasks.md) via Claude CLI
4. Maestro writes executor.config.yaml and spawns `spec-runner run --all`
5. spec-runner executes tasks sequentially in the worktree
6. On exit code 0: zadacha -> DONE, branch `feature/scheduler-trigger` has commits
7. On non-zero: zadacha -> FAILED, retry up to 2 times

## Known Risks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| spec-runner CLI flags changed | Spawn fails | Check `--all` flag exists (verified: yes) |
| executor.config.yaml format mismatch | spec-runner ignores config or errors | Compare Maestro's `to_executor_config()` with spec-runner's expected schema |
| Spec generation prompt too generic | Poor quality tasks.md | Manual intervention, improve prompt later |
| croniter not in proctor-a deps | Agent can't add the feature | Agent should `uv add croniter` as part of implementation |
| Worktree creation on non-main branch | Git errors | Verify proctor-a is on main branch before launch |

## Success Criteria

- [ ] Worktree created at expected path
- [ ] spec-runner process starts and produces logs
- [ ] At least one task executed by spec-runner
- [ ] Branch `feature/scheduler-trigger` has commits
- [ ] Maestro reports final status (DONE or FAILED with clear error)

## Follow-up

After first run, log findings in DOGFOOD_LOG.md and fix issues. Then scale to 2-3 zadachi for proctor-a Phase 2.
