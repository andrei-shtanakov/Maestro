"""CLI interface for Maestro orchestrator.

This module provides a command-line interface using Typer for:
- Running tasks from YAML configuration files
- Checking task status
- Retrying failed tasks
- Stopping the scheduler
- Resuming interrupted runs
"""

import asyncio
import contextlib
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from maestro import (
    ClaudeCodeSpawner,
    ConfigError,
    CycleError,
    Database,
    StateRecovery,
    TaskNotFoundError,
    create_database,
    create_notification_manager,
    create_scheduler_from_config,
    load_config,
)
from maestro import merge_logs as _merge_logs
from maestro.benchmark import (
    BenchmarkRunner,
    MaestroATPAdapter,
    SpawnerResponder,
    report_benchmark_to_arbiter,
)
from maestro.benchmark.models import BenchmarkResult
from maestro.catalog_cli import models_app
from maestro.config import load_orchestrator_config
from maestro.coordination.arbiter_client import ArbiterClient, ArbiterClientConfig
from maestro.coordination.routing import RoutingStrategy, make_routing_strategy
from maestro.dag import DAG
from maestro.decomposer import ProjectDecomposer
from maestro.event_log import create_event_logger
from maestro.git import GitManager
from maestro.logging_bridge import setup_logging
from maestro.models import ArbiterMode, OrchestratorConfig, TaskStatus, WorkstreamStatus
from maestro.orchestrator import Orchestrator
from maestro.pr_manager import PRManager
from maestro.preflight import (
    ValidationIssue,
    ValidationReport,
    validate_project,
)
from maestro.scaffold import ScaffoldError, generate_project_yaml
from maestro.spawners import (
    AiderSpawner,
    AnnounceSpawner,
    CodexSpawner,
    OpencodeSpawner,
)
from maestro.spawners.base import AgentSpawner
from maestro.workspace import WorkspaceManager


if TYPE_CHECKING:
    from maestro.cost_tracker import CostReport
    from maestro.gates import ApprovalMarker


# Default paths
DEFAULT_DB_DIR = Path.home() / ".maestro"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "maestro.db"
PID_FILE = DEFAULT_DB_DIR / "maestro.pid"

# Rich console for pretty output
console = Console()
err_console = Console(stderr=True)

# Typer app
app = typer.Typer(
    name="maestro",
    help="AI Agent Orchestrator for coordinating multiple coding agents.",
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(models_app, name="models")


# Benchmark constants and helpers
_ALLOWED_BENCH_AGENTS = ("claude_code", "codex_cli", "aider", "opencode")

# Mirrors each spawner's (now-removed) `is_available()`: `shutil.which(cli)
# is not None`. Kept here so the CLI can probe PATH directly without an
# async ExecutionBackend.can_run() round-trip inside a sync command.
_BENCH_CLI_BY_AGENT: dict[str, str] = {
    "claude_code": "claude",
    "codex_cli": "codex",
    "aider": "aider",
    "opencode": "opencode",
}


def _agent_cli_available(agent: str) -> bool:
    """Whether the agent's CLI binary is on PATH.

    Mirrors the removed spawner ``is_available()``. Kept as a
    module-level seam so tests can monkeypatch availability without
    requiring the real CLI binary on PATH.
    """
    return shutil.which(_BENCH_CLI_BY_AGENT[agent]) is not None


def _bench_spawner_for(agent: str) -> AgentSpawner:
    """Fresh spawner for a benchmark run. Module-level for test monkeypatching."""
    from maestro.spawners import (
        AiderSpawner,
        ClaudeCodeSpawner,
        CodexSpawner,
        OpencodeSpawner,
    )

    factories: dict[str, type[AgentSpawner]] = {
        "claude_code": ClaudeCodeSpawner,
        "codex_cli": CodexSpawner,
        "aider": AiderSpawner,
        "opencode": OpencodeSpawner,
    }
    return factories[agent]()


async def _benchmark_flow(
    adapter,
    responder,
    benchmark_id: str,
    run_id: str | None,
    arbiter_bin: str | None,
    no_report: bool,
    notes: Console,
) -> BenchmarkResult:
    """Run the benchmark, then dispatch the (optional) arbiter report."""
    async with adapter:
        result = await BenchmarkRunner(adapter, responder).run(
            benchmark_id, run_id=run_id
        )
    if no_report:
        notes.print("arbiter report skipped (--no-report)")
        return result
    if not arbiter_bin:
        notes.print("arbiter report skipped (MAESTRO_ARBITER_BIN unset)")
        return result
    return await _report_with_lifecycle(result, arbiter_bin, notes)


async def _report_with_lifecycle(
    result: BenchmarkResult, arbiter_bin: str, notes: Console
) -> BenchmarkResult:
    """M4 fire-and-forget report with explicit client lifecycle.

    start() failure counts as a report failure (report_status="failed"),
    never as a run failure; stop() is awaited on every path so the
    subprocess can't leak. Paths follow the smoke-script convention:
    the binary lives at <repo>/target/release/arbiter-mcp.
    """
    bin_path = Path(arbiter_bin)
    repo = bin_path.parent.parent.parent
    config = ArbiterClientConfig(
        binary_path=str(bin_path),
        config_dir=str(repo / "config"),
        tree_path=str(repo / "models" / "agent_policy_tree.json"),
    )
    client = ArbiterClient(config)
    try:
        await client.start()
        result = await report_benchmark_to_arbiter(result, client)
    except Exception as exc:  # start() failure = report failure, not run failure
        result = result.model_copy(
            update={"report_status": "failed", "report_error": str(exc)}
        )
    finally:
        # stop() is idempotent and safe after failed start(), so
        # unconditional best-effort cleanup is sufficient.
        with contextlib.suppress(Exception):
            await client.stop()
    return result


def _get_status_style(status: TaskStatus) -> str:
    """Return Rich style for task status."""
    styles = {
        TaskStatus.DONE: "green",
        TaskStatus.RUNNING: "yellow",
        TaskStatus.VALIDATING: "yellow",
        TaskStatus.FAILED: "red",
        TaskStatus.NEEDS_REVIEW: "red",
        TaskStatus.PENDING: "dim",
        TaskStatus.READY: "cyan",
        TaskStatus.AWAITING_APPROVAL: "magenta",
        TaskStatus.ABANDONED: "dim red",
    }
    return styles.get(status, "white")


def _format_status(status: TaskStatus) -> Text:
    """Format task status with color."""
    style = _get_status_style(status)
    return Text(status.value.upper(), style=style)


def _ensure_db_dir() -> None:
    """Ensure the default database directory exists."""
    DEFAULT_DB_DIR.mkdir(parents=True, exist_ok=True)


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
        try:
            existing_pid = os.read(fd, 32).decode().strip()
        except OSError:
            existing_pid = "unknown"
        os.close(fd)
        err_console.print(
            f"[red]Maestro is already running (PID: {existing_pid}). "
            f"Stop it first with 'maestro stop'.[/red]"
        )
        raise SystemExit(1) from None
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(os.getpid()).encode())
    return fd


