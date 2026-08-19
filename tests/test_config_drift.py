"""Resume-time config drift detection (#198)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from maestro.config_drift import (
    COMPARED_FIELDS,
    ConfigDrift,
    find_config_drift,
    render_config_drift,
)
from maestro.models import Workstream, WorkstreamConfig


PREFIX = "feature/"


class _ConnectedDb:
    is_connected = True


class _Stats:
    total_workstreams = 0


def _config(ws_id: str = "a1", **overrides: Any) -> WorkstreamConfig:
    base: dict[str, Any] = {
        "id": ws_id,
        "title": "Contract",
        "description": "Do the contract work",
        "scope": ["src/**", "tests/**"],
        "depends_on": [],
        "priority": 0,
        "backend": None,
    }
    base.update(overrides)
    return WorkstreamConfig(**base)


def _persisted(config: WorkstreamConfig, prefix: str = PREFIX) -> Workstream:
    return Workstream.from_config(config, branch_prefix=prefix)


def test_identical_config_is_not_drift() -> None:
    cfg = _config()
    assert not find_config_drift([cfg], [_persisted(cfg)], PREFIX)


def test_reordering_scope_or_depends_on_is_not_drift() -> None:
    """Order carries no meaning; reporting it would train operators to ignore."""
    persisted = _persisted(_config(scope=["a/**", "b/**"], depends_on=["x", "y"]))
    reordered = _config(scope=["b/**", "a/**"], depends_on=["y", "x"])
    assert not find_config_drift([reordered], [persisted], PREFIX)


def test_the_reported_case_added_scope_entries() -> None:
    """#198 verbatim: paths appended to scope after an ex-post gate block."""
    persisted = _persisted(_config(scope=["pyproject.toml", "tools/**"]))
    edited = _config(scope=["pyproject.toml", "tools/**", "conftest.py"])

    drift = find_config_drift([edited], [persisted], PREFIX)

    assert drift
    assert [(f.workstream_id, f.field) for f in drift.fields] == [("a1", "scope")]
    assert drift.fields[0].persisted == ["pyproject.toml", "tools/**"]
    assert drift.fields[0].configured == ["conftest.py", "pyproject.toml", "tools/**"]
    assert drift.all_refreshable


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "Renamed"),
        ("description", "Different work"),
        ("scope", ["other/**"]),
        ("depends_on", ["b2"]),
        ("priority", 50),
        ("backend", "docker"),
    ],
)
def test_every_config_derived_field_is_compared(field: str, value: Any) -> None:
    cfg = _config()
    drift = find_config_drift([_config(**{field: value})], [_persisted(cfg)], PREFIX)
    assert [f.field for f in drift.fields] == [field]


def test_branch_prefix_change_is_drift_too() -> None:
    """`branch` is derived, not declared — and silently ignored the same way."""
    persisted = _persisted(_config(), prefix="feature/")
    drift = find_config_drift([_config()], [persisted], "ws/")
    assert [f.field for f in drift.fields] == ["branch"]
    assert drift.fields[0].persisted == "feature/a1"
    assert drift.fields[0].configured == "ws/a1"


def test_compared_fields_covers_every_field_from_config() -> None:
    """Tripwire: a new WorkstreamConfig field must not silently escape the check.

    `Workstream.from_config` is the exact set of fields a config decides. If it
    grows one, this test fails rather than letting the new field join the class
    of edits that apply silently.
    """
    cfg = _config()
    derived = Workstream.from_config(cfg, branch_prefix=PREFIX)
    from_config_fields = {
        name
        for name in WorkstreamConfig.model_fields
        if name != "id" and hasattr(derived, name)
    }
    assert from_config_fields <= set(COMPARED_FIELDS)


def test_added_and_removed_workstreams_are_drift() -> None:
    persisted = [_persisted(_config("a1")), _persisted(_config("gone"))]
    drift = find_config_drift([_config("a1"), _config("new")], persisted, PREFIX)
    assert drift.added_ids == ("new",)
    assert drift.removed_ids == ("gone",)
    assert not drift.all_refreshable, "a changed DAG shape has no refresh path"


def test_auto_decomposition_run_has_nothing_to_compare() -> None:
    """No declared workstreams -> no drift, not "everything was removed"."""
    assert not find_config_drift([], [_persisted(_config())], PREFIX)


