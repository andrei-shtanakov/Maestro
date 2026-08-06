"""Operator-initiated workstream rework (#124) — validation and addendum.

Spec: docs/superpowers/specs/2026-08-05-workstream-rework-design.md.
The DB transaction itself lives in `Database.record_workstream_rework`;
this module holds everything that runs BEFORE it (liveness proof, refresh
validation, HEAD reading — all fail-closed via `ReworkRefused`, leaving
zero trace on refusal) and the addendum builder used by the
orchestrator's resume dispatch.
"""

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from maestro.models import OrchestratorConfig, Workstream


if TYPE_CHECKING:
    from maestro.database import Database


@dataclass(frozen=True)
class RefreshEvidence:
    """Audit evidence of a `--refresh-from` description/scope refresh."""

    config_path: str
    config_hash: str  # sha256 over the exact bytes parsed
    old_description: str
    new_description: str
    old_scope: list[str]
    new_scope: list[str]


class ReworkRefused(Exception):
    """Fail-closed refusal; message is operator-facing."""


def _is_pid_alive(pid: int) -> bool:
    """Signal-0 liveness probe; mirrors orchestrator._is_pid_alive.

    Duplicated 4-liner (importing maestro.orchestrator here would pull the
    whole orchestration stack into the CLI path); keep semantics in sync.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def prove_no_live_process(db: "Database", ws: Workstream) -> str | None:
    """Positive liveness proof (#124): raise ReworkRefused unless NO process
    of the previous attempt can still be running.

    Three conditions, all required (pid-NULL alone is insufficient —
    recovery clears pids when parking live-orphan/uncertain workstreams):
    both pid fields NULL; no open execution handle; no unresolved
    recovery-ambiguity marker. A marker with a preserved pid is re-probed:
    proven dead passes (evidence JSON returned for the audit row), alive
    refuses; a marker with no probeable evidence (spawn_uncertain /
    live_handle) refuses until `maestro workstream-resolve-ambiguity`.
    """
    if ws.process_pid is not None or ws.generation_pid is not None:
        raise ReworkRefused(
            f"workstream '{ws.id}' has a recorded pid "
            f"(process_pid={ws.process_pid}, generation_pid="
            f"{ws.generation_pid}) — a process may be live or a spawn may "
            "be in progress; wait for recovery or investigate"
        )
    open_handles = [
        h
        for h in await db.get_open_execution_handles()
        if h.get("entity_kind") == "workstream" and h.get("entity_id") == ws.id
    ]
    if open_handles:
        ids = ", ".join(str(h.get("execution_id")) for h in open_handles)
        raise ReworkRefused(
            f"workstream '{ws.id}' has an open execution handle ({ids}) — "
            "a durable execution may still be live; let recovery/GC "
            "reconcile it first"
        )
    if ws.recovery_ambiguity is None:
        return None
    try:
        marker = json.loads(ws.recovery_ambiguity)
    except json.JSONDecodeError as exc:
        raise ReworkRefused(
            f"workstream '{ws.id}' carries an unreadable recovery-ambiguity "
            f"marker ({exc}); resolve it explicitly via "
            "`maestro workstream-resolve-ambiguity`"
        ) from exc
    pid = marker.get("pid")
    # Strictly-positive real pid only: bool is an int subclass, and a
    # corrupted marker with pid<=0 would probe "dead" — non-evidence must
    # refuse, never pass (fail closed).
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ReworkRefused(
            f"workstream '{ws.id}' was parked by recovery with no probeable "
            f"evidence (kind={marker.get('kind')!r}) — verify by hand and "
            "resolve via `maestro workstream-resolve-ambiguity`"
        )
    if _is_pid_alive(pid):
        raise ReworkRefused(
            f"workstream '{ws.id}': recovery-preserved pid {pid} is still "
            "alive — clean it up before reworking"
        )
    return json.dumps(
        {
            "probe": "pid",
            "pid": pid,
            "alive": False,
            "checked_at": datetime.now(UTC).isoformat(),
        }
    )


async def read_head_sha(worktree: Path) -> str:
    """HEAD sha of the worktree, fail-closed on any doubt."""
    if not worktree.exists():  # noqa: ASYNC240 — one fast stat, CLI context
        raise ReworkRefused(f"worktree missing: {worktree}")
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(worktree),
        "rev-parse",
        "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise ReworkRefused(
            f"cannot determine HEAD of {worktree}: {detail or 'git failed'}"
        )
    return stdout.decode().strip()


def validate_refresh(ws: Workstream, config_path: Path) -> RefreshEvidence | None:
    """Validate a `--refresh-from` request BEFORE the transaction.

    Reads the config file ONCE; the audit hash covers the exact bytes that
    are parsed. Only the same-ID workstream's description/scope may change;
    topology fields (`depends_on`, `priority`) refuse. A changed scope is
    re-validated (normalization + preflight overlap against the config's
    other workstreams). Returns None when nothing changed.
    """
    try:
        data = config_path.read_bytes()
    except OSError as exc:
        raise ReworkRefused(f"cannot read {config_path}: {exc}") from exc
    config_hash = hashlib.sha256(data).hexdigest()
    config = _parse_config_bytes(data, config_path)

    entry = next((w for w in config.workstreams if w.id == ws.id), None)
    if entry is None:
        raise ReworkRefused(f"{config_path} contains no workstream with id '{ws.id}'")
    if sorted(entry.depends_on) != sorted(ws.depends_on):
        raise ReworkRefused(
            "refusing to change depends_on via rework — topology edits "
            "require re-validating the whole DAG; edit the config and "
            "re-orchestrate instead"
        )
    if entry.priority != ws.priority:
        raise ReworkRefused(
            "refusing to change priority via rework — topology edits "
            "require re-validating the whole DAG"
        )
    if entry.description == ws.description and list(entry.scope) == list(ws.scope):
        return None
    if list(entry.scope) != list(ws.scope):
        _validate_refreshed_scope(config, ws.id)
    return RefreshEvidence(
        config_path=str(config_path),
        config_hash=config_hash,
        old_description=ws.description,
        new_description=entry.description,
        old_scope=list(ws.scope),
        new_scope=list(entry.scope),
    )


def _parse_config_bytes(data: bytes, source: Path) -> OrchestratorConfig:
    """Parse an OrchestratorConfig from the exact bytes that were hashed."""
    from maestro.config import ConfigError, resolve_env_vars

    try:
        raw = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise ReworkRefused(f"invalid YAML in {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ReworkRefused(f"{source} is not a YAML mapping")
    try:
        resolved = resolve_env_vars(raw, source)
        return OrchestratorConfig(**resolved)
    except (ValidationError, ConfigError) as exc:
        raise ReworkRefused(f"invalid config {source}: {exc}") from exc


def _validate_refreshed_scope(config: OrchestratorConfig, ws_id: str) -> None:
    """Run the refreshed scope through the normal preflight validation.

    Any error naming this workstream, or a warning-severity scope-overlap
    involving it (a DAG-ordered overlap is info and passes), refuses.
    """
    from maestro.preflight import validate_project

    report = validate_project(config, check_fs=False)
    for issue in report.issues:
        if ws_id not in issue.workstream_ids:
            continue
        if issue.severity == "error":
            raise ReworkRefused(f"refreshed config invalid: {issue.message}")
        if issue.severity == "warning" and issue.code == "scope-overlap":
            raise ReworkRefused(
                f"refreshed scope overlaps a concurrent workstream: {issue.message}"
            )


def build_operator_rework_addendum(reason_row: dict[str, Any]) -> str | None:
    """Author-facing addendum from the audit row's `instructions` ONLY.

    `reason` is the operator's immutable audit explanation and never
    enters the prompt (#124).
    """
    instructions = reason_row.get("instructions")
    if not instructions:
        return None
    return (
        "## Operator rework instructions\n\n"
        "The previous attempt was rejected by the operator. Apply the\n"
        "following instructions in this attempt:\n\n"
        f"{instructions}\n"
    )