def _release_pid_lock(fd: int, pid_file: Path | None = None) -> None:
    """Release PID file lock and remove the file."""
    if pid_file is None:
        pid_file = PID_FILE
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except OSError:
        pass
    with contextlib.suppress(FileNotFoundError):
        pid_file.unlink()


def _read_pid_file() -> int | None:
    """Read PID from file, return None if not found."""
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


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

        # Also show new untracked files
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
                line
                for line in result_untracked.stdout.strip().split("\n")
                if line.startswith("??")
            ]
            if new_files:
                console.print("\n[bold]New files:[/bold]")
                for f in new_files:
                    console.print(f"  [green]{f[3:]}[/green]")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass  # git not available or timeout


def _get_git_head(workdir: Path) -> str:
    """Get current git HEAD SHA."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _display_auto_commits(
    workdir: Path,
    head_before: str,
) -> None:
    """Display commits created during the run (by auto-commit)."""
    if not head_before:
        return
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "--oneline",
                "--stat",
                f"{head_before}..HEAD",
            ],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            console.print("\n[bold]Commits created during run:[/bold]")
            console.print(result.stdout.rstrip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _display_tasks_table(tasks: list, title: str = "Tasks") -> None:
    """Display tasks in a rich table."""
    if not tasks:
        console.print("[dim]No tasks found.[/dim]")
        return

    table = Table(title=title, show_header=True, header_style="bold")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Title", style="white")
    table.add_column("Status", no_wrap=True)
    table.add_column("Agent", style="dim")
    table.add_column("Retries", justify="center")
    table.add_column("Error", style="red", max_width=40)

    for task in tasks:
        status_text = _format_status(task.status)
        retry_str = f"{task.retry_count}/{task.max_retries}"
        error = (
            task.error_message[:37] + "..."
            if task.error_message and len(task.error_message) > 40
            else (task.error_message or "")
        )

        table.add_row(
            task.id,
            task.title,
            status_text,
            task.agent_type.value,
            retry_str,
            error,
        )

    console.print(table)


def _display_summary(tasks: list) -> None:
    """Display a summary of task statuses."""
    if not tasks:
        return

    status_counts: dict[TaskStatus, int] = {}
    for task in tasks:
        status_counts[task.status] = status_counts.get(task.status, 0) + 1

    parts = []
    for status in TaskStatus:
        count = status_counts.get(status, 0)
        if count > 0:
            style = _get_status_style(status)
            parts.append(f"[{style}]{status.value}: {count}[/{style}]")

    console.print("\n" + " | ".join(parts))


def _print_validation_report(report: ValidationReport) -> None:
    """Render preflight issues and a summary line."""
    for issue in report.issues:
        color = "red" if issue.severity == "error" else "yellow"
        location = (
            f" {', '.join(issue.workstream_ids)}:" if issue.workstream_ids else ""
        )
        console.print(
            f"[{color}]{issue.severity}[/{color}] "
            f"{escape(f'[{issue.code}]')}{escape(location)} "
            f"{escape(issue.message)}"
        )
    n_err, n_warn = len(report.errors), len(report.warnings)
    style = "red" if n_err else ("yellow" if n_warn else "green")
    console.print(f"[{style}]{n_err} errors, {n_warn} warnings[/{style}]")


async def _run_scheduler(
    config_path: Path,
    db_path: Path,
    resume: bool,
    log_dir: Path | None,
    clean: bool = False,
) -> None:
    """Run the scheduler with the given configuration."""
    # Load configuration
    try:
        config = load_config(config_path)
    except ConfigError as e:
        err_console.print(f"[red]Configuration error:[/red] {e}")
        raise typer.Exit(1) from e

    # Validate DAG
    try:
        dag = DAG(config.tasks)
        warnings = dag.check_scope_overlaps()
        for warning in warnings:
            console.print(f"[yellow]Warning:[/yellow] {warning}")
    except CycleError as e:
        err_console.print(f"[red]DAG error:[/red] {e}")
        raise typer.Exit(1) from e

    # Ensure DB directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Clean existing state if requested
    if clean and db_path.exists():  # noqa: ASYNC240
        db_path.unlink()  # noqa: ASYNC240
        console.print("[yellow]Cleaned database for fresh start[/yellow]")

    # Create or connect to database
    db = await create_database(db_path)
    lock_fd: int | None = None

    # R-03: pick routing strategy. StaticRouting if cfg.arbiter is None or
    # disabled; ArbiterRouting (with its subprocess) when enabled.
    # Build inside try/finally so db.close() and lock release always run even
    # if arbiter startup raises (e.g. ArbiterStartupError with optional=false).
    arbiter_cfg = config.arbiter
    arbiter_mode = arbiter_cfg.mode if arbiter_cfg is not None else ArbiterMode.ADVISORY
    routing: RoutingStrategy | None = None

    try:
        routing = await make_routing_strategy(arbiter_cfg)

        # Determine the log directory and activate the structured event log
        # BEFORE any resume/recovery work: StateRecovery.recover() emits events
        # via get_event_logger() (e.g. RECOVERY_ARBITER_DECISIONS_CLOSED), so
        # activating the logger later would drop recovery events on --resume.
        workdir = Path(config.repo).expanduser()  # noqa: ASYNC240
        if log_dir is None:
            log_dir = workdir / "logs"
        create_event_logger(log_dir)

        # Check if resuming
        if resume:
            existing_tasks = await db.get_all_tasks()
            if existing_tasks:
                console.print(
                    f"[cyan]Resuming with {len(existing_tasks)} existing tasks[/cyan]"
                )

                # Perform state recovery for orphaned tasks
                recovery = StateRecovery(db, execution=config.execution)
                if await recovery.needs_recovery():
                    console.print(
                        "[yellow]Detected orphaned tasks, performing recovery...[/yellow]"
                    )
                    stats = await recovery.recover(routing=routing)
                    console.print(
                        Panel(
                            f"[green]Recovery complete[/green]\n"
                            f"RUNNING → READY: {stats.running_recovered}\n"
                            f"VALIDATING → READY: {stats.validating_recovered}\n"
                            f"Total recovered: {stats.total_recovered}\n"
                            f"Already done: {stats.tasks_done}",
                            title="State Recovery",
                        )
                    )
            else:
                console.print(
                    "[yellow]No existing tasks found, starting fresh[/yellow]"
                )

        # Setup spawners — all five built-ins so YAML configs with
        # agent_type: codex_cli / aider / announce / opencode work out of
        # the box, matching what examples/hello.yaml, examples/tasks.yaml,
        # and the arbiter policy tree's agent set expect.
        spawners: dict[str, AgentSpawner] = {
            "claude_code": ClaudeCodeSpawner(),
            "codex_cli": CodexSpawner(),
            "aider": AiderSpawner(),
            "announce": AnnounceSpawner(),
            "opencode": OpencodeSpawner(),
        }

        # Setup notifications
        notifications = create_notification_manager(config.notifications)

        # Setup streaming progress callback
        _task_start_times: dict[str, datetime] = {}

        def _on_status_change(
            task_id: str,
            old_status: str,
            new_status: str,
        ) -> None:
            now = datetime.now(UTC)
            timestamp = now.strftime("%H:%M:%S")
            if new_status == "running":
                _task_start_times[task_id] = now
                console.print(
                    f"[dim]{timestamp}[/dim] "
                    f"[cyan]{task_id}[/cyan]: "
                    f"[yellow]RUNNING[/yellow]"
                )
            elif new_status == "done":
                elapsed = ""
                if task_id in _task_start_times:
                    delta = now - _task_start_times[task_id]
                    minutes = int(delta.total_seconds() // 60)
                    seconds = int(delta.total_seconds() % 60)
                    elapsed = f" [dim]({minutes}m{seconds:02d}s)[/dim]"
                console.print(
                    f"[dim]{timestamp}[/dim] "
                    f"[cyan]{task_id}[/cyan]: "
                    f"[green]DONE[/green]{elapsed}"
                )
            elif new_status == "failed":
                console.print(
                    f"[dim]{timestamp}[/dim] [cyan]{task_id}[/cyan]: [red]FAILED[/red]"
                )
            elif new_status == "needs_review":
                console.print(
                    f"[dim]{timestamp}[/dim] "
                    f"[cyan]{task_id}[/cyan]: "
                    f"[red]NEEDS_REVIEW[/red]"
                )
            elif new_status == "ready" and old_status == "failed":
                console.print(
                    f"[dim]{timestamp}[/dim] "
                    f"[cyan]{task_id}[/cyan]: "
                    f"[yellow]RETRYING[/yellow]"
                )

        # Create scheduler
        scheduler = await create_scheduler_from_config(
            db=db,
            tasks=config.tasks,
            spawners=spawners,  # type: ignore[arg-type]  # variance of invariant dict
            max_concurrent=config.max_concurrent,
            workdir=workdir,
            log_dir=log_dir,
            notification_manager=notifications,
            on_status_change=_on_status_change,
            auto_commit=(config.git.auto_commit if config.git else False),
            routing=routing,
            arbiter_mode=arbiter_mode,
            arbiter_enabled=arbiter_cfg is not None and arbiter_cfg.enabled,
            execution=config.execution,
        )
        if arbiter_cfg is not None:
            scheduler._abandon_outcome_after_s = arbiter_cfg.abandon_outcome_after_s

        # Display initial state
        all_tasks = await db.get_all_tasks()
        _display_tasks_table(all_tasks, "Starting Tasks")

        # Acquire PID lock
        lock_fd = _acquire_pid_lock()

        console.print(
            Panel(
                f"[green]Scheduler started[/green]\n"
                f"Project: {config.project}\n"
                f"Max concurrent: {config.max_concurrent}\n"
                f"Tasks: {len(config.tasks)}",
                title="Maestro",
            )
        )

        # Record HEAD before run for commit summary
        head_before = _get_git_head(workdir)

        # Run scheduler
        await scheduler.run()

        # Show what agents changed
        _display_git_summary(workdir)
        _display_auto_commits(workdir, head_before)

        # Display final state
        all_tasks = await db.get_all_tasks()
        console.print()
        _display_tasks_table(all_tasks, "Final Status")
        _display_summary(all_tasks)

        # Check for failures
        failed_tasks = [
            t
            for t in all_tasks
            if t.status in (TaskStatus.FAILED, TaskStatus.NEEDS_REVIEW)
        ]
        if failed_tasks:
            console.print(
                f"\n[red]Warning: {len(failed_tasks)} task(s) failed or need review[/red]"
            )
            raise typer.Exit(1)

        console.print("\n[green]All tasks completed successfully![/green]")

    finally:
        if routing is not None:
            await routing.aclose()
        await db.close()
        if lock_fd is not None:
            _release_pid_lock(lock_fd)


async def _show_status(db_path: Path) -> None:
    """Show status of all tasks in the database."""
    # Path.exists() is fast sync I/O, acceptable in async context
    if not db_path.exists():  # noqa: ASYNC240
        err_console.print(f"[red]Database not found:[/red] {db_path}")
        err_console.print("Run 'maestro run <config>' first to create tasks.")
        raise typer.Exit(1)

    db = Database(db_path)
    await db.connect()

    try:
        tasks = await db.get_all_tasks()
        _display_tasks_table(tasks, "Task Status")
        _display_summary(tasks)

        # Show running info
        pid = _read_pid_file()
        if pid:
            console.print(f"\n[cyan]Scheduler running (PID: {pid})[/cyan]")
        else:
            console.print("\n[dim]Scheduler not running[/dim]")

    finally:
        await db.close()


async def _retry_task(db_path: Path, task_id: str) -> None:
    """Retry a failed task by resetting its status to READY."""
    # Path.exists() is fast sync I/O, acceptable in async context
    if not db_path.exists():  # noqa: ASYNC240
        err_console.print(f"[red]Database not found:[/red] {db_path}")
        raise typer.Exit(1)

    db = Database(db_path)
    await db.connect()

    try:
        # Get the task
        try:
            task = await db.get_task(task_id)
        except TaskNotFoundError:
            err_console.print(f"[red]Task not found:[/red] {task_id}")
            raise typer.Exit(1) from None

        # Check if task can be retried
        retryable_statuses = {TaskStatus.FAILED, TaskStatus.NEEDS_REVIEW}
        if task.status not in retryable_statuses:
            err_console.print(
                f"[red]Cannot retry task in status:[/red] {task.status.value}"
            )
            err_console.print(
                f"Task must be in one of: {', '.join(s.value for s in retryable_statuses)}"
            )
            raise typer.Exit(1)

        # Reset retry count and status
        await db.update_task_status(
            task_id,
            TaskStatus.READY,
            error_message=None,
            retry_count=0,
        )

        console.print(f"[green]Task '{task_id}' reset to READY status[/green]")
        console.print("Run 'maestro run --resume' to continue execution.")

    finally:
        await db.close()


async def _approve_task(db_path: Path, task_id: str) -> None:
    """Approve a task that is waiting for approval."""
    # Path.exists() is fast sync I/O, acceptable in async context
    if not db_path.exists():  # noqa: ASYNC240
        err_console.print(f"[red]Database not found:[/red] {db_path}")
        raise typer.Exit(1)

    db = Database(db_path)
    await db.connect()

    try:
        # Get the task
        try:
            task = await db.get_task(task_id)
        except TaskNotFoundError:
            err_console.print(f"[red]Task not found:[/red] {task_id}")
            raise typer.Exit(1) from None

        # Check if task is awaiting approval
        if task.status != TaskStatus.AWAITING_APPROVAL:
            err_console.print(
                f"[red]Task is not awaiting approval:[/red] {task.status.value}"
            )
            err_console.print(
                "Only tasks with status 'awaiting_approval' can be approved."
            )
            raise typer.Exit(1)

        # Approve the task by transitioning to RUNNING
        await db.update_task_status(task_id, TaskStatus.READY)

        console.print(f"[green]Task '{task_id}' approved and set to READY[/green]")
        console.print("The scheduler will pick it up on the next iteration.")

    finally:
        await db.close()


def _stop_scheduler() -> None:
    """Stop the running scheduler by sending SIGTERM."""
    pid = _read_pid_file()
    if pid is None:
        err_console.print("[yellow]No running scheduler found[/yellow]")
        raise typer.Exit(0)

    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Sent stop signal to scheduler (PID: {pid})[/green]")
    except ProcessLookupError:
        err_console.print(
            f"[yellow]Process {pid} not found, removing stale PID file[/yellow]"
        )
        with contextlib.suppress(FileNotFoundError):
            PID_FILE.unlink()
    except PermissionError:
        err_console.print(f"[red]Permission denied to stop process {pid}[/red]")
        raise typer.Exit(1) from None


@app.command("run")
def run_command(
    config: Annotated[
        Path,
        typer.Argument(
            help="Path to YAML configuration file",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ],
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            "-d",
            help="Path to SQLite database file",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume",
            "-r",
            help="Resume from existing database state",
        ),
    ] = False,
    log_dir: Annotated[
        Path | None,
        typer.Option(
            "--log-dir",
            "-l",
            help="Directory for task log files",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
    clean: Annotated[
        bool,
        typer.Option(
            "--clean",
            help="Reset all tasks and start fresh",
        ),
    ] = False,
) -> None:
    """Run tasks from a YAML configuration file.

    The scheduler will execute tasks respecting their dependencies,
    up to the configured concurrency limit.

    Examples:
        maestro run tasks.yaml
        maestro run tasks.yaml --resume
        maestro run tasks.yaml --clean
        maestro run tasks.yaml --db /path/to/state.db
    """
    setup_logging("maestro")
    db_path = db or DEFAULT_DB_PATH

    try:
        asyncio.run(_run_scheduler(config, db_path, resume, log_dir, clean))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        raise typer.Exit(130) from None


@app.command("status")
def status_command(
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            "-d",
            help="Path to SQLite database file",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Show status of all tasks.

    Displays a table of all tasks with their current status,
    retry counts, and any error messages.

    Examples:
        maestro status
        maestro status --db /path/to/state.db
    """
    db_path = db or DEFAULT_DB_PATH
    asyncio.run(_show_status(db_path))


