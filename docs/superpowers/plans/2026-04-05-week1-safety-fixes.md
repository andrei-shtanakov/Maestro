# Week 1: Safety Fixes + First Real Launch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Maestro Mode 1 safe to run on a real project by fixing 4 critical issues, then do the first real dogfooding run.

**Architecture:** Four independent safety fixes (no dependencies between them), followed by writing a real tasks.yaml and running it. Each fix is small (1-2 files), tested, and committed independently.

**Tech Stack:** Python 3.12+, pytest (anyio), fcntl (POSIX), asyncio, Pydantic, Typer

---

## Task 1: Add flock on PID file (T-02)

**Files:**
- Modify: `maestro/cli.py:46-110`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test for PID file locking**

Add to `tests/test_cli.py`:

```python
import fcntl

from maestro.cli import _acquire_pid_lock, _release_pid_lock, PID_FILE


class TestPidFileLocking:
    """Tests for PID file exclusive locking."""

    def test_acquire_lock_creates_pid_file(self, tmp_path: Path) -> None:
        """Test that acquiring lock creates PID file with current PID."""
        pid_file = tmp_path / "maestro.pid"
        lock_fd = _acquire_pid_lock(pid_file)
        assert lock_fd is not None
        assert pid_file.exists()
        assert pid_file.read_text().strip() == str(os.getpid())
        _release_pid_lock(lock_fd, pid_file)

    def test_acquire_lock_fails_when_already_locked(self, tmp_path: Path) -> None:
        """Test that second lock attempt raises SystemExit."""
        pid_file = tmp_path / "maestro.pid"
        lock_fd = _acquire_pid_lock(pid_file)
        assert lock_fd is not None
        with pytest.raises(SystemExit):
            _acquire_pid_lock(pid_file)
        _release_pid_lock(lock_fd, pid_file)

    def test_release_lock_removes_pid_file(self, tmp_path: Path) -> None:
        """Test that releasing lock removes PID file."""
        pid_file = tmp_path / "maestro.pid"
        lock_fd = _acquire_pid_lock(pid_file)
        _release_pid_lock(lock_fd, pid_file)
        assert not pid_file.exists()

    def test_stale_pid_file_is_overwritten(self, tmp_path: Path) -> None:
        """Test that a stale PID file (no lock held) is overwritten."""
        pid_file = tmp_path / "maestro.pid"
        pid_file.write_text("99999")  # stale PID
        lock_fd = _acquire_pid_lock(pid_file)
        assert lock_fd is not None
        assert pid_file.read_text().strip() == str(os.getpid())
        _release_pid_lock(lock_fd, pid_file)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_cli.py::TestPidFileLocking -v`
Expected: FAIL with `ImportError` — `_acquire_pid_lock` doesn't exist yet.

- [ ] **Step 3: Implement PID file locking**

Replace the existing `_write_pid_file`, `_read_pid_file`, `_remove_pid_file` in `maestro/cli.py` with:

```python
import fcntl


def _acquire_pid_lock(pid_file: Path | None = None) -> int:
    """Acquire exclusive lock on PID file.

    Args:
        pid_file: Path to PID file. Defaults to PID_FILE.

    Returns:
        File descriptor for the lock (caller must keep it open).

    Raises:
        SystemExit: If another Maestro instance is already running.
    """
    if pid_file is None:
        pid_file = PID_FILE
    _ensure_db_dir()
    fd = os.open(str(pid_file), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Read existing PID for error message
        try:
            existing_pid = os.read(fd, 32).decode().strip()
        except OSError:
            existing_pid = "unknown"
        os.close(fd)
        err_console.print(
            f"[red]Maestro is already running (PID: {existing_pid}). "
            f"Stop it first with 'maestro stop'.[/red]"
        )
        raise SystemExit(1)
    # Write our PID
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(os.getpid()).encode())
    return fd


def _release_pid_lock(fd: int, pid_file: Path | None = None) -> None:
    """Release PID file lock and remove the file.

    Args:
        fd: File descriptor from _acquire_pid_lock.
        pid_file: Path to PID file. Defaults to PID_FILE.
    """
    if pid_file is None:
        pid_file = PID_FILE
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except OSError:
        pass
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass
```

