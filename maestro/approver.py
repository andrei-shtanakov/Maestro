"""approver_cmd hook (#137): contract models, guards, bounded runner.

Implements the approved design
`docs/superpowers/specs/2026-08-06-expost-approver-cmd-design.md`
(revision 4). The hook is an automated operator over the existing
approval API — this module holds the pure/contract half: request
envelope (`maestro.approval-request/v1`), verdict document validation
with the strict run-keyed handshake (`maestro.approval-verdict/v1`),
and the bounded subprocess runner. Orchestrator wiring (guards,
scheduling, the PASS transaction) lives in `maestro/orchestrator.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


logger = logging.getLogger(__name__)

APPROVAL_REQUEST_SCHEMA = "maestro.approval-request/v1"
APPROVAL_VERDICT_SCHEMA = "maestro.approval-verdict/v1"

# §5.4 field limits — over-limit is a protocol ERROR, never truncation.
MAX_SUMMARY_CHARS = 2000
MAX_FINDINGS = 50
MAX_FINDING_DETAIL_CHARS = 4000
MAX_CRITICS = 8
MAX_NAME_CHARS = 200
STDERR_TAIL_CHARS = 500


class ApproverFinding(BaseModel):
    """One critic finding inside the verdict document."""

    model_config = ConfigDict(extra="forbid")

    severity: str = Field(max_length=50)
    title: str = Field(max_length=500)
    detail: str = Field(default="", max_length=MAX_FINDING_DETAIL_CHARS)


class ApproverCritic(BaseModel):
    """One critic's identity and individual verdict."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=MAX_NAME_CHARS)
    harness: str = Field(max_length=MAX_NAME_CHARS)
    model: str = Field(max_length=MAX_NAME_CHARS)
    verdict: Literal["PASS", "FAIL", "ERROR"]


class ApprovalVerdict(BaseModel):
    """The verdict document (`maestro.approval-verdict/v1`), §5.3."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str = Field(alias="schema")
    approval_run_id: str
    workstream_id: str
    phase: str
    sha: str
    verdict: Literal["PASS", "FAIL", "ERROR"]
    summary: str = Field(default="", max_length=MAX_SUMMARY_CHARS)
    findings: list[ApproverFinding] = Field(
        default_factory=list, max_length=MAX_FINDINGS
    )
    critics: list[ApproverCritic] = Field(default_factory=list, max_length=MAX_CRITICS)
    cost_usd: float | None = Field(default=None, ge=0)


class AuthorInfo(BaseModel):
    """Workstream author provenance carried in the block context."""

    model_config = ConfigDict(extra="forbid")

    harness: str
    model: str | None = None


class BlockContext(BaseModel):
    """Durable snapshot of what the ex-post gate saw (§7.1).

    Persisted at parking time into `gate_block_contexts`; the request
    envelope is built ONLY from this — never recomputed on resume.
    """

    model_config = ConfigDict(extra="forbid")

    tier: str | None = None
    flags: list[str] = Field(default_factory=list)
    block_reason: str
    declared_scope: list[str] = Field(default_factory=list)
    changed_paths: list[str] = Field(default_factory=list)
    escaped_paths: list[str] = Field(default_factory=list)
    author: AuthorInfo


class EchoFields(BaseModel):
    """The four run-keyed identity fields the document must echo (§5.3)."""

    model_config = ConfigDict(frozen=True)

    approval_run_id: str
    workstream_id: str
    phase: str
    sha: str


def validate_verdict(
    raw: bytes | str,
    expected: EchoFields,
    *,
    author_model: str | None,
) -> ApprovalVerdict | str:
    """Validate stdout bytes into a verdict, or return a protocol-error text.

    Applies the §5.3 handshake: strict JSON (no trailing garbage), the
    document schema with §5.4 field limits, exact echo of all four
    identity fields, non-empty critics, and declared-provenance
    independence (no critic model equal to the author's model — a
    validation of what the command declares, not proof; `author_model
    is None` passes vacuously).
    """
    try:
        text = raw.decode() if isinstance(raw, bytes) else raw
        decoded = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"protocol error: stdout is not a single JSON document: {exc}"
    try:
        doc = ApprovalVerdict.model_validate(decoded)
    except ValidationError as exc:
        return f"protocol error: invalid verdict document: {exc.error_count()} issue(s): {exc}"
    if doc.schema_version != APPROVAL_VERDICT_SCHEMA:
        return (
            f"protocol error: unexpected schema {doc.schema_version!r} "
            f"(want {APPROVAL_VERDICT_SCHEMA!r})"
        )
    for field in ("approval_run_id", "workstream_id", "phase", "sha"):
        got = getattr(doc, field)
        want = getattr(expected, field)
        if got != want:
            return f"protocol error: echo mismatch on {field}: {got!r} != {want!r}"
    if not doc.critics:
        return "protocol error: critics must be non-empty"
    if author_model is not None:
        offenders = [c.name for c in doc.critics if c.model == author_model]
        if offenders:
            return (
                "protocol error: declared critic model equals the author "
                f"model ({author_model!r}): {', '.join(offenders)}"
            )
    return doc


def build_request_envelope(
    context: BlockContext,
    *,
    approval_run_id: str,
    workstream_id: str,
    phase: str,
    sha: str,
    base_branch: str,
    diff: str,
    worktree: str,
    auto_approvals_used: int,
    evaluations_used: int,
) -> dict[str, object]:
    """Render the `maestro.approval-request/v1` envelope (§5.1).

    Decision context comes verbatim from the persisted `BlockContext`;
    only run identity and counters vary between evaluations of the same
    block.
    """
    return {
        "schema": APPROVAL_REQUEST_SCHEMA,
        "approval_run_id": approval_run_id,
        "workstream_id": workstream_id,
        "phase": phase,
        "sha": sha,
        "base_branch": base_branch,
        "tier": context.tier,
        "flags": list(context.flags),
        "block_reason": context.block_reason,
        "declared_scope": list(context.declared_scope),
        "changed_paths": list(context.changed_paths),
        "escaped_paths": list(context.escaped_paths),
        "diff": diff,
        "worktree": worktree,
        "author": context.author.model_dump(),
        "auto_approvals_used": auto_approvals_used,
        "evaluations_used": evaluations_used,
    }


@dataclass(frozen=True)
class CmdOutcome:
    """Result of one bounded approver_cmd execution (§5.4/§8).

    `stdout` is set only on a clean zero-exit run within bounds;
    `error` carries the failure class otherwise. `stderr_tail` is the
    truncated evidence-only tail — it must never reach the DB.
    """

    stdout: bytes | None
    stderr_tail: str
    error: str | None


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    """Read up to `limit` bytes; returns (data, overflowed)."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return b"".join(chunks), False
        total += len(chunk)
        if total > limit:
            chunks.append(chunk[: limit - total])  # keep exactly `limit`
            return b"".join(chunks), True
        chunks.append(chunk)


