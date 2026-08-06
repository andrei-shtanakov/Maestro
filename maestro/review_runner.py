"""Runner for `maestro review-pr` — one PR's review cycle end to end.

Spec: `docs/superpowers/specs/2026-08-06-post-pr-review-command-design.md`
(revision 3). Owns the ordering the spec fixes: acquire the per-PR lock
→ prepare the workspace (materialize, push-recover a continuation) →
write the crash sentinel → invoke `spec-runner review-pr --json` →
validate the report → finalize the run record (CAS) → notify → apply
the retention policy.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import ulid

from maestro.notifications.base import Notification, NotificationEvent
from maestro.review_pr import (
    MIN_SPEC_RUNNER_VERSION,
    outcome_for_exit,
    parse_pr_url,
    validate_report,
)
from maestro.review_workspace import (
    AlreadyRunning,
    PreconditionError,
    PrLock,
    ReviewPaths,
    cleanup_after_run,
)
from maestro.spec_runner import parse_spec_runner_version


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from maestro.database import Database


logger = logging.getLogger(__name__)

__all__ = [
    "EXIT_ALREADY_RUNNING",
    "ReviewInvocation",
    "check_spec_runner_version",
    "invoke_spec_runner",
    "run_review",
]

# Maestro-specific exit code on top of spec-runner's 0/1/2 (spec §6).
EXIT_ALREADY_RUNNING = 3

_EVENT_BY_OUTCOME = {
    "complete": NotificationEvent.POST_PR_REVIEW_COMPLETE,
    "needs_human": NotificationEvent.POST_PR_REVIEW_NEEDS_HUMAN,
    "infra_error": NotificationEvent.POST_PR_REVIEW_ERROR,
}


class _Notifier(Protocol):
    async def notify(self, notification: Notification, /) -> Any: ...


@dataclass(frozen=True)
class ReviewInvocation:
    """Injectable `spec-runner review-pr` call (real one: `invoke_spec_runner`)."""

    call: Callable[..., Awaitable[tuple[int, str, str]]]

    async def __call__(self, **kwargs: Any) -> tuple[int, str, str]:
        return await self.call(**kwargs)


def check_spec_runner_version(output: str, *, returncode: int) -> str | None:
    """Command-scoped gate: spec-runner must be >= 2.21.0 (spec §7.1).

    Below that, `review-pr --json` can interleave diagnostics with the
    report on stdout (fixed upstream in #116/#117), so a stored report is
    not a reliable machine interface. Returns None when supported, or a
    human-readable problem description.
    """
    if returncode != 0:
        return (
            f"`spec-runner --version` exited with code {returncode}; "
            f"`maestro review-pr` requires >= {MIN_SPEC_RUNNER_VERSION}"
        )
    parsed = parse_spec_runner_version(output)
    if parsed is None:
        first = output.splitlines()[0] if output.splitlines() else ""
        return (
            f"unrecognized `spec-runner --version` output: {first!r}; "
            f"`maestro review-pr` requires >= {MIN_SPEC_RUNNER_VERSION}"
        )
    required = tuple(int(n) for n in MIN_SPEC_RUNNER_VERSION.split("."))
    if parsed >= required:
        return None
    found = ".".join(str(n) for n in parsed)
    return (
        f"the installed spec-runner is {found}; `maestro review-pr` requires "
        f">= {MIN_SPEC_RUNNER_VERSION} (that release made `review-pr --json` "
        "emit exactly one JSON document per exit path — spec-runner#116). "
        "Upgrade spec-runner (e.g. `uv tool upgrade spec-runner`)."
    )


async def invoke_spec_runner(
    *, workspace: Path, pr_url: str, state_db: Path
) -> tuple[int, str, str]:
    """Run `spec-runner review-pr <url> --json` in the review workspace.

    The durable state path is forced via `SPEC_RUNNER_STATE_FILE`-style
    config on the command line: spec-runner reads `state_file` from its
    config, so Maestro writes a minimal config into the workspace before
    the call (harness-owned keys only — §7).
    """
    config = workspace / "spec-runner.config.yaml"
    config.write_text(
        # Harness-owned invariants (§7): absolute state file OUTSIDE the
        # checkout, and the in-run post-PR stage off (Maestro drives the
        # loop externally; the stage would double-fire).
        f"state_file: {state_db}\nreview_pr:\n  post_pr: off\n",
        encoding="utf-8",
    )
    process = await asyncio.create_subprocess_exec(
        "spec-runner",
        "review-pr",
        pr_url,
        "--json",
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return (
        process.returncode or 0,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


async def run_review(
    *,
    db: Database,
    workstream_id: str,
    pr_url: str,
    repo_path: Path,
    paths: ReviewPaths,
    invocation: ReviewInvocation,
    prepare: Callable[..., Awaitable[tuple[Path, str]]],
    spec_runner_version: str | None,
    notifier: _Notifier | None = None,
    discard_local: bool = False,
) -> int:
    """Review one PR; returns the CLI exit code (0/1/2, or 3 when locked).

    `prepare` materializes the workspace and returns
    `(workspace, head_sha)` — injected so the git/GitHub mechanics stay
    testable in isolation.
    """
    ref = parse_pr_url(pr_url)
    try:
        lock = PrLock(paths)
        lock.__enter__()
    except AlreadyRunning as exc:
        # No run row: nothing ran (spec §6.1).
        logger.warning("review-pr: %s", exc)
        return EXIT_ALREADY_RUNNING
    try:
        try:
            workspace, head_sha = await prepare(
                repo_path=repo_path,
                paths=paths,
                ref=ref,
                discard_local=discard_local,
            )
        except PreconditionError as exc:
            # Fail-closed before the sentinel: nothing was invoked, and
            # the workspace/state are untouched — surfaced as infra.
            logger.error("review-pr: workspace not usable: %s", exc)
            await _notify(notifier, "infra_error", workstream_id, pr_url)
            return 1

        review_run_id = str(ulid.new())
        await db.insert_review_run(
            review_run_id,
            workstream_id=workstream_id,
            pr_url=ref.canonical_url,
            repo=ref.owner_repo,
            pr_number=ref.number,
            input_head_sha=head_sha,
            workspace_path=str(workspace),
            spec_runner_version=spec_runner_version,
        )

        exit_code, stdout, stderr = await invocation(
            workspace=workspace,
            pr_url=ref.canonical_url,
            state_db=paths.state_db,
        )
        report = validate_report(stdout, process_exit=exit_code)
        if isinstance(report, str):
            # An unusable report is an infrastructure failure regardless
            # of the process's own exit code — never store raw output.
            if stderr.strip():
                logger.debug("review-pr stderr tail: %s", stderr.strip()[-500:])
            await db.finalize_review_run(
                review_run_id,
                exit_code=1,
                outcome="infra_error",
                reason=report[:500],
                report_json=None,
                output_head_sha=None,
            )
            await _notify(notifier, "infra_error", workstream_id, pr_url)
            return 1

        outcome = outcome_for_exit(exit_code)
        await db.finalize_review_run(
            review_run_id,
            exit_code=exit_code,
            outcome=outcome,
            reason=report.error,
            report_json=json.dumps(report.model_dump(exclude_none=True)),
            output_head_sha=report.head_sha,
        )
        await _notify(notifier, outcome, workstream_id, pr_url)
        cleanup_after_run(repo_path=repo_path, paths=paths, exit_code=exit_code)
        return exit_code
    finally:
        lock.__exit__(None, None, None)


async def _notify(
    notifier: _Notifier | None, outcome: str, workstream_id: str, pr_url: str
) -> None:
    if notifier is None:
        return
    from maestro.models import WorkstreamStatus

    try:
        await notifier.notify(
            Notification(
                event=_EVENT_BY_OUTCOME[outcome],
                subject_id=workstream_id,
                subject_title=f"PR review ({outcome})",
                entity_kind="workstream",
                status=WorkstreamStatus.DONE,
                url=pr_url,
            )
        )
    except Exception:
        logger.exception("review-pr: notification failed")
