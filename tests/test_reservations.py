from maestro.execution.reservations import _covers, anchor_of


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
