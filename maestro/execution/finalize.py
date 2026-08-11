"""Single-owner finalization: reap, then persist-phase → collect → persist-phase
→ cleanup. DB transitions happen BETWEEN phases (via callbacks), so a crash in
the collect→cleanup window can never leave durable state that lies.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from maestro.execution.backend import TaskHandle
from maestro.execution.models import ExecutionResult


_Callback = Callable[[], Awaitable[None]] | None


class EvidenceCaptureFailed(Exception):
    """An `on_collected` callback could not capture the run's evidence (#164).

    Deliberately distinct from an ordinary exception out of that callback.
    `on_collected` carries two unrelated responsibilities — persisting the
    collected phase (Mode 1's durable-state honesty) and capturing the
    post-mortem archive (#164) — and they need opposite handling. A failed
    durable write is a crash that must surface to the caller, so it keeps
    propagating exactly as before. A failed capture is an *expected* outcome
    with a defined response: skip cleanup, preserve the workspace, report it.
    """


@dataclass
class FinalizationResult:
    """Outcome of finalizing a handle."""

    execution: ExecutionResult
    collect_error: str | None = None
    archive_error: str | None = None
    cleanup_error: str | None = None
    collect_succeeded: bool = False
    cleanup_attempted: bool = False

    @property
    def cleaned(self) -> bool:
        return self.cleanup_attempted and self.cleanup_error is None


async def finalize_handle(
    handle: TaskHandle,
    *,
    on_terminal: _Callback = None,
    on_collected: _Callback = None,
) -> FinalizationResult:
    """Reap → persist terminal → collect → persist collected → cleanup.

    If ``collect()`` raises, finalization returns immediately WITHOUT running
    ``cleanup()`` — the remote/local workspace is preserved so a collect
    conflict can be inspected or retried rather than silently destroyed.

    An ``on_collected`` that raises ``EvidenceCaptureFailed`` is treated the
    same way (#164, spec §6.5). Post-mortem capture hangs off that callback
    because it is the one moment every transport agrees on: collect has
    applied, and nothing has been destroyed yet. If capture fails and cleanup
    ran anyway, the run would destroy the only surviving copy of the executor
    logs — on ssh they are never collected back — and would look successful
    doing it. It is reported as ``archive_error`` rather than
    ``collect_error`` so the caller can tell "the diff never made it back"
    from "the evidence never made it out"; both preserve the workspace, but
    they need different messages.

    **Any other exception from ``on_collected`` still propagates**, unchanged.
    That callback also persists the collected phase, and a failed durable
    write is a crash the caller must see — Mode 1's scheduler relies on it to
    leave the handle honestly at ``terminal`` rather than reporting a
    finalization that did not happen.
    """
    execution = await handle.wait()
    if on_terminal is not None:
        await on_terminal()
    try:
        await handle.collect()
    except Exception as e:
        # Collect failed/conflicted: DO NOT clean up — resources are preserved.
        return FinalizationResult(execution, collect_error=str(e))
    if on_collected is not None:
        try:
            await on_collected()
        except EvidenceCaptureFailed as e:
            # Evidence capture failed: DO NOT clean up (see docstring).
            return FinalizationResult(
                execution, archive_error=str(e), collect_succeeded=True
            )
    cleanup_error: str | None = None
    try:
        await handle.cleanup()
    except Exception as e:
        cleanup_error = str(e)
    return FinalizationResult(
        execution,
        cleanup_error=cleanup_error,
        collect_succeeded=True,
        cleanup_attempted=True,
    )


class _Finalizable(Protocol):
    """Structural type for entities that own a handle and a finalize task."""

    handle: TaskHandle
    finalize_task: "asyncio.Task[FinalizationResult] | None"


def ensure_finalize_task(
    running: _Finalizable,
    *,
    on_terminal: _Callback = None,
    on_collected: _Callback = None,
) -> "asyncio.Task[FinalizationResult]":
    """Create the single finalization task for a running entity (idempotent)."""
    if running.finalize_task is None:
        running.finalize_task = asyncio.create_task(
            finalize_handle(
                running.handle, on_terminal=on_terminal, on_collected=on_collected
            )
        )
    return running.finalize_task