def _kill_group(process: asyncio.subprocess.Process) -> None:
    """Kill the command's whole process group (it spawns critics)."""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # already gone / not ours
        process.kill()


async def run_approver_cmd(
    argv: list[str],
    envelope_json: bytes,
    *,
    timeout_seconds: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    env: dict[str, str],
) -> CmdOutcome:
    """Run approver_cmd with bounded I/O and a wall-clock timeout (§8.2).

    argv exec (no shell), own session/process group, envelope on stdin.
    Timeout or stdout overflow kills the process group and yields a
    protocol-level error outcome; partial stdout is never interpreted.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return CmdOutcome(
            stdout=None, stderr_tail="", error=f"spawn: {type(exc).__name__}"
        )

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    async def _feed_stdin() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(envelope_json)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # command exited early; its exit code tells the story
        finally:
            with_suppress_close(process.stdin)

    async def _run() -> CmdOutcome:
        feed = asyncio.ensure_future(_feed_stdin())
        try:
            (stdout, out_over), (stderr, _err_over) = await asyncio.gather(
                _read_bounded(process.stdout, max_stdout_bytes),  # type: ignore[arg-type]
                _read_bounded(process.stderr, max_stderr_bytes),  # type: ignore[arg-type]
            )
            tail = stderr.decode(errors="replace")[-STDERR_TAIL_CHARS:]
            if out_over:
                _kill_group(process)
                await process.wait()
                return CmdOutcome(
                    stdout=None, stderr_tail=tail, error="stdout_overflow"
                )
            returncode = await process.wait()
            if returncode != 0:
                return CmdOutcome(
                    stdout=None, stderr_tail=tail, error=f"exit {returncode}"
                )
            return CmdOutcome(stdout=stdout, stderr_tail=tail, error=None)
        finally:
            feed.cancel()

    try:
        return await asyncio.wait_for(_run(), timeout=timeout_seconds)
    except TimeoutError:
        _kill_group(process)
        await process.wait()
        return CmdOutcome(stdout=None, stderr_tail="", error="timeout")


def with_suppress_close(stdin: asyncio.StreamWriter) -> None:
    """Close the child's stdin, ignoring an already-broken pipe."""
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        stdin.close()
