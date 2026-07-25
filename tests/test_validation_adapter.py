from pathlib import Path

from maestro.execution.models import ExecutionResult
from maestro.models import Task, TaskStatus
from maestro.validator import (
    build_validation_request,
    execution_result_to_validation,
)


def _task() -> Task:
    return Task(
        id="t1",
        title="t",
        prompt="p",
        workdir="/work/dir",
        status=TaskStatus.VALIDATING,
        validation_cmd="pytest -q",
    )


def test_build_request_shape():
    req = build_validation_request(
        _task(), backend_id="local", run_id="val-t1-1", attempt=1
    )
    assert req.argv == ["pytest", "-q"]
    assert req.workdir == Path("/work/dir")
    assert req.capture_output is True
    assert req.collect.mode == "none"
    assert req.inherit_env is True
    assert req.backend_id == "local"
    assert req.timeout_seconds == 300


def test_map_success():
    res = ExecutionResult(
        exit_code=0, stdout_tail="ok", stderr_tail="", output_log_path=Path("/x")
    )
    vr = execution_result_to_validation(res)
    assert vr.success is True
    assert vr.exit_code == 0
    assert vr.output == "ok"


def test_map_failure_and_timeout():
    fail = execution_result_to_validation(
        ExecutionResult(
            exit_code=2, stdout_tail="", stderr_tail="boom", output_log_path=Path("/x")
        )
    )
    assert fail.success is False
    assert fail.error_message == "Exit code: 2"
    assert fail.output == "boom"

    to = execution_result_to_validation(
        ExecutionResult(exit_code=None, timed_out=True, output_log_path=Path("/x"))
    )
    assert to.success is False
    assert to.timed_out is True
