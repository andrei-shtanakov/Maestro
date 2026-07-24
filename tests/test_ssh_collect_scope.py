import pytest

from maestro.execution.ssh_collect import (
    CollectConflict,
    capture_baseline,
    path_in_scope,
    plan_collect,
)


def test_path_in_scope_matches_subtree_and_glob():
    assert path_in_scope("src/api/x.py", ["src/**"]) is True
    assert path_in_scope("src/api/x.py", ["src/api"]) is True
    assert path_in_scope("src/api/x.py", ["src/api/*.py"]) is True
    assert path_in_scope("docs/readme.md", ["src/**"]) is False


def test_path_in_scope_normalizes_dot_segments():
    # A `./`-prefixed scope must match its own in-scope paths (clean rels
    # never carry `./`), mirroring reservations.anchor_of normalization —
    # otherwise the scope would arm/lock the workdir yet reject its own
    # changes at collect.
    assert path_in_scope("src/a.py", ["./src/**"]) is True
    assert path_in_scope("src/a.py", ["./src"]) is True
    assert path_in_scope("a/b/x.py", ["a/./b/**"]) is True
    assert path_in_scope("docs/r.md", ["./src/**"]) is False


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_plan_collect_rejects_out_of_scope_change(tmp_path):
    worktree = tmp_path / "wt"
    staging = tmp_path / "st"
    _write(worktree / "src" / "a.py", "orig")
    _write(worktree / "docs" / "r.md", "orig")
    baseline = capture_baseline(worktree, excludes=[])
    # Remote changed BOTH an in-scope and an out-of-scope file:
    _write(staging / "src" / "a.py", "changed")
    _write(staging / "docs" / "r.md", "changed")
    with pytest.raises(CollectConflict):
        plan_collect(worktree, staging, baseline, forbidden=[".git"], scope=["src/**"])


def test_plan_collect_bounds_plan_to_scope(tmp_path):
    worktree = tmp_path / "wt"
    staging = tmp_path / "st"
    _write(worktree / "src" / "a.py", "orig")
    _write(worktree / "src" / "b.py", "orig")
    baseline = capture_baseline(worktree, excludes=[])
    _write(staging / "src" / "a.py", "changed")
    _write(staging / "src" / "b.py", "orig")  # unchanged
    plan = plan_collect(
        worktree, staging, baseline, forbidden=[".git"], scope=["src/**"]
    )
    assert plan.modified == ["src/a.py"]


def test_plan_collect_rejects_out_of_scope_deletion(tmp_path):
    worktree = tmp_path / "wt"
    staging = tmp_path / "st"
    _write(worktree / "src" / "a.py", "orig")
    _write(worktree / "docs" / "r.md", "orig")
    baseline = capture_baseline(worktree, excludes=[])
    # Remote deleted the out-of-scope file (omitted from staging) and left
    # the in-scope file untouched.
    _write(staging / "src" / "a.py", "orig")
    with pytest.raises(CollectConflict):
        plan_collect(worktree, staging, baseline, forbidden=[".git"], scope=["src/**"])


def test_plan_collect_empty_scope_rejects_any_change(tmp_path):
    worktree = tmp_path / "wt"
    staging = tmp_path / "st"
    _write(worktree / "src" / "a.py", "orig")
    baseline = capture_baseline(worktree, excludes=[])
    _write(staging / "src" / "a.py", "changed")
    with pytest.raises(CollectConflict):
        plan_collect(worktree, staging, baseline, forbidden=[".git"], scope=[])
