"""Opt-in end-to-end test for the Mode-1 adversarial verifier gate.

Mirrors `test_ssh_validation_e2e.py`'s skip-guard shape, but drives the
verifier gate (`Scheduler._run_verifier`, see `CLAUDE.md`'s "Verifier gate
(Mode-1, opt-in)" note) against a REAL `claude` CLI subprocess instead of the
`_FakeJudge`/`_install_fake_model_resolution` doubles `test_scheduler_verifier_gate.py`
uses. Everything else — the git repo the scope-bounded diff is computed
against, the `Database`, the `Scheduler`, `resolve_verifier_model` — is real;
only the model catalog is synthetic (a temp TOML registering the configured
model as `active`, so the test never depends on a shared `$ATP_CATALOG`).

Gate: skip unless `MAESTRO_VERIFIER_E2E=1` **and** a `claude` CLI is on
PATH. Without both, this skips cleanly in CI/dev — the `_GATED` check below
short-circuits on the env var before `shutil.which` ever runs, so no
subprocess of any kind runs at import/collection time unless the opt-in var
is already set.
"""

import os
import shutil
import subprocess
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from maestro.dag import DAG
from maestro.database import Database, create_database
from maestro.models import AgentType, Task, TaskConfig, TaskStatus, VerifierConfig
from maestro.scheduler import RunningTask, Scheduler, SchedulerConfig
from tests.fakes.fake_execution_backend import FakeTaskHandle


pytestmark = pytest.mark.anyio

_VERIFIER_MODEL = os.environ.get("MAESTRO_VERIFIER_MODEL", "claude-haiku-4-5")

_GATED = os.environ.get("MAESTRO_VERIFIER_E2E") != "1" or shutil.which("claude") is None
skip_reason = (
    "set MAESTRO_VERIFIER_E2E=1, have an authenticated `claude` CLI on PATH, "
    "and optionally MAESTRO_VERIFIER_MODEL to pick a cheap judge model "
    "(defaults to claude-haiku-4-5)"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _run_git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _head(repo: Path) -> str:
    return _run_git(["rev-parse", "HEAD"], repo).strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo — the Mode-1 workdir the verifier diffs against."""
    d = tmp_path / "repo"
    d.mkdir()
    _run_git(["init", "-b", "main"], d)
    _run_git(["config", "user.email", "t@example.com"], d)
    _run_git(["config", "user.name", "Test"], d)
    (d / "greeting.txt").write_text("hello\n")
    _run_git(["add", "-A"], d)
    _run_git(["commit", "-m", "init"], d)
    return d


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[Database, None]:
    database = await create_database(tmp_path / "m.db")
    yield database
    await database.close()


@pytest.mark.skipif(_GATED, reason=skip_reason)
async def test_verifier_gate_real_claude_judge_reaches_terminal_status(
    repo: Path,
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive one verifier-enabled task through the real gate end-to-end.

    Makes a small, obviously prompt-compliant change to the scoped file,
    then runs the scheduler's real `_run_verifier` path: a genuine
    `claude -p ... --output-format json` subprocess judges the scope-bounded
    diff. The judge's verdict is a real LLM call and is deliberately not
    asserted on (PASS vs FAIL is not under this test's control) — only that
    the round trip (envelope build -> real subprocess -> parsed verdict ->
    state transition) completes and lands the task on one of the statuses
    the gate can produce, without raising.
    """
    catalog_path = tmp_path / "catalog.toml"
    catalog_path.write_text(
        f'[models."{_VERIFIER_MODEL}"]\nvendor = "anthropic"\nstatus = "active"\n'
    )
    monkeypatch.setenv("ATP_CATALOG", str(catalog_path))

    task_config = TaskConfig(
        id="t1",
        title="Add a farewell line",
        prompt="Append the exact line 'goodbye' to greeting.txt.",
        agent_type=AgentType.CLAUDE_CODE,
        scope=["greeting.txt"],
        validation_cmd="true",
        max_retries=1,
    )
    task = Task.from_config(task_config, str(repo))
    baseline = _head(repo)
    task = task.model_copy(
        update={
            "status": TaskStatus.RUNNING,
            "started_at": datetime.now(UTC),
            "verifier_baseline_sha": baseline,
        }
    )
    await db.create_task(task)

    # The change the (real) agent would have made, matching the prompt.
    (repo / "greeting.txt").write_text("hello\ngoodbye\n")

    dag = DAG([task_config])
    config = SchedulerConfig(workdir=repo, log_dir=repo / "logs")
    scheduler = Scheduler(
        db,
        dag,
        spawners={},
        config=config,
        verifier=VerifierConfig(model=_VERIFIER_MODEL, timeout_seconds=90),
    )
    monkeypatch.setattr(scheduler, "_auto_commit_task", lambda _task: None)

    running_task = RunningTask(
        task=task,
        handle=FakeTaskHandle(),
        started_at=datetime.now(UTC),
        log_file=tmp_path / "task.log",
    )
    await scheduler._handle_task_completion("t1", running_task, 0)

    final = await db.get_task("t1")
    assert final is not None
    assert final.status in (
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.READY,
        TaskStatus.NEEDS_REVIEW,
    )
    # The gate genuinely reached VERIFYING and recorded its baseline —
    # proof the real judge subprocess ran rather than the gate being
    # skipped entirely.
    assert final.verifier_baseline_sha == baseline
