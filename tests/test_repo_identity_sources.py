import subprocess
from pathlib import Path

import pytest

from maestro.repo_identity import IdentityError, identity_from_checkout, local_key


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    return path


def test_checkout_with_origin_uses_the_remote(tmp_path):
    repo = _init_repo(tmp_path / "work")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/app.git")
    key = identity_from_checkout(repo)
    assert key.as_path_parts() == ("github.com", "acme", "app")
    assert key.local is False


def test_checkout_without_origin_falls_into_local(tmp_path):
    repo = _init_repo(tmp_path / "solo")
    key = identity_from_checkout(repo)
    assert key.local is True
    assert key.as_path_parts()[0] == "_local"
    assert key.as_path_parts()[1].startswith("solo-")


def test_two_local_repos_with_the_same_basename_do_not_collide(tmp_path):
    a = _init_repo(tmp_path / "a" / "project")
    b = _init_repo(tmp_path / "b" / "project")
    assert (
        identity_from_checkout(a).as_path_parts()
        != identity_from_checkout(b).as_path_parts()
    )


def test_local_key_is_stable_across_calls(tmp_path):
    repo = _init_repo(tmp_path / "solo")
    assert local_key(repo).as_path_parts() == local_key(repo).as_path_parts()


def test_non_git_directory_refuses(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(IdentityError):
        identity_from_checkout(plain)
