import stat

from maestro.repo_identity import RepoKey
from maestro.state_paths import (
    ensure_private_dir,
    locks_dir,
    project_dir,
    run_dir,
    runs_dir,
    state_db_path,
)


KEY = RepoKey(host="github.com", owner="acme", repo="app")
LOCAL = RepoKey(host="_local", owner="", repo="app-abc123", local=True)


def test_project_dir_includes_host(tmp_path):
    assert (
        project_dir(KEY, home=tmp_path)
        == tmp_path / "projects" / "github.com" / "acme" / "app"
    )


def test_local_key_uses_two_segments(tmp_path):
    assert (
        project_dir(LOCAL, home=tmp_path)
        == tmp_path / "projects" / "_local" / "app-abc123"
    )


def test_run_paths(tmp_path):
    rid = "01M0000000000000000000000"
    assert runs_dir(KEY, home=tmp_path) == project_dir(KEY, home=tmp_path) / "runs"
    assert run_dir(KEY, rid, home=tmp_path) == runs_dir(KEY, home=tmp_path) / rid
    assert (
        state_db_path(KEY, rid, home=tmp_path)
        == run_dir(KEY, rid, home=tmp_path) / "state.db"
    )
    assert locks_dir(KEY, home=tmp_path) == project_dir(KEY, home=tmp_path) / "locks"


def test_ensure_private_dir_is_0700(tmp_path):
    target = ensure_private_dir(tmp_path / "a" / "b")
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "a").stat().st_mode) == 0o700


def test_ensure_private_dir_is_idempotent(tmp_path):
    first = ensure_private_dir(tmp_path / "x")
    second = ensure_private_dir(tmp_path / "x")
    assert first == second
    assert stat.S_IMODE(second.stat().st_mode) == 0o700


def test_home_honours_env(tmp_path, monkeypatch):
    from maestro.state_paths import maestro_home

    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "custom"))
    assert maestro_home() == tmp_path / "custom"
