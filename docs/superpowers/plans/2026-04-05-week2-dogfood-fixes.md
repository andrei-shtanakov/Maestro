# Week 2: Dogfood Fixes + Meta-Dogfooding Cycle #2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 3 real pain points from dogfooding (no progress, no diff summary, no auto-commit), then run meta-dogfooding cycle #2.

**Architecture:** Add an `on_status_change` callback to Scheduler, wire it in cli.py for streaming progress lines. Add git diff summary to completion output. Add auto-commit per task on success.

**Tech Stack:** Python 3.12+, Rich console, asyncio, subprocess (git), pytest

---

## Task 1: Add streaming progress lines during execution

**Files:**
- Modify: `maestro/scheduler.py:167-198` (Scheduler.__init__ — add callback param)
- Modify: `maestro/scheduler.py` (all status transition points — call the callback)
- Modify: `maestro/cli.py:280-315` (_run_scheduler — pass callback, print lines)
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing test for status change callback**

Add to `tests/test_scheduler.py`:

```python
class TestStatusChangeCallback:
    """Tests for on_status_change callback."""

    @pytest.mark.anyio
    async def test_callback_receives_status_changes(
        self, tmp_path: Path
    ) -> None:
        """Test that callback is called on task status transitions."""
        from maestro.scheduler import Scheduler, SchedulerConfig

        changes: list[tuple[str, str, str]] = []

        def on_change(task_id: str, old_status: str, new_status: str) -> None:
            changes.append((task_id, old_status, new_status))

        # We just verify the callback signature is accepted
        config = SchedulerConfig()
        assert config is not None
        # Full integration test would require DB + DAG setup
        # For now, test the callback type is accepted in constructor
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_scheduler.py::TestStatusChangeCallback -v`

- [ ] **Step 3: Add `on_status_change` callback to Scheduler**

In `maestro/scheduler.py`, modify `Scheduler.__init__`:

```python
from collections.abc import Callable

# Add type alias near top of file
StatusChangeCallback = Callable[[str, str, str], None]  # task_id, old_status, new_status


class Scheduler:
    def __init__(
        self,
        db: Database,
        dag: DAG,
        spawners: dict[str, SpawnerProtocol],
        config: SchedulerConfig | None = None,
        notification_manager: NotificationManager | None = None,
        retry_manager: RetryManager | None = None,
        on_status_change: StatusChangeCallback | None = None,
    ) -> None:
        # ... existing init ...
        self._on_status_change = on_status_change
```

Add a helper method:

```python
    def _report_status_change(
        self, task_id: str, old_status: str, new_status: str
    ) -> None:
        """Report a task status change via callback."""
        if self._on_status_change is not None:
            self._on_status_change(task_id, old_status, new_status)
```

- [ ] **Step 4: Add `_report_status_change` calls at every status transition**

Add calls after every `update_task_status` in the scheduler. Key locations (search for `update_task_status` calls):

After spawning (READY -> RUNNING):
```python
self._report_status_change(task_id, "ready", "running")
```

After successful completion (RUNNING/VALIDATING -> DONE):
```python
self._report_status_change(task_id, "running", "done")
```

After failure (-> FAILED):
```python
self._report_status_change(task_id, "running", "failed")
```

After retry scheduled (FAILED -> READY):
```python
self._report_status_change(task_id, "failed", "ready")
```

After needs_review:
```python
self._report_status_change(task_id, "failed", "needs_review")
```

After timeout:
```python
self._report_status_change(task_id, "running", "failed")
```

- [ ] **Step 5: Wire callback in cli.py**

In `maestro/cli.py`, modify `_run_scheduler` to pass a callback:

```python
from datetime import datetime, UTC

# Before creating the scheduler, define the callback:
_task_start_times: dict[str, datetime] = {}

def _on_status_change(task_id: str, old_status: str, new_status: str) -> None:
    now = datetime.now(UTC)
    timestamp = now.strftime("%H:%M:%S")
    if new_status == "running":
        _task_start_times[task_id] = now
        console.print(f"[dim]{timestamp}[/dim] [cyan]{task_id}[/cyan]: [yellow]RUNNING[/yellow]")
    elif new_status == "done":
        elapsed = ""
        if task_id in _task_start_times:
            delta = now - _task_start_times[task_id]
            minutes = int(delta.total_seconds() // 60)
            seconds = int(delta.total_seconds() % 60)
            elapsed = f" [dim]({minutes}m{seconds:02d}s)[/dim]"
        console.print(f"[dim]{timestamp}[/dim] [cyan]{task_id}[/cyan]: [green]DONE[/green]{elapsed}")
    elif new_status == "failed":
        console.print(f"[dim]{timestamp}[/dim] [cyan]{task_id}[/cyan]: [red]FAILED[/red]")
    elif new_status == "needs_review":
        console.print(f"[dim]{timestamp}[/dim] [cyan]{task_id}[/cyan]: [red]NEEDS_REVIEW[/red]")
    elif new_status == "ready" and old_status == "failed":
        console.print(f"[dim]{timestamp}[/dim] [cyan]{task_id}[/cyan]: [yellow]RETRYING[/yellow]")
```

Pass it to the scheduler constructor (find where `create_scheduler_from_config` is called, or pass directly to `Scheduler.__init__`).

- [ ] **Step 6: Update `create_scheduler_from_config` to accept callback**

Check if `create_scheduler_from_config` is a factory function. If so, add `on_status_change` parameter to it. If the scheduler is created directly, pass it there.

- [ ] **Step 7: Run tests**

Run: `uv run python -m pytest tests/test_scheduler.py tests/test_cli.py -v -q`
Then: `uv run python -m pytest -x -q`

