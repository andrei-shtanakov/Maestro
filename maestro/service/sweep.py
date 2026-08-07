"""Stale-worktree sweep before a tick decides (spec §3.4).

Bounded and conservative: only a worktree whose workstream is terminal
**and** whose branch is already merged into the base branch is removed.
A NEEDS_REVIEW workstream, an unmerged branch or a dirty tree is kept
and reported — an unattended sweep must never be the thing that destroys
work a human still needs. Review workspaces are not in scope here at
all; they have their own retention policy and their own `--gc`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from maestro.models import Workstream

from maestro.models import WorkstreamStatus


__all__ = ["SweepReport", "sweep_stale_worktrees"]

_TERMINAL = {WorkstreamStatus.DONE, WorkstreamStatus.ABANDONED}


@dataclass
class SweepReport:
    """What the sweep did — everything kept carries a reason."""

    removed: list[str] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)
    pruned: bool = False


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _is_merged(repo: Path, branch: str, base: str) -> bool:
    result = _git(repo, "merge-base", "--is-ancestor", branch, base)
    return result.returncode == 0


def _is_dirty(worktree: Path) -> bool:
    result = _git(worktree, "status", "--porcelain")
    return result.returncode != 0 or bool(result.stdout.strip())


def sweep_stale_worktrees(
    *, repo_path: Path, base_branch: str, workstreams: list[Workstream]
) -> SweepReport:
    """Prune administrative records, then remove only provably-safe trees."""
    report = SweepReport()
    prune = _git(repo_path, "worktree", "prune")
    report.pruned = prune.returncode == 0

    for ws in workstreams:
        if not ws.workspace_path:
            continue
        path = Path(ws.workspace_path)
        if not path.exists():
            continue  # the prune above already dealt with the record
        if ws.status not in _TERMINAL:
            report.kept.append((ws.id, f"workstream is {ws.status.value}"))
            continue
        if _is_dirty(path):
            report.kept.append((ws.id, "worktree is dirty"))
            continue
        if not _is_merged(repo_path, ws.branch, base_branch):
            report.kept.append((ws.id, f"branch not merged into {base_branch}"))
            continue
        removal = _git(repo_path, "worktree", "remove", "--force", str(path))
        if removal.returncode == 0:
            report.removed.append(ws.id)
        else:
            report.kept.append((ws.id, f"removal failed: {removal.stderr.strip()}"))
    return report
