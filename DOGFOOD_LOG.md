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

### 5. No `--clean` / `--force` flag to re-run completed tasks
- **Date**: 2026-04-05
- **Severity**: HIGH
- **Mode**: Mode 1 (Scheduler)
- **Backlog ref**: new
- **Description**: Running `maestro run` again with the same config exits instantly because all tasks are DONE in SQLite. No way to re-run without manually deleting `~/.maestro/maestro.db`. The `--resume` flag exists but there's no `--clean` or `--force`.
- **Expected**: `maestro run config.yaml --clean` should reset all tasks to PENDING. Or `maestro run config.yaml --force` should re-run regardless of DB state.
- **Workaround**: `rm ~/.maestro/maestro.db` before re-run.
- **FIXED**: 2026-04-05 — `maestro run config.yaml --clean` deletes DB for fresh start.

### 6. Auto-commit summary not shown in final output
- **Date**: 2026-04-05
- **Severity**: MEDIUM
- **Mode**: Mode 1 (Scheduler)
- **Backlog ref**: new
- **Description**: When `auto_commit: true`, the git diff summary shows nothing (changes already committed). But user has no way to see WHICH commits were created. The final output should list auto-commits made during the run.
- **Expected**: After "All tasks completed", show: `Auto-commits: 3 commits created` with `git log --oneline` of those commits.
- **Workaround**: `git log --oneline` manually after run.
- **FIXED**: 2026-04-05 — Shows `git log --oneline --stat before..HEAD` with per-task file changes.

### 7. Spec generation prompt produces unparseable tasks.md
- **Date**: 2026-04-06
- **Severity**: BLOCKER
- **Mode**: Mode 2 (Orchestrator)
- **Backlog ref**: new
- **Description**: `SPEC_GENERATION_PROMPT` in `decomposer.py` describes the tasks.md format loosely. spec-runner's parser (`task.py`) requires exact format: `### TASK-NNN: Name` headers and `🔴 P0 | ⬜ TODO | Est: 2h` metadata lines. Generated tasks.md had 6 validation warnings and 0 parseable tasks, causing "No tasks ready to execute".
- **Expected**: Generated tasks.md should be parseable by spec-runner without warnings.
- **Workaround**: None (pipeline silently "succeeds" with no work done).
- **FIXED**: 2026-04-06 — Updated prompt with exact format template and strict rules.

### 8. spec-runner exit code 0 on "No tasks ready" = false success
- **Date**: 2026-04-06
- **Severity**: HIGH
- **Mode**: Mode 2 (Orchestrator)
- **Backlog ref**: new
- **Description**: When spec-runner finds no ready tasks it logs "No tasks ready to execute" and exits with code 0. Maestro treats exit code 0 as success, transitions zadacha to DONE. Result: empty branch, no work done, reported as successful.
- **Expected**: Either spec-runner should exit non-zero when no tasks executed, or Maestro should verify the branch has new commits before marking DONE.
- **Workaround**: None yet. Need to add commit check in `_handle_success()`.

### 9. Worktree cleaned up before inspection on false success
- **Date**: 2026-04-06
- **Severity**: MEDIUM
- **Mode**: Mode 2 (Orchestrator)
- **Backlog ref**: new
- **Description**: After "success" (bug #8), Maestro immediately cleans up the worktree. The generated spec files (requirements.md, design.md, tasks.md) are lost — impossible to diagnose why spec-runner didn't parse tasks.
- **Expected**: On DONE, keep worktree if no commits were made (suspicious). Or always keep worktree for manual inspection, add explicit cleanup command.
- **Workaround**: None.

### 10. executor.config.yaml written to wrong path
- **Date**: 2026-04-06
- **Severity**: BLOCKER
- **Mode**: Mode 2 (Orchestrator)
- **Backlog ref**: new
- **Description**: `WorkspaceManager.setup_spec_runner()` wrote `executor.config.yaml` to workspace root, but spec-runner reads it from `spec/executor.config.yaml`. Config was silently ignored — `main_branch`, `max_retries`, `test_command` etc. all used defaults.
- **Expected**: Config should be at `spec/executor.config.yaml`.
- **Workaround**: None.
- **FIXED**: 2026-04-06 — Config now written to `spec/executor.config.yaml`.

### 11. Stale spec-runner state DB in worktree
- **Date**: 2026-04-06
- **Severity**: BLOCKER
- **Mode**: Mode 2 (Orchestrator)
- **Backlog ref**: new
- **Description**: When proctor-a has `spec/.executor-state.db` committed to git, the worktree inherits it. spec-runner reads the old state (11/12 tasks DONE from Phase 1) and only executes 1 new task instead of all generated tasks.
- **Expected**: Fresh worktree should have clean spec-runner state.
- **Workaround**: None.
- **FIXED**: 2026-04-06 — `setup_spec_runner()` now cleans stale state files (.db, .json, .lock, .progress, .history).

### 12. Spec generation skipped when tasks.md exists in repo
- **Date**: 2026-04-06
- **Severity**: BLOCKER
- **Mode**: Mode 2 (Orchestrator)
- **Backlog ref**: new
- **Description**: `_spawn_zadacha()` checked `if not tasks_file.exists()` before generating spec. When the repo already has `spec/tasks.md` (from previous project phase), generation was skipped entirely. spec-runner then ran old completed tasks.
- **Expected**: Always generate fresh spec for each zadacha.
- **Workaround**: None.
- **FIXED**: 2026-04-06 — Removed the exists() check, always regenerate spec.
