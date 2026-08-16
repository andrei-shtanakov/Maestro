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
from typing import TYPE_CHECKING, Annotated, Any, cast

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
    NotificationManager,
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
from maestro.database import verifier_requeue_block_reason
from maestro.decomposer import ProjectDecomposer, resolve_spec_gen_settings
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
from maestro.repo_identity import (
    IdentityError,
    identity_from_checkout,
    parse_remote_url,
)
from maestro.review_pr import PrRef, parse_pr_url
from maestro.review_runner import (
    EXIT_ALREADY_RUNNING,
    ReviewInvocation,
    check_spec_runner_version,
    invoke_spec_runner,
    run_review,
)
from maestro.review_workspace import (
    ReviewPaths,
    fetch_pr_meta,
    gc_pr,
    materialize,
    recover_push,
)
from maestro.run_bootstrap import RunIsLive, bootstrap_run
from maestro.run_registry import (
    TERMINAL_RUN_STATUSES,
    AmbiguousRun,
    NoResumableRun,
    home_usage,
    resolve_run_for_command,
    resolve_runs,
)
from maestro.scaffold import ScaffoldError, generate_project_yaml
from maestro.service.locks import Stage  # noqa: TC001 — runtime cast target
from maestro.service.tick import TickResult, run_argv, run_tick
from maestro.service.units import (
    CREDENTIAL_ENV_BY_HARNESS,
    PreflightError,
    UnitSpec,
    ensure_env_file,
    preflight_environment,
    probe_environment,
    render_launchd,
    render_systemd,
    unit_name,
)
from maestro.spawners import (
    AiderSpawner,
    AnnounceSpawner,
    CodexSpawner,
    OpencodeSpawner,
)
from maestro.spawners.base import AgentSpawner
from maestro.state_paths import maestro_home
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
service_app = typer.Typer(
    help="Scheduled autonomous runs (launchd/systemd wrapper)",
    no_args_is_help=True,
)