@app.command("retry")
def retry_command(
    task_id: Annotated[
        str,
        typer.Argument(help="ID of the task to retry"),
    ],
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            "-d",
            help="Path to SQLite database file",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Retry a failed task.

    Resets the task status to READY and clears the retry count,
    allowing it to be picked up by the scheduler again.

    Examples:
        maestro retry task-001
        maestro retry task-001 --db /path/to/state.db
    """
    db_path = db or DEFAULT_DB_PATH
    asyncio.run(_retry_task(db_path, task_id))


@app.command("stop")
def stop_command() -> None:
    """Stop the running scheduler.

    Sends a termination signal to the scheduler process.
    The scheduler will complete any final cleanup before exiting.

    Examples:
        maestro stop
    """
    _stop_scheduler()


@app.command("approve")
def approve_command(
    task_id: Annotated[
        str,
        typer.Argument(help="ID of the task to approve"),
    ],
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            "-d",
            help="Path to SQLite database file",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Approve a task waiting for approval.

    Approves a task that has requires_approval=true and is in
    AWAITING_APPROVAL status, allowing the scheduler to execute it.

    Examples:
        maestro approve task-001
        maestro approve task-001 --db /path/to/state.db
    """
    db_path = db or DEFAULT_DB_PATH
    asyncio.run(_approve_task(db_path, task_id))


@app.command("validate")
def validate_command(
    config: Annotated[
        Path,
        typer.Argument(
            help="Path to project YAML configuration",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ],
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Treat warnings as errors (exit 1)"),
    ] = False,
    no_fs: Annotated[
        bool,
        typer.Option(
            "--no-fs",
            help=(
                "Skip filesystem checks (repo existence, glob matching). "
                "Only the static overlap heuristic runs; it can miss "
                "overlaps the filesystem tier would catch."
            ),
        ),
    ] = False,
) -> None:
    """Validate a Mode-2 project.yaml without running it.

    Checks dependency cycles, scope overlaps, and repository sanity.
    Exit code 0 when there are no errors (warnings allowed unless
    --strict), 1 otherwise.
    """
    try:
        project = load_orchestrator_config(config)
    except ConfigError as e:
        _print_validation_report(
            ValidationReport(
                issues=[
                    ValidationIssue(severity="error", code="schema", message=str(e))
                ]
            )
        )
        raise typer.Exit(1) from e

    report = validate_project(project, check_fs=not no_fs)
    _print_validation_report(report)
    if not report.ok or (strict and report.warnings):
        raise typer.Exit(1)


@app.command("init")
def init_command(
    path: Annotated[
        Path,
        typer.Argument(help="Output path for the generated config"),
    ] = Path("project.yaml"),
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite an existing file"),
    ] = False,
    project: Annotated[
        str | None,
        typer.Option(
            "--project", help="Project name (default: current directory name)"
        ),
    ] = None,
) -> None:
    """Generate a Mode-2 project.yaml scaffold for the current directory.

    Values are autofilled from the git environment (remote URL, base
    branch); everything else gets commented, schema-valid defaults.
    """
    if path.exists() and not force:
        err_console.print(
            f"[red]{path} already exists.[/red] Use --force to overwrite."
        )
        raise typer.Exit(1)

    try:
        content = generate_project_yaml(Path.cwd(), project=project)
    except ScaffoldError as e:
        err_console.print(f"[red]Scaffold error:[/red] {e}")
        raise typer.Exit(1) from e

    path.write_text(content, encoding="utf-8")
    console.print(
        f"[green]Wrote {path}.[/green] Next: edit the workstreams, "
        f"then run 'maestro validate {path}'."
    )


