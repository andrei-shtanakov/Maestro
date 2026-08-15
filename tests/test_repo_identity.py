import pytest

from maestro.repo_identity import IdentityError, parse_remote_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://github.com/andrei-shtanakov/kapelle",
            ("github.com", "andrei-shtanakov", "kapelle"),
        ),
        (
            "https://github.com/andrei-shtanakov/kapelle.git",
            ("github.com", "andrei-shtanakov", "kapelle"),
        ),
        (
            "git@github.com:andrei-shtanakov/kapelle.git",
            ("github.com", "andrei-shtanakov", "kapelle"),
        ),
        ("ssh://git@gitlab.com/acme/app.git", ("gitlab.com", "acme", "app")),
        (
            "https://git.company.example/acme/app",
            ("git.company.example", "acme", "app"),
        ),
    ],
)
def test_parse_remote_url_forms(url, expected):
    key = parse_remote_url(url)
    assert (key.host, key.owner, key.repo) == expected
    assert key.local is False


def test_github_is_case_folded():
    a = parse_remote_url("https://github.com/Andrei-Shtanakov/Kapelle")
    b = parse_remote_url("https://github.com/andrei-shtanakov/kapelle")
    assert a.as_path_parts() == b.as_path_parts()


def test_other_hosts_are_not_case_folded():
    a = parse_remote_url("https://git.company.example/Acme/App")
    b = parse_remote_url("https://git.company.example/acme/app")
    assert a.as_path_parts() != b.as_path_parts()


def test_two_hosts_same_owner_repo_are_distinct():
    gh = parse_remote_url("https://github.com/acme/app")
    gl = parse_remote_url("https://gitlab.com/acme/app")
    assert gh.as_path_parts() != gl.as_path_parts()


@pytest.mark.parametrize(
    "bad", ["", "not-a-url", "https://github.com/only-owner", "file:///tmp/x"]
)
def test_unparseable_remote_refuses(bad):
    with pytest.raises(IdentityError):
        parse_remote_url(bad)


def test_path_parts_are_filesystem_safe():
    key = parse_remote_url("https://github.com/acme/app")
    assert key.as_path_parts() == ("github.com", "acme", "app")
    assert all(
        "/" not in part and part not in ("", ".", "..") for part in key.as_path_parts()
    )