def test_render_states_the_persisted_version_stands_and_names_the_remedy() -> None:
    persisted = _persisted(_config(scope=["src/**"]))
    drift = find_config_drift(
        [_config(scope=["src/**", "tests/**"])], [persisted], PREFIX
    )

    text = render_config_drift(drift, "project.yaml")

    assert "project.yaml" in text
    assert "PERSISTED" in text
    assert "Nothing was dispatched." in text
    # The reported failure mode was reading the refusal as "my edit did not
    # help", so the remedy has to be in the message, not in the docs.
    assert "workstream-rework" in text
    assert "--refresh-from" in text
    assert "src/**" in text and "tests/**" in text


def test_render_separates_refreshable_from_frozen_edits() -> None:
    persisted = _persisted(_config(title="Old", scope=["src/**"]))
    edited = _config(title="New", scope=["src/**", "tests/**"])

    text = render_config_drift(
        find_config_drift([edited], [persisted], PREFIX), "p.yaml"
    )

    assert "scope — adopt with: maestro workstream-rework" in text
    assert "title — cannot be adopted into a live run" in text


def test_empty_drift_is_falsy_and_renders_nothing_actionable() -> None:
    assert not ConfigDrift()
    assert not ConfigDrift().all_refreshable


# --- Orchestrator wiring: drift must not pre-empt recovery ----------------


