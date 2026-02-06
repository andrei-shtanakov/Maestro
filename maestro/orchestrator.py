"""Multi-process orchestrator for Maestro.

This module provides the Orchestrator class that coordinates
multiple spec-runner processes, each running in its own git
worktree. It handles the full lifecycle: decomposition, workspace
setup, process spawning, monitoring, and PR creation.
"""

import asyncio
import contextlib
import json
import logging
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from maestro.database import Database
from maestro.decomposer import ProjectDecomposer
from maestro.models import (
    OrchestratorConfig,
    Zadacha,
    ZadachaConfig,
    ZadachaStatus,
)
from maestro.pr_manager import PRManager, PRManagerError
from maestro.workspace import WorkspaceManager


class OrchestratorError(Exception):
    """Base exception for orchestrator errors."""


@dataclass
class RunningZadacha:
    """Represents a currently running zadacha process."""

    zadacha: Zadacha
    process: asyncio.subprocess.Process
    started_at: datetime
    workspace_path: Path
    log_file: Path


@dataclass
class OrchestratorStats:
    """Statistics for an orchestration run."""

    total_zadachi: int = 0
    completed: int = 0
    failed: int = 0
    prs_created: int = 0
    start_time: datetime = field(default_factory=lambda: datetime.now(UTC))


