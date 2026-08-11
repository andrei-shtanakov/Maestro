"""Post-mortem archive capture (#164, spec §6).

One worktree's executor evidence — the state database and the harness logs —
copied out of the worktree before anything destroys it. Two callers depend on
this being the *only* moment the data is reachable:

- **the completeness gate**, which reads the archived snapshot rather than a
  live worktree, so local and SSH runs take one code path instead of two;
- **the operator**, for whom this is the entire post-factum record. In the
  incident that motivated #164 the worktree cleanup destroyed
  `spec/.executor-<prefix>logs/`, and the cause of a premature `exit 0` became
  unknowable.

Capture is therefore fail-closed: `PostmortemCaptureError` means the caller
must not clean anything up (spec §6.5).
"""

import json
import logging
import re
import shutil
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from maestro.models import SPEC_PREFIX, PostmortemConfig


logger = logging.getLogger(__name__)


MANIFEST_FILENAME = "manifest.json"
MANIFEST_SCHEMA = "maestro.postmortem-manifest/v1"
STATE_FILENAME = "executor-state.db"
LOGS_DIRNAME = "logs"
_PARTIAL_SUFFIX = ".partial"


class PostmortemCaptureError(Exception):
    """Capture failed — the caller must preserve the workspace it was about
    to destroy, and must not treat the archive as an available gate input."""


@dataclass(frozen=True)
class ArchiveResult:
    """A committed archive: the directory exists and is complete."""

    path: Path
    bytes_written: int
    truncated: bool
    logs_omitted: int