- [ ] **Step 4: Update `run` command to use locking**

In the `run` command function in `maestro/cli.py`, find where `_write_pid_file(os.getpid())` is called and replace with:

```python
lock_fd = _acquire_pid_lock()
try:
    # ... existing run logic ...
finally:
    _release_pid_lock(lock_fd)
```

Do the same for the `orchestrate` command if it uses `_write_pid_file`.

Keep `_read_pid_file` for the `stop` command — it still needs to read the PID. Remove `_write_pid_file` entirely.

- [ ] **Step 5: Update test imports**

In `tests/test_cli.py`, update the import list to include `_acquire_pid_lock` and `_release_pid_lock` (replacing `_write_pid_file` if it's imported there). Check that existing tests still use `_read_pid_file` correctly for the `stop` command tests.

- [ ] **Step 6: Run all tests to verify**

Run: `uv run python -m pytest tests/test_cli.py -v`
Expected: All tests PASS, including existing ones.

- [ ] **Step 7: Commit**

```bash
git add maestro/cli.py tests/test_cli.py
git commit -m "feat: add flock-based PID file locking to prevent double-start (T-02)"
```

---

## Task 2: Increase shutdown grace period to 5s (T-05)

**Files:**
- Modify: `maestro/scheduler.py:121-134` (SchedulerConfig)
- Modify: `maestro/scheduler.py:720-733` (_handle_timeout)
- Modify: `maestro/scheduler.py:805-819` (_cleanup)
- Modify: `maestro/orchestrator.py:609-619` (_cleanup)
- Test: `tests/test_scheduler.py`
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Write failing test for configurable grace period**

Add to `tests/test_scheduler.py`:

```python
from maestro.scheduler import SchedulerConfig


class TestSchedulerGracePeriod:
    """Tests for configurable shutdown grace period."""

    def test_default_grace_period_is_5(self) -> None:
        """Test that default grace period is 5 seconds."""
        config = SchedulerConfig()
        assert config.shutdown_grace_seconds == 5.0

    def test_custom_grace_period(self) -> None:
        """Test that grace period can be customized."""
        config = SchedulerConfig(shutdown_grace_seconds=10.0)
        assert config.shutdown_grace_seconds == 10.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_scheduler.py::TestSchedulerGracePeriod -v`
Expected: FAIL — `shutdown_grace_seconds` attribute doesn't exist.

- [ ] **Step 3: Add `shutdown_grace_seconds` to SchedulerConfig**

In `maestro/scheduler.py`, modify the `SchedulerConfig` dataclass (line ~121):

```python
@dataclass
class SchedulerConfig:
    """Configuration for the scheduler.

    Attributes:
        max_concurrent: Maximum number of concurrent tasks.
        poll_interval: Seconds between scheduler loop iterations.
        workdir: Base working directory for tasks.
        log_dir: Directory for task log files.
        shutdown_grace_seconds: Seconds to wait between SIGTERM and SIGKILL.
    """

    max_concurrent: int = 3
    poll_interval: float = 1.0
    workdir: Path = field(default_factory=lambda: Path.cwd())
    log_dir: Path = field(default_factory=lambda: Path.cwd() / "logs")
    shutdown_grace_seconds: float = 5.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_scheduler.py::TestSchedulerGracePeriod -v`
Expected: PASS

- [ ] **Step 5: Replace hardcoded 0.5s in scheduler**

In `maestro/scheduler.py`, the `Scheduler.__init__` stores config as `self._config`. Replace the two `asyncio.sleep(0.5)` occurrences:

**In `_handle_timeout` (line ~727):**
```python
            running_task.process.terminate()
            # Give it time to terminate gracefully
            await asyncio.sleep(self._config.shutdown_grace_seconds)
            if running_task.process.poll() is None:
                running_task.process.kill()
```

**In `_cleanup` (line ~813):**
```python
                running_task.process.terminate()
                # Give processes time to terminate gracefully
                await asyncio.sleep(self._config.shutdown_grace_seconds)
                if running_task.process.poll() is None:
                    running_task.process.kill()
```

- [ ] **Step 6: Replace hardcoded 0.5s in orchestrator**

In `maestro/orchestrator.py`, the `_cleanup` method (line ~614). The orchestrator doesn't have `SchedulerConfig`, so add a class attribute:

Find the `Orchestrator.__init__` and add `self._shutdown_grace_seconds: float = 5.0` (or read from `self._config` if `OrchestratorConfig` has it).

Then in `_cleanup` (line ~614):
```python
                running.process.terminate()
                await asyncio.sleep(self._shutdown_grace_seconds)
                if running.process.returncode is None:
                    running.process.kill()
```

- [ ] **Step 7: Run full test suites**

Run: `uv run python -m pytest tests/test_scheduler.py tests/test_orchestrator.py -v`
Expected: All PASS.

- [ ] **Step 8: Commit**

```bash
git add maestro/scheduler.py maestro/orchestrator.py tests/test_scheduler.py
git commit -m "feat: increase shutdown grace period from 0.5s to 5s (T-05)"
```

---

## Task 3: Add DEBUG logging to silent except blocks (T-07)

**Files:**
- Modify: `maestro/scheduler.py:732-733`
- Modify: `maestro/scheduler.py:818-819`
- Modify: `maestro/orchestrator.py:426-427`
- Modify: `maestro/orchestrator.py:618-619`
- Modify: `maestro/pr_manager.py:163-164`
- Test: no new tests needed (logging changes, verified by inspection)

- [ ] **Step 1: Fix scheduler.py — _handle_timeout (line 732)**

Replace:
```python
        except OSError:
            pass  # Process may have already exited
```
With:
```python
        except OSError as e:
            logger.debug("Failed to terminate timed-out process for task %s: %s", task_id, e)
```

- [ ] **Step 2: Fix scheduler.py — _cleanup (line 818)**

Replace:
```python
            except OSError:
                pass
```
With:
```python
            except OSError as e:
                logger.debug("Failed to terminate process for task %s during cleanup: %s", task_id, e)
```

- [ ] **Step 3: Fix orchestrator.py — state file parsing (line 426)**

Replace:
```python
        except (json.JSONDecodeError, OSError):
            pass  # State file may be partially written
```
With:
```python
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("Failed to read state file for zadacha %s: %s", zid, e)
```

Ensure `logger` is defined at module level in `orchestrator.py`. Check the top of the file for:
```python
logger = logging.getLogger(__name__)
```

- [ ] **Step 4: Fix orchestrator.py — _cleanup (line 618)**

Replace:
```python
            except OSError:
                pass
```
With:
```python
            except OSError as e:
                logger.debug("Failed to terminate process for zadacha %s during cleanup: %s", zid, e)
```

- [ ] **Step 5: Fix pr_manager.py (line 163)**

Replace:
```python
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
```
With:
```python
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.debug("Failed to get default branch: %s", e)
```

Ensure `logger` is defined at module level in `pr_manager.py`. Check the top of the file for:
```python
logger = logging.getLogger(__name__)
```

- [ ] **Step 6: Run full test suite to verify no regressions**

Run: `uv run python -m pytest -x -q`
Expected: All 944+ tests PASS.

- [ ] **Step 7: Commit**

```bash
git add maestro/scheduler.py maestro/orchestrator.py maestro/pr_manager.py
git commit -m "fix: add DEBUG logging to silent except blocks (T-07)"
```

---

## Task 4: Fix blocking async calls (T-14)

**Files:**
- Modify: `maestro/orchestrator.py:347-352` (_spawn_zadacha)
- Modify: `maestro/scheduler.py:727-731` (_handle_timeout)
- Modify: `maestro/scheduler.py:813-817` (_cleanup)
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Write failing test for non-blocking log file open**

Add to `tests/test_orchestrator.py`:

```python
class TestNonBlockingSpawn:
    """Tests that spawn operations don't block the event loop."""

    @pytest.mark.anyio
    async def test_log_file_opened_without_sync_io(self, tmp_path: Path) -> None:
        """Test that log files are opened using async-safe approach."""
        log_file = tmp_path / "test.log"
        # The fix should use os.open() instead of log_file.open("w") in async context
        fd = os.open(str(log_file), os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        assert fd > 0
        os.close(fd)
        assert log_file.exists()
```

- [ ] **Step 2: Fix orchestrator.py — sync log_file.open("w") in async context**

In `maestro/orchestrator.py`, find `_spawn_zadacha` method around line 347-352:

```python
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workspace,
            stdout=log_file.open("w"),
            stderr=asyncio.subprocess.STDOUT,
        )
```

Replace with:

```python
        log_fd = os.open(str(log_file), os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=workspace,
                stdout=log_fd,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception:
            os.close(log_fd)
            raise
```

Add `import os` at the top of orchestrator.py if not already present.

- [ ] **Step 3: Fix scheduler.py — sync process.wait() calls**

In `maestro/scheduler.py` `_handle_timeout` (line ~731), replace:
```python
            running_task.process.wait()
```
With:
```python
            await asyncio.get_event_loop().run_in_executor(
                None, running_task.process.wait
            )
```

In `_cleanup` (line ~817), replace:
```python
                running_task.process.wait()
```
With:
```python
                await asyncio.get_event_loop().run_in_executor(
                    None, running_task.process.wait
                )
```

- [ ] **Step 4: Run full test suite**

Run: `uv run python -m pytest -x -q`
Expected: All 944+ tests PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/orchestrator.py maestro/scheduler.py
git commit -m "fix: replace blocking sync calls with async-safe alternatives (T-14)"
```

---

## Task 5: Create DOGFOOD_LOG.md template

**Files:**
- Create: `DOGFOOD_LOG.md`

- [ ] **Step 1: Create the dogfooding log template**

Create `DOGFOOD_LOG.md` in the project root with this content:

```markdown
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

(to be filled during first dogfooding run)
```

- [ ] **Step 2: Commit**

```bash
git add DOGFOOD_LOG.md
git commit -m "docs: add DOGFOOD_LOG.md template for dogfooding tracking"
```

---

## Task 6: Write first real tasks.yaml for dogfooding

**Files:**
- Create: `examples/dogfood-maestro.yaml`

- [ ] **Step 1: Verify existing example configs for YAML schema reference**

Run: `ls examples/`

Read any existing YAML examples to understand the exact schema (field names, structure).

- [ ] **Step 2: Write a real dogfooding config**

Create `examples/dogfood-maestro.yaml` that uses Maestro to improve itself — a mix of simple quick wins from the backlog:

```yaml
project: "maestro-dogfood"
repo: "/Users/Andrei_Shtanakov/labs/all_ai_orchestrators/Maestro"
max_concurrent: 2

defaults:
  agent_type: "claude_code"
  timeout_minutes: 15
  max_retries: 1

tasks:
  - id: add-jitter
    title: "Add jitter to retry backoff"
    prompt: |
      In maestro/retry.py, modify RetryManager.get_delay() to add random jitter.
      The current formula is: base_delay * (2 ** retry_count), capped at max_delay.
      Add random jitter: after calculating delay, multiply by random.uniform(0.7, 1.3).
      Import random at the top of the file.
      Update tests in tests/test_retry.py:
      - Existing exact-value tests should check delay is within the expected range.
      - Add a test that calls get_delay(0) 100 times and verifies not all values are identical.
      Run: uv run python -m pytest tests/test_retry.py -v
    scope:
      - "maestro/retry.py"
      - "tests/test_retry.py"

  - id: json-schema
    title: "Generate JSON Schema for YAML configs"
    prompt: |
      Create maestro/schemas/ directory.
      Write a script maestro/schemas/generate.py that:
      1. Imports ProjectConfig and OrchestratorConfig from maestro.models
      2. Calls .model_json_schema() on each
      3. Writes the result to maestro/schemas/project_config.json and orchestrator_config.json
      Run the script and commit the generated JSON files.
      Run: uv run python maestro/schemas/generate.py
    scope:
      - "maestro/schemas/**"

  - id: dedup-cycles
    title: "Remove duplicate cycle detection from models.py"
    prompt: |
      In maestro/models.py, the method validate_no_cyclic_dependencies() in
      ProjectConfig (around line 402-441) duplicates cycle detection that already
      exists in DAG.__init__() in maestro/dag.py.
      Remove validate_no_cyclic_dependencies() from ProjectConfig.
      Update tests in tests/test_models.py if any tests specifically test
      cycle detection through ProjectConfig validation.
      Run: uv run python -m pytest tests/test_models.py tests/test_dag.py -v
    scope:
      - "maestro/models.py"
      - "tests/test_models.py"
      - "tests/test_dag.py"
    depends_on: []

  - id: entry-points
    title: "Formalize spawner entry points in pyproject.toml"
    prompt: |
      In pyproject.toml, add an entry-points section for spawner discovery:
      [project.entry-points."maestro.spawners"]
      claude_code = "maestro.spawners.claude_code:ClaudeCodeSpawner"
      codex = "maestro.spawners.codex:CodexSpawner"
      aider = "maestro.spawners.aider:AiderSpawner"
      announce = "maestro.spawners.announce:AnnounceSpawner"
      Verify the class names match what is actually exported in each file.
    scope:
      - "pyproject.toml"

  - id: max-concurrent-cap
    title: "Raise max_concurrent cap from 10 to 100"
    prompt: |
      In maestro/models.py, find all Field() definitions with le=10 for max_concurrent
      (in ProjectConfig and OrchestratorConfig). Change le=10 to le=100.
      Update the description strings accordingly.
      Update tests in tests/test_models.py to verify max_concurrent=50 is valid.
      Run: uv run python -m pytest tests/test_models.py -v -k "concurrent"
    scope:
      - "maestro/models.py"
      - "tests/test_models.py"
    depends_on: [dedup-cycles]
```

- [ ] **Step 3: Validate the YAML against Maestro's config schema**

Run: `uv run python -c "from maestro.config import load_config; c = load_config('examples/dogfood-maestro.yaml'); print(f'Valid: {len(c.tasks)} tasks')"`

If this fails, fix the YAML to match the actual schema (check field names in `maestro/models.py` `TaskConfig` and `ProjectConfig`).

- [ ] **Step 4: Commit**

```bash
git add examples/dogfood-maestro.yaml
git commit -m "feat: add dogfooding YAML config for Maestro self-improvement"
```

---

## Task 7: First dogfooding run

**Files:**
- Modify: `DOGFOOD_LOG.md` (log findings)

- [ ] **Step 1: Run Maestro on itself**

```bash
uv run maestro run examples/dogfood-maestro.yaml
```

- [ ] **Step 2: Monitor and observe**

Watch the dashboard (if available) or tail logs:
```bash
tail -f logs/*.log
```

Check `uv run maestro status` periodically.

- [ ] **Step 3: Log all issues in DOGFOOD_LOG.md**

For every problem encountered — bugs, confusing output, missing features, crashes — add an entry to `DOGFOOD_LOG.md` with severity and backlog reference.

- [ ] **Step 4: Triage and prioritize**

After the run completes (or fails), review DOGFOOD_LOG.md:
- Which issues are BLOCKER for continued dogfooding?
- Which match existing backlog items (T-01..T-34)?
- What was unexpected (not in the theoretical backlog)?

- [ ] **Step 5: Commit the log**

```bash
git add DOGFOOD_LOG.md
git commit -m "docs: log findings from first dogfooding run"
```

---

## Execution Order

Tasks 1-4 are **independent** — they can be executed in parallel by separate agents.
Task 5 is trivial and independent.
Task 6 depends on Tasks 1-4 being merged (the config should run on the fixed codebase).
Task 7 depends on all previous tasks.

```
T1 (flock) --------+
T2 (grace period) --+
T3 (debug logging) -+---> T6 (write tasks.yaml) ---> T7 (first run)
T4 (async fixes) ---+
T5 (dogfood log) --+
```
