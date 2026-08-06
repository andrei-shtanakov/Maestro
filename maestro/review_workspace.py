"""Review workspace, per-PR lock, and push recovery for `maestro review-pr`.

Spec: `docs/superpowers/specs/2026-08-06-post-pr-review-command-design.md`
(revision 3), §3 (workspace + durable state), §3.1 (fail-closed
materialization and the Maestro-owned push recovery), §4 (retention),
§6 (lock).

The invariant this module exists for: spec-runner's resumable state
(never process a comment twice, never reply twice) lives in its
`state_file`, which defaults to *inside* the checkout. Here the state
directory is a sibling of the workspace, so a removed checkout never
takes the idempotency guarantee — or unpushed fix commits — with it.
"""

from __future__ import annotations

import fcntl
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from maestro.review_pr import PrRef, classify_precondition, repo_key


if TYPE_CHECKING:
    from types import TracebackType


__all__ = [
    "AlreadyRunning",
    "PrLock",
    "PrMeta",
    "PreconditionError",
    "ReviewPaths",
    "cleanup_after_run",
    "fetch_pr_meta",
    "materialize",
    "recover_push",
]

DEFAULT_ROOT = Path.home() / ".maestro"


class PreconditionError(RuntimeError):
    """A fail-closed materialization precondition was not met (§3.1)."""


class AlreadyRunning(RuntimeError):
    """Another process holds this PR's review lock (§6)."""


@dataclass(frozen=True)
class ReviewPaths:
    """Filesystem layout for one PR's review — workspace vs durable state."""

    workspace: Path
    state_dir: Path
    pr_number: int

    @classmethod
    def for_pr(cls, ref: PrRef, *, root: Path | None = None) -> ReviewPaths:
        """Layout for `ref`, keyed by (repo, PR number) — §3."""
        base = root or DEFAULT_ROOT
        key = repo_key(ref.owner_repo)
        return cls(
            workspace=base / "review-workspaces" / key / str(ref.number),
            state_dir=base / "review-state" / key / str(ref.number),
            pr_number=ref.number,
        )

    @property
    def state_db(self) -> Path:
        """Absolute `state_file` handed to spec-runner (outside the checkout)."""
        return self.state_dir / "executor-state.db"

    @property
    def lock_file(self) -> Path:
        """flock target for the per-PR lock."""
        return self.state_dir / "lock"


class PrLock:
    """Durable per-PR advisory lock, held for the whole review cycle.

    `flock` is released by the OS when the holder dies, so a crashed run
    never leaves a stale lock behind — no unlock protocol needed.
    Same-host only (documented non-goal in v1).
    """

    def __init__(self, paths: ReviewPaths) -> None:
        self._paths = paths
        self._handle = None

    def __enter__(self) -> PrLock:
        self._paths.state_dir.mkdir(parents=True, exist_ok=True)
        handle = self._paths.lock_file.open("w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            msg = (
                f"review of PR #{self._paths.pr_number} is already running "
                "(lock held by another process)"
            )
            raise AlreadyRunning(msg) from exc
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


@dataclass(frozen=True)
class PrMeta:
    """PR facts Maestro verifies before every invocation (spec §3.1)."""

    head_ref: str
    head_sha: str
    is_open: bool


