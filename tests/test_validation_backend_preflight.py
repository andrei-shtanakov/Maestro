import pytest

from maestro.execution.exec_config import ExecutionConfig
from maestro.models import Task, TaskStatus
from maestro.preflight import ValidationBackendError, check_validation_backends


def _task(vb: str, backend: str | None = None) -> Task:
    return Task(
        id="t1",
        title="t",
        prompt="p",
        workdir="/tmp",
        status=TaskStatus.READY,
        validation_cmd="pytest",
        validation_backend=vb,
        backend=backend,
    )


SSH_CFG = ExecutionConfig.model_validate(
    {
        "default_backend": "local",
        "backends": {
            "local": {
                "transport": {"type": "local"},
                "isolation": {"type": "bare"},
            },
            "gpu": {
                "transport": {"type": "ssh", "host": "gpu", "workdir_root": "/t"},
                "isolation": {"type": "bare"},
            },
        },
    }
)


def test_explicit_ssh_validation_backend_fails():
    with pytest.raises(ValidationBackendError, match="gpu"):
        check_validation_backends([_task("gpu")], SSH_CFG)


def test_same_on_ssh_task_fails():
    with pytest.raises(ValidationBackendError):
        check_validation_backends([_task("same", backend="gpu")], SSH_CFG)


def test_local_and_default_pass():
    check_validation_backends([_task("local", backend="gpu")], SSH_CFG)
    check_validation_backends([_task("same", backend="local")], SSH_CFG)


def test_no_validation_cmd_skipped():
    t = Task(
        id="t",
        title="t",
        prompt="p",
        workdir="/tmp",
        status=TaskStatus.READY,
        validation_backend="gpu",
    )
    check_validation_backends([t], SSH_CFG)  # no cmd -> no gate
