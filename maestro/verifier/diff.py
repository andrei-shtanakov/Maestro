"""Deterministic scope-bounded patch/manifest builder + identity hashes.

Verifier-gate Mode-1 (design §5, §6.1): the judge reads a task's cumulative
in-scope diff vs a baseline commit. Determinism is load-bearing — the patch
bytes get hashed into `artifact_sha256`, which is sealed into the verdict
document's identity — so two calls with identical repo state must produce
byte-identical patches and identical hashes.

This module is pure: it shells out to `git` and does not import anything
from later verifier-gate slices (the provider, execution context, etc.).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict


class PatchTooLargeError(RuntimeError):
    """The scope-bounded patch exceeds the configured `max_bytes` limit.

    Maps to a §9 envelope-preflight ERROR upstream (fail-closed ->
    `NEEDS_REVIEW`); callers must not swallow this.
    """


class BinaryChangeError(RuntimeError):
    """An in-scope path has a binary change the judge cannot read as text.

    Maps to a §9 envelope-preflight ERROR upstream (fail-closed ->
    `NEEDS_REVIEW`); callers must not swallow this.
    """


PathStatus = Literal["added", "modified", "deleted", "binary"]

_STATUS_BY_CODE: dict[str, PathStatus] = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
}


class PathEntry(BaseModel):
    """One path in the scope-bounded manifest."""

    model_config = ConfigDict(frozen=True)

    path: str
    status: PathStatus


class ScopePatch(BaseModel):
    """The deterministic scope-bounded patch and its manifest."""

    model_config = ConfigDict(frozen=True)

    patch_bytes: bytes
    manifest: list[PathEntry]


class TaskLike(Protocol):
    """The structural shape `compute_identity` needs from a task.

    Deliberately not `maestro.models.Task`/`TaskConfig` — this module stays
    dependency-free and pure; both runtime models satisfy this shape.
    """

    title: str
    prompt: str
    validation_cmd: str | None
    scope: list[str]


def build_scope_patch(
    worktree: Path,
    baseline_sha: str,
    scope: list[str],
    *,
    max_bytes: int,
) -> ScopePatch:
    """Build the deterministic scope-bounded patch + manifest for a task.

    Diffs the working tree against `baseline_sha`, restricted to `scope`
    pathspecs. Tracked changes come from `git diff`; new untracked in-scope
    files are included via `git add -N` into a **copy** of the index (a
    temporary `GIT_INDEX_FILE`) so the real working index is never mutated.

    Args:
        worktree: Path to the git worktree (or repo) to diff in.
        baseline_sha: The commit-ish to diff against.
        scope: Non-empty list of pathspecs the diff is bounded to.
        max_bytes: Maximum allowed size (in bytes) of the resulting patch.

    Returns:
        A `ScopePatch` with deterministic `patch_bytes` and a sorted
        `manifest`.

    Raises:
        ValueError: `scope` is empty.
        BinaryChangeError: an in-scope path has a binary change.
        PatchTooLargeError: the patch exceeds `max_bytes`.
    """
    if not scope:
        raise ValueError("scope must be non-empty")

    git_dir = _git_dir(worktree)
    real_index = git_dir / "index"

    with tempfile.TemporaryDirectory(prefix="maestro-verifier-idx-") as tmp_dir:
        temp_index = Path(tmp_dir) / "index"
        if real_index.exists():
            shutil.copyfile(real_index, temp_index)
        env = {"GIT_INDEX_FILE": str(temp_index)}

        untracked = _list_untracked_in_scope(worktree, scope, env)
        if untracked:
            _run_git(["add", "-N", "--", *untracked], cwd=worktree, env=env)

        patch_bytes = _run_git(
            ["diff", "--no-ext-diff", baseline_sha, "--", *scope],
            cwd=worktree,
            env=env,
        ).stdout
        name_status = _run_git(
            ["diff", "--no-ext-diff", "--name-status", baseline_sha, "--", *scope],
            cwd=worktree,
            env=env,
        ).stdout.decode()
        numstat = _run_git(
            ["diff", "--no-ext-diff", "--numstat", baseline_sha, "--", *scope],
            cwd=worktree,
            env=env,
        ).stdout.decode()

    binary_paths = _binary_paths_from_numstat(numstat)
    if binary_paths:
        raise BinaryChangeError(
            "in-scope binary change(s) not supported by the verifier judge: "
            f"{', '.join(sorted(binary_paths))}"
        )

    if len(patch_bytes) > max_bytes:
        raise PatchTooLargeError(
            f"scope-bounded patch is {len(patch_bytes)} bytes, "
            f"exceeds max_bytes={max_bytes}"
        )

    manifest = _build_manifest(name_status)
    return ScopePatch(patch_bytes=patch_bytes, manifest=manifest)


def compute_identity(task: TaskLike, patch: ScopePatch) -> tuple[str, str, str]:
    """Compute the artifact/criteria/verified-scope identity hashes (§5).

    Deterministic: identical `task` fields and identical `patch` contents
    always produce identical hashes, regardless of `task.scope` ordering or
    duplicates. `profile_sha256` (the judge-policy hash) is NOT computed
    here — that is Task 6's responsibility.

    Args:
        task: Anything with `title`, `prompt`, `validation_cmd`, `scope`.
        patch: The `ScopePatch` built by `build_scope_patch`.

    Returns:
        `(artifact_sha256, criteria_sha256, verified_scope_sha256)`, each a
        64-character lowercase hex SHA-256 digest.
    """
    envelope = _canonical_patch_envelope(patch)
    artifact_sha256 = hashlib.sha256(envelope).hexdigest()
    # In this slice both hashes pin the same scope-bounded envelope; they
    # are computed as distinct values because a later slice may extend the
    # artifact envelope with additional judge-input framing (e.g.
    # instructions) while verified_scope_sha256 must keep pinning only the
    # scope-bounded diff content itself (design §5).
    verified_scope_sha256 = hashlib.sha256(envelope).hexdigest()

    normalized_scope = sorted(set(task.scope))
    criteria_obj = {
        "title": task.title,
        "prompt": task.prompt,
        "validation_cmd": task.validation_cmd,
        "normalized_scope": normalized_scope,
    }
    criteria_sha256 = hashlib.sha256(_canonical_json(criteria_obj)).hexdigest()

    return artifact_sha256, criteria_sha256, verified_scope_sha256


def _canonical_json(obj: object) -> bytes:
    """Canonical (sorted-key, compact) JSON bytes for stable hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical_patch_envelope(patch: ScopePatch) -> bytes:
    """The canonical scope-bounded envelope: sorted manifest header + patch."""
    manifest_json = _canonical_json(
        [
            {"path": entry.path, "status": entry.status}
            for entry in sorted(patch.manifest, key=lambda e: e.path)
        ]
    )
    return manifest_json + b"\n" + patch.patch_bytes