def fetch_pr_meta(ref: PrRef) -> PrMeta:
    """Read PR state/draft/head from the GitHub API — fail-closed (§3.1).

    PR identity is verified here, on Maestro's side: spec-runner takes
    the URL as a positional argument and re-reads the metadata itself,
    and `ExecutorConfig` has no expected-repo/PR/head fields to force.

    Raises:
        PreconditionError: gh failure, closed/merged/draft PR, or a head
            branch that lives in a different repository (fork PRs are
            out of scope for the fix path).
    """
    result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(ref.number),
            "--repo",
            ref.owner_repo,
            "--json",
            "state,isDraft,headRefName,headRefOid,headRepository,headRepositoryOwner",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = f"`gh pr view` failed for {ref.canonical_url}: {result.stderr.strip()}"
        raise PreconditionError(msg)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        msg = f"unparseable `gh pr view` output for {ref.canonical_url}: {exc}"
        raise PreconditionError(msg) from exc

    state = str(payload.get("state", "")).upper()
    if state != "OPEN":
        msg = f"PR {ref.canonical_url} is {state or 'in an unknown state'} — not open"
        raise PreconditionError(msg)
    if payload.get("isDraft"):
        msg = f"PR {ref.canonical_url} is a draft — review is fail-closed on drafts"
        raise PreconditionError(msg)

    head_owner = (payload.get("headRepositoryOwner") or {}).get("login")
    head_repo = (payload.get("headRepository") or {}).get("name")
    if head_owner != ref.owner or head_repo != ref.repo:
        msg = (
            f"PR {ref.canonical_url} head repository is "
            f"{head_owner}/{head_repo}, expected {ref.owner_repo} — "
            "cross-repository (fork) heads are out of scope"
        )
        raise PreconditionError(msg)

    return PrMeta(
        head_ref=str(payload["headRefName"]),
        head_sha=str(payload["headRefOid"]),
        is_open=True,
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = f"git {' '.join(args)} failed: {result.stderr.strip()}"
        raise PreconditionError(msg)
    return result.stdout.strip()


def _is_dirty(workspace: Path) -> bool:
    return bool(_git(workspace, "status", "--porcelain"))


def _is_ancestor(workspace: Path, maybe_ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "merge-base",
            "--is-ancestor",
            maybe_ancestor,
            descendant,
        ],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def materialize(
    *,
    repo_path: Path,
    paths: ReviewPaths,
    head_ref: str,
    head_sha: str,
    discard_local: bool = False,
) -> Path:
    """Create or restore the review workspace on the PR head (§3.1).

    Args:
        repo_path: The project repository the worktree is cut from.
        paths: Workspace/state layout for this PR.
        head_ref: The PR head branch name.
        head_sha: The PR head SHA as reported by the GitHub API.
        discard_local: Reset a local continuation instead of keeping it —
            explicit, audited by the caller, never the default.

    Returns:
        The workspace path, ready for spec-runner (possibly needing
        `recover_push` first when a continuation is present).

    Raises:
        PreconditionError: dirty tree, diverged remote (force-push), or
            any git failure — fail-closed, nothing is reset implicitly.
    """
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_path, "fetch", "origin", head_ref)

    if not paths.workspace.exists():
        paths.workspace.parent.mkdir(parents=True, exist_ok=True)
        _git(
            repo_path,
            "worktree",
            "add",
            "--force",
            str(paths.workspace),
            head_sha,
        )
        return paths.workspace

    local_head = _git(paths.workspace, "rev-parse", "HEAD")
    if discard_local:
        _git(paths.workspace, "reset", "--hard", head_sha)
        _git(paths.workspace, "clean", "-fd")
        return paths.workspace

    state = classify_precondition(
        local_head=local_head,
        remote_head=head_sha,
        ancestor=_is_ancestor(paths.workspace, head_sha, local_head),
        dirty=_is_dirty(paths.workspace),
    )
    if state == "dirty":
        msg = (
            f"review workspace {paths.workspace} is dirty — commit the changes "
            "(they become a recognized continuation) or re-run with "
            "--discard-local"
        )
        raise PreconditionError(msg)
    if state == "diverged":
        msg = (
            f"review workspace {paths.workspace} has diverged from the PR head "
            f"({local_head} vs {head_sha}) — the remote was likely force-pushed; "
            "inspect it, then re-run with --discard-local to reset"
        )
        raise PreconditionError(msg)
    return paths.workspace


def recover_push(*, workspace: Path, head_ref: str, expected_remote_sha: str) -> str:
    """Publish a local continuation so spec-runner's strict check passes (§3.1.4).

    spec-runner refuses to mutate unless `local_head == remote head_sha`,
    so a saved continuation would fail forever if handed over as-is.
    This pushes it with `--force-with-lease=<ref>:<expected>` — a plain
    fast-forward publish that is *refused* if the remote moved off the
    verified expected SHA. Never an unconditional force.

    Returns:
        The new remote head (== local HEAD) on success.

    Raises:
        PreconditionError: the remote moved (race) or the push was
            rejected — the caller must not invoke spec-runner.
    """
    local_head = _git(workspace, "rev-parse", "HEAD")
    result = subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "push",
            f"--force-with-lease={head_ref}:{expected_remote_sha}",
            "origin",
            f"HEAD:{head_ref}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = (
            f"recovery push of the local continuation failed (remote moved or "
            f"push rejected): {result.stderr.strip()}"
        )
        raise PreconditionError(msg)
    return local_head


def cleanup_after_run(*, repo_path: Path, paths: ReviewPaths, exit_code: int) -> None:
    """Apply the retention policy (§4) — durable state is always kept.

    Only a complete run (exit 0) releases the checkout; `needs_human`
    (2) and `infra_error` (1) keep it so a human can inspect, and so
    unpushed fix commits are never destroyed.
    """
    if exit_code != 0 or not paths.workspace.exists():
        return
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_path),
            "worktree",
            "remove",
            "--force",
            str(paths.workspace),
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        shutil.rmtree(paths.workspace, ignore_errors=True)
        subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "prune"],
            capture_output=True,
            check=False,
        )
