"""Tests for `maestro.verifier.diff`: deterministic scope-bounded patch/manifest
builder + identity hashes (verifier-gate Mode-1, design §5).
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from maestro.verifier.diff import (
    BinaryChangeError,
    PatchTooLargeError,
    ScopePatch,
    build_scope_patch,
    compute_identity,
)


if TYPE_CHECKING:
    from pathlib import Path


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _head(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).stdout.decode().strip()


def _commit_all(repo: Path, message: str) -> str:
    _git(["add", "-A"], repo)
    _git(["commit", "-m", message], repo)
    return _head(repo)


@pytest.fixture
def scoped_repo(git_repo: Path) -> Path:
    """`git_repo` with an in-scope dir and an out-of-scope dir, committed.

    This commit is the "baseline" every test diffs against.
    """
    (git_repo / "in_scope").mkdir()
    (git_repo / "out_scope").mkdir()
    (git_repo / "in_scope" / "a.txt").write_text("line1\nline2\n")
    (git_repo / "in_scope" / "to_delete.txt").write_text("bye\n")
    (git_repo / "out_scope" / "b.txt").write_text("out1\n")
    _commit_all(git_repo, "seed scope dirs")
    return git_repo


class TestBuildScopePatch:
    """Scope-boundedness, untracked inclusion, determinism, index safety."""

    def test_includes_in_scope_excludes_out_of_scope(self, scoped_repo: Path) -> None:
        baseline = _head(scoped_repo)

        (scoped_repo / "in_scope" / "a.txt").write_text("line1\nline2 CHANGED\n")
        (scoped_repo / "in_scope" / "to_delete.txt").unlink()
        (scoped_repo / "in_scope" / "new_untracked.txt").write_text("new content\n")
        (scoped_repo / "out_scope" / "b.txt").write_text("out1 CHANGED\n")
        (scoped_repo / "out_scope" / "new_untracked_out.txt").write_text("nope\n")

        result = build_scope_patch(
            scoped_repo, baseline, ["in_scope"], max_bytes=1_000_000
        )

        text = result.patch_bytes.decode()
        assert "a.txt" in text
        assert "new_untracked.txt" in text
        assert "line2 CHANGED" in text
        assert "b.txt" not in text
        assert "new_untracked_out.txt" not in text

        paths = {e.path for e in result.manifest}
        assert not any("out_scope" in p for p in paths)

        statuses = {e.path: e.status for e in result.manifest}
        modified_path = next(p for p in paths if p.endswith("a.txt"))
        added_path = next(p for p in paths if p.endswith("new_untracked.txt"))
        deleted_path = next(p for p in paths if p.endswith("to_delete.txt"))
        assert statuses[modified_path] == "modified"
        assert statuses[added_path] == "added"
        assert statuses[deleted_path] == "deleted"

    def test_deterministic_across_two_runs(self, scoped_repo: Path) -> None:
        baseline = _head(scoped_repo)
        (scoped_repo / "in_scope" / "a.txt").write_text("line1\nchanged\n")
        (scoped_repo / "in_scope" / "untracked.txt").write_text("hello\n")

        first = build_scope_patch(scoped_repo, baseline, ["in_scope"], max_bytes=10_000)
        second = build_scope_patch(
            scoped_repo, baseline, ["in_scope"], max_bytes=10_000
        )

        assert first.patch_bytes == second.patch_bytes
        assert first.manifest == second.manifest

        id_first = compute_identity(_Task("t", "p", "pytest", ["in_scope"]), first)
        id_second = compute_identity(_Task("t", "p", "pytest", ["in_scope"]), second)
        assert id_first == id_second

    def test_real_index_unchanged_after_build(self, scoped_repo: Path) -> None:
        baseline = _head(scoped_repo)
        (scoped_repo / "in_scope" / "a.txt").write_text("line1\nchanged\n")
        (scoped_repo / "in_scope" / "untracked.txt").write_text("hello\n")

        before = _git(["status", "--porcelain"], scoped_repo).stdout
        build_scope_patch(scoped_repo, baseline, ["in_scope"], max_bytes=10_000)
        after = _git(["status", "--porcelain"], scoped_repo).stdout

        assert before == after

    def test_binary_in_scope_change_raises(self, scoped_repo: Path) -> None:
        baseline = _head(scoped_repo)
        (scoped_repo / "in_scope" / "blob.bin").write_bytes(bytes(range(256)) * 4)

        with pytest.raises(BinaryChangeError):
            build_scope_patch(scoped_repo, baseline, ["in_scope"], max_bytes=1_000_000)

    def test_binary_out_of_scope_change_is_ignored(self, scoped_repo: Path) -> None:
        baseline = _head(scoped_repo)
        (scoped_repo / "out_scope" / "blob.bin").write_bytes(bytes(range(256)) * 4)
        (scoped_repo / "in_scope" / "a.txt").write_text("line1\nchanged\n")

        result = build_scope_patch(
            scoped_repo, baseline, ["in_scope"], max_bytes=1_000_000
        )
        assert any(e.path.endswith("a.txt") for e in result.manifest)

    def test_oversize_patch_raises(self, scoped_repo: Path) -> None:
        baseline = _head(scoped_repo)
        (scoped_repo / "in_scope" / "a.txt").write_text("x\n" * 10_000)

        with pytest.raises(PatchTooLargeError):
            build_scope_patch(scoped_repo, baseline, ["in_scope"], max_bytes=10)

    def test_empty_scope_raises(self, scoped_repo: Path) -> None:
        baseline = _head(scoped_repo)
        with pytest.raises(ValueError):
            build_scope_patch(scoped_repo, baseline, [], max_bytes=1000)


class _Task:
    """Minimal stand-in satisfying `compute_identity`'s structural `TaskLike`."""

    def __init__(
        self,
        title: str,
        prompt: str,
        validation_cmd: str | None,
        scope: list[str],
    ) -> None:
        self.title = title
        self.prompt = prompt
        self.validation_cmd = validation_cmd
        self.scope = scope