# Applied to EVERY git invocation in this module (not just the patch-text
# `diff`) — a path is otherwise C-quoted by git whenever it contains
# non-ASCII bytes, which would (a) break `git ls-files`/`add -N` pathspec
# round-tripping for untracked non-ASCII files and (b) make `--name-status`
# disagree with the (quotepath=false) patch text for tracked ones, corrupting
# the manifest and the identity hashes derived from it.
_GIT_BASE_ARGS = ["-c", "core.quotepath=false"]


def _run_git(
    args: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    full_env = dict(os.environ)
    full_env["GIT_TERMINAL_PROMPT"] = "0"
    if env:
        full_env.update(env)
    return subprocess.run(
        ["git", *_GIT_BASE_ARGS, *args],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        check=True,
    )


def _git_dir(worktree: Path) -> Path:
    """Resolve the (possibly worktree-specific) git directory for `worktree`."""
    result = _run_git(["rev-parse", "--git-dir"], cwd=worktree)
    raw = result.stdout.decode().strip()
    path = Path(raw)
    return path if path.is_absolute() else worktree / path


def _list_untracked_in_scope(
    worktree: Path, scope: list[str], env: dict[str, str]
) -> list[str]:
    result = _run_git(
        ["ls-files", "--others", "--exclude-standard", "--", *scope],
        cwd=worktree,
        env=env,
    )
    return [line for line in result.stdout.decode().splitlines() if line]


def _binary_paths_from_numstat(numstat: str) -> set[str]:
    """Paths git's `--numstat` marks binary (`-\\t-\\t<path>`)."""
    binary: set[str] = set()
    for line in numstat.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0] == "-" and parts[1] == "-":
            binary.add(parts[2])
    return binary


def _build_manifest(name_status: str) -> list[PathEntry]:
    """Parse `git diff --name-status` output into a sorted, deduped manifest."""
    entries: dict[str, PathStatus] = {}
    for line in name_status.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        code = parts[0]
        path = parts[-1]
        entries[path] = _STATUS_BY_CODE.get(code[0], "modified")
    return [PathEntry(path=path, status=entries[path]) for path in sorted(entries)]
