"""Baseline capture + two-phase transactional collect.

Phase 1 (plan_collect): pure preflight — zero worktree mutation. Detects the
remote's changes vs a pre-run baseline, rejects conflicts (parallel local
mutation on a remote-touched path), forbidden paths and symlink/traversal
escapes. Phase 2 (apply_collect): back up affected paths into a journal, apply
atomically per file, and on any error restore the whole journal.
"""

import fnmatch
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


class CollectConflict(Exception):
    """Preflight rejected the collect; no worktree changes were made."""


@dataclass
class CollectPlan:
    modified: list[str]
    deleted: list[str]


def _excluded(rel: str, excludes: list[str]) -> bool:
    parts = rel.split("/")
    for pat in excludes:
        if fnmatch.fnmatch(rel, pat) or any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
    return False


def path_in_scope(rel: str, scope: list[str]) -> bool:
    """True if `rel` is covered by any scope entry (self, subtree, or glob)."""
    for pat in scope:
        norm = pat.strip("/")
        if rel == norm or rel.startswith(norm + "/"):
            return True
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, norm + "/*"):
            return True
    return False


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk(root: Path, excludes: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            abs_p = Path(dirpath) / name
            rel = abs_p.relative_to(root).as_posix()
            if _excluded(rel, excludes):
                continue
            if abs_p.is_symlink():
                continue  # symlinks handled/validated in plan_collect
            out[rel] = _sha(abs_p)
    return out


def capture_baseline(worktree: Path, *, excludes: list[str]) -> dict[str, str]:
    """{relpath: sha256} for all non-excluded regular files in the worktree."""
    return _walk(worktree, excludes)


def _rel_escapes(worktree: Path, rel: str) -> bool:
    target = (worktree / rel).resolve()
    root = worktree.resolve()
    return root != target and root not in target.parents


def plan_collect(
    worktree: Path,
    staging: Path,
    baseline: dict[str, str],
    *,
    forbidden: list[str],
    scope: list[str] | None = None,
) -> CollectPlan:
    """Preflight; raises CollectConflict on any violation. No side effects.

    When `scope` is a list (Mode-1 remote), any modified/deleted path
    outside the scope is a CollectConflict and the returned plan is bounded
    to in-scope paths. An empty list (`scope=[]`) is a valid, fully-closed
    scope: every changed path is out-of-scope, so any change raises
    CollectConflict (a no-change collect still returns an empty plan).
    `scope=None` keeps the whole-worktree behavior (Mode-2) — this is the
    only value that skips the gate.
    """
    remote = _walk(staging, forbidden)
    # Symlink / traversal guard over the raw staging tree.
    for dirpath, _dirs, files in os.walk(staging):
        for name in files:
            abs_p = Path(dirpath) / name
            rel = abs_p.relative_to(staging).as_posix()
            if abs_p.is_symlink():
                raise CollectConflict(f"symlink in staging rejected: {rel}")
            if ".." in rel.split("/") or _rel_escapes(worktree, rel):
                raise CollectConflict(f"path escapes worktree: {rel}")

    modified = sorted(r for r, sha in remote.items() if baseline.get(r) != sha)
    deleted = sorted(r for r in baseline if r not in remote)

    if scope is not None:
        for rel in [*modified, *deleted]:
            if not path_in_scope(rel, scope):
                raise CollectConflict(f"out-of-scope change rejected: {rel}")

    for rel in [*modified, *deleted]:
        if _excluded(rel, forbidden):
            raise CollectConflict(f"forbidden path in change set: {rel}")
        current_p = worktree / rel
        current = _sha(current_p) if current_p.is_file() else None
        if current != baseline.get(rel):
            raise CollectConflict(
                f"local worktree diverged from baseline on remote-touched path: {rel}"
            )

    for rel in modified:
        _check_structural_conflict(worktree, rel)

    return CollectPlan(modified=modified, deleted=deleted)


def _check_structural_conflict(worktree: Path, rel: str) -> None:
    """Raise if applying `rel` would clash with the worktree's file/dir shape.

    Read-only: only stats existing paths, never mutates. Two cases:
    an ancestor of `rel` is currently a regular file (can't create a nested
    path under a file), or `rel` itself currently exists as a directory
    (can't replace a directory with a file).
    """
    parts = rel.split("/")
    ancestor = worktree
    for part in parts[:-1]:
        ancestor = ancestor / part
        if ancestor.is_file():
            raise CollectConflict(
                f"structural conflict: {ancestor.relative_to(worktree).as_posix()} "
                f"is a file, cannot create nested path {rel}"
            )
    target = worktree / rel
    if target.is_dir():
        raise CollectConflict(
            f"structural conflict: {rel} is a directory, cannot replace with a file"
        )


def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.maestro-tmp"
    shutil.copyfile(src, tmp)
    tmp.replace(dst)


def apply_collect(
    worktree: Path,
    staging: Path,
    plan: CollectPlan,
    *,
    journal_dir: Path,
) -> None:
    """Apply with a rollback journal; restore everything on any error."""
    journal_dir.mkdir(parents=True, exist_ok=True)
    backed: list[tuple[str, Path | None]] = []  # (rel, backup_path or None if absent)
    try:
        for rel in [*plan.modified, *plan.deleted]:
            target = worktree / rel
            if target.is_file():
                bak = journal_dir / rel
                bak.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target, bak)
                backed.append((rel, bak))
            else:
                backed.append((rel, None))
        for rel in plan.modified:
            _atomic_copy(staging / rel, worktree / rel)
        for rel in plan.deleted:
            (worktree / rel).unlink(missing_ok=True)
    except Exception:
        for rel, bak in backed:
            target = worktree / rel
            if bak is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(bak, target)
        raise
    shutil.rmtree(journal_dir, ignore_errors=True)
