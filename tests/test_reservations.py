from pathlib import Path

import pytest

from maestro.execution.exec_config import (
    BackendSpec,
    BareIsolation,
    ExecutionConfig,
    SshTransport,
)
from maestro.execution.reservations import (
    UnboundedRemoteScopeError,
    _covers,
    anchor_of,
    canonical_workdir,
    compute_armed_workdirs,
    is_ssh_task,
    overlaps,
    scope_to_reservation,
    validate_ssh_scopes,
)
from maestro.models import AgentType, Task, TaskStatus


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


def _task(tid: str, workdir: str, backend: str | None, scope: list[str]) -> Task:
    return Task(
        id=tid,
        title=tid,
        prompt="do x",
        agent_type=AgentType.CLAUDE_CODE,
        workdir=workdir,
        status=TaskStatus.PENDING,
        backend=backend,
        scope=scope,
    )


def _exec_with_ssh() -> ExecutionConfig:
    return ExecutionConfig(
        default_backend="local",
        backends={
            "remote": BackendSpec(
                transport=SshTransport(type="ssh", host="h", workdir_root="/remote"),
                isolation=BareIsolation(),
            )
        },
    )


def test_is_ssh_task_by_transport_not_name():
    ex = _exec_with_ssh()
    assert is_ssh_task(_task("t1", "/r", "remote", ["src/**"]), ex) is True
    assert is_ssh_task(_task("t2", "/r", "local", []), ex) is False
    assert is_ssh_task(_task("t3", "/r", "unknown", []), ex) is False


def test_compute_armed_workdirs(tmp_path):
    ex = _exec_with_ssh()
    wd_armed = str(tmp_path / "armed")
    wd_plain = str(tmp_path / "plain")
    tasks = [
        _task("t1", wd_armed, "remote", ["src/**"]),
        _task("t2", wd_armed, "local", []),
        _task("t3", wd_plain, "local", []),
    ]
    armed = compute_armed_workdirs(tasks, ex)

    assert canonical_workdir(wd_armed) in armed
    assert canonical_workdir(wd_plain) not in armed


def test_validate_ssh_scopes_rejects_unbounded():
    ex = _exec_with_ssh()
    with pytest.raises(UnboundedRemoteScopeError):
        validate_ssh_scopes([_task("t1", "/r", "remote", [])], ex)


def test_validate_ssh_scopes_accepts_bounded():
    ex = _exec_with_ssh()
    validate_ssh_scopes([_task("t1", "/r", "remote", ["src/**"])], ex)  # no raise
