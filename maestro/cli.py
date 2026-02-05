"""CLI interface for Maestro orchestrator.

This module provides a command-line interface using Typer for:
- Running tasks from YAML configuration files
- Checking task status
- Retrying failed tasks
- Stopping the scheduler
- Resuming interrupted runs
"""

import asyncio
import os
import signal
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
from maestro.dag import DAG
from maestro.models import TaskStatus


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


def _write_pid_file(pid: int) -> None:
    """Write PID to file for stop command."""
    _ensure_db_dir()
    PID_FILE.write_text(str(pid))


def _read_pid_file() -> int | None:
    """Read PID from file, return None if not found."""
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _remove_pid_file() -> None:
    """Remove PID file."""
    if PID_FILE.exists():
        PID_FILE.unlink()


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

    # Create or connect to database
    db = await create_database(db_path)

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
                    stats = await recovery.recover()
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

        # Create scheduler
        scheduler = await create_scheduler_from_config(
            db=db,
            tasks=config.tasks,
            spawners=spawners,  # type: ignore[arg-type]
            max_concurrent=config.max_concurrent,
            workdir=workdir,
            log_dir=log_dir,
            notification_manager=notifications,
        )

        # Display initial state
        all_tasks = await db.get_all_tasks()
        _display_tasks_table(all_tasks, "Starting Tasks")

        # Write PID for stop command
        _write_pid_file(os.getpid())

        console.print(
            Panel(
                f"[green]Scheduler started[/green]\n"
                f"Project: {config.project}\n"
                f"Max concurrent: {config.max_concurrent}\n"
                f"Tasks: {len(config.tasks)}",
                title="Maestro",
            )
        )

        # Run scheduler
        await scheduler.run()

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
        await db.close()
        _remove_pid_file()


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


def _stop_scheduler() -> None:
    """Stop the running scheduler by sending SIGTERM."""
    pid = _read_pid_file()
    if pid is None:
        err_console.print("[yellow]No running scheduler found[/yellow]")
        raise typer.Exit(0)

    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Sent stop signal to scheduler (PID: {pid})[/green]")
        _remove_pid_file()
    except ProcessLookupError:
        err_console.print(
            f"[yellow]Process {pid} not found, removing stale PID file[/yellow]"
        )
        _remove_pid_file()
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
) -> None:
    """Run tasks from a YAML configuration file.

    The scheduler will execute tasks respecting their dependencies,
    up to the configured concurrency limit.

    Examples:
        maestro run tasks.yaml
        maestro run tasks.yaml --resume
        maestro run tasks.yaml --db /path/to/state.db
    """
    db_path = db or DEFAULT_DB_PATH

    # Check if scheduler is already running
    pid = _read_pid_file()
    if pid is not None:
        try:
            os.kill(pid, 0)  # Check if process exists
            err_console.print(f"[red]Scheduler already running (PID: {pid})[/red]")
            err_console.print("Use 'maestro stop' to stop it first.")
            raise typer.Exit(1)
        except ProcessLookupError:
            # Process doesn't exist, remove stale PID file
            _remove_pid_file()

    try:
        asyncio.run(_run_scheduler(config, db_path, resume, log_dir))
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
