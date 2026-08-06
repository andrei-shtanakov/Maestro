"""`maestro review-pr` — pure helpers for the post-PR review wrapper.

Implements the approved design
`docs/superpowers/specs/2026-08-06-post-pr-review-command-design.md`
(revision 3). This module holds the side-effect-free half: the
collision-free review-workspace key, PR-URL parsing, the upstream
report contract, precondition classification, and the exit mapping.
Workspace/lock/push mechanics live in `maestro/review_workspace.py`;
the command itself in `maestro/cli.py`.

Upstream boundary: `spec-runner review-pr <url> --json` (>= 2.21.0,
spec-runner #116/#117) emits **exactly one JSON document on stdout** on
every exit path, and the payload carries `exit_code` so a stored report
is self-describing. Exits: 0 complete, 1 fail-closed, 2 NEEDS_HUMAN.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError


__all__ = [
    "MIN_SPEC_RUNNER_VERSION",
    "PrRef",
    "ReviewReport",
    "classify_precondition",
    "outcome_for_exit",
    "parse_pr_url",
    "repo_key",
]

# The json-purity release (spec-runner #116/#117). Command-scoped floor:
# higher than Mode-2's own `SPEC_RUNNER_REQUIRED_VERSION` because a
# verbatim-stored report is unparseable without it.
MIN_SPEC_RUNNER_VERSION = "2.21.0"

_PR_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)"
    r"(?:[/#?].*)?$"
)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

Precondition = Literal["ready", "continuation", "dirty", "diverged"]
Outcome = Literal["complete", "needs_human", "infra_error"]


def repo_key(owner_repo: str) -> str:
    """Filesystem key for a repository — sanitized slug plus a short hash.

    Sanitization alone collides (`a-b/c` and `a/b-c` both become
    `a-b-c`), and a collision would let two repositories share one
    review workspace and one durable state directory.
    """
    slug = _UNSAFE.sub("-", owner_repo.replace("/", "-")).strip("-")
    digest = hashlib.sha256(owner_repo.encode()).hexdigest()[:8]
    return f"{slug}-{digest}"


class PrRef(BaseModel):
    """A parsed GitHub PR reference."""

    model_config = ConfigDict(frozen=True)

    owner: str
    repo: str
    number: int
    canonical_url: str

    @property
    def owner_repo(self) -> str:
        """`owner/repo`, the canonical repository identity."""
        return f"{self.owner}/{self.repo}"


def parse_pr_url(url: str) -> PrRef:
    """Parse a GitHub PR URL into its canonical reference.

    Tolerates trailing paths/fragments (`/files`, `#issuecomment-…`) and
    normalizes them away — the canonical URL is what gets passed to
    spec-runner as the positional argument.

    Raises:
        ValueError: If the string is not a GitHub PR URL.
    """
    match = _PR_URL_RE.match(url.strip())
    if match is None:
        msg = f"not a GitHub PR URL: {url!r}"
        raise ValueError(msg)
    owner = match.group("owner")
    repo = match.group("repo")
    number = int(match.group("number"))
    return PrRef(
        owner=owner,
        repo=repo,
        number=number,
        canonical_url=f"https://github.com/{owner}/{repo}/pull/{number}",
    )


class ReviewReport(BaseModel):
    """The `spec-runner review-pr --json` document (>= 2.21.0).

    One model covers both shapes: the full report (exit 0/2) and the
    fail-closed one (exit 1: `{repo, pr_number, error, exit_code}` with
    `repo`/`pr_number` possibly null).
    """

    model_config = ConfigDict(extra="forbid")

    repo: str | None = None
    pr_number: int | None = None
    exit_code: int
    head_sha: str | None = None
    new_comments: int | None = None
    comments: list[dict] | None = None
    counts: dict[str, int] | None = None
    needs_human: bool | None = None
    error: str | None = None


def validate_report(raw: str, *, process_exit: int) -> ReviewReport | str:
    """Parse the report, or return an error string (never raises).

    Fail-closed checks: stdout must be exactly one JSON document (the
    spec-runner #116 guarantee — anything else means an unsupported
    version or a broken run), the `exit_code` must be a known value, and
    it must agree with the process's own exit code (a self-describing
    report that contradicts reality is not storable evidence).
    """
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        return f"report is not a single JSON document: {exc}"
    try:
        report = ReviewReport.model_validate(decoded)
    except ValidationError as exc:
        return f"report does not match the spec-runner contract: {exc}"
    if report.exit_code not in (0, 1, 2):
        return f"unknown exit_code in report: {report.exit_code}"
    if report.exit_code != process_exit:
        return (
            f"report exit_code {report.exit_code} disagrees with the "
            f"process exit {process_exit}"
        )
    return report


def classify_precondition(
    *, local_head: str, remote_head: str, ancestor: bool, dirty: bool
) -> Precondition:
    """Classify a restored review workspace (spec §3.1).

    - `ready` — local HEAD == remote head: hand straight to spec-runner.
    - `continuation` — local is ahead of an unchanged remote (fix
      commits whose push failed): Maestro pushes them first (§3.1.4).
    - `dirty` — uncommitted changes: refuse; the operator commits them
      (forming a continuation) or discards via `--discard-local`.
    - `diverged` — remote moved off our base (force-push): refuse.

    `dirty` outranks `continuation`: spec-runner refuses to mutate a
    dirty tree, so a dirty continuation is not usable as-is either.
    """
    if dirty:
        return "dirty"
    if local_head == remote_head:
        return "ready"
    return "continuation" if ancestor else "diverged"


def outcome_for_exit(exit_code: int) -> Outcome:
    """Map a spec-runner exit code onto the audit outcome (spec §5.1)."""
    if exit_code == 0:
        return "complete"
    if exit_code == 2:
        return "needs_human"
    return "infra_error"
