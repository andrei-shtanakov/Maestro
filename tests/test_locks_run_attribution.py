import pytest

from maestro.repo_identity import RepoKey
from maestro.service.locks import AlreadyRunning, ScopedLock, read_holder_run_id


KEY = RepoKey(host="github.com", owner="acme", repo="app")
OTHER = RepoKey(host="github.com", owner="acme", repo="other")


def test_holder_is_readable_while_held(tmp_path):
    with ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A", root=tmp_path):
        assert read_holder_run_id(KEY, "orchestrate", root=tmp_path) == "RUN-A"


def test_holder_is_cleared_on_release(tmp_path):
    with ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A", root=tmp_path):
        pass
    assert read_holder_run_id(KEY, "orchestrate", root=tmp_path) is None


def test_same_repo_and_stage_is_exclusive(tmp_path):
    with (
        ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A", root=tmp_path),
        pytest.raises(AlreadyRunning),
        ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-B", root=tmp_path),
    ):
        pass


def test_different_repos_do_not_serialise(tmp_path):
    with (
        ScopedLock(key=KEY, stage="orchestrate", run_id="RUN-A", root=tmp_path),
        ScopedLock(key=OTHER, stage="orchestrate", run_id="RUN-B", root=tmp_path),
    ):
        assert read_holder_run_id(OTHER, "orchestrate", root=tmp_path) == "RUN-B"


def test_lock_identity_does_not_depend_on_a_database_path(tmp_path):
    from maestro.service.locks import stage_lock_path

    assert stage_lock_path(KEY, "orchestrate", root=tmp_path) == stage_lock_path(
        KEY, "orchestrate", root=tmp_path
    )
