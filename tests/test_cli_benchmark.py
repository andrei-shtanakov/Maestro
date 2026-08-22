"""Tests for `maestro benchmark` (R-06b M5)."""

import shutil
import sys
from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

import maestro.cli as cli_mod
from maestro.benchmark.models import BenchmarkResult, FinalizedScore
from maestro.cli import app
from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.spawners.base import AgentSpawner
from tests.fakes.benchmark_score import finalized_score


runner = CliRunner()


class FakeTask:
    def __init__(self, task_index: int, prompt: str) -> None:
        self.task_index = task_index
        self.prompt = prompt


class FakeRun:
    """Two-task fake BenchmarkRun."""

    def __init__(self) -> None:
        self.run_id = "fake-run-1"
        self.submitted: list[tuple[int, str]] = []

    async def tasks(self):
        yield FakeTask(0, "prompt zero")
        yield FakeTask(1, "prompt one")

    async def submit(self, task_index: int, response: str) -> None:
        self.submitted.append((task_index, response))

    async def finalize(self) -> FinalizedScore:
        return finalized_score(0.75, {"accuracy": 0.75})


class FakeAdapter:
    """Stands in for MaestroATPAdapter."""

    instances: ClassVar[list["FakeAdapter"]] = []

    def __init__(self, platform_url: str) -> None:
        self.platform_url = platform_url
        self.started: list[tuple[str, str]] = []
        FakeAdapter.instances.append(self)

    @classmethod
    def from_env(cls, platform_url: str = "http://localhost:8000", **_: object):
        return cls(platform_url)

    async def start_run(self, benchmark_id: str, agent_name: str) -> FakeRun:
        self.started.append((benchmark_id, agent_name))
        return FakeRun()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


_OK_LOG = '{"result": "ok", "usage": {"input_tokens": 10, "output_tokens": 5}}'


class FakeBenchSpawner(AgentSpawner):
    """Runs a trivial real subprocess that emits a claude-format log.

    Uses ``sys.executable`` rather than the actual CLI so these tests
    stay agent-CLI-agnostic and don't depend on ``claude``/``codex``/etc.
    being installed — the responder now drives a real ``LocalBackend``,
    so ``build_request`` must produce an argv that genuinely runs.
    """

    def __init__(self, agent_type_str: str = "claude_code") -> None:
        self._agent_type = agent_type_str

    @property
    def agent_type(self) -> str:
        return self._agent_type

    def build_request(
        self, task, context, workdir, log_file, run_id, retry_context="", *, model=None
    ) -> ExecutionRequest:
        return ExecutionRequest(
            run_id=run_id,
            argv=[sys.executable, "-c", f"import sys; sys.stdout.write({_OK_LOG!r})"],
            workdir=workdir,
            log_path=log_file,
            collect=CollectPolicy(mode="none"),
        )


@pytest.fixture(autouse=True)
def _reset_fakes(monkeypatch: pytest.MonkeyPatch):
    FakeAdapter.instances = []
    monkeypatch.setattr(cli_mod, "MaestroATPAdapter", FakeAdapter)
    monkeypatch.setattr(
        cli_mod, "_bench_spawner_for", lambda agent: FakeBenchSpawner(agent)
    )
    monkeypatch.setattr(cli_mod, "_agent_cli_available", lambda _agent: True)
    monkeypatch.delenv("MAESTRO_ARBITER_BIN", raising=False)
    monkeypatch.delenv("MAESTRO_ATP_BASE_URL", raising=False)


class TestAgentValidation:
    def test_auto_rejected_before_atp(self) -> None:
        result = runner.invoke(app, ["benchmark", "b1", "--agent", "auto"])
        assert result.exit_code == 1
        assert "routing sentinel" in result.output
        assert FakeAdapter.instances == []

    def test_announce_rejected_before_atp(self) -> None:
        result = runner.invoke(app, ["benchmark", "b1", "--agent", "announce"])
        assert result.exit_code == 1
        assert "no-op echo" in result.output
        assert FakeAdapter.instances == []

    def test_unknown_agent_names_allowed_set(self) -> None:
        result = runner.invoke(app, ["benchmark", "b1", "--agent", "nosuch"])
        assert result.exit_code == 1
        assert "claude_code" in result.output and "opencode" in result.output
        assert FakeAdapter.instances == []

    def test_unavailable_agent_cli_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(
            cli_mod._BENCH_CLI_BY_AGENT,
            "claude_code",
            "definitely-not-a-real-cli-xyz",
        )
        # Override the autouse fixture's blanket True so this test genuinely
        # exercises the real `shutil.which` PATH lookup (against a binary
        # name that cannot exist) rather than the faked-available default.
        monkeypatch.setattr(
            cli_mod,
            "_agent_cli_available",
            lambda agent: shutil.which(cli_mod._BENCH_CLI_BY_AGENT[agent]) is not None,
        )
        result = runner.invoke(app, ["benchmark", "b1", "--agent", "claude_code"])
        assert result.exit_code == 1
        assert "not found in PATH" in result.output
        assert FakeAdapter.instances == []

    def test_timeout_zero_rejected(self) -> None:
        result = runner.invoke(
            app, ["benchmark", "b1", "--agent", "claude_code", "--timeout", "0"]
        )
        assert result.exit_code == 2
        assert FakeAdapter.instances == []

    def test_markup_hostile_agent_name_no_traceback(self) -> None:
        result = runner.invoke(app, ["benchmark", "b1", "--agent", "[/]"])
        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Traceback" not in result.output


