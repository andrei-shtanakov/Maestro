# Maestro Dogfood Log

Tracking issues, bugs, and UX friction found during real usage of Maestro.

## Format

Each entry:
- **Date**: when found
- **Severity**: BLOCKER / HIGH / MEDIUM / LOW
- **Mode**: Mode 1 (Scheduler) / Mode 2 (Orchestrator)
- **Backlog ref**: T-XX if matches existing backlog item
- **Description**: what happened
- **Expected**: what should have happened
- **Workaround**: if any

---

## Entries

### 1. No progress indicator while tasks are RUNNING
- **Date**: 2026-04-05
- **Severity**: HIGH
- **Mode**: Mode 1 (Scheduler)
- **Backlog ref**: relates to T-12 (streaming events from codebuff in SUGGESTIONS.md)
- **Description**: After "Scheduler started" panel, the screen shows nothing for ~4 minutes while 5 tasks execute. No indication of which tasks are RUNNING, no progress, no elapsed time. User has no idea if it's working or hung.
- **Expected**: Live-updating table showing PENDING -> READY -> RUNNING -> DONE transitions, elapsed time per task, or at minimum a spinner/heartbeat.
- **Workaround**: `tail -f logs/*.log` in another terminal.
- **FIXED**: 2026-04-05 — Streaming progress lines added (option B). Future: Rich Live table (option A).

### 2. No easy way to see what agents changed
- **Date**: 2026-04-05
- **Severity**: HIGH
- **Mode**: Mode 1 (Scheduler)
- **Backlog ref**: relates to T-13 (result_summary)
- **Description**: After "All tasks completed successfully!" there's no way to see what was actually changed. No diff summary, no commit list, no `git status` output. User must manually run `git diff` and `git status` to understand what happened.
- **Expected**: Final summary should include: files changed per task, or at least `git diff --stat`.
- **Workaround**: `git diff --stat` manually after run.
- **FIXED**: 2026-04-05 — Git diff summary + new files shown after completion.

### 3. Agents don't commit their work
- **Date**: 2026-04-05
- **Severity**: MEDIUM
- **Mode**: Mode 1 (Scheduler)
- **Backlog ref**: new (not in backlog)
- **Description**: All 5 agents made changes to the working tree but none committed. Changes are left as uncommitted modifications. If Maestro crashes or user runs `git checkout .`, all work is lost.
- **Expected**: Each agent should commit its changes (with a descriptive message) upon successful completion, or Maestro should auto-commit on task DONE.
- **Workaround**: Manual `git add` + `git commit` after run.
- **FIXED**: 2026-04-05 — Auto-commit per task added (`git: auto_commit: true` in YAML).

### 4. Warning about VIRTUAL_ENV mismatch
- **Date**: 2026-04-05
- **Severity**: LOW
- **Mode**: Mode 1 (Scheduler)
- **Backlog ref**: new
- **Description**: `warning: VIRTUAL_ENV=/Users/.../Maestro/.venv does not match the project environment path .venv` shown at start. Confusing but harmless.
- **Expected**: No warning, or suppress it.
- **Workaround**: Ignore.
