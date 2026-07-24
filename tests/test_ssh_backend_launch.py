from maestro.execution.ssh_backend import LaunchNotStarted
from maestro.execution.ssh_handle import CollectSpec


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
