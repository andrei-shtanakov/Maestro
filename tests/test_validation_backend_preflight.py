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


def test_explicit_ssh_validation_backend_passes():
    # PR2: SSH validation backends are supported — no rejection.
    check_validation_backends([_task("gpu")], SSH_CFG)


def test_same_on_ssh_task_passes():
    check_validation_backends([_task("same", backend="gpu")], SSH_CFG)


def test_local_and_default_pass():
    check_validation_backends([_task("local", backend="gpu")], SSH_CFG)
    check_validation_backends([_task("same", backend="local")], SSH_CFG)


def test_same_with_no_backend_resolves_to_ssh_default_and_passes():
    # default_backend is SSH and the task pins no backend -> `same` resolves to
    # the SSH default, exactly as BackendResolver.resolve(None) would. SSH is
    # supported (PR2), so this must pass, not raise.
    ssh_default = ExecutionConfig.model_validate(
        {
            "default_backend": "gpu",
            "backends": {
                "gpu": {
                    "transport": {"type": "ssh", "host": "gpu", "workdir_root": "/t"},
                    "isolation": {"type": "bare"},
                },
            },
        }
    )
    check_validation_backends([_task("same", backend=None)], ssh_default)


def test_unknown_named_validation_backend_fails():
    # An explicitly named validation_backend that resolves to no known
    # backend must still fail fast at preflight, not surface a late
    # ExecutionConfigError inside the validation run.
    with pytest.raises(ValidationBackendError, match="does-not-exist"):
        check_validation_backends([_task("does-not-exist")], SSH_CFG)


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