def capture_archive(
    *,
    spec_dir: Path,
    root: Path,
    identity: Mapping[str, Any],
    counters: Mapping[str, int | None],
    config: PostmortemConfig,
    prefix: str = SPEC_PREFIX,
) -> ArchiveResult:
    """Copy the executor evidence for one execution into a committed archive.

    Everything is written into a sibling `…{_PARTIAL_SUFFIX}/` directory and
    committed with a single `os.replace()` of the directory, which is atomic
    within a filesystem. A crash therefore leaves either an ignorable
    `.partial/` or a complete archive — never a half-written directory that
    looks finished.

    Args:
        spec_dir: The worktree's `spec/` directory (post-collect for ssh).
        root: Archive root, anchored to the DB directory by the caller —
            never to the process cwd (spec §6.1).
        identity: Run identity recorded verbatim into the manifest.
        counters: `done` / `planned` / `noop_done` / `state_total`.
        config: Retention policy (byte cap).
        prefix: spec-runner's `spec_prefix`, which names both artifacts.

    A missing state database is **not** a capture failure. It is recorded as
    `state_missing: true` and the archive is committed with whatever else
    exists, because capture runs for failed runs too: a spec-runner that died
    before creating its database must still keep its retry path, and turning
    that into a capture failure would convert a retryable FAILED into
    NEEDS_REVIEW. The completeness gate sees the absent counters and fails
    closed on its own, which is the correct place for that decision.

    Raises:
        PostmortemCaptureError: The archive itself could not be written
            (unwritable root, no space, an unreadable state database).
            Nothing is committed.
    """
    state_db = spec_dir / f".executor-{prefix}state.db"
    workstream_id = str(identity["workstream_id"])
    final = root / workstream_id / _archive_dirname(identity)
    partial = final.with_name(final.name + _PARTIAL_SUFFIX)

    try:
        if partial.exists():
            shutil.rmtree(partial)
        partial.mkdir(parents=True)
        state_missing = not state_db.is_file()
        if not state_missing:
            _snapshot_state(state_db, partial / STATE_FILENAME)
        else:
            logger.warning("no executor state db at %s; archiving logs only", state_db)
        written, truncated, omitted = _copy_logs(
            spec_dir / f".executor-{prefix}logs",
            partial / LOGS_DIRNAME,
            budget=config.max_archive_bytes,
        )
        manifest = _build_manifest(
            identity,
            counters,
            bytes_written=written,
            truncated=truncated,
            logs_omitted=omitted,
            state_missing=state_missing,
        )
        (partial / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
        if final.exists():
            shutil.rmtree(final)
        partial.replace(final)
    except PostmortemCaptureError:
        _discard(partial)
        raise
    except Exception as exc:
        _discard(partial)
        msg = f"post-mortem capture failed for {workstream_id}: {exc}"
        raise PostmortemCaptureError(msg) from exc

    return ArchiveResult(
        path=final,
        bytes_written=written,
        truncated=truncated,
        logs_omitted=omitted,
    )


def archive_is_committed(path: Path | str) -> bool:
    """True only for a complete archive that is actually on disk.

    The cleanup guard's question. A row in `postmortem_archives` is not the
    answer: the row can outlive the directory (a hand-pruned archive, a
    volume restored from an older snapshot), and cleanup destroys the last
    copy of the logs — so it asks the filesystem, not the bookkeeping.

    The manifest is the commit marker: `capture_archive` writes it inside the
    `.partial/` directory before the atomic rename, so a directory holding
    one is by construction a fully written archive. A `.partial/` name is
    rejected outright even if it looks complete — it never survived the
    rename, so nothing may be concluded from its contents.
    """
    archive = Path(path)
    if archive.name.endswith(_PARTIAL_SUFFIX):
        return False
    return archive.is_dir() and (archive / MANIFEST_FILENAME).is_file()


def read_manifest(archive: Path | str) -> dict[str, Any] | None:
    """Load a committed archive's manifest, or None when unreadable.

    This is the completeness gate's input: it reads the numbers the capture
    recorded rather than re-reading a worktree, which is what keeps local and
    ssh runs on one code path. None means the gate fails closed — an
    unreadable manifest is not an empty one.
    """
    path = Path(archive) / MANIFEST_FILENAME
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("unreadable post-mortem manifest %s: %s", path, exc)
        return None
    return loaded if isinstance(loaded, dict) else None


def prune_archives(root: Path, workstream_id: str, *, keep: int) -> list[Path]:
    """Remove archives for `workstream_id` beyond the newest `keep`.

    Called only AFTER a fresh archive is committed, so a failure here can
    never cost the evidence that was just captured. `.partial/` directories
    are ignored — they are crash garbage, never candidates for retention.

    Returns:
        The paths removed (empty when nothing needed pruning).
    """
    workstream_dir = root / workstream_id
    if not workstream_dir.is_dir():
        return []
    archives = sorted(
        (
            p
            for p in workstream_dir.iterdir()
            if p.is_dir() and not p.name.endswith(_PARTIAL_SUFFIX)
        ),
        key=lambda p: p.name,
    )
    doomed = archives[: max(0, len(archives) - keep)]
    removed: list[Path] = []
    for path in doomed:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            logger.warning("post-mortem prune failed for %s: %s", path, exc)
            continue
        removed.append(path)
    return removed


def _archive_dirname(identity: Mapping[str, Any]) -> str:
    """`<utc-compact>-<execution_id>` — keyed to one execution attempt.

    Sorting by this name is chronological, which is what `prune_archives`
    relies on instead of stat times (a copy or restore rewrites mtimes).
    """
    captured = str(identity["captured_at"])
    compact = captured.replace("-", "").replace(":", "").replace(".", "")
    return f"{compact}-{identity['execution_id']}"


def _snapshot_state(src: Path, dst: Path) -> None:
    """Consistent copy of a live WAL database.

    A byte-wise copy of `.db` without its `-wal` is not a snapshot; opening
    the source read-only and using `sqlite3.Connection.backup()` yields a
    point-in-time consistent database that opens on its own. Mirrors
    `execution/ssh_mirror.snapshot_locally`, which solved this for the
    progress mirror.
    """
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(str(dst))
    try:
        with target:
            source.backup(target)
    finally:
        target.close()
        source.close()


def _copy_logs(src: Path, dst: Path, *, budget: int) -> tuple[int, bool, int]:
    """Copy log files newest-first until `budget` bytes are spent.

    Newest-first because the tail of a run explains why it stopped; an
    alphabetical or oldest-first walk would spend the budget on the part an
    operator needs least. Returns `(bytes_written, truncated, omitted)`.
    """
    dst.mkdir(parents=True, exist_ok=True)
    if not src.is_dir():
        return (0, False, 0)

    files = sorted(
        (p for p in src.iterdir() if p.is_file()),
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )
    written = 0
    omitted = 0
    for path in files:
        size = path.stat().st_size
        if written + size > budget:
            omitted += 1
            continue
        shutil.copy2(path, dst / path.name)
        written += size
    return (written, omitted > 0, omitted)


def _build_manifest(
    identity: Mapping[str, Any],
    counters: Mapping[str, int | None],
    *,
    bytes_written: int,
    truncated: bool,
    logs_omitted: int,
    state_missing: bool = False,
) -> dict[str, Any]:
    """The archive's self-describing record.

    Self-describing on purpose: an operator holding only the directory can
    reconstruct what ran and what the gate saw, without the Maestro DB.
    """
    done = counters.get("done")
    noop_done = counters.get("noop_done")
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "done": done,
        "planned": counters.get("planned"),
        "noop_done": noop_done,
        "state_total": counters.get("state_total"),
        "all_no_op": bool(done) and done == noop_done,
        "state_missing": state_missing,
        "bytes_written": bytes_written,
        "truncated": truncated,
        "logs_omitted": logs_omitted,
    }
    manifest.update(identity)
    return manifest


def _discard(partial: Path) -> None:
    """Best-effort removal of a failed capture's staging directory."""
    if not partial.exists():
        return
    try:
        shutil.rmtree(partial)
    except OSError as exc:
        logger.warning("could not remove partial archive %s: %s", partial, exc)


RECAPTURE_MARKER_PREFIX = "maestro:recapture-required"

_RECAPTURE_RE = re.compile(
    re.escape(RECAPTURE_MARKER_PREFIX) + r" execution=([A-Za-z0-9._:-]{1,128})"
)


def build_recapture_marker(execution_id: str) -> str:
    """Durable "retry the capture, nothing else" token (#164).

    A failed capture leaves the workstream in NEEDS_REVIEW with no approval
    marker — correct, since there is no result to approve, only an archive
    root to fix. But without a second token that state is an operational dead
    end: a plain requeue falls through to the full respawn and re-runs the
    work the operator was trying to preserve. This names the execution whose
    evidence still needs capturing, so `maestro workstream-recapture` can
    retry exactly that and nothing more.
    """
    return f"{RECAPTURE_MARKER_PREFIX} execution={execution_id}"


def parse_recapture_marker(error_message: str | None) -> str | None:
    """Extract the execution id from a recapture token, or None."""
    if not error_message:
        return None
    match = _RECAPTURE_RE.search(error_message)
    return match.group(1) if match else None