def _print_benchmark_summary(result: BenchmarkResult, wd: Path, notes: Console) -> None:
    notes.print(
        f"benchmark [bold]{escape(result.benchmark_id)}[/bold] | agent "
        f"{escape(result.agent_id)} | run {escape(result.run_id)}"
    )
    notes.print(
        f"score: [bold]{result.score}[/bold]"
        + (
            f" | components: {result.score_components}"
            if result.score_components
            else ""
        )
    )
    table = Table(title="Tasks")
    table.add_column("#")
    table.add_column("duration s")
    table.add_column("tokens")
    table.add_column("cost")
    table.add_column("error")
    for t in result.per_task:
        table.add_row(
            str(t.task_index),
            f"{t.duration_seconds:.1f}",
            str(t.tokens_used) if t.tokens_used is not None else "-",
            f"{t.cost_usd:.4f}" if t.cost_usd is not None else "-",
            escape(t.error) if t.error else "",
        )
    notes.print(table)
    notes.print(
        f"totals: tokens={result.total_tokens} cost={result.total_cost_usd} "
        f"duration={result.duration_seconds:.1f}s"
    )
    notes.print(
        f"arbiter report: {result.report_status}"
        + (f" ({escape(result.report_error)})" if result.report_error else "")
    )
    notes.print(f"logs: {escape(str(wd / 'logs'))}")