class Orchestrator:
    """Coordinates multiple spec-runner processes.

    Main loop:
    1. Decompose project into zadachi (if needed)
    2. Resolve ready zadachi from DAG
    3. Create workspace + spawn spec-runner for each
    4. Monitor processes, read progress
    5. On completion: push + create PR + cleanup
    """

    def __init__(
        self,
        db: Database,
        workspace_mgr: WorkspaceManager,
        decomposer: ProjectDecomposer,
        pr_manager: PRManager,
        config: OrchestratorConfig,
        log_dir: Path | None = None,
    ) -> None:
        """Initialize orchestrator.

        Args:
            db: Database for state persistence.
            workspace_mgr: Manager for worktree workspaces.
            decomposer: Project decomposer for spec gen.
            pr_manager: PR creation manager.
            config: Orchestrator configuration.
            log_dir: Directory for log files.
        """
        self._db = db
        self._workspace_mgr = workspace_mgr
        self._decomposer = decomposer
        self._pr_manager = pr_manager
        self._config = config
        self._log_dir = log_dir or Path(config.repo_path).expanduser() / "logs"

        self._running: dict[str, RunningZadacha] = {}
        self._shutdown_requested = False
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._logger = logging.getLogger(__name__)
        self._stats = OrchestratorStats()

    @property
    def is_running(self) -> bool:
        """Check if orchestrator is running."""
        return self._loop is not None and not self._shutdown_requested

    async def run(self) -> OrchestratorStats:
        """Run the orchestrator main loop.

        Returns:
            Statistics for the orchestration run.

        Raises:
            OrchestratorError: If database not connected.
        """
        if not self._db.is_connected:
            msg = "Database must be connected"
            raise OrchestratorError(msg)

        self._loop = asyncio.get_running_loop()
        self._setup_signal_handlers()
        self._log_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Step 1: Ensure zadachi exist
            await self._ensure_zadachi()

            # Step 2: Main loop
            await self._main_loop()
        finally:
            await self._cleanup()

        return self._stats

    async def _ensure_zadachi(self) -> None:
        """Ensure zadachi are in the database.

        If no zadachi exist, run decomposition.
        """
        existing = await self._db.get_all_zadachi()

        if existing:
            self._logger.info("Found %d existing zadachi", len(existing))
            self._stats.total_zadachi = len(existing)
            return

        # Use manually specified zadachi from config
        if self._config.zadachi:
            self._logger.info(
                "Creating %d zadachi from config",
                len(self._config.zadachi),
            )
            await self._create_zadachi_from_configs(self._config.zadachi)
            return

        # Auto-decompose
        if not self._config.description:
            msg = (
                "No zadachi in config and no project description for auto-decomposition"
            )
            raise OrchestratorError(msg)

        self._logger.info("Auto-decomposing project")
        configs = self._decomposer.decompose(self._config.description)
        await self._create_zadachi_from_configs(configs)

    async def _create_zadachi_from_configs(self, configs: list[ZadachaConfig]) -> None:
        """Create Zadacha records in DB from configs."""
        for config in configs:
            zadacha = Zadacha.from_config(
                config,
                branch_prefix=self._config.branch_prefix,
            )
            await self._db.create_zadacha(zadacha)

        self._stats.total_zadachi = len(configs)
        self._logger.info("Created %d zadachi in database", len(configs))

    async def _main_loop(self) -> None:
        """Main orchestration loop."""
        poll_interval = 2.0

        while not self._shutdown_requested:
            # Get completed zadacha IDs
            completed_ids = await self._get_completed_ids()

            # Check if all done
            if await self._all_zadachi_complete():
                self._logger.info("All zadachi complete")
                break

            # Resolve ready zadachi
            ready_ids = await self._resolve_ready(completed_ids)

            # Spawn up to max_concurrent
            await self._spawn_ready(ready_ids)

            # Monitor running processes
            await self._monitor_running()

            # Wait before next iteration
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=poll_interval,
                )

    async def _get_completed_ids(self) -> set[str]:
        """Get IDs of completed zadachi."""
        done = await self._db.get_zadachi_by_status(ZadachaStatus.DONE)
        return {z.id for z in done}

    async def _all_zadachi_complete(self) -> bool:
        """Check if all zadachi are in terminal states."""
        all_z = await self._db.get_all_zadachi()
        terminal = {
            ZadachaStatus.DONE,
            ZadachaStatus.ABANDONED,
        }

        for z in all_z:
            if z.status not in terminal:
                if z.status == ZadachaStatus.NEEDS_REVIEW:
                    continue
                return False

        return True

    async def _resolve_ready(self, completed_ids: set[str]) -> list[str]:
        """Resolve zadachi that are ready to run.

        A zadacha is ready when:
        - Status is PENDING or READY
        - All dependencies are completed
        - Not already running
        """
        all_z = await self._db.get_all_zadachi()
        ready: list[str] = []

        for z in all_z:
            if z.id in self._running:
                continue
            if z.status not in (
                ZadachaStatus.PENDING,
                ZadachaStatus.READY,
            ):
                continue

            # Check all dependencies completed
            if z.depends_on and not set(z.depends_on).issubset(completed_ids):
                continue

            ready.append(z.id)

        # Sort by priority (descending)
        all_by_id = {z.id: z for z in all_z}
        ready.sort(
            key=lambda zid: all_by_id[zid].priority,
            reverse=True,
        )

        return ready

    async def _spawn_ready(self, ready_ids: list[str]) -> None:
        """Spawn ready zadachi up to concurrency limit."""
        available = self._config.max_concurrent - len(self._running)

        for zid in ready_ids[:available]:
            if self._shutdown_requested:
                break

            try:
                await self._spawn_zadacha(zid)
            except Exception as e:
                self._logger.error(
                    "Failed to spawn zadacha '%s': %s",
                    zid,
                    e,
                )
                await self._db.update_zadacha_status(
                    zid,
                    ZadachaStatus.FAILED,
                    error_message=str(e),
                )

    async def _spawn_zadacha(self, zadacha_id: str) -> None:
        """Spawn a spec-runner process for a zadacha."""
        zadacha = await self._db.get_zadacha(zadacha_id)

        # Transition to DECOMPOSING for spec generation
        await self._db.update_zadacha_status(
            zadacha_id,
            ZadachaStatus.DECOMPOSING,
            expected_status=zadacha.status,
        )

        # Create workspace
        if not self._workspace_mgr.workspace_exists(zadacha_id):
            workspace = self._workspace_mgr.create_workspace(zadacha_id, zadacha.branch)
        else:
            workspace = self._workspace_mgr.get_workspace_path(zadacha_id)

        # Update workspace path in DB
        await self._db.update_zadacha_status(
            zadacha_id,
            ZadachaStatus.DECOMPOSING,
            workspace_path=str(workspace),
        )

        # Generate spec if not exists
        spec_dir = workspace / "spec"
        tasks_file = spec_dir / "tasks.md"
        if not tasks_file.exists():
            zadacha_config = ZadachaConfig(
                id=zadacha.id,
                title=zadacha.title,
                description=zadacha.description,
                scope=zadacha.scope,
                depends_on=zadacha.depends_on,
                priority=zadacha.priority,
            )
            self._decomposer.generate_spec(zadacha_config, workspace)

        # Setup spec-runner config
        executor_config = self._config.spec_runner.to_executor_config()
        # Set main_branch to the zadacha branch (so spec-runner
        # merges subtask branches back to it)
        executor_config.setdefault("executor", {})["main_branch"] = zadacha.branch
        self._workspace_mgr.setup_spec_runner(workspace, executor_config)

        # Transition to READY then RUNNING
        await self._db.update_zadacha_status(zadacha_id, ZadachaStatus.READY)
        await self._db.update_zadacha_status(
            zadacha_id,
            ZadachaStatus.RUNNING,
            expected_status=ZadachaStatus.READY,
        )

        # Spawn spec-runner
        log_file = self._log_dir / f"{zadacha_id}.log"

        cmd = ["spec-runner", "run", "--all"]

        # Add callback URL if REST API is running
        # (optional — we also poll state files)
        if self._config.callback_url:
            cmd.extend(["--callback-url", self._config.callback_url])

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workspace,
            stdout=log_file.open("w"),
            stderr=asyncio.subprocess.STDOUT,
        )

        # Update PID in DB
        await self._db.update_zadacha_status(
            zadacha_id,
            ZadachaStatus.RUNNING,
            process_pid=process.pid,
        )

        self._running[zadacha_id] = RunningZadacha(
            zadacha=zadacha.model_copy(
                update={
                    "status": ZadachaStatus.RUNNING,
                    "workspace_path": str(workspace),
                }
            ),
            process=process,
            started_at=datetime.now(UTC),
            workspace_path=workspace,
            log_file=log_file,
        )

        self._logger.info(
            "Spawned spec-runner for '%s' (PID %d) in %s",
            zadacha_id,
            process.pid,
            workspace,
        )

    async def _monitor_running(self) -> None:
        """Monitor running spec-runner processes."""
        completed: list[str] = []

        for zid, running in self._running.items():
            # Read progress from state file
            await self._update_progress(zid, running)

            # Check if process finished (returncode is None while running)
            return_code = running.process.returncode

            if return_code is not None:
                await self._handle_completion(zid, running, return_code)
                completed.append(zid)

        for zid in completed:
            del self._running[zid]

    async def _update_progress(
        self,
        zadacha_id: str,
        running: RunningZadacha,
    ) -> None:
        """Read spec-runner state file for progress."""
        state_file = running.workspace_path / "spec" / ".executor-state.json"

        if not state_file.exists():
            return

        try:
            loop = asyncio.get_running_loop()
            content = await loop.run_in_executor(None, state_file.read_text)
            state = json.loads(content)

            # Count task statuses
            tasks = state.get("tasks", {})
            total = len(tasks)
            done = sum(1 for t in tasks.values() if t.get("status") == "success")
            progress = f"{done}/{total} done"

            await self._db.update_zadacha_status(
                zadacha_id,
                ZadachaStatus.RUNNING,
                subtask_progress=progress,
            )
        except (json.JSONDecodeError, OSError):
            pass  # State file may be partially written

    async def _handle_completion(
        self,
        zadacha_id: str,
        running: RunningZadacha,
        return_code: int,
    ) -> None:
        """Handle spec-runner process completion."""
        if return_code == 0:
            self._logger.info(
                "Zadacha '%s' completed successfully",
                zadacha_id,
            )
            await self._handle_success(zadacha_id, running)
        else:
            self._logger.warning(
                "Zadacha '%s' failed (code %d)",
                zadacha_id,
                return_code,
            )
            await self._handle_failure(
                zadacha_id,
                f"spec-runner exited with code {return_code}",
            )

    async def _handle_success(
        self,
        zadacha_id: str,
        _running: RunningZadacha,
    ) -> None:
        """Handle successful zadacha completion.

        Push branch, create PR, cleanup workspace.
        """
        zadacha = await self._db.get_zadacha(zadacha_id)

        # Transition to MERGING
        await self._db.update_zadacha_status(
            zadacha_id,
            ZadachaStatus.MERGING,
            expected_status=ZadachaStatus.RUNNING,
        )

        # Push branch and create PR
        if self._config.auto_pr:
            try:
                pr_url = self._pr_manager.push_and_create_pr(
                    branch=zadacha.branch,
                    title=f"[Maestro] {zadacha.title}",
                    body=self._build_pr_body(zadacha),
                    base_branch=self._config.base_branch,
                )

                await self._db.update_zadacha_status(
                    zadacha_id,
                    ZadachaStatus.PR_CREATED,
                    pr_url=pr_url,
                )

                self._stats.prs_created += 1
                self._logger.info(
                    "Created PR for '%s': %s",
                    zadacha_id,
                    pr_url,
                )
            except PRManagerError as e:
                self._logger.warning(
                    "Failed to create PR for '%s': %s",
                    zadacha_id,
                    e,
                )
                # Still mark as PR_CREATED (PR may exist)
                await self._db.update_zadacha_status(
                    zadacha_id,
                    ZadachaStatus.PR_CREATED,
                    error_message=f"PR creation note: {e}",
                )

        # Mark as DONE
        current = await self._db.get_zadacha(zadacha_id)
        if current.status == ZadachaStatus.PR_CREATED:
            await self._db.update_zadacha_status(
                zadacha_id,
                ZadachaStatus.DONE,
                expected_status=ZadachaStatus.PR_CREATED,
            )
        elif current.status == ZadachaStatus.MERGING:
            # No PR created (auto_pr=False)
            # MERGING -> can't go to DONE directly, so
            # transition through PR_CREATED
            await self._db.update_zadacha_status(
                zadacha_id,
                ZadachaStatus.PR_CREATED,
            )
            await self._db.update_zadacha_status(
                zadacha_id,
                ZadachaStatus.DONE,
                expected_status=ZadachaStatus.PR_CREATED,
            )

        self._stats.completed += 1

        # Cleanup workspace
        self._workspace_mgr.cleanup_workspace(zadacha_id)

    async def _handle_failure(
        self,
        zadacha_id: str,
        error_message: str,
    ) -> None:
        """Handle zadacha failure with retry logic."""
        zadacha = await self._db.get_zadacha(zadacha_id)

        if zadacha.can_retry():
            new_count = zadacha.retry_count + 1
            self._logger.info(
                "Retrying zadacha '%s' (%d/%d)",
                zadacha_id,
                new_count,
                zadacha.max_retries,
            )
            await self._db.update_zadacha_status(
                zadacha_id,
                ZadachaStatus.FAILED,
                error_message=error_message,
                retry_count=new_count,
            )
            await self._db.update_zadacha_status(
                zadacha_id,
                ZadachaStatus.READY,
                expected_status=ZadachaStatus.FAILED,
            )
        else:
            self._logger.warning(
                "Zadacha '%s' exhausted retries",
                zadacha_id,
            )
            await self._db.update_zadacha_status(
                zadacha_id,
                ZadachaStatus.FAILED,
                error_message=error_message,
            )
            await self._db.update_zadacha_status(
                zadacha_id,
                ZadachaStatus.NEEDS_REVIEW,
                expected_status=ZadachaStatus.FAILED,
            )
            self._stats.failed += 1

    def _build_pr_body(self, zadacha: Zadacha) -> str:
        """Build PR body from zadacha info."""
        scope_str = "\n".join(f"- `{s}`" for s in zadacha.scope)
        return (
            f"## Summary\n\n"
            f"{zadacha.description}\n\n"
            f"## Scope\n\n"
            f"{scope_str}\n\n"
            f"## Progress\n\n"
            f"{zadacha.subtask_progress or 'N/A'}\n\n"
            f"---\n"
            f"🤖 Generated by Maestro Orchestrator"
        )

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        if self._loop is None:
            return

        for sig in (signal.SIGTERM, signal.SIGINT):
            self._loop.add_signal_handler(sig, self._handle_shutdown_signal)

    def _handle_shutdown_signal(self) -> None:
        """Handle shutdown signal."""
        self._shutdown_requested = True
        self._shutdown_event.set()

    async def shutdown(self) -> None:
        """Request graceful shutdown."""
        self._shutdown_requested = True
        self._shutdown_event.set()

    async def _cleanup(self) -> None:
        """Cleanup running processes on shutdown."""
        for zid, running in list(self._running.items()):
            try:
                running.process.terminate()
                await asyncio.sleep(0.5)
                if running.process.returncode is None:
                    running.process.kill()
                await running.process.wait()
            except OSError:
                pass

            try:
                await self._db.update_zadacha_status(
                    zid,
                    ZadachaStatus.FAILED,
                    error_message="Orchestrator shutdown",
                )
                await self._db.update_zadacha_status(
                    zid,
                    ZadachaStatus.READY,
                    expected_status=ZadachaStatus.FAILED,
                )
            except Exception as e:
                self._logger.warning(
                    "Failed to update zadacha '%s' during cleanup: %s",
                    zid,
                    e,
                )

        self._running.clear()

        if self._loop:
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(ValueError):
                    self._loop.remove_signal_handler(sig)

        self._loop = None
