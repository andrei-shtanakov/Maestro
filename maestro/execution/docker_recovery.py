"""Fail-closed recovery classification for docker-backed executions.

`probe_execution` answers a single question during crash recovery: "is it
safe to silently re-READY this entity, or might a container from the
previous attempt still be alive?" Any ambiguity (a found container in any
state, more than one match, a label mismatch, or a docker-daemon error)
answers "no" — the caller must route the entity to human review instead of
quietly re-running it over a possibly-live container.

`gc_terminal_handle` is a separate, narrower operation: for a handle whose
*entity* has already reached a terminal status (finalize ran; only the
container cleanup confirmation is missing), it performs an ownership-checked
`docker rm` so leftover containers don't accumulate. It never changes entity
status — callers are responsible for updating the handle's persisted state.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DockerProbe(Protocol):
    """Structural type for the docker operations recovery needs.

    Matches `DockerCli`'s async signatures for `ps_ids_by_label`/`inspect`/
    `rm` without depending on the concrete class, so daemon-free fakes in
    tests satisfy the type checker without subclassing.
    """

    async def ps_ids_by_label(self, key: str, value: str) -> list[str]: ...

    async def inspect(self, name: str) -> dict[str, Any] | None: ...

    async def rm(self, name: str) -> None: ...


def labels_match(
    actual: Mapping[str, str | None], expected: Mapping[str, str | None]
) -> bool:
    """True iff every expected label is present on `actual` with an equal,
    non-None value. Used for full-set ownership verification (Phase 2c).

    Defined here (rather than in `ssh_docker_probe.py`) so this module never
    needs to import the ssh-specific module; `ssh_docker_probe` re-exports
    this function instead.
    """
    for key, value in expected.items():
        if value is None or actual.get(key) != value:
            return False
    return True


@dataclass
class RecoveryVerdict:
    """Outcome of probing whether a container may still be live.

    Attributes:
        needs_review: True if the entity must be routed to human review
            instead of being silently re-queued.
        reason: Short human-readable explanation of the verdict.
    """

    needs_review: bool
    reason: str


async def probe_execution(
    execution_id: str,
    docker: DockerProbe,
    expected_labels: Mapping[str, str] | None = None,
) -> RecoveryVerdict:
    """Classify whether an execution is safe to silently re-queue.

    Fails closed: any confirmed container (in any state — running, exited,
    dead, paused, restarting), any ambiguity (more than one match, or a
    label mismatch on the single match), or a docker-daemon/probe error all
    result in `needs_review=True`. Only a clean "no container found" answer
    proceeds.

    Args:
        execution_id: The `maestro.execution_id` label value to probe for
            (also the unique key used to find the container).
        docker: Docker CLI wrapper (injectable for tests).
        expected_labels: When provided and non-empty, the full label set
            (Phase 2c, SSH+Docker path) the found container must match via
            `labels_match` — not just `maestro.execution_id`. When
            None/empty (the local-docker path, which has no persisted full
            set), the check falls back to single-`execution_id` equality.

    Returns:
        RecoveryVerdict describing whether review is required and why.
    """
    try:
        ids = await docker.ps_ids_by_label("maestro.execution_id", execution_id)
    except Exception as e:
        return RecoveryVerdict(True, f"probe failed: {e}")
    if not ids:
        return RecoveryVerdict(False, "no container found")
    if len(ids) > 1:
        return RecoveryVerdict(True, f"ambiguous: {len(ids)} containers")
    try:
        info = await docker.inspect(ids[0])
    except Exception as e:
        return RecoveryVerdict(True, f"inspect failed: {e}")
    labels = (info or {}).get("Config", {}).get("Labels") or {}
    if expected_labels:
        if not labels_match(labels, expected_labels):
            return RecoveryVerdict(True, "label mismatch on found container")
    elif labels.get("maestro.execution_id") != execution_id:
        return RecoveryVerdict(True, "label mismatch on found container")
    return RecoveryVerdict(True, "live/leftover container found")


async def gc_terminal_handle(
    row: dict[str, Any],
    docker: DockerProbe,
    expected_labels: Mapping[str, str] | None = None,
) -> str:
    """Best-effort, ownership-checked GC for a `terminal` execution handle.

    The entity behind `row` has already reached a terminal status (finalize
    ran); this only cleans up the leftover container, if any. It never
    raises: probe/inspect/rm failures are reported in the returned outcome
    string instead, so a caller sweeping many rows can continue past one
    that fails. Callers should only persist a `"cleaned"` state transition
    when the outcome indicates nothing is left to clean (`"no container
    found"` or `"removed"`) — an `"ambiguous"`, `"mismatch"`, or `"gc
    failed"` outcome means the row should be left as `terminal` for the
    next sweep / a human to retry.

    Args:
        row: An `execution_handles` row dict (as returned by
            `Database.get_open_execution_handles()`) with at least
            `execution_id`.
        docker: Docker CLI wrapper (injectable for tests).
        expected_labels: When provided and non-empty, the full label set
            the found container must match via `labels_match` (Phase 2c,
            SSH+Docker path) instead of the single-`execution_id` check
            used on the local-docker path.

    Returns:
        One of: `"no container found"`, `"removed"`,
        `"ambiguous: N containers, skipped"`, `"label mismatch, skipped"`,
        or `"gc failed: <error>"`.
    """
    execution_id = row["execution_id"]
    try:
        ids = await docker.ps_ids_by_label("maestro.execution_id", execution_id)
    except Exception as e:
        return f"gc failed: {e}"
    if not ids:
        return "no container found"
    if len(ids) > 1:
        return f"ambiguous: {len(ids)} containers, skipped"
    try:
        info = await docker.inspect(ids[0])
    except Exception as e:
        return f"gc failed: {e}"
    labels = (info or {}).get("Config", {}).get("Labels") or {}
    if expected_labels:
        if not labels_match(labels, expected_labels):
            return "label mismatch, skipped"
    elif labels.get("maestro.execution_id") != execution_id:
        return "label mismatch, skipped"
    try:
        await docker.rm(ids[0])
    except Exception as e:
        return f"gc failed: {e}"
    return "removed"


GC_CLEAN_OUTCOMES = frozenset({"no container found", "removed"})
"""Outcomes of `gc_terminal_handle` after which the handle is safe to mark
`cleaned` — nothing docker-side is left to account for."""