@app.command("benchmark")
def benchmark(
    benchmark_id: str = typer.Argument(..., help="ATP benchmark id to run"),
    agent: str = typer.Option(
        ...,
        "--agent",
        help="Harness: claude_code | codex_cli | aider | opencode. Model "
        "comes from MAESTRO_CLAUDE_MODEL / MAESTRO_CODEX_MODEL / "
        "MAESTRO_OPENCODE_MODEL or the catalog default (aider ignores model).",
    ),
    workdir: Path | None = typer.Option(
        None, "--workdir", help="Working dir (default: fresh temp dir; kept)"
    ),
    timeout: float = typer.Option(
        300.0, "--timeout", help="Per-task timeout in seconds (must be > 0)"
    ),
    run_id: str | None = typer.Option(
        None, "--run-id", help="Explicit run id (CI retry idempotency)"
    ),
    atp_url: str | None = typer.Option(
        None,
        "--atp-url",
        help="ATP base URL (default: $MAESTRO_ATP_BASE_URL, else "
        "http://localhost:8000)",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print BenchmarkResult JSON on stdout (notes → stderr)"
    ),
    no_report: bool = typer.Option(
        False, "--no-report", help="Skip arbiter reporting even if configured"
    ),
) -> None:
    """Run one ATP benchmark against one local agent harness (R-06b M5).

    Exit codes: 0 = run completed (per-task errors live in the score and
    the table, not the exit code); 1 = infrastructure failure; 2 = bad
    --timeout. With MAESTRO_ARBITER_BIN set, the result is reported to the
    arbiter fire-and-forget (a report failure never fails the run).
    """
    setup_logging("maestro")
    err = Console(stderr=True)
    # With --json, stdout must stay byte-for-byte JSON: ALL notes → stderr.
    notes = err if json_output else console

    if agent == "auto":
        err.print(
            "[red]--agent auto is a routing sentinel[/red] — pick a concrete "
            f"harness: {', '.join(_ALLOWED_BENCH_AGENTS)}"
        )
        raise typer.Exit(1)
    if agent == "announce":
        err.print(
            "[red]announce is a no-op echo harness[/red] — benchmarking it "
            "would record a fake success as routing signal"
        )
        raise typer.Exit(1)
    if agent not in _ALLOWED_BENCH_AGENTS:
        err.print(
            f"[red]unknown agent {escape(repr(agent))}[/red] — allowed: "
            f"{', '.join(_ALLOWED_BENCH_AGENTS)}"
        )
        raise typer.Exit(1)

    if timeout <= 0:
        err.print("[red]--timeout must be > 0[/red]")
        raise typer.Exit(2)

    spawner = _bench_spawner_for(agent)
    if not _agent_cli_available(agent):
        err.print(f"[red]agent CLI '{escape(agent)}' not found in PATH[/red]")
        raise typer.Exit(1)

    wd = workdir or Path(tempfile.mkdtemp(prefix="maestro-bench-"))
    wd.mkdir(parents=True, exist_ok=True)
    log_dir = wd / "logs"
    log_dir.mkdir(exist_ok=True)
    # Announce BEFORE the run: on a crash the partial logs must be findable.
    # Write directly to file to avoid Rich's line wrapping.
    notes.file.write(f"workdir: {wd}\n")
    notes.file.flush()

    url = atp_url or os.environ.get("MAESTRO_ATP_BASE_URL") or "http://localhost:8000"
    adapter = MaestroATPAdapter.from_env(platform_url=url)
    responder = SpawnerResponder(
        spawner, workdir=wd, log_dir=log_dir, timeout_seconds=timeout
    )
    arbiter_bin = os.environ.get("MAESTRO_ARBITER_BIN")

    try:
        result = asyncio.run(
            _benchmark_flow(
                adapter,
                responder,
                benchmark_id,
                run_id,
                arbiter_bin,
                no_report,
                notes,
            )
        )
    except Exception as exc:
        err.print(f"[red]benchmark failed[/red]: {escape(str(exc))}")
        err.print(
            "hint: check the ATP endpoint (--atp-url / $MAESTRO_ATP_BASE_URL) "
            "and token (ATP_TOKEN env or ~/.atp/config.json)"
        )
        raise typer.Exit(1) from exc

    _print_benchmark_summary(result, wd, notes)
    if json_output:
        # sys.stdout directly: byte-for-byte JSON, no Rich wrapping.
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")