app = typer.Typer(
    name="maestro",
    help="AI Agent Orchestrator for coordinating multiple coding agents.",
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(models_app, name="models")
app.add_typer(service_app, name="service")


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
    colors = {"error": "red", "warning": "yellow", "info": "cyan"}
    for issue in report.issues:
        color = colors[issue.severity]
        location = (
            f" {', '.join(issue.workstream_ids)}:" if issue.workstream_ids else ""
        )
        console.print(
            f"[{color}]{issue.severity}[/{color}] "
            f"{escape(f'[{issue.code}]')}{escape(location)} "
            f"{escape(issue.message)}"
        )
    n_err, n_warn, n_info = (
        len(report.errors),
        len(report.warnings),
        len(report.infos),
    )
    style = "red" if n_err else ("yellow" if n_warn else "green")
    summary = f"{n_err} errors, {n_warn} warnings"
    if n_info:
        summary += f", {n_info} info"
    console.print(f"[{style}]{summary}[/{style}]")


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
    notifications: NotificationManager | None = None

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
                recovery = StateRecovery(
                    db, execution=config.execution, verifier=config.verifier
                )
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
                            f"VERIFYING → NEEDS_REVIEW: "
                            f"{stats.verifying_recovered}\n"
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
            verifier=config.verifier,
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
        if notifications is not None:
            # Bounded drain of queued notification deliveries (webhook).
            await notifications.aclose()
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

        # Requeue handle-fence (Task 11, spec §8): the shared
        # `verifier_requeue_block_reason` fence (also used by the
        # dashboard's `POST /api/tasks/{task_id}/retry`) fails this closed
        # while a verifier-originated NEEDS_REVIEW's
        # `execution_phase='verification'` handle is still open (not yet
        # reconciled to `cleaned` by recovery/GC) — the judge subprocess it
        # represents may still be alive.
        block_reason = await verifier_requeue_block_reason(db, task)
        if block_reason is not None:
            err_console.print(f"[red]Cannot retry task:[/red] {block_reason}.")
            err_console.print(
                "Wait for recovery/GC to close it (or investigate the "
                "judge process) before retrying."
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


def _quarantine_cell(quarantined_at: datetime | None) -> Text:
    """Flag plus age for the quarantine column (#166).

    The age matters as much as the flag: a quarantine raised a minute ago is an
    incident in progress, one standing for two days is a forgotten blocker, and
    an operator scanning the table should not have to open the row to tell them
    apart.
    """
    if quarantined_at is None:
        return Text("-", style="dim")
    delta = datetime.now(UTC) - quarantined_at
    hours = delta.total_seconds() / 3600
    age = f"{int(delta.total_seconds() // 60)}m" if hours < 1 else f"{hours:.0f}h"
    if hours >= 24:
        age = f"{int(hours // 24)}d"
    return Text(f"YES ({age})", style="red")


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
    # Operator reworks (#124) are unbounded but must not be invisible:
    # the column appears once any workstream has one, yellow at threshold.
    show_reworks = any(getattr(z, "operator_rework_count", 0) > 0 for z in workstreams)
    # Quarantine (#166): a workstream whose delivery an operator has forbidden
    # must never look ordinary. The column appears only when one exists, and
    # shows the AGE — an incident an operator forgot about for two days reads
    # differently from one raised a minute ago.
    show_quarantine = any(
        getattr(z, "quarantined_at", None) is not None for z in workstreams
    )

    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Title", style="white")
    table.add_column("Status", no_wrap=True)
    table.add_column("Branch", style="dim")
    table.add_column("Progress", justify="center")
    if show_reworks:
        table.add_column("Reworks", justify="center")
    if show_quarantine:
        table.add_column("Quarantined", justify="center")
    table.add_column("PR", style="blue", max_width=30)

    for z in workstreams:
        style = _get_workstream_status_style(z.status)
        status_text = Text(z.status.value.upper(), style=style)
        pr_text = z.pr_url or ""
        if len(pr_text) > 30:
            pr_text = pr_text[-27:] + "..."

        row = [
            z.id,
            z.title,
            status_text,
            z.branch,
            z.subtask_progress or "-",
        ]
        if show_reworks:
            count = getattr(z, "operator_rework_count", 0)
            rework_style = "yellow" if count >= _REWORK_WARN_THRESHOLD else "dim"
            row.append(Text(str(count) if count else "-", style=rework_style))
        if show_quarantine:
            row.append(_quarantine_cell(getattr(z, "quarantined_at", None)))
        row.append(pr_text)
        table.add_row(*row)

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
    db_path: Path | None,
    resume: bool,
    run: str | None,
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

    # Identity and the run must be resolved — and ORCHESTRA_PIPELINE_ID
    # exported — before logging initializes (obs.py falls back to a fresh
    # ULID otherwise). `--db` is an explicit override that skips the
    # resolver entirely.
    if db_path is not None:
        resolved_db_path = db_path
    else:
        try:
            bootstrap = await bootstrap_run(config, resume=resume, run_id_override=run)
        except IdentityError as e:
            err_console.print(f"[red]Cannot resolve repository identity:[/red] {e}")
            raise typer.Exit(1) from e
        except RunIsLive as e:
            err_console.print(f"[red]Refusing to start a second run:[/red] {e}")
            raise typer.Exit(1) from e
        except NoResumableRun as e:
            err_console.print(f"[red]No resumable run:[/red] {e}")
            raise typer.Exit(1) from e
        except AmbiguousRun as e:
            err_console.print(f"[red]Several runs could be resumed:[/red] {e}")
            raise typer.Exit(1) from e
        resolved_db_path = bootstrap.db_path

        # A fresh run gets its own directory; the previous run is not
        # touched. Say what is being left behind so the operator sees it
        # here rather than discovering it later.
        if bootstrap.fresh:
            leftover = [
                r
                for r in await resolve_runs(bootstrap.key)
                if r.run_id != bootstrap.run_id
                and r.status not in TERMINAL_RUN_STATUSES
            ]
            if leftover:
                ids = ", ".join(f"{r.run_id} ({r.status})" for r in leftover)
                console.print(
                    f"[yellow]Starting a fresh run; leaving {len(leftover)} "
                    f"non-terminal run(s) behind: {ids}[/yellow]"
                )

    setup_logging("maestro")

    # Ensure DB directory exists
    resolved_db_path.parent.mkdir(parents=True, exist_ok=True)

    # Create or connect to database
    db = await create_database(resolved_db_path)

    if resume:
        existing_workstreams = await db.get_all_workstreams()
        if existing_workstreams:
            console.print(
                f"[cyan]Resuming with {len(existing_workstreams)} existing workstreams[/cyan]"
            )
    elif db_path is not None:
        # `--db` bypasses the resolver and names one file directly (Task
        # 9's contract) — there is no per-run directory here for a
        # previous run to survive in, so a plain (`--db`, no `--resume`)
        # start must still clear, exactly as it always has. Without this,
        # `orchestrate --db x.db` would silently continue an existing DAG
        # through this door — the same destructive-by-design-turned-
        # auto-resume this task exists to prevent, just reached
        # differently than the resolver path below.
        #
        # The resolver path (db_path is None) never reaches here with
        # state to clear: a fresh run gets its own empty directory by
        # construction, and a run reached via `--resume` or `--run <id>`
        # must never be cleared — that would delete exactly the state the
        # operator asked to act on.
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
    notifications: NotificationManager | None = None

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
        spec_gen_budget, spec_gen_timeout = resolve_spec_gen_settings(
            config.domain, config.spec_runner.spec_gen_budget_usd
        )
        decomposer = ProjectDecomposer(
            repo_path=repo_path,
            spec_gen_budget_usd=spec_gen_budget,
            spec_gen_timeout_minutes=spec_gen_timeout,
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
        if notifications is not None:
            # Bounded drain of queued notification deliveries (webhook).
            await notifications.aclose()
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
    run: Annotated[
        str | None,
        typer.Option(
            "--run",
            help="Act on this run id instead of the resolver's choice.",
        ),
    ] = None,
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
    try:
        asyncio.run(_run_orchestrator(config, db, resume, run, log_dir))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        raise typer.Exit(130) from None


#: `--run` for the workstream command family; the text is one string so the
#: eight commands cannot drift apart.
_RUN_OPTION_HELP = "Act on this run id instead of the resolver's choice."


def _workstream_db_path(db: Path | None, run: str | None) -> Path:
    """The database a workstream command must act on (spec §C.3).

    A workstream id is unique per database, not per repository: once state is
    per-run, the same id exists in several databases, so the command resolves
    `(repository, run)` before it opens anything. `--db` is the escape hatch
    of spec §E — given, it selects that database directly and the resolver is
    not consulted at all, identity included.

    Every refusal is an operator-facing message plus exit 1, never a
    traceback (the pattern of `_run_orchestrator` and `_service_run`).

    **What was resolved is always said out loud.** Resolving silently relocates
    the accident §C.3 exists to remove rather than removing it: an operator
    whose shell sits in one checkout while the run they mean belongs to another
    repository gets the wrong database, and — workstream ids in this ecosystem
    being short and repeated — quite possibly a workstream of the same name in
    it, with a success message on top.
    """
    if db is not None:
        if run is not None:
            # `--db` selects a database directly and the resolver is not
            # consulted at all (spec §E), so `--run` has nothing to steer.
            # Acting on `--db` while quietly dropping `--run` would be the
            # same wrong-database success this function exists to prevent.
            err_console.print(
                "[red]--db and --run cannot be combined:[/red] --db names a "
                "database directly (spec §E), so the resolver --run steers is "
                "never consulted. Drop one."
            )
            raise typer.Exit(1)
        console.print(f"[dim]acting on database {db}[/dim]", soft_wrap=True)
        return db

    cwd = Path.cwd()
    try:
        key = identity_from_checkout(cwd)
    except IdentityError as e:
        err_console.print(
            f"[red]Cannot resolve repository identity:[/red] {e}\n"
            "Run this from the repository's checkout, or pass --db <path>."
        )
        raise typer.Exit(1) from e

    repo = "/".join(key.as_path_parts())
    # The key and where it came from are named on every refusal. `bootstrap_run`
    # derives identity from `config.repo_url` while these eight derive it from
    # the checkout, and the two diverge for a fork or for
    # `maestro orchestrate ../b/project.yaml`. Advising `orchestrate` here would
    # mint a *second* project tree under the wrong key; showing the key makes
    # the mismatch visible while it is still one command's worth of damage.
    # (Unifying the identity source is Task 13's, which owns the remaining
    # `DEFAULT_DB_PATH` call sites.)
    origin = f"Resolved {repo} from the checkout at {cwd}."
    try:
        info = asyncio.run(resolve_run_for_command(key, run_id=run))
    except NoResumableRun as e:
        err_console.print(f"[red]No resumable run:[/red] {e}", soft_wrap=True)
        err_console.print(origin, soft_wrap=True)
        err_console.print(
            "If that is not the repository you meant, run this from that "
            "repository's checkout, or pass --db <path> to name the database "
            "directly."
        )
        raise typer.Exit(1) from e
    except AmbiguousRun as e:
        err_console.print(f"[red]Several runs could be resumed:[/red] {e}")
        err_console.print(origin, soft_wrap=True)
        err_console.print("Pass --run <run-id>, or --db <path> to pick one directly.")
        raise typer.Exit(1) from e
    console.print(f"[dim]acting on {repo}, run {info.run_id}[/dim]", soft_wrap=True)
    return info.db_path


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
    run: Annotated[
        str | None,
        typer.Option("--run", help=_RUN_OPTION_HELP),
    ] = None,
) -> None:
    """Show status of all workstreams.

    Examples:
        maestro workstreams
        maestro workstreams --db /path/to/state.db
    """
    db_path = _workstream_db_path(db, run)
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
    run: Annotated[
        str | None,
        typer.Option("--run", help=_RUN_OPTION_HELP),
    ] = None,
) -> None:
    """Approve a NEEDS_REVIEW workstream (gates re-queue) back to READY.

    Examples:
        maestro workstream-approve risk-model-docs-rule --db run/maestro.db
    """
    db_path = _workstream_db_path(db, run)

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


@app.command("postmortem")
def postmortem_command(
    config_file: Annotated[Path, typer.Argument(help="Path to project.yaml")],
    gc: Annotated[
        bool,
        typer.Option("--gc", help="Apply the retention policy and prune old archives"),
    ] = False,
    db: Annotated[
        Path | None,
        typer.Option("--db", "-d", help="Path to SQLite database file"),
    ] = None,
) -> None:
    """Operator-driven post-mortem archive retention (#164, spec §6.3).

    Applies the SAME policy the orchestrator applies after each successful
    capture (`postmortem.keep_per_workstream`), for the case where archives
    accumulated before the policy was tightened or a prune failed at the time.
    Never touches the newest archive of any workstream.

    Examples:
        maestro postmortem project.yaml --gc
        maestro postmortem project.yaml --gc --db run/maestro.db
    """
    if not gc:
        console.print(
            "[yellow]Nothing to do: pass --gc to apply the retention policy.[/yellow]"
        )
        raise typer.Exit(1)
    db_path = db or DEFAULT_DB_PATH

    async def _run() -> tuple[int, int]:
        from maestro.config import load_orchestrator_config
        from maestro.database import Database
        from maestro.postmortem import prune_archives

        config = load_orchestrator_config(config_file)
        keep = config.postmortem.keep_per_workstream
        root = Path(db_path).parent / "postmortem"
        database = Database(db_path)
        await database.connect()
        try:
            workstreams = await database.get_all_workstreams()
            pruned = 0
            for workstream in workstreams:
                removed = prune_archives(root, workstream.id, keep=keep)
                for path in removed:
                    execution_id = (
                        path.name.split("-", 1)[1] if "-" in path.name else path.name
                    )
                    await database.delete_postmortem_archive(
                        workstream.id, execution_id
                    )
                pruned += len(removed)
            return pruned, keep
        finally:
            await database.close()

    try:
        pruned, keep = asyncio.run(_run())
    except (OSError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(
        f"[green]Pruned {pruned} post-mortem archive(s); "
        f"keeping the newest {keep} per workstream.[/green]"
    )


@app.command("workstream-quarantine")
def workstream_quarantine_command(
    workstream_id: Annotated[str, typer.Argument(help="Workstream ID to quarantine")],
    reason: Annotated[
        str, typer.Option("--reason", help="Why this result must not progress")
    ],
    db: Annotated[
        Path | None,
        typer.Option("--db", "-d", help="Path to SQLite database file"),
    ] = None,
    run: Annotated[
        str | None,
        typer.Option("--run", help=_RUN_OPTION_HELP),
    ] = None,
) -> None:
    """Forbid a workstream's result from progressing (#166).

    Stops dispatch and withholds delivery: a finished quarantined workstream
    goes to NEEDS_REVIEW instead of merging. **A running execution is NOT
    terminated** — it finishes normally, because killing work to isolate it is
    the loss this command exists to avoid. Independent workstreams are
    untouched.

    Refuses once delivery has started (MERGING/PR_CREATED/DONE): after that the
    remedy is a revert, and accepting the quarantine would claim to have
    prevented something that already happened.

    Idempotent — a second call keeps the original timestamp, which reads as the
    age of the incident.

    Examples:
        maestro workstream-quarantine w-adapters --reason "false DONE, 1/9"
    """
    db_path = _workstream_db_path(db, run)

    async def _run() -> str:
        import getpass

        from maestro.database import Database

        database = Database(db_path)
        await database.connect()
        try:
            # `get_workstream` RAISES WorkstreamNotFoundError for an unknown id;
            # it never returns None. Checking for None would be dead code and
            # the real exception would escape the ValueError handler below.
            before = await database.get_workstream(workstream_id)
            already = before.quarantined_at is not None
            await database.quarantine_workstream(
                workstream_id, reason=reason, actor=getpass.getuser()
            )
            return "already" if already else "new"
        finally:
            await database.close()

    from maestro.database import WorkstreamNotFoundError

    try:
        outcome = asyncio.run(_run())
    except (ValueError, WorkstreamNotFoundError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if outcome == "already":
        console.print(
            f"[yellow]Workstream '{workstream_id}' was already quarantined; "
            f"the original reason and timestamp are kept.[/yellow]"
        )
    else:
        console.print(
            f"[green]Workstream '{workstream_id}' quarantined: no new dispatch, "
            f"delivery withheld. A running execution keeps going and will park "
            f"in NEEDS_REVIEW when it finishes.[/green]"
        )
    console.print(
        f"Lift with: maestro workstream-unquarantine {workstream_id} "
        f'--reason "<why it is safe now>"'
    )


@app.command("workstream-unquarantine")
def workstream_unquarantine_command(
    workstream_id: Annotated[str, typer.Argument(help="Workstream ID to release")],
    reason: Annotated[
        str, typer.Option("--reason", help="Why it is safe to progress again")
    ],
    db: Annotated[
        Path | None,
        typer.Option("--db", "-d", help="Path to SQLite database file"),
    ] = None,
    run: Annotated[
        str | None,
        typer.Option("--run", help=_RUN_OPTION_HELP),
    ] = None,
) -> None:
    """Lift a quarantine — an audited action, and nothing more (#166).

    Clears the quarantine so dispatch and delivery are allowed again. It does
    **not** change the workstream's status, does **not** start anything, and is
    **not** an approval: whatever gate or review the workstream still owes, it
    still owes. The orchestrator picks it up on its own next loop if its status
    makes it eligible.

    Examples:
        maestro workstream-unquarantine w-adapters --reason "false DONE fixed"
    """
    db_path = _workstream_db_path(db, run)

    async def _run() -> str:
        import getpass

        from maestro.database import Database

        database = Database(db_path)
        await database.connect()
        try:
            # See the note in workstream-quarantine: an unknown id raises.
            before = await database.get_workstream(workstream_id)
            if before.quarantined_at is None:
                return "not_quarantined"
            await database.unquarantine_workstream(
                workstream_id, reason=reason, actor=getpass.getuser()
            )
            return before.status.value
        finally:
            await database.close()

    from maestro.database import WorkstreamNotFoundError

    try:
        outcome = asyncio.run(_run())
    except (ValueError, WorkstreamNotFoundError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if outcome == "not_quarantined":
        console.print(
            f"[yellow]Workstream '{workstream_id}' is not quarantined; "
            f"nothing to lift.[/yellow]"
        )
        return
    console.print(
        f"[green]Quarantine lifted for '{workstream_id}' (status unchanged: "
        f"{outcome.upper()}). This is not an approval — any gate it owed, it "
        f"still owes.[/green]"
    )


@app.command("workstream-continue")
def workstream_continue_command(
    workstream_id: Annotated[str, typer.Argument(help="Workstream ID to continue")],
    db: Annotated[
        Path | None,
        typer.Option("--db", "-d", help="Path to SQLite database file"),
    ] = None,
    run: Annotated[
        str | None,
        typer.Option("--run", help=_RUN_OPTION_HELP),
    ] = None,
) -> None:
    """Queue a continuation over the workstream's EXISTING tasks.md (#166).

    Re-runs spec-runner against the plan that is already there: no
    regeneration, no author respawn, no new sha. Use it when a run was
    interrupted — a stop, a kill, a crash — and the remaining tasks should
    simply be finished.

    This only QUEUES the continuation; the orchestrator dispatches it on its
    next loop. The preconditions are checked here so a refusal is fast and
    readable, and checked AGAIN immediately before the spawn — between the two
    a live process can appear, the worktree can vanish, or tasks.md can change,
    and only the later check can be trusted.

    Not an approval and not a rework: nothing about the result is accepted and
    no spec is regenerated.

    Examples:
        maestro workstream-continue w-adapters
    """
    db_path = _workstream_db_path(db, run)

    async def _run() -> tuple[int, str | None]:
        from maestro.continuation import (
            classify_continuation_readiness,
            describe_continuation_count,
        )
        from maestro.database import Database
        from maestro.models import SPEC_PREFIX, WorkstreamStatus
        from maestro.orchestrator import _maybe_live_orphan
        from maestro.tasks_spec import (
            DanglingDependency,
            find_dangling_dependencies,
        )

        database = Database(db_path)
        await database.connect()
        try:
            workstream = await database.get_workstream(workstream_id)
            if workstream.status != WorkstreamStatus.NEEDS_REVIEW:
                raise ValueError(
                    f"workstream '{workstream_id}' is {workstream.status}, "
                    f"only NEEDS_REVIEW can be queued for continuation"
                )

            # A snapshot check, deliberately not the authority: the orchestrator
            # re-checks the same four facts just before spawning.
            recorded = workstream.workspace_path
            worktree = Path(recorded) if recorded else None
            spec_dir = worktree / "spec" if worktree else None
            dangling: list[DanglingDependency] = []
            state_present = False
            if spec_dir is not None:
                tasks_path = spec_dir / f"{SPEC_PREFIX}tasks.md"
                try:
                    dangling = find_dangling_dependencies(
                        tasks_path.read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeDecodeError):
                    # An unreadable plan is not "no dependencies": there is
                    # nothing to continue. Calling it fine here would queue a
                    # continuation the orchestrator is certain to refuse, which
                    # is the opposite of the fast readable refusal this check
                    # exists for.
                    dangling = [
                        DanglingDependency(task_id="<file>", missing="tasks.md")
                    ]
                state_present = (
                    spec_dir / f".executor-{SPEC_PREFIX}state.db"
                ).is_file()
            verdict = classify_continuation_readiness(
                worktree_exists=bool(worktree and worktree.is_dir()),
                # The SAME predicate the orchestrator uses, not a hand-rolled
                # `pid > 0`: that misses the spawning sentinel (-1), the window
                # in which a spawn was in flight and a process may well exist.
                live_execution=_maybe_live_orphan(workstream.process_pid),
                dangling=dangling,
                state_db_present=state_present,
            )
            if not verdict.ok:
                raise ValueError(
                    f"cannot continue ({verdict.reason}): {verdict.message}"
                )

            await database.requeue_for_continuation(workstream_id)
            return (
                workstream.continuation_count,
                describe_continuation_count(workstream.continuation_count),
            )
        finally:
            await database.close()

    from maestro.database import WorkstreamNotFoundError

    try:
        started, warning = asyncio.run(_run())
    except (ValueError, WorkstreamNotFoundError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(
        f"[green]Workstream '{workstream_id}' queued for continuation over its "
        f"existing tasks.md (NEEDS_REVIEW -> READY).[/green]"
    )
    console.print(
        f"Continuations actually started so far: {started}. "
        f"The orchestrator dispatches this one on its next loop, after "
        f"re-checking the preconditions."
    )
    if warning is not None:
        console.print(f"[yellow]{warning}[/yellow]")
        console.print(
            "[dim]This is a warning, not a limit — continuing again is allowed.[/dim]"
        )


@app.command("workstream-recapture")
def workstream_recapture_command(
    workstream_id: Annotated[str, typer.Argument(help="Workstream ID to recapture")],
    db: Annotated[
        Path | None,
        typer.Option("--db", "-d", help="Path to SQLite database file"),
    ] = None,
    run: Annotated[
        str | None,
        typer.Option("--run", help=_RUN_OPTION_HELP),
    ] = None,
) -> None:
    """Retry ONLY post-mortem evidence capture for a blocked workstream (#164).

    Use after a `post-mortem capture failed` block: the run itself finished and
    its worktree is intact, only the archive write failed (an unwritable
    archive root, no space). This re-runs that one step for the same execution
    and then continues the normal success pipeline — no executor, no
    decomposition, no new sha.

    This is NOT an approval: nothing about the result is accepted here. Fix
    whatever made the archive root unwritable first, then run this.

    Examples:
        maestro workstream-recapture w-contracts --db run/maestro.db
    """
    db_path = _workstream_db_path(db, run)

    async def _run() -> str:
        from maestro.database import Database
        from maestro.models import WorkstreamStatus
        from maestro.postmortem import parse_recapture_marker

        database = Database(db_path)
        await database.connect()
        try:
            workstream = await database.get_workstream(workstream_id)
            if workstream is None:
                raise ValueError(f"workstream '{workstream_id}' not found")
            if workstream.status != WorkstreamStatus.NEEDS_REVIEW:
                raise ValueError(
                    f"workstream '{workstream_id}' is {workstream.status}, "
                    f"only NEEDS_REVIEW can be requeued for recapture"
                )
            execution_id = parse_recapture_marker(workstream.error_message)
            if execution_id is None:
                raise ValueError(
                    f"workstream '{workstream_id}' carries no recapture token — "
                    f"it was not blocked by a post-mortem capture failure, so "
                    f"there is no capture to retry"
                )
            await database.requeue_for_recapture(workstream_id)
            return execution_id
        finally:
            await database.close()

    try:
        execution_id = asyncio.run(_run())
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(
        f"[green]Workstream '{workstream_id}' requeued to recapture evidence "
        f"for execution {execution_id} (NEEDS_REVIEW -> READY).[/green]"
    )
    console.print(
        f"Resume with: maestro orchestrate <project.yaml> --db {db_path} --resume"
    )


async def _rework_workstream(
    db: "Database",
    workstream_id: str,
    *,
    reason: str,
    instructions: str | None,
    refresh_from: Path | None,
) -> int:
    """Operator rework (#124): validation, liveness proof, then the CAS.

    Everything fallible runs BEFORE the DB transaction, so a refusal
    leaves zero trace. Returns the new audit seq (== the new
    operator_rework_count).
    """
    import getpass

    from maestro.models import WorkstreamStatus
    from maestro.rework import (
        ReworkRefused,
        prove_no_live_process,
        read_head_sha,
        validate_refresh,
    )

    ws = await db.get_workstream(workstream_id)
    if ws.status not in (WorkstreamStatus.NEEDS_REVIEW, WorkstreamStatus.FAILED):
        raise ReworkRefused(
            f"workstream '{workstream_id}' is {ws.status.value} — "
            "not reworkable (only NEEDS_REVIEW and FAILED are)"
        )
    evidence = await prove_no_live_process(db, ws)
    if not ws.workspace_path:
        raise ReworkRefused(
            f"workstream '{workstream_id}' has no worktree recorded — nothing to rework"
        )
    prior_head_sha = await read_head_sha(Path(ws.workspace_path))
    refresh = validate_refresh(ws, refresh_from) if refresh_from else None
    return await db.record_workstream_rework(
        workstream_id,
        prior_status=ws.status,
        prior_count=ws.operator_rework_count,
        prior_marker=ws.recovery_ambiguity,
        reason=reason,
        instructions=instructions,
        initiator=getpass.getuser(),
        prior_error_message=ws.error_message,
        prior_head_sha=prior_head_sha,
        liveness_evidence=evidence,
        refresh=refresh,
    )


async def _resolve_ambiguity(
    db: "Database", workstream_id: str, *, statement: str
) -> None:
    """Explicit recovery-ambiguity resolution (#124), audited."""
    import getpass

    await db.resolve_recovery_ambiguity(
        workstream_id, statement=statement, initiator=getpass.getuser()
    )


_REWORK_WARN_THRESHOLD = 3


@app.command("workstream-rework")
def workstream_rework_command(
    workstream_id: Annotated[str, typer.Argument(help="Workstream ID to rework")],
    reason: Annotated[
        str,
        typer.Option(
            "--reason",
            help="Immutable audit explanation (never used as a prompt)",
        ),
    ],
    instructions: Annotated[
        str | None,
        typer.Option(
            "--instructions",
            help="Instructions appended to the next attempt's description",
        ),
    ] = None,
    refresh_from: Annotated[
        Path | None,
        typer.Option(
            "--refresh-from",
            help="Re-read this workstream's description/scope from a project.yaml",
        ),
    ] = None,
    db: Annotated[
        Path | None,
        typer.Option("--db", "-d", help="Path to SQLite database file"),
    ] = None,
    run: Annotated[
        str | None,
        typer.Option("--run", help=_RUN_OPTION_HELP),
    ] = None,
) -> None:
    """Rework a NEEDS_REVIEW/FAILED workstream: re-decompose in the same
    worktree with new instructions. NOT an approval — the ex-post gate
    re-evaluates the new work from scratch.

    Examples:
        maestro workstream-rework my-ws --reason "review rejected the diff" \\
            --instructions "split the migration into two steps"
    """
    from maestro.rework import ReworkRefused

    db_path = _workstream_db_path(db, run)

    async def _run() -> int:
        from maestro.database import Database

        database = Database(db_path)
        await database.connect()
        try:
            return await _rework_workstream(
                database,
                workstream_id,
                reason=reason,
                instructions=instructions,
                refresh_from=refresh_from,
            )
        finally:
            await database.close()

    try:
        seq = asyncio.run(_run())
    except (ReworkRefused, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(
        f"[green]Workstream '{workstream_id}' queued for rework "
        f"(-> READY, operator rework #{seq}).[/green]"
    )
    if seq >= _REWORK_WARN_THRESHOLD:
        console.print(
            f"[yellow]{seq} operator reworks on this workstream; consider "
            "whether it needs redesign instead.[/yellow]"
        )
    console.print(
        f"Resume with: maestro orchestrate <project.yaml> --db {db_path} --resume"
    )


@app.command("workstream-resolve-ambiguity")
def workstream_resolve_ambiguity_command(
    workstream_id: Annotated[str, typer.Argument(help="Workstream ID")],
    statement: Annotated[
        str,
        typer.Option(
            "--statement",
            help="How the absence of a live process was verified",
        ),
    ],
    db: Annotated[
        Path | None,
        typer.Option("--db", "-d", help="Path to SQLite database file"),
    ] = None,
    run: Annotated[
        str | None,
        typer.Option("--run", help=_RUN_OPTION_HELP),
    ] = None,
) -> None:
    """Resolve a recovery-ambiguity marker after manual cleanup (#124).

    Recovery parks possibly-live workstreams in NEEDS_REVIEW with a durable
    marker; when the marker carries no probeable pid (spawn-uncertain /
    live-handle) `workstream-rework` refuses until this explicit, audited
    resolution.

    Examples:
        maestro workstream-resolve-ambiguity my-ws \\
            --statement "checked ps/docker on the runner: nothing left"
    """
    db_path = _workstream_db_path(db, run)

    async def _run() -> None:
        from maestro.database import Database

        database = Database(db_path)
        await database.connect()
        try:
            await _resolve_ambiguity(database, workstream_id, statement=statement)
        finally:
            await database.close()

    try:
        asyncio.run(_run())
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(
        f"[green]Recovery-ambiguity marker on '{workstream_id}' resolved "
        "(audited).[/green]"
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


DEFAULT_SERVICE_LOG_DIR = DEFAULT_DB_DIR / "service-logs"
DEFAULT_SERVICE_ENV_FILE = DEFAULT_DB_DIR / "service.env"


def platform_units_dir(platform: str) -> Path:
    """Where the generated unit belongs for this platform (user scope)."""
    if platform == "launchd":
        return Path.home() / "Library" / "LaunchAgents"
    return Path.home() / ".config" / "systemd" / "user"


def _default_platform() -> str:
    return "launchd" if sys.platform.startswith("darwin") else "systemd"


@service_app.command("run")
def service_run_command(
    config: Annotated[Path, typer.Argument(help="Path to project YAML")],
    stage: Annotated[
        str, typer.Option("--stage", help="orchestrate | review")
    ] = "orchestrate",
    db_path: Annotated[
        Path | None, typer.Option("--db", help="Path to state database")
    ] = None,
    no_sweep: Annotated[
        bool, typer.Option("--no-sweep", help="Skip the stale-worktree sweep")
    ] = False,
) -> None:
    """Run one scheduled tick — what the service manager calls.

    Exit codes: 0 handled (including skipped, no-op, and a review tick
    whose PRs need a human), 1 infrastructure failure, 2 an orchestrate
    run that ended with failures.
    """
    if stage not in ("orchestrate", "review"):
        err_console.print(f"[red]Unknown stage:[/red] {stage}")
        raise typer.Exit(1)
    code = asyncio.run(
        _service_run(
            config_path=config,
            stage=cast("Stage", stage),
            db_path=db_path,
            sweep=not no_sweep,
        )
    )
    raise typer.Exit(code)


async def _service_run(
    *, config_path: Path, stage: "Stage", db_path: Path | None, sweep: bool
) -> int:
    project = load_orchestrator_config(config_path)

    # `--db` overrides run and database-path resolution — but NOT identity:
    # the lock key below must always be the repository's real RepoKey, never
    # a fabricated one (that fabrication is exactly what Task 6 removed from
    # service/tick.py). A scheduled tick always resumes an existing run and
    # must never mint one (spec §A.1); an unattended entry point must refuse
    # with a clear reason rather than a raw traceback.
    try:
        if db_path is not None:
            resolved_db_path = db_path
            key = parse_remote_url(project.repo_url)
        else:
            bootstrap = await bootstrap_run(project, resume=True, run_id_override=None)
            resolved_db_path = bootstrap.db_path
            key = bootstrap.key
    except IdentityError as e:
        err_console.print(f"[red]Cannot resolve repository identity:[/red] {e}")
        raise typer.Exit(1) from e
    except NoResumableRun as e:
        err_console.print(
            f"[red]No resumable run for '{project.project}':[/red] {e}\n"
            "Run 'maestro orchestrate <config>' once to establish a run, "
            "or pass --db <path> to point at an existing database directly."
        )
        raise typer.Exit(1) from e
    except AmbiguousRun as e:
        err_console.print(
            f"[red]Several runs could be resumed for '{project.project}':[/red] {e}\n"
            "Pass --db <path> to pick one directly."
        )
        raise typer.Exit(1) from e

    db = Database(resolved_db_path)
    await db.connect()
    notifications = create_notification_manager(project.notifications)
    try:
        result: TickResult = await run_tick(
            db=db,
            key=key,
            project=project.project,
            config_path=config_path,
            db_path=resolved_db_path,
            repo_path=Path(project.repo_path).expanduser(),  # noqa: ASYNC240
            base_branch=project.base_branch,
            stage=stage,
            runner=run_argv,
            notifier=notifications,
            log_dir=DEFAULT_SERVICE_LOG_DIR,
            sweep=sweep,
        )
        console.print(
            f"{project.project} [{stage}]: {result.decision} -> "
            f"{result.outcome} (exit {result.exit_code})"
        )
        return result.exit_code
    finally:
        await notifications.aclose()
        await db.close()


@service_app.command("install")
def service_install_command(
    config: Annotated[Path, typer.Argument(help="Path to project YAML")],
    stage: Annotated[
        str, typer.Option("--stage", help="orchestrate | review")
    ] = "orchestrate",
    schedule: Annotated[
        str | None, typer.Option("--schedule", help='Daily time, e.g. "03:00"')
    ] = None,
    every: Annotated[
        int | None, typer.Option("--every", help="Interval in minutes")
    ] = None,
    platform: Annotated[
        str | None, typer.Option("--platform", help="launchd | systemd")
    ] = None,
    db_path: Annotated[
        Path | None, typer.Option("--db", help="Path to state database")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the unit, write nothing")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing unit")
    ] = False,
    require_env: Annotated[
        list[str] | None,
        typer.Option(
            "--require-env",
            help="Extra credential names the unit needs (repeatable)",
        ),
    ] = None,
    skip_credential_check: Annotated[
        bool,
        typer.Option(
            "--skip-credential-check",
            help="Install without verifying credentials (documented escape hatch)",
        ),
    ] = False,
    load: Annotated[
        bool, typer.Option("--load/--no-load", help="Load the unit after writing")
    ] = True,
) -> None:
    """Generate and load the platform unit for a scheduled tick."""
    if stage not in ("orchestrate", "review"):
        err_console.print(f"[red]Unknown stage:[/red] {stage}")
        raise typer.Exit(1)
    target_platform = platform or _default_platform()
    project = load_orchestrator_config(config)
    resolved_db = db_path or DEFAULT_DB_PATH

    env_file = DEFAULT_SERVICE_ENV_FILE
    if not dry_run:
        ensure_env_file(env_file)
    # Maestro never calls a model API itself — it spawns harness CLIs
    # that authenticate themselves — so the check is satisfied by either
    # an exported key or the CLI's own credential store. Only the total
    # absence of both is a genuine "cannot authenticate at 03:00".
    required = (
        [] if skip_credential_check else [*CREDENTIAL_ENV_BY_HARNESS["claude_code"]]
    )
    for name in require_env or []:
        if name not in required:
            required.append(name)
    if skip_credential_check:
        console.print(
            "[yellow]Credential check skipped[/yellow] — if the harness cannot "
            "authenticate non-interactively, scheduled ticks will fail."
        )
    if dry_run:
        # A preview must render even when the environment is incomplete;
        # problems are reported, not fatal.
        preflight, problems = probe_environment(
            harness_binaries=["maestro", "spec-runner"],
            required_env=required,
            env_file=env_file,
        )
        for problem in problems:
            console.print(f"[yellow]Would refuse to install:[/yellow] {problem}")
    else:
        try:
            preflight = preflight_environment(
                harness_binaries=["maestro", "spec-runner"],
                required_env=required,
                env_file=env_file,
            )
        except PreflightError as exc:
            err_console.print(f"[red]Refusing to install:[/red] {exc}")
            raise typer.Exit(1) from exc

    spec = UnitSpec(
        project=project.project,
        stage=cast("Stage", stage),
        config_path=config.resolve(),
        db_path=resolved_db,
        maestro_bin=preflight.maestro_bin,
        path=preflight.path,
        env_file=env_file,
        log_dir=DEFAULT_SERVICE_LOG_DIR,
        schedule=schedule if every is None else None,
        every_minutes=every,
    )
    name = unit_name(project.project, stage, platform=cast("Any", target_platform))
    units_dir = platform_units_dir(target_platform)

    if target_platform == "launchd":
        files = {f"{name}.plist": render_launchd(spec)}
    else:
        service_text, timer_text = render_systemd(spec)
        files = {f"{name}.service": service_text, f"{name}.timer": timer_text}

    if dry_run:
        for filename, text in files.items():
            console.print(f"[dim]--- {units_dir / filename} ---[/dim]")
            console.print(text)
        return

    existing = [f for f in files if (units_dir / f).exists()]
    if existing and not force:
        err_console.print(
            f"[red]Unit already exists:[/red] {', '.join(existing)} "
            "(pass --force to overwrite)"
        )
        raise typer.Exit(1)

    units_dir.mkdir(parents=True, exist_ok=True)
    for filename, text in files.items():
        (units_dir / filename).write_text(text, encoding="utf-8")
    console.print(f"[green]Installed[/green] {name} in {units_dir}")
    if load:
        _load_unit(target_platform, name, units_dir)


@service_app.command("uninstall")
def service_uninstall_command(
    config: Annotated[Path, typer.Argument(help="Path to project YAML")],
    stage: Annotated[
        str, typer.Option("--stage", help="orchestrate | review")
    ] = "orchestrate",
    platform: Annotated[
        str | None, typer.Option("--platform", help="launchd | systemd")
    ] = None,
    db_path: Annotated[
        Path | None, typer.Option("--db", help="Unused; accepted for symmetry")
    ] = None,
    load: Annotated[
        bool, typer.Option("--load/--no-load", help="Unload before removing")
    ] = True,
) -> None:
    """Unload and remove the generated unit."""
    target_platform = platform or _default_platform()
    project = load_orchestrator_config(config)
    name = unit_name(project.project, stage, platform=cast("Any", target_platform))
    units_dir = platform_units_dir(target_platform)
    if load:
        _unload_unit(target_platform, name, units_dir)
    removed = 0
    for suffix in (".plist", ".service", ".timer"):
        path = units_dir / f"{name}{suffix}"
        if path.exists():
            path.unlink()
            removed += 1
    console.print(f"Removed {removed} unit file(s) for {name}")


@service_app.command("status")
def service_status_command(
    config: Annotated[Path, typer.Argument(help="Path to project YAML")],
    stage: Annotated[
        str | None, typer.Option("--stage", help="Filter by stage")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="How many ticks")] = 10,
    db_path: Annotated[
        Path | None, typer.Option("--db", help="Path to state database")
    ] = None,
) -> None:
    """Show recent ticks for this project."""
    asyncio.run(
        _service_status(
            config_path=config,
            stage=stage,
            limit=limit,
            db_path=db_path or DEFAULT_DB_PATH,
        )
    )


async def _service_status(
    *, config_path: Path, stage: str | None, limit: int, db_path: Path
) -> None:
    project = load_orchestrator_config(config_path)
    db = Database(db_path)
    await db.connect()
    try:
        rows = await db.list_service_ticks(project.project, stage=stage, limit=limit)
    finally:
        await db.close()
    if not rows:
        console.print("[dim]No ticks recorded yet.[/dim]")
        return
    table = Table(title=f"Service ticks — {project.project}")
    for column in ("Started", "Stage", "Decision", "Outcome", "Exit"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row["started_at"]),
            row["stage"],
            row["decision"],
            str(row["outcome"] or "-"),
            str(row["exit_code"] if row["exit_code"] is not None else "-"),
        )
    console.print(table)


def _load_unit(platform: str, name: str, units_dir: Path) -> None:
    if platform == "launchd":
        cmd = ["launchctl", "load", str(units_dir / f"{name}.plist")]
    else:
        cmd = ["systemctl", "--user", "enable", "--now", f"{name}.timer"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        err_console.print(
            f"[yellow]Unit written but not loaded:[/yellow] {result.stderr.strip()}"
        )


def _unload_unit(platform: str, name: str, units_dir: Path) -> None:
    if platform == "launchd":
        cmd = ["launchctl", "unload", str(units_dir / f"{name}.plist")]
    else:
        cmd = ["systemctl", "--user", "disable", "--now", f"{name}.timer"]
    subprocess.run(cmd, capture_output=True, check=False)


@app.command("review-pr")
def review_pr_command(
    config: Annotated[
        Path,
        typer.Argument(help="Path to project YAML configuration"),
    ],
    workstream_id: Annotated[
        str | None,
        typer.Argument(help="Workstream whose PR to review (omit with --all)"),
    ] = None,
    review_all: Annotated[
        bool,
        typer.Option("--all", help="Review every workstream PR (sequential)"),
    ] = False,
    gc: Annotated[
        bool,
        typer.Option(
            "--gc",
            help="Sweep workspaces and durable state of closed/merged PRs",
        ),
    ] = False,
    discard_local: Annotated[
        bool,
        typer.Option(
            "--discard-local",
            help="Reset a saved local continuation instead of publishing it",
        ),
    ] = False,
    db_path: Annotated[
        Path | None, typer.Option("--db", help="Path to state database")
    ] = None,
) -> None:
    """Drive the spec-runner review-bot loop over Maestro-created PRs.

    Post-delivery and advisory: the workstream is already DONE and the
    feature SHA already merged locally, so review fixes move the PR head
    only. Requires spec-runner >= 2.21.0.

    Exit codes: 0 complete, 1 infrastructure failure, 2 needs human,
    3 already running (another process holds this PR's lock).

    Examples:
        maestro review-pr project.yaml ws-006
        maestro review-pr project.yaml --all
    """
    if not review_all and not gc and workstream_id is None:
        err_console.print("[red]Provide a workstream id, --all, or --gc[/red]")
        raise typer.Exit(1)
    asyncio.run(
        _review_pr(
            config_path=config,
            workstream_id=workstream_id,
            review_all=review_all,
            gc=gc,
            discard_local=discard_local,
            db_path=db_path or DEFAULT_DB_PATH,
        )
    )


async def _prepare_review_workspace(
    *,
    repo_path: Path,
    paths: ReviewPaths,
    ref: PrRef,
    discard_local: bool,
) -> tuple[Path, str]:
    """Materialize the workspace and reconcile a saved continuation (spec 3.1)."""
    meta = fetch_pr_meta(ref)
    workspace = materialize(
        repo_path=repo_path,
        paths=paths,
        head_ref=meta.head_ref,
        head_sha=meta.head_sha,
        discard_local=discard_local,
    )
    local_head = _local_head(workspace)
    if local_head != meta.head_sha:
        # A continuation: publish it so spec-runner's strict
        # local_head == remote head_sha check can pass (spec 3.1.4).
        pushed = recover_push(
            workspace=workspace,
            head_ref=meta.head_ref,
            expected_remote_sha=meta.head_sha,
        )
        return workspace, pushed
    return workspace, meta.head_sha


async def _review_pr(
    *,
    config_path: Path,
    workstream_id: str | None,
    review_all: bool,
    gc: bool,
    discard_local: bool,
    db_path: Path,
) -> None:
    project = load_orchestrator_config(config_path)
    if gc:
        await _review_pr_gc(project=project, db_path=db_path)
        return
    probe = _probe_spec_runner_version()
    problem = check_spec_runner_version(probe[1], returncode=probe[0])
    if problem is not None:
        err_console.print(f"[red]spec-runner unsupported:[/red] {problem}")
        raise typer.Exit(1)

    db = Database(db_path)
    await db.connect()
    notifications = create_notification_manager(project.notifications)
    try:
        workstreams = await db.get_all_workstreams()
        if review_all:
            candidates = [w for w in workstreams if w.pr_url]
        else:
            candidates = [w for w in workstreams if w.id == workstream_id]
            if not candidates:
                err_console.print(f"[red]Workstream not found:[/red] {workstream_id}")
                raise typer.Exit(1)
            if not candidates[0].pr_url:
                err_console.print(f"[red]Workstream '{workstream_id}' has no PR[/red]")
                raise typer.Exit(1)

        seen: set[tuple[str, int]] = set()
        codes: list[int] = []
        for workstream in candidates:
            assert workstream.pr_url is not None
            try:
                ref = parse_pr_url(workstream.pr_url)
            except ValueError as exc:
                err_console.print(f"[red]{workstream.id}:[/red] {exc}")
                codes.append(1)
                continue
            key = (ref.owner_repo, ref.number)
            if key in seen:  # distinct workstreams may share one PR
                continue
            seen.add(key)
            paths = ReviewPaths.for_pr(ref)
            code = await run_review(
                db=db,
                workstream_id=workstream.id,
                pr_url=workstream.pr_url,
                repo_path=Path(project.repo_path).expanduser(),  # noqa: ASYNC240
                paths=paths,
                invocation=ReviewInvocation(invoke_spec_runner),
                prepare=_prepare_review_workspace,
                spec_runner_version=probe[1].strip() or None,
                notifier=notifications,
                discard_local=discard_local,
            )
            codes.append(code)
            console.print(f"{workstream.id}: {ref.canonical_url} -> exit {code}")

        # Aggregate: infra failure dominates, then needs-human (spec 6.1);
        # a locked PR (3) is reported but never decides the aggregate.
        if 1 in codes:
            raise typer.Exit(1)
        if 2 in codes:
            raise typer.Exit(2)
        if codes and all(c == EXIT_ALREADY_RUNNING for c in codes):
            raise typer.Exit(EXIT_ALREADY_RUNNING)
    finally:
        await notifications.aclose()
        await db.close()


async def _review_pr_gc(*, project: OrchestratorConfig, db_path: Path) -> None:
    """Sweep review workspaces/state of closed or merged PRs (spec §4)."""
    db = Database(db_path)
    await db.connect()
    try:
        swept = 0
        for workstream in await db.get_all_workstreams():
            if not workstream.pr_url:
                continue
            try:
                ref = parse_pr_url(workstream.pr_url)
            except ValueError:
                continue
            if gc_pr(
                repo_path=Path(project.repo_path).expanduser(),  # noqa: ASYNC240
                paths=ReviewPaths.for_pr(ref),
                ref=ref,
            ):
                swept += 1
                console.print(f"[dim]swept[/dim] {ref.canonical_url}")
        console.print(f"Swept {swept} finished PR(s)")
    finally:
        await db.close()


def _probe_spec_runner_version() -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["spec-runner", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return result.returncode, result.stdout


def _local_head(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


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


def _runs(count: int) -> str:
    return f"{count} run" if count == 1 else f"{count} runs"


def _repositories(count: int) -> str:
    return f"{count} repository" if count == 1 else f"{count} repositories"


def _format_bytes(size: int) -> str:
    """A size an operator can read at a glance, in powers of 1024."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")  # pragma: no cover


@app.command("state-usage")
def state_usage_command() -> None:
    """Report what `~/.maestro` holds, per repository (spec §D).

    Retention is deliberately not a policy here — this is the trigger for
    one: growth becomes visible before it becomes a problem. The size counts
    the whole project directory, `locks/` and any leftover `.staging/`
    included, not just `runs/`.

    On a machine that predates the split there is no `projects/` at all and
    the legacy `maestro.db` of spec §E is the whole of `~/.maestro` — it is
    named here rather than reported as an empty home. It is sized by `stat`
    and never opened.

    Examples:
        maestro state-usage
    """
    usage = home_usage()
    if usage.repositories:
        total = 0
        for repo in usage.repositories:
            total += repo.size
            unread = f"  ({len(repo.unreadable)} unreadable)" if repo.unreadable else ""
            console.print(
                f"{'/'.join(repo.key.as_path_parts())}  {_runs(repo.run_count)}  "
                f"{_format_bytes(repo.size)}{unread}",
                soft_wrap=True,
            )
        console.print(
            f"TOTAL  {_repositories(len(usage.repositories))}  "
            f"{_runs(sum(r.run_count for r in usage.repositories))}  "
            f"{_format_bytes(total)}"
        )
    else:
        console.print(f"No project state under {maestro_home()}.")

    if usage.legacy_db is not None:
        console.print(
            f"Legacy database (spec §E, not counted above): {usage.legacy_db}  "
            f"{_format_bytes(usage.legacy_db_size)}",
            soft_wrap=True,
        )

    skipped = [p for repo in usage.repositories for p in repo.unreadable]
    skipped.extend(usage.unreadable)
    if skipped:
        # A skipped subtree that just vanishes from the byte total is an
        # unknown rendered as clean. Say what was missed, and say the totals
        # are short because of it.
        console.print(
            f"[yellow]{len(skipped)} path(s) could not be read; their bytes are "
            f"NOT in the totals above:[/yellow]"
        )
        for path in skipped:
            console.print(f"  {path}", soft_wrap=True)


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
    UNKNOWN, never as $0. Shows TOTAL plus breakdowns by harness, by task,
    by execution phase (task/validation/verification), and by model (rows
    with no recorded model group under "UNKNOWN").

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
    console.print(_table("By phase", "Phase", report.by_phase))
    console.print(_table("By model", "Model", report.by_model))


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
