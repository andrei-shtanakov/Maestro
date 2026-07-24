from pathlib import Path

from maestro.execution.reservations import (
    _covers,
    anchor_of,
    canonical_workdir,
    overlaps,
    scope_to_reservation,
)


def test_anchor_of_literal_prefix():
    assert anchor_of("src/api/*.py") == "src/api"
    assert anchor_of("pkg/**") == "pkg"
    assert anchor_of("lib/**/x.py") == "lib"


def test_anchor_of_leading_wildcard_is_root():
    assert anchor_of("**") == ""
    assert anchor_of("*.py") == ""
    assert anchor_of("**/x") == ""


def test_anchor_of_pure_literal_is_itself():
    assert anchor_of("config.yaml") == "config.yaml"
    assert anchor_of("a/b/c.txt") == "a/b/c.txt"


def test_covers_prefix_and_root():
    assert _covers("", "anything/here") is True
    assert _covers("src", "src/api/x.py") is True
    assert _covers("src", "src") is True
    assert _covers("src", "srcfoo/x") is False  # segment boundary, not substring
    assert _covers("src/api", "src") is False


def test_scope_to_reservation_empty_is_whole_workdir():
    r = scope_to_reservation("/repo", [])
    assert r.anchors == frozenset({""})


def test_scope_to_reservation_anchors():
    r = scope_to_reservation("/repo", ["src/api/*.py", "docs/**"])
    assert r.anchors == frozenset({"src/api", "docs"})


def test_overlaps_same_workdir_shared_subtree():
    a = scope_to_reservation("/repo", ["src/**"])
    b = scope_to_reservation("/repo", ["src/api/x.py"])
    assert overlaps(a, b) is True


def test_disjoint_scopes_do_not_overlap():
    a = scope_to_reservation("/repo", ["src/**"])
    b = scope_to_reservation("/repo", ["docs/**"])
    assert overlaps(a, b) is False


def test_whole_workdir_overlaps_everything_on_same_workdir():
    a = scope_to_reservation("/repo", [])  # {""}
    b = scope_to_reservation("/repo", ["docs/**"])
    assert overlaps(a, b) is True


def test_different_workdirs_never_overlap():
    a = scope_to_reservation("/repo-a", [])
    b = scope_to_reservation("/repo-b", [])
    assert overlaps(a, b) is False


def test_canonical_workdir_is_absolute(tmp_path: Path):
    assert canonical_workdir(tmp_path).is_absolute()
