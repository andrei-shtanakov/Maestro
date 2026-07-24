from maestro.execution.models import CollectPolicy
from maestro.execution.ssh_backend import LaunchNotStarted, _collect_scope
from maestro.execution.ssh_handle import CollectSpec


def test_collect_scope_keyed_off_mode_not_include():
    # scope_paths with a real scope -> that scope
    assert _collect_scope(
        CollectPolicy(mode="scope_paths", include=["src/**"])
    ) == ["src/**"]
    # scope_paths with an EMPTY include -> stays [] (fail-closed), not None
    assert _collect_scope(CollectPolicy(mode="scope_paths", include=[])) == []
    # non-scope_paths modes -> None (Mode-2 whole-worktree), even with a stray include
    assert _collect_scope(CollectPolicy(mode="whole_worktree")) is None
    assert _collect_scope(CollectPolicy(mode="none", include=["src/**"])) is None


def test_collectspec_has_scope_field():
    spec = CollectSpec(
        worktree=None,  # type: ignore[arg-type]
        staging_dir=None,  # type: ignore[arg-type]
        journal_dir=None,  # type: ignore[arg-type]
        baseline={},
        scope=["src/**"],
    )
    assert spec.scope == ["src/**"]


def test_launch_not_started_is_exception():
    assert issubclass(LaunchNotStarted, Exception)