class TestComputeIdentity:
    """Stability, criteria sensitivity, artifact sensitivity."""

    def test_stable_across_calls(self) -> None:
        patch = ScopePatch(patch_bytes=b"diff --git a b\n", manifest=[])
        task = _Task("t", "p", "pytest", ["in_scope"])

        first = compute_identity(task, patch)
        second = compute_identity(task, patch)

        assert first == second
        artifact, criteria, verified_scope = first
        assert len(artifact) == 64
        assert len(criteria) == 64
        assert len(verified_scope) == 64

    def test_criteria_hash_changes_with_validation_cmd(self) -> None:
        patch = ScopePatch(patch_bytes=b"diff --git a b\n", manifest=[])
        task_a = _Task("t", "p", "pytest", ["in_scope"])
        task_b = _Task("t", "p", "pytest -x", ["in_scope"])

        _, criteria_a, _ = compute_identity(task_a, patch)
        _, criteria_b, _ = compute_identity(task_b, patch)

        assert criteria_a != criteria_b

    def test_criteria_hash_stable_under_scope_reordering(self) -> None:
        patch = ScopePatch(patch_bytes=b"x", manifest=[])
        task_a = _Task("t", "p", "pytest", ["b", "a", "a"])
        task_b = _Task("t", "p", "pytest", ["a", "b"])

        _, criteria_a, _ = compute_identity(task_a, patch)
        _, criteria_b, _ = compute_identity(task_b, patch)

        assert criteria_a == criteria_b

    def test_artifact_hash_changes_with_patch_bytes(self) -> None:
        task = _Task("t", "p", "pytest", ["in_scope"])
        patch_a = ScopePatch(patch_bytes=b"aaa", manifest=[])
        patch_b = ScopePatch(patch_bytes=b"bbb", manifest=[])

        artifact_a, _, _ = compute_identity(task, patch_a)
        artifact_b, _, _ = compute_identity(task, patch_b)

        assert artifact_a != artifact_b