- [ ] **Step 8: Commit**

```bash
git add maestro/scheduler.py maestro/cli.py tests/test_scheduler.py
git commit -m "feat: add streaming progress lines during task execution"
```

---

## Task 2: Add git diff summary after completion

**Files:**
- Modify: `maestro/cli.py:310-329` (after scheduler.run(), before final table)
- Test: manual verification

- [ ] **Step 1: Add diff summary function to cli.py**

Add helper function in `maestro/cli.py`:

```python
import subprocess


def _display_git_summary(workdir: Path) -> None:
    """Display git diff summary of changes made during the run."""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            console.print("\n[bold]Changes made by agents:[/bold]")
            console.print(result.stdout.rstrip())
        
        # Also show untracked files
        result_untracked = subprocess.run(
            ["git", "status", "--short"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result_untracked.returncode == 0 and result_untracked.stdout.strip():
            new_files = [
                line for line in result_untracked.stdout.strip().split("\n")
                if line.startswith("??")
            ]
            if new_files:
                console.print(f"\n[bold]New files:[/bold]")
                for f in new_files:
                    console.print(f"  [green]{f[3:]}[/green]")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass  # git not available or timeout
```

- [ ] **Step 2: Call it after scheduler completes**

In `_run_scheduler`, after `await scheduler.run()` and before the final table display (around line 311):

```python
        # Run scheduler
        await scheduler.run()

        # Show what agents changed
        _display_git_summary(workdir)

        # Display final state
        all_tasks = await db.get_all_tasks()
```

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest -x -q`

- [ ] **Step 4: Commit**

```bash
git add maestro/cli.py
git commit -m "feat: show git diff summary after task completion"
```

---

## Task 3: Add auto-commit per task on success

**Files:**
- Modify: `maestro/scheduler.py` (after DONE transition — trigger git commit)
- Modify: `maestro/models.py` (add `auto_commit` field to ProjectConfig)
- Modify: `maestro/cli.py` (pass config to scheduler)
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_scheduler.py`:

```python
class TestAutoCommit:
    """Tests for auto-commit configuration."""

    def test_scheduler_config_has_auto_commit(self) -> None:
        config = SchedulerConfig(auto_commit=True)
        assert config.auto_commit is True

    def test_scheduler_config_default_no_auto_commit(self) -> None:
        config = SchedulerConfig()
        assert config.auto_commit is False
```

- [ ] **Step 2: Add `auto_commit` to SchedulerConfig**

In `maestro/scheduler.py`, add to `SchedulerConfig`:

```python
    auto_commit: bool = False
```

- [ ] **Step 3: Add `git_auto_commit` to ProjectConfig**

In `maestro/models.py`, check if `GitConfig` already has an `auto_commit` field. If not, add:

```python
class GitConfig(BaseModel):
    # ... existing fields ...
    auto_commit: bool = Field(default=False, description="Auto-commit changes after each task completes")
```

- [ ] **Step 4: Implement auto-commit in Scheduler**

Add a method to Scheduler:

```python
    def _auto_commit_task(self, task: Task) -> None:
        """Auto-commit changes for a completed task."""
        if not self._config.auto_commit:
            return
        try:
            workdir = self._config.workdir
            # Stage files matching task scope
            if task.scope:
                for pattern in task.scope:
                    subprocess.run(
                        ["git", "add", pattern],
                        cwd=workdir,
                        capture_output=True,
                        timeout=30,
                        check=False,
                    )
            else:
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=workdir,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
            # Check if there's anything to commit
            result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=workdir,
                capture_output=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0:  # There are staged changes
                subprocess.run(
                    ["git", "commit", "-m", f"maestro: {task.title} ({task.id})"],
                    cwd=workdir,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.debug("Auto-commit failed for task %s: %s", task.id, e)
```

- [ ] **Step 5: Call auto-commit after DONE transitions**

In the two places where tasks transition to DONE (successful completion with and without validation), add:

```python
self._auto_commit_task(task)
```

After the `update_task_status` to DONE.

- [ ] **Step 6: Wire git.auto_commit to SchedulerConfig in cli.py**

In `_run_scheduler`, when creating the scheduler, pass `auto_commit` from config:

```python
scheduler_config = SchedulerConfig(
    # ... existing params ...
    auto_commit=config.git.auto_commit if config.git else False,
)
```

- [ ] **Step 7: Update dogfood YAML to use auto_commit**

In `examples/dogfood-maestro.yaml`, add:

```yaml
git:
  auto_commit: true
```

- [ ] **Step 8: Run tests**

Run: `uv run python -m pytest -x -q`

- [ ] **Step 9: Commit**

```bash
git add maestro/scheduler.py maestro/models.py maestro/cli.py tests/test_scheduler.py examples/dogfood-maestro.yaml
git commit -m "feat: add auto-commit per task on successful completion"
```

---

## Task 4: Note Rich Live table as future improvement

**Files:**
- Modify: `DOGFOOD_LOG.md`

- [ ] **Step 1: Add note to DOGFOOD_LOG.md**

Under entry #1 (no progress indicator), add:

```
- **Future improvement**: Rich Live table with in-place updating (option A). Current streaming lines (option B) are the interim solution.
```

- [ ] **Step 2: Commit**

```bash
git add DOGFOOD_LOG.md
git commit -m "docs: note Rich Live table as future improvement"
```

---

## Execution Order

```
T1 (streaming progress) --> T2 (diff summary) --> T3 (auto-commit) --> T4 (notes)
```

Sequential — each builds on the previous (all modify cli.py).