# =================================================================
# Multi-Process Orchestration Commands
# =================================================================


def _get_workstream_status_style(
    status: WorkstreamStatus,
) -> str:
    """Return Rich style for workstream status."""
    styles = {
        WorkstreamStatus.DONE: "green",
        WorkstreamStatus.RUNNING: "yellow",
        WorkstreamStatus.DECOMPOSING: "yellow",
        WorkstreamStatus.MERGING: "yellow",
        WorkstreamStatus.PR_CREATED: "blue",
        WorkstreamStatus.FAILED: "red",
        WorkstreamStatus.NEEDS_REVIEW: "red",
        WorkstreamStatus.PENDING: "dim",
        WorkstreamStatus.READY: "cyan",
        WorkstreamStatus.ABANDONED: "dim red",
    }
    return styles.get(status, "white")


def _display_workstreams_table(workstreams: list, title: str = "Workstreams") -> None:
    """Display workstreams in a rich table."""
    if not workstreams:
        console.print("[dim]No workstreams found.[/dim]")
        return

    table = Table(
        title=title,
        show_header=True,
        header_style="bold",
    )
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Title", style="white")
    table.add_column("Status", no_wrap=True)
    table.add_column("Branch", style="dim")
    table.add_column("Progress", justify="center")
    table.add_column("PR", style="blue", max_width=30)

    for z in workstreams:
        style = _get_workstream_status_style(z.status)
        status_text = Text(z.status.value.upper(), style=style)
        pr_text = z.pr_url or ""
        if len(pr_text) > 30:
            pr_text = pr_text[-27:] + "..."

        table.add_row(
            z.id,
            z.title,
            status_text,
            z.branch,
            z.subtask_progress or "-",
            pr_text,
        )

    console.print(table)


def _resolve_orchestrator_paths(
    config: OrchestratorConfig,
    log_dir: Path | None,
) -> tuple[Path, Path, Path]:
    """Resolve paths from orchestrator config.

    Returns:
        Tuple of (repo_path, workspace_base, log_dir).
    """
    repo_path = Path(config.repo_path).expanduser()
    workspace_base = Path(config.workspace_base).expanduser()
    resolved_log_dir = log_dir if log_dir is not None else repo_path / "logs"
    return repo_path, workspace_base, resolved_log_dir


async def _run_orchestrator(
    config_path: Path,
    db_path: Path,
    resume: bool,
    log_dir: Path | None,
) -> None:
    """Run the multi-process orchestrator."""
    try:
        config = load_orchestrator_config(config_path)
    except ConfigError as e:
        err_console.print(f"[red]Configuration error:[/red] {e}")
        raise typer.Exit(1) from e

    report = validate_project(config)
    if report.issues:
        _print_validation_report(report)
    if not report.ok:
        err_console.print(
            "[red]Preflight validation failed.[/red] "
            f"Run 'maestro validate {config_path}' for details."
        )
        raise typer.Exit(1)

    # Ensure DB directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Create or connect to database
    db = await create_database(db_path)

    if resume:
        existing_workstreams = await db.get_all_workstreams()
        if existing_workstreams:
            console.print(
                f"[cyan]Resuming with {len(existing_workstreams)} existing workstreams[/cyan]"
            )
    else:
        existing_workstreams = await db.get_all_workstreams()
        if existing_workstreams:
            console.print(
                f"[yellow]Clearing {len(existing_workstreams)} existing workstreams "
                "state (use --resume to continue where you left off).[/yellow]"
            )
            for workstream in existing_workstreams:
                await db.delete_workstream(workstream.id)

    repo_path, workspace_base, log_dir = _resolve_orchestrator_paths(config, log_dir)

    # Activate the structured event log (events.jsonl) — without this the
    # module-global logger is None and every workstream lifecycle event is
    # dropped (the dispatcher's event_logger_getter returns None).
    create_event_logger(log_dir)

    lock_fd: int | None = None

    try:
        # Initialize components
        git_mgr = GitManager(
            repo_path=repo_path,
            base_branch=config.base_branch,
            branch_prefix=config.branch_prefix,
        )
        workspace_mgr = WorkspaceManager(
            git_manager=git_mgr,
            workspace_base=workspace_base,
        )
        decomposer = ProjectDecomposer(
            repo_path=repo_path,
            spec_gen_budget_usd=config.spec_runner.spec_gen_budget_usd,
        )
        pr_manager = PRManager(git_manager=git_mgr)

        # Setup notifications (mirrors mode-1's `run` wiring above)
        notifications = create_notification_manager(config.notifications)

        def _on_status_change(
            workstream_id: str,
            old_status: str,
            new_status: str,
        ) -> None:
            timestamp = datetime.now(UTC).strftime("%H:%M:%S")
            style = {
                "running": "yellow",
                "done": "green",
                "failed": "red",
                "needs_review": "red",
            }.get(new_status, "white")
            console.print(
                f"[dim]{timestamp}[/dim] "
                f"[cyan]{workstream_id}[/cyan]: "
                f"[{style}]{new_status.upper()}[/{style}]"
            )

        # Create orchestrator
        orchestrator = Orchestrator(
            db=db,
            workspace_mgr=workspace_mgr,
            decomposer=decomposer,
            pr_manager=pr_manager,
            config=config,
            log_dir=log_dir,
            notifier=notifications,
            on_status_change=_on_status_change,
        )

        # Acquire PID lock
        lock_fd = _acquire_pid_lock()

        console.print(
            Panel(
                f"[green]Orchestrator started[/green]\n"
                f"Project: {config.project}\n"
                f"Max concurrent: {config.max_concurrent}\n"
                f"Workspace: {workspace_base}\n"
                f"Auto PR: {config.auto_pr}",
                title="Maestro Orchestrator",
            )
        )

        # Run
        stats = await orchestrator.run()

        # Display final state
        workstreams = await db.get_all_workstreams()
        console.print()
        _display_workstreams_table(workstreams, "Final Status")

        console.print(
            Panel(
                f"Total: {stats.total_workstreams}\n"
                f"Completed: {stats.completed}\n"
                f"Failed: {stats.failed}\n"
                f"PRs created: {stats.prs_created}",
                title="Summary",
            )
        )

        if stats.failed > 0:
            raise typer.Exit(1)

    finally:
        await db.close()
        if lock_fd is not None:
            _release_pid_lock(lock_fd)