class TestHappyPath:
    def test_run_prints_score_and_tasks(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "benchmark",
                "swe-mini",
                "--agent",
                "claude_code",
                "--workdir",
                str(tmp_path / "wd"),
            ],
        )
        assert result.exit_code == 0
        assert "0.75" in result.output
        assert "duration" in result.output
        assert "swe-mini" in result.output
        assert str(tmp_path / "wd") in result.output  # workdir announced
        assert "skipped" in result.output  # arbiter note (env unset)
        adapter = FakeAdapter.instances[0]
        assert adapter.started == [("swe-mini", "claude_code")]

    def test_json_stdout_is_pure_json(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "benchmark",
                "swe-mini",
                "--agent",
                "claude_code",
                "--workdir",
                str(tmp_path / "wd"),
                "--json",
            ],
        )
        assert result.exit_code == 0
        parsed = BenchmarkResult.model_validate_json(result.stdout)
        assert parsed.benchmark_id == "swe-mini"
        assert parsed.score == 0.75
        assert len(parsed.per_task) == 2

    def test_run_id_forwarded(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "benchmark",
                "swe-mini",
                "--agent",
                "claude_code",
                "--workdir",
                str(tmp_path / "wd"),
                "--run-id",
                "ci-42",
                "--json",
            ],
        )
        assert result.exit_code == 0
        parsed = BenchmarkResult.model_validate_json(result.stdout)
        assert parsed.run_id == "ci-42"

    def test_task_error_still_exit_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing task lands in the table, not the exit code."""

        class ErroringSpawner(FakeBenchSpawner):
            def build_request(
                self,
                task,
                context,
                workdir,
                log_file,
                run_id,
                retry_context="",
                *,
                model=None,
            ) -> ExecutionRequest:
                return ExecutionRequest(
                    run_id=run_id,
                    argv=[
                        sys.executable,
                        "-c",
                        "import sys; sys.stdout.write('no usable json'); sys.exit(1)",
                    ],
                    workdir=workdir,
                    log_path=log_file,
                    collect=CollectPolicy(mode="none"),
                )

        monkeypatch.setattr(
            cli_mod, "_bench_spawner_for", lambda agent: ErroringSpawner(agent)
        )
        result = runner.invoke(
            app,
            [
                "benchmark",
                "b",
                "--agent",
                "claude_code",
                "--workdir",
                str(tmp_path / "wd"),
            ],
        )
        assert result.exit_code == 0
        assert "exit code" in result.output  # responder's error string in the table


class TestAtpUrl:
    def test_flag_beats_env_beats_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wd = str(tmp_path / "wd")
        runner.invoke(app, ["benchmark", "b", "--agent", "aider", "--workdir", wd])
        assert FakeAdapter.instances[-1].platform_url == "http://localhost:8000"

        monkeypatch.setenv("MAESTRO_ATP_BASE_URL", "http://atp.example:9000")
        runner.invoke(app, ["benchmark", "b", "--agent", "aider", "--workdir", wd])
        assert FakeAdapter.instances[-1].platform_url == "http://atp.example:9000"

        runner.invoke(
            app,
            [
                "benchmark",
                "b",
                "--agent",
                "aider",
                "--workdir",
                wd,
                "--atp-url",
                "http://flag.example:1234",
            ],
        )
        assert FakeAdapter.instances[-1].platform_url == "http://flag.example:1234"


class TestAtpFailure:
    def test_start_run_failure_is_message_not_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FailingAdapter(FakeAdapter):
            async def start_run(self, benchmark_id: str, agent_name: str):
                raise RuntimeError("401 Unauthorized")

        monkeypatch.setattr(cli_mod, "MaestroATPAdapter", FailingAdapter)
        result = runner.invoke(
            app,
            [
                "benchmark",
                "b",
                "--agent",
                "claude_code",
                "--workdir",
                str(tmp_path / "wd"),
            ],
        )
        assert result.exit_code == 1
        assert "401 Unauthorized" in result.output
        assert "ATP_TOKEN" in result.output  # resolution-chain hint
        assert "Traceback" not in result.output

    def test_markup_hostile_atp_error_keeps_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FailingAdapter(FakeAdapter):
            async def start_run(self, benchmark_id: str, agent_name: str):
                raise RuntimeError("boom [/][bold]")

        monkeypatch.setattr(cli_mod, "MaestroATPAdapter", FailingAdapter)
        result = runner.invoke(
            app,
            [
                "benchmark",
                "b",
                "--agent",
                "claude_code",
                "--workdir",
                str(tmp_path / "wd"),
            ],
        )
        assert result.exit_code == 1
        assert "ATP_TOKEN" in result.output  # hint survived the hostile text


class FakeArbiterClient:
    """Records lifecycle calls; behavior configured by class attrs."""

    instances: ClassVar[list["FakeArbiterClient"]] = []
    fail_start: ClassVar[bool] = False

    def __init__(self, config) -> None:
        self.config = config
        self.calls: list[str] = []
        FakeArbiterClient.instances.append(self)

    async def start(self) -> None:
        self.calls.append("start")
        if FakeArbiterClient.fail_start:
            raise RuntimeError("arbiter binary refused to start")

    async def stop(self) -> None:
        self.calls.append("stop")

    async def report_benchmark_raw(self, payload: dict) -> dict:
        self.calls.append("report")
        return {"status": "created"}


@pytest.fixture()
def _arbiter_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """MAESTRO_ARBITER_BIN pointing at a plausible repo layout."""
    repo = tmp_path / "arbiter-repo"
    bin_path = repo / "target" / "release" / "arbiter-mcp"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "config").mkdir()
    (repo / "models").mkdir()
    (repo / "models" / "agent_policy_tree.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MAESTRO_ARBITER_BIN", str(bin_path))
    FakeArbiterClient.instances = []
    FakeArbiterClient.fail_start = False
    monkeypatch.setattr(cli_mod, "ArbiterClient", FakeArbiterClient)
    return bin_path


class TestArbiterReport:
    def _invoke(self, tmp_path: Path, *extra: str):
        return runner.invoke(
            app,
            [
                "benchmark",
                "b",
                "--agent",
                "claude_code",
                "--workdir",
                str(tmp_path / "wd"),
                *extra,
            ],
        )

    def test_lifecycle_start_report_stop(
        self, tmp_path: Path, _arbiter_env: Path
    ) -> None:
        result = self._invoke(tmp_path, "--json")
        assert result.exit_code == 0
        client = FakeArbiterClient.instances[0]
        assert client.calls == ["start", "report", "stop"]
        parsed = BenchmarkResult.model_validate_json(result.stdout)
        assert parsed.report_status == "ok"

    def test_start_failure_is_report_failure_not_run_failure(
        self, tmp_path: Path, _arbiter_env: Path
    ) -> None:
        FakeArbiterClient.fail_start = True
        result = self._invoke(tmp_path, "--json")
        assert result.exit_code == 0  # fire-and-forget
        parsed = BenchmarkResult.model_validate_json(result.stdout)
        assert parsed.report_status == "failed"
        assert "refused to start" in (parsed.report_error or "")
        client = FakeArbiterClient.instances[0]
        assert client.calls[0] == "start"
        assert client.calls[-1] == "stop"  # stop() on the failure path too

    def test_no_report_skips_client_entirely(
        self, tmp_path: Path, _arbiter_env: Path
    ) -> None:
        result = self._invoke(tmp_path, "--no-report")
        assert result.exit_code == 0
        assert FakeArbiterClient.instances == []
        assert "skipped (--no-report)" in result.output

    def test_stop_failure_is_swallowed(
        self, tmp_path: Path, _arbiter_env: Path
    ) -> None:
        """A raising stop() must not fail the run or lose the result."""

        class StopFails(FakeArbiterClient):
            async def stop(self) -> None:
                self.calls.append("stop")
                raise RuntimeError("stop exploded")

        # NB: fixture already monkeypatched ArbiterClient to FakeArbiterClient;
        # re-patch to the failing subclass for this test.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cli_mod, "ArbiterClient", StopFails)
            result = self._invoke(tmp_path, "--json")
        assert result.exit_code == 0
        parsed = BenchmarkResult.model_validate_json(result.stdout)
        assert parsed.report_status == "ok"  # report succeeded before stop blew up

    def test_env_unset_skips_with_note(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path)
        assert result.exit_code == 0
        assert "MAESTRO_ARBITER_BIN unset" in result.output
