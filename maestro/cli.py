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
import signal
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
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
from maestro.config import load_orchestrator_config
from maestro.coordination.routing import make_routing_strategy
from maestro.dag import DAG
from maestro.decomposer import ProjectDecomposer
from maestro.git import GitManager
from maestro.models import ArbiterMode, OrchestratorConfig, TaskStatus, ZadachaStatus
from maestro.orchestrator import Orchestrator
from maestro.pr_manager import PRManager
from maestro.workspace import WorkspaceManager


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
    arbiter_cfg = config.arbiter
    routing = await make_routing_strategy(arbiter_cfg)
    arbiter_mode = arbiter_cfg.mode if arbiter_cfg is not None else ArbiterMode.ADVISORY

    try:
        # Check if resuming
        if resume:
            existing_tasks = await db.get_all_tasks()
            if existing_tasks:
                console.print(
                    f"[cyan]Resuming with {len(existing_tasks)} existing tasks[/cyan]"
                )

                # Perform state recovery for orphaned tasks
                recovery = StateRecovery(db)
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

        # Setup spawners
        spawners: dict[str, ClaudeCodeSpawner] = {
            "claude_code": ClaudeCodeSpawner(),
        }

        # Determine log directory
        # Note: Path operations are fast sync I/O, acceptable in async context
        workdir = Path(config.repo).expanduser()  # noqa: ASYNC240
        if log_dir is None:
            log_dir = workdir / "logs"

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
            spawners=spawners,  # type: ignore[arg-type]
            max_concurrent=config.max_concurrent,
            workdir=workdir,
            log_dir=log_dir,
            notification_manager=notifications,
            on_status_change=_on_status_change,
            auto_commit=(config.git.auto_commit if config.git else False),
            routing=routing,
            arbiter_mode=arbiter_mode,
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


# =================================================================
# Multi-Process Orchestration Commands
# =================================================================


def _get_zadacha_status_style(
    status: ZadachaStatus,
) -> str:
    """Return Rich style for zadacha status."""
    styles = {
        ZadachaStatus.DONE: "green",
        ZadachaStatus.RUNNING: "yellow",
        ZadachaStatus.DECOMPOSING: "yellow",
        ZadachaStatus.MERGING: "yellow",
        ZadachaStatus.PR_CREATED: "blue",
        ZadachaStatus.FAILED: "red",
        ZadachaStatus.NEEDS_REVIEW: "red",
        ZadachaStatus.PENDING: "dim",
        ZadachaStatus.READY: "cyan",
        ZadachaStatus.ABANDONED: "dim red",
    }
    return styles.get(status, "white")


def _display_zadachi_table(zadachi: list, title: str = "Zadachi") -> None:
    """Display zadachi in a rich table."""
    if not zadachi:
        console.print("[dim]No zadachi found.[/dim]")
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

    for z in zadachi:
        style = _get_zadacha_status_style(z.status)
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

    # Ensure DB directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Create or connect to database
    db = await create_database(db_path)

    if resume:
        existing_zadachi = await db.get_all_zadachi()
        if existing_zadachi:
            console.print(
                f"[cyan]Resuming with {len(existing_zadachi)} existing zadachi[/cyan]"
            )
    else:
        existing_zadachi = await db.get_all_zadachi()
        if existing_zadachi:
            console.print(
                f"[yellow]Clearing {len(existing_zadachi)} existing zadachi "
                "state (use --resume to continue where you left off).[/yellow]"
            )
            for zadacha in existing_zadachi:
                await db.delete_zadacha(zadacha.id)

    repo_path, workspace_base, log_dir = _resolve_orchestrator_paths(config, log_dir)
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
        )
        pr_manager = PRManager(git_manager=git_mgr)

        # Create orchestrator
        orchestrator = Orchestrator(
            db=db,
            workspace_mgr=workspace_mgr,
            decomposer=decomposer,
            pr_manager=pr_manager,
            config=config,
            log_dir=log_dir,
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
        zadachi = await db.get_all_zadachi()
        console.print()
        _display_zadachi_table(zadachi, "Final Status")

        console.print(
            Panel(
                f"Total: {stats.total_zadachi}\n"
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


async def _show_zadachi_status(db_path: Path) -> None:
    """Show status of all zadachi."""
    if not db_path.exists():  # noqa: ASYNC240
        err_console.print(f"[red]Database not found:[/red] {db_path}")
        raise typer.Exit(1)

    db = Database(db_path)
    await db.connect()

    try:
        zadachi = await db.get_all_zadachi()
        _display_zadachi_table(zadachi, "Zadachi Status")
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

    Decomposes the project into independent zadachi,
    creates isolated worktrees, and runs spec-runner
    in each one.

    Examples:
        maestro orchestrate project.yaml
        maestro orchestrate project.yaml --resume
    """
    db_path = db or DEFAULT_DB_PATH

    try:
        asyncio.run(_run_orchestrator(config, db_path, resume, log_dir))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        raise typer.Exit(130) from None


@app.command("zadachi")
def zadachi_command(
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            "-d",
            help="Path to SQLite database file",
        ),
    ] = None,
) -> None:
    """Show status of all zadachi.

    Examples:
        maestro zadachi
        maestro zadachi --db /path/to/state.db
    """
    db_path = db or DEFAULT_DB_PATH
    asyncio.run(_show_zadachi_status(db_path))


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
    table.add_column("Zadacha", style="cyan")
    table.add_column("Path", style="dim")

    for d in dirs:
        table.add_row(d.name, str(d))

    console.print(table)


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