async def _show_workstreams_status(db_path: Path) -> None:
    """Show status of all workstreams."""
    if not db_path.exists():  # noqa: ASYNC240
        err_console.print(f"[red]Database not found:[/red] {db_path}")
        raise typer.Exit(1)

    db = Database(db_path)
    await db.connect()

    try:
        workstreams = await db.get_all_workstreams()
        _display_workstreams_table(workstreams, "Workstreams Status")
    finally:
        await db.close()


@app.command("orchestrate")
def orchestrate_command(
    config: Annotated[
        Path,
        typer.Argument(
            help="Path to project YAML configuration",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ],
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            "-d",
            help="Path to SQLite database file",
        ),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume",
            "-r",
            help="Resume from existing database state",
        ),
    ] = False,
    log_dir: Annotated[
        Path | None,
        typer.Option(
            "--log-dir",
            "-l",
            help="Directory for log files",
        ),
    ] = None,
) -> None:
    """Run multi-process orchestration from project config.

    Decomposes the project into independent workstreams,
    creates isolated worktrees, and runs spec-runner
    in each one.

    Examples:
        maestro orchestrate project.yaml
        maestro orchestrate project.yaml --resume
    """
    setup_logging("maestro")
    db_path = db or DEFAULT_DB_PATH

    try:
        asyncio.run(_run_orchestrator(config, db_path, resume, log_dir))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        raise typer.Exit(130) from None


@app.command("workstreams")
def workstreams_command(
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            "-d",
            help="Path to SQLite database file",
        ),
    ] = None,
) -> None:
    """Show status of all workstreams.

    Examples:
        maestro workstreams
        maestro workstreams --db /path/to/state.db
    """
    db_path = db or DEFAULT_DB_PATH
    asyncio.run(_show_workstreams_status(db_path))


async def _approve_workstream(
    db: "Database", workstream_id: str
) -> "ApprovalMarker | None":
    """Operator approval: record the gate approval + NEEDS_REVIEW -> READY
    in one transaction (gates v1.3, H-9).

    Parses the approval marker from the stored block reason; with a marker
    the (phase, sha) approval is recorded durably in gate_approvals — the
    single approval authority the gates consult. Without a marker this is
    a plain requeue and records nothing. Returns the recorded marker (or
    None) so the CLI can say exactly what was approved.
    """
    from maestro.gates import parse_approval_marker
    from maestro.models import WorkstreamStatus

    workstream = await db.get_workstream(workstream_id)
    if workstream is None:
        raise ValueError(f"workstream '{workstream_id}' not found")
    if workstream.status != WorkstreamStatus.NEEDS_REVIEW:
        raise ValueError(
            f"workstream '{workstream_id}' is {workstream.status}, "
            f"only NEEDS_REVIEW can be approved"
        )
    marker = parse_approval_marker(workstream.error_message)
    await db.approve_workstream_with_gate_record(
        workstream_id,
        marker.phase if marker else None,
        marker.sha if marker else None,
    )
    return marker


@app.command("workstream-approve")
def workstream_approve_command(
    workstream_id: Annotated[str, typer.Argument(help="Workstream ID to approve")],
    db: Annotated[
        Path | None,
        typer.Option("--db", "-d", help="Path to SQLite database file"),
    ] = None,
) -> None:
    """Approve a NEEDS_REVIEW workstream (gates re-queue) back to READY.

    Examples:
        maestro workstream-approve risk-model-docs-rule --db run/maestro.db
    """
    db_path = db or DEFAULT_DB_PATH

    async def _run() -> "ApprovalMarker | None":
        from maestro.database import Database

        database = Database(db_path)
        await database.connect()
        try:
            return await _approve_workstream(database, workstream_id)
        finally:
            await database.close()

    try:
        marker = asyncio.run(_run())
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if marker is not None:
        console.print(
            f"[green]Workstream '{workstream_id}' approved "
            f"(NEEDS_REVIEW -> READY); recorded approval "
            f"phase={marker.phase} sha={marker.sha[:12]}.[/green]"
        )
    else:
        console.print(
            f"[yellow]Workstream '{workstream_id}' re-queued "
            f"(NEEDS_REVIEW -> READY) — no gate marker in error_message, "
            f"NO approval recorded.[/yellow]"
        )
    console.print(
        f"Resume with: maestro orchestrate <project.yaml> --db {db_path} --resume"
    )


