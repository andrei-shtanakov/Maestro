from datetime import datetime

import pytest

from maestro.execution.finalize import EvidenceCaptureFailed, finalize_handle
from maestro.execution.models import (
    CollectResult,
    ExecutionHandleRef,
    ExecutionResult,
)


class _Handle:
    def __init__(self, *, collect_raises=False):
        self.calls: list[str] = []
        self._collect_raises = collect_raises
        self.ref = ExecutionHandleRef(
            backend_id="local",
            run_id="test-run",
            transport_ref="",
            started_at=datetime.now(),
        )

    @property
    def os_pid(self) -> int | None:
        return None

    def poll(self) -> int | None:
        return None

    async def wait(self):
        self.calls.append("wait")
        return ExecutionResult(exit_code=0, output_log_path="/tmp/x")

    async def terminate(self, grace_seconds: float) -> None:
        pass

    async def kill(self) -> None:
        pass

    async def collect(self):
        self.calls.append("collect")
        if self._collect_raises:
            raise RuntimeError("conflict")
        return CollectResult(applied=True)

    async def cleanup(self):
        self.calls.append("cleanup")


@pytest.mark.anyio
async def test_collect_success_marks_between_phases_then_cleans():
    order: list[str] = []
    h = _Handle()
    fin = await finalize_handle(
        h,
        on_terminal=lambda: order.append("mark_terminal") or _noop(),
        on_collected=lambda: order.append("mark_collected") or _noop(),
    )
    assert h.calls == ["wait", "collect", "cleanup"]
    assert order == ["mark_terminal", "mark_collected"]
    assert fin.collect_succeeded and fin.cleaned


@pytest.mark.anyio
async def test_collect_failure_skips_cleanup_and_preserves():
    h = _Handle(collect_raises=True)
    fin = await finalize_handle(h, on_terminal=_acb(), on_collected=_acb())
    assert h.calls == ["wait", "collect"]  # NO cleanup
    assert not fin.collect_succeeded
    assert not fin.cleanup_attempted
    assert fin.collect_error == "conflict"


@pytest.mark.anyio
async def test_on_collected_failure_skips_cleanup_and_preserves():
    """A failing `on_collected` preserves the workspace, like a failed collect.

    #164 hangs post-mortem capture off this callback, and the capture runs at
    the only moment the evidence still exists — after collect, before the
    remote `rm -rf`. If capture fails and cleanup proceeded anyway, the run
    would destroy the only copy of the logs and look successful doing it.
    """
    h = _Handle()

    async def boom():
        raise EvidenceCaptureFailed("archive unwritable")

    fin = await finalize_handle(h, on_terminal=_acb(), on_collected=boom)

    assert h.calls == ["wait", "collect"]  # NO cleanup
    assert not fin.cleanup_attempted
    assert fin.archive_error == "archive unwritable"
    assert fin.collect_error is None  # collect itself was fine
    assert fin.collect_succeeded


@pytest.mark.anyio
async def test_other_on_collected_errors_still_propagate():
    """Only capture failures are absorbed; a failed durable write is a crash.

    `on_collected` also persists the collected phase, and Mode 1's scheduler
    depends on that failure surfacing so the handle is honestly left at
    `terminal` instead of reporting a finalization that never happened.
    """
    h = _Handle()

    async def boom():
        raise RuntimeError("simulated crash before the collected write lands")

    with pytest.raises(RuntimeError, match="simulated crash"):
        await finalize_handle(h, on_terminal=_acb(), on_collected=boom)

    assert h.calls == ["wait", "collect"]  # still NO cleanup


@pytest.mark.anyio
async def test_successful_finalization_reports_no_archive_error():
    h = _Handle()

    fin = await finalize_handle(h, on_terminal=_acb(), on_collected=_acb())

    assert fin.archive_error is None
    assert fin.cleaned


async def _noop():
    return None


def _acb():
    async def cb():
        return None

    return cb
