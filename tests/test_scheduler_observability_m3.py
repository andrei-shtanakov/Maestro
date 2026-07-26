"""Observability M3 — scheduler.tick (emit-on-change) and task.route span.

Mirrors tests/test_scheduler_observability.py: the scheduler binds `_obs_log`
at import, so we reload obs into a tmp ORCHESTRA_LOG_DIR and rebind the
scheduler module's `_obs_log` to a fresh logger; `obs.span` uses the reloaded
module directly.
"""

from __future__ import annotations

import importlib
import json
from typing import TYPE_CHECKING

import pytest

from maestro.dag import DAG
from maestro.models import RouteAction, RouteDecision


# NOTE: SchedulerConfig lives in maestro.scheduler (not maestro.models); the
# tests build it from the reloaded module as sched_mod.SchedulerConfig(...).

if TYPE_CHECKING:
    from pathlib import Path


def _reload_obs_and_scheduler(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import maestro._vendor.obs as obs

    importlib.reload(obs)
    obs.init_logging("maestro")
    import maestro.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_obs_log", obs.get_logger("maestro.scheduler"))
    return obs, sched_mod


def _read_records(tmp_path: Path) -> list[dict]:
    files = list(tmp_path.glob("maestro-*.jsonl"))
    assert len(files) == 1, f"expected 1 jsonl file, got {len(files)}: {files}"
    return [json.loads(line) for line in files[0].read_text().splitlines()]


def _make_scheduler(sched_mod, tmp_path):
    fake_db = type("FakeDb", (), {"db_path": str(tmp_path / "fake.db")})()
    return sched_mod.Scheduler(
        db=fake_db,  # _emit_tick / _route_task never touch db beyond db_path
        dag=DAG([]),
        spawners={},
        config=sched_mod.SchedulerConfig(workdir=tmp_path, log_dir=tmp_path / "logs"),
    )


def _ticks(tmp_path: Path) -> list[dict]:
    return [
        r
        for r in _read_records(tmp_path)
        if r["Attributes"].get("event") == "scheduler.tick"
    ]


def test_emit_tick_first_and_on_change_only(tmp_path, monkeypatch) -> None:
    _obs, sched_mod = _reload_obs_and_scheduler(monkeypatch, tmp_path)
    sched = _make_scheduler(sched_mod, tmp_path)

    sched._emit_tick(2, 1, 0)  # first → emits
    sched._emit_tick(2, 1, 0)  # identical → skipped
    sched._emit_tick(1, 2, 0)  # changed → emits

    ticks = _ticks(tmp_path)
    assert len(ticks) == 2
    assert ticks[0]["Attributes"]["ready"] == 2
    assert ticks[0]["Attributes"]["running"] == 1
    assert ticks[0]["Attributes"]["completed"] == 0
    assert ticks[1]["Attributes"]["ready"] == 1
    assert ticks[1]["Attributes"]["running"] == 2
    assert ticks[0]["Resource"]["service.name"] == "maestro"


def test_emit_tick_oscillation_reemits(tmp_path, monkeypatch) -> None:
    _obs, sched_mod = _reload_obs_and_scheduler(monkeypatch, tmp_path)
    sched = _make_scheduler(sched_mod, tmp_path)
    sched._emit_tick(1, 0, 0)  # A
    sched._emit_tick(0, 1, 0)  # B
    sched._emit_tick(1, 0, 0)  # A again → re-emits (compare is vs previous only)
    assert len(_ticks(tmp_path)) == 3


class _StubRouting:
    def __init__(self, decision=None, exc=None):
        self._decision = decision
        self._exc = exc

    async def route(self, task):
        if self._exc is not None:
            raise self._exc
        return self._decision

    async def report_outcome(self, task, outcome):  # unused here
        return None


def _make_task():
    from maestro.models import Task

    # Task required fields: id, title, prompt, workdir.
    return Task(id="t-1", title="t-1", prompt="p", workdir="/tmp")


def _events(tmp_path, name):
    return [r for r in _read_records(tmp_path) if r["Attributes"].get("event") == name]


@pytest.mark.anyio
async def test_route_span_ended_carries_decision_attrs(tmp_path, monkeypatch) -> None:
    _obs, sched_mod = _reload_obs_and_scheduler(monkeypatch, tmp_path)
    sched = _make_scheduler(sched_mod, tmp_path)
    sched._routing = _StubRouting(
        decision=RouteDecision(action=RouteAction.ASSIGN, decision_id=None, reason="ok")
    )

    decision = await sched._route_task(_make_task())
    assert decision.decision_id is None  # static route → None is expected

    started = _events(tmp_path, "task.route.started")
    ended = _events(tmp_path, "task.route.ended")
    assert len(started) == 1 and len(ended) == 1
    # started carries only task_id; the decision attrs are on ended.
    assert started[0]["Attributes"]["task_id"] == "t-1"
    assert "action" not in started[0]["Attributes"]
    assert ended[0]["Attributes"]["action"] == "assign"
    assert ended[0]["Attributes"]["decision_id"] is None


@pytest.mark.anyio
async def test_route_span_records_arbiter_decision_id(tmp_path, monkeypatch) -> None:
    _obs, sched_mod = _reload_obs_and_scheduler(monkeypatch, tmp_path)
    sched = _make_scheduler(sched_mod, tmp_path)
    sched._routing = _StubRouting(
        decision=RouteDecision(
            action=RouteAction.ASSIGN, decision_id="d-123", reason="ok"
        )
    )
    await sched._route_task(_make_task())
    ended = _events(tmp_path, "task.route.ended")
    assert ended[0]["Attributes"]["decision_id"] == "d-123"


@pytest.mark.anyio
async def test_route_span_failed_on_exception(tmp_path, monkeypatch) -> None:
    _obs, sched_mod = _reload_obs_and_scheduler(monkeypatch, tmp_path)
    sched = _make_scheduler(sched_mod, tmp_path)
    sched._routing = _StubRouting(exc=RuntimeError("arbiter down"))

    with pytest.raises(RuntimeError):
        await sched._route_task(_make_task())

    failed = _events(tmp_path, "task.route.failed")
    assert len(failed) == 1
    assert "error" in failed[0]["Attributes"]