@app.command("check-scope")
def check_scope_command(
    workstream_id: Annotated[str, typer.Argument(help="Workstream ID to check")],
    base: Annotated[
        str, typer.Option("--base", "-b", help="Base branch to diff against")
    ],
    db: Annotated[
        Path | None, typer.Option("--db", "-d", help="Path to SQLite database file")
    ] = None,
) -> None:
    """Raw scope-containment check for a workstream's worktree.

    Exit 0 = clean or empty scope; 1 = escapes found; 2 = invalid input.
    An existing approval prints an informational note but never changes the
    exit code (this reports the containment fact, not the gate's policy).

    Examples:
        maestro check-scope my-ws --base main --db run/maestro.db
    """
    from maestro.changed_paths import changed_paths_since
    from maestro.database import Database, WorkstreamNotFoundError
    from maestro.scope_gate import find_escapes, normalize

    db_path = db or DEFAULT_DB_PATH

    async def _run() -> int:
        database = Database(db_path)
        await database.connect()
        try:
            try:
                ws = await database.get_workstream(workstream_id)
            except WorkstreamNotFoundError:
                console.print(f"[red]workstream '{workstream_id}' not found[/red]")
                return 2
            if not ws.workspace_path:
                console.print(
                    f"[red]workstream '{workstream_id}' has no worktree[/red]"
                )
                return 2
            worktree = Path(ws.workspace_path)
            # Path.exists() is fast sync I/O, acceptable in async context
            if not worktree.exists():  # noqa: ASYNC240
                console.print(f"[red]worktree missing: {worktree}[/red]")
                return 2
            if not ws.scope:
                console.print("[dim]empty scope — nothing to enforce.[/dim]")
                return 0
            try:
                paths = await changed_paths_since(base, "HEAD", worktree)
            except RuntimeError as exc:
                console.print(f"[red]git error: {exc}[/red]")
                return 2
            escapes = find_escapes(normalize(paths), normalize(ws.scope))
            if not escapes:
                console.print("[green]in scope — no escapes.[/green]")
                return 0
            console.print("[red]scope escape:[/red]")
            for p in escapes:
                console.print(f"  {p}")
            # Raw check: an existing ex_post approval is informational only and
            # does NOT change the exit code (spec §7). Any recorded ex_post
            # approval for this workstream is enough to print the note.
            approvals = await database.list_gate_approvals(workstream_id)
            for phase, sha in approvals:
                if phase == "ex_post":
                    console.print(f"[dim]note: approved (ex_post, {sha[:12]})[/dim]")
                    break
            return 1
        finally:
            await database.close()

    raise typer.Exit(asyncio.run(_run()))


@app.command("workspaces")
def workspaces_command(
    workspace_base: Annotated[
        Path | None,
        typer.Option(
            "--path",
            "-p",
            help="Base directory for workspaces",
        ),
    ] = None,
) -> None:
    """List active workspaces.

    Examples:
        maestro workspaces
        maestro workspaces --path /tmp/maestro-ws
    """
    base = workspace_base or Path("/tmp/maestro-ws")

    if not base.exists():
        console.print("[dim]No workspaces found.[/dim]")
        return

    dirs = [
        p for p in sorted(base.iterdir()) if p.is_dir() and not p.name.startswith(".")
    ]

    if not dirs:
        console.print("[dim]No workspaces found.[/dim]")
        return

    table = Table(
        title="Active Workspaces",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Workstream", style="cyan")
    table.add_column("Path", style="dim")

    for d in dirs:
        table.add_row(d.name, str(d))

    console.print(table)


@app.command("merge-logs")
def merge_logs_cmd(
    target: Annotated[
        str,
        typer.Argument(help="Pipeline dir or pipeline_id"),
    ],
) -> None:
    """Time-sort per-pid JSONL under a pipeline directory into merged.jsonl."""
    raise SystemExit(_merge_logs.main([target]))


@app.command("costs")
def costs_command(
    db: Annotated[
        Path | None, typer.Option("--db", "-d", help="Path to SQLite database file")
    ] = None,
) -> None:
    """Database-wide cost summary (read-only) over recorded task costs.

    NOTE: this aggregates the whole database, which may span several runs
    (one DB survives --resume); it is a database-wide summary, not a run total.
    Costs of unpriced harnesses with no self-reported cost are shown as
    UNKNOWN, never as $0.

    Examples:
        maestro costs --db run/maestro.db
    """
    from maestro.cost_tracker import summarize_costs
    from maestro.database import DatabaseError, read_all_costs_readonly

    db_path = db or DEFAULT_DB_PATH

    async def _run() -> int:
        try:
            costs = await read_all_costs_readonly(db_path)
        except DatabaseError as exc:
            err_console.print(f"[red]{exc}[/red]")
            return 2
        if not costs:
            console.print("[dim]No cost records.[/dim]")
            return 0
        _render_cost_report(summarize_costs(costs))
        return 0

    raise typer.Exit(asyncio.run(_run()))


def _render_cost_report(report: "CostReport") -> None:
    """Render a CostReport as Rich tables: TOTAL, by harness, by task."""
    from maestro.cost_tracker import CostGroup

    def _row(g: CostGroup) -> tuple[str, ...]:
        return (
            g.label,
            f"${g.known_cost_usd:.4f}",
            f"{g.input_tokens}/{g.output_tokens}",
            str(g.tasks),
            str(g.attempts),
            str(g.unknown_attempts),
            str(g.unknown_tasks),
        )

    def _table(title: str, first_col: str, groups: list[CostGroup]) -> Table:
        t = Table(title=title, show_header=True, header_style="bold")
        t.add_column(first_col)
        for col in (
            "Known $",
            "Tokens in/out",
            "Tasks",
            "Attempts",
            "Unknown attempts",
            "Unknown tasks",
        ):
            t.add_column(col, justify="right")
        for g in groups:
            t.add_row(*_row(g))
        return t

    console.print(_table("Cost — database-wide TOTAL", "Scope", [report.total]))
    console.print(_table("By harness", "Harness", report.by_harness))
    console.print(_table("By task", "Task", report.by_task))


@app.callback()
def callback() -> None:
    """Maestro - AI Agent Orchestrator.

    Coordinates multiple AI coding agents working on different
    parts of the same project, managing task dependencies and
    execution order.
    """


def main() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