@pytest.mark.anyio
async def test_drift_halts_only_after_recovery_has_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drift forbids dispatch/decomposition/delivery — never recovery.

    A halt raised before reconciliation would trade one silent failure for
    another: a typo in `title` would leave a crash-stranded execution
    unobserved. The order is the guarantee, so it is asserted directly.
    """
    from maestro.orchestrator import ConfigDriftDetected, Orchestrator

    calls: list[str] = []

    async def _ensure(self: Orchestrator) -> None:
        calls.append("ensure")
        self._config_drift = find_config_drift(
            [_config(scope=["src/**", "tests/**"])],
            [_persisted(_config(scope=["src/**"]))],
            branch_prefix=PREFIX,
        )

    async def _recover(self: Orchestrator) -> int:
        calls.append("recover")
        return 0

    async def _approver(self: Orchestrator) -> None:
        calls.append("approver")

    async def _main_loop(self: Orchestrator) -> None:
        calls.append("main_loop")

    async def _cleanup(self: Orchestrator) -> None:
        calls.append("cleanup")

    monkeypatch.setattr(Orchestrator, "_ensure_workstreams", _ensure)
    monkeypatch.setattr(Orchestrator, "_recover_stranded_workstreams", _recover)
    monkeypatch.setattr(Orchestrator, "_finalize_interrupted_approver_runs", _approver)
    monkeypatch.setattr(Orchestrator, "_main_loop", _main_loop)
    monkeypatch.setattr(Orchestrator, "_cleanup", _cleanup)

    orch = Orchestrator.__new__(Orchestrator)
    orch._db = _ConnectedDb()  # type: ignore[assignment]
    orch._config_drift = ConfigDrift()
    orch._stats = _Stats()  # type: ignore[assignment]
    orch._log_dir = Path()
    orch._generating = {}
    orch._running = {}
    orch._force_shutdown = False
    orch._setup_signal_handlers = lambda: None  # type: ignore[method-assign]

    with pytest.raises(ConfigDriftDetected) as exc_info:
        await orch.run()

    assert calls == ["ensure", "recover", "approver", "cleanup"], (
        "recovery and approver reconciliation must run before the halt, "
        "and the main loop must not"
    )
    assert exc_info.value.drift.all_refreshable


@pytest.mark.anyio
async def test_detection_fires_through_the_real_product_path(tmp_path: Path) -> None:
    """A real Orchestrator over a real DB — not a monkeypatched stand-in.

    The ordering test above patches `_ensure_workstreams`, which is where the
    detection itself lives; without this test the detection code would never
    be reached by any test even though the suite looked green.
    """
    from unittest.mock import MagicMock

    from maestro.database import Database
    from maestro.models import OrchestratorConfig, WorkstreamStatus
    from maestro.orchestrator import Orchestrator

    db = Database(tmp_path / "orch.db")
    await db.connect()
    try:
        persisted = _persisted(_config("a1", scope=["src/**"]))
        persisted.status = WorkstreamStatus.NEEDS_REVIEW
        await db.create_workstream(persisted)

        orch = Orchestrator(
            db=db,
            workspace_mgr=MagicMock(),
            decomposer=MagicMock(),
            pr_manager=MagicMock(),
            config=OrchestratorConfig(
                project="p",
                repo_url="https://github.com/t/r",
                repo_path=str(tmp_path / "repo"),
                workspace_base=str(tmp_path / "ws"),
                workstreams=[_config("a1", scope=["src/**", "tests/**"])],
            ),
        )

        await orch._ensure_workstreams()

        assert orch._config_drift, "detection never reached through the real path"
        assert [f.field for f in orch._config_drift.fields] == ["scope"]
    finally:
        await db.close()


@pytest.mark.anyio
async def test_unchanged_config_leaves_the_real_path_silent(tmp_path: Path) -> None:
    """The common case must stay a no-op: resume with an untouched config."""
    from unittest.mock import MagicMock

    from maestro.database import Database
    from maestro.models import OrchestratorConfig
    from maestro.orchestrator import Orchestrator

    db = Database(tmp_path / "orch.db")
    await db.connect()
    try:
        cfg = _config("a1")
        await db.create_workstream(_persisted(cfg))
        orch = Orchestrator(
            db=db,
            workspace_mgr=MagicMock(),
            decomposer=MagicMock(),
            pr_manager=MagicMock(),
            config=OrchestratorConfig(
                project="p",
                repo_url="https://github.com/t/r",
                repo_path=str(tmp_path / "repo"),
                workspace_base=str(tmp_path / "ws"),
                workstreams=[cfg],
            ),
        )
        await orch._ensure_workstreams()
        assert not orch._config_drift
    finally:
        await db.close()


# --- Empty `workstreams:` — the hole Copilot found on #199 ----------------


def test_deleted_workstreams_section_is_drift_when_the_run_declared_them() -> None:
    """The same silence, a different edit shape: the section removed entirely.

    Without provenance this is indistinguishable from an auto-decomposed run,
    so `find_config_drift` would have returned "no drift" and the resume would
    have continued quietly — exactly what #198 is about.
    """
    persisted = [_persisted(_config("a1")), _persisted(_config("a2"))]
    drift = find_config_drift([], persisted, PREFIX, workstreams_declared=True)
    assert drift
    assert drift.removed_ids == ("a1", "a2")
    assert not drift.all_refreshable


def test_auto_decomposed_run_is_silent_even_with_an_empty_config() -> None:
    persisted = [_persisted(_config("a1"))]
    assert not find_config_drift([], persisted, PREFIX, workstreams_declared=False)


def test_unknown_provenance_fails_open() -> None:
    """Runs predating migration 28 answer NULL — treated as auto-decomposed.

    Fail-open is the deliberate choice: halting every legacy auto-decomposed
    run on resume would be a worse defect than the hole left open for the
    short life of a per-run state directory.
    """
    persisted = [_persisted(_config("a1"))]
    assert not find_config_drift([], persisted, PREFIX, workstreams_declared=None)


@pytest.mark.anyio
async def test_provenance_is_recorded_and_read_back_through_the_real_path(
    tmp_path: Path,
) -> None:
    """Creation records it; a later resume reads it and reports the removal."""
    from unittest.mock import MagicMock

    from maestro.database import Database
    from maestro.models import OrchestratorConfig
    from maestro.orchestrator import Orchestrator

    def _orch(db: Database, workstreams: list[WorkstreamConfig]) -> Orchestrator:
        return Orchestrator(
            db=db,
            workspace_mgr=MagicMock(),
            decomposer=MagicMock(),
            pr_manager=MagicMock(),
            config=OrchestratorConfig(
                project="p",
                repo_url="https://github.com/t/r",
                repo_path=str(tmp_path / "repo"),
                workspace_base=str(tmp_path / "ws"),
                workstreams=workstreams,
            ),
        )

    db = Database(tmp_path / "orch.db")
    await db.connect()
    try:
        await db.create_run_row(
            run_id="01RUN", repo_key="github.com/t/r", started_at="2026-08-19T00:00:00Z"
        )

        # First orchestrate: workstreams declared in the config.
        await _orch(db, [_config("a1")])._ensure_workstreams()
        row = await db.get_run_row()
        assert row is not None
        assert row["workstreams_declared"] == 1

        # Resume after the operator deleted the `workstreams:` section.
        resumed = _orch(db, [])
        await resumed._ensure_workstreams()
        assert resumed._config_drift.removed_ids == ("a1",)
    finally:
        await db.close()
