"""Tests for the Agent Spawner module."""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro.models import AgentType, Task
from maestro.spawners import (
    AgentSpawner,
    AiderSpawner,
    AnnounceSpawner,
    ClaudeCodeSpawner,
    CodexSpawner,
)


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def sample_task(temp_dir: Path) -> Task:
    """Provide a sample task for testing."""
    return Task(
        id="test-task-1",
        title="Implement Feature X",
        prompt="Please implement feature X with the following requirements...",
        workdir=str(temp_dir),
        agent_type=AgentType.CLAUDE_CODE,
        scope=["src/module.py", "tests/test_module.py"],
    )


@pytest.fixture
def sample_task_no_scope(temp_dir: Path) -> Task:
    """Provide a sample task without scope for testing."""
    return Task(
        id="test-task-2",
        title="Fix Bug Y",
        prompt="Please fix the bug described in issue #123",
        workdir=str(temp_dir),
        agent_type=AgentType.CLAUDE_CODE,
    )


@pytest.fixture
def claude_spawner() -> ClaudeCodeSpawner:
    """Provide a Claude Code spawner instance."""
    return ClaudeCodeSpawner()


# =============================================================================
# Unit Tests: AgentSpawner ABC
# =============================================================================


class TestAgentSpawnerABC:
    """Tests for the AgentSpawner abstract base class."""

    def test_cannot_instantiate_abc(self) -> None:
        """Test that AgentSpawner cannot be instantiated directly."""
        with pytest.raises(TypeError, match="abstract"):
            AgentSpawner()  # type: ignore[abstract]

    def test_concrete_spawner_must_implement_agent_type(self) -> None:
        """Test that concrete spawners must implement agent_type."""

        class IncompleteSpawner(AgentSpawner):
            def is_available(self) -> bool:
                return True

            def spawn(
                self,
                task: Task,
                context: str,
                workdir: Path,
                log_file: Path,
                retry_context: str = "",
            ) -> subprocess.Popen[bytes]:
                return MagicMock()

        with pytest.raises(TypeError, match="agent_type"):
            IncompleteSpawner()  # type: ignore[abstract]

    def test_concrete_spawner_must_implement_is_available(self) -> None:
        """Test that concrete spawners must implement is_available."""

        class IncompleteSpawner(AgentSpawner):
            @property
            def agent_type(self) -> str:
                return "test"

            def spawn(
                self,
                task: Task,
                context: str,
                workdir: Path,
                log_file: Path,
                retry_context: str = "",
            ) -> subprocess.Popen[bytes]:
                return MagicMock()

        with pytest.raises(TypeError, match="is_available"):
            IncompleteSpawner()  # type: ignore[abstract]

    def test_concrete_spawner_must_implement_spawn(self) -> None:
        """Test that concrete spawners must implement spawn."""

        class IncompleteSpawner(AgentSpawner):
            @property
            def agent_type(self) -> str:
                return "test"

            def is_available(self) -> bool:
                return True

        with pytest.raises(TypeError, match="spawn"):
            IncompleteSpawner()  # type: ignore[abstract]


# =============================================================================
# Unit Tests: Prompt Building
# =============================================================================


class TestPromptBuilding:
    """Tests for prompt building functionality."""

    def test_build_prompt_with_context_and_scope(
        self,
        claude_spawner: ClaudeCodeSpawner,
        sample_task: Task,
    ) -> None:
        """Test prompt building with context and scope."""
        context = "[task-0]: Completed initial setup successfully."
        prompt = claude_spawner.build_prompt(sample_task, context)

        # Should contain task title
        assert sample_task.title in prompt
        # Should contain task prompt
        assert sample_task.prompt in prompt
        # Should contain context
        assert context in prompt
        # Should contain scope
        assert "src/module.py" in prompt
        assert "tests/test_module.py" in prompt

    def test_build_prompt_without_context(
        self,
        claude_spawner: ClaudeCodeSpawner,
        sample_task: Task,
    ) -> None:
        """Test prompt building without context."""
        prompt = claude_spawner.build_prompt(sample_task, "")

        # Should contain task title
        assert sample_task.title in prompt
        # Should contain fallback message for no context
        assert "No prior context available" in prompt

    def test_build_prompt_without_scope(
        self,
        claude_spawner: ClaudeCodeSpawner,
        sample_task_no_scope: Task,
    ) -> None:
        """Test prompt building without scope restriction."""
        context = "Some context"
        prompt = claude_spawner.build_prompt(sample_task_no_scope, context)

        # Should indicate any files can be modified
        assert "any" in prompt.lower()

    def test_build_prompt_structure(
        self,
        claude_spawner: ClaudeCodeSpawner,
        sample_task: Task,
    ) -> None:
        """Test that prompt has expected structure."""
        context = "Previous task output"
        prompt = claude_spawner.build_prompt(sample_task, context)

        # Verify sections exist in order
        task_pos = prompt.find("Task:")
        prompt_pos = prompt.find(sample_task.prompt)
        context_pos = prompt.find("Context from completed dependencies:")
        scope_pos = prompt.find("Scope (files you can modify):")

        assert task_pos >= 0
        assert prompt_pos > task_pos
        assert context_pos > prompt_pos
        assert scope_pos > context_pos


# =============================================================================
# Unit Tests: is_available Check
# =============================================================================


class TestIsAvailable:
    """Tests for is_available functionality."""

    def test_claude_available_when_in_path(
        self,
        claude_spawner: ClaudeCodeSpawner,
    ) -> None:
        """Test is_available returns True when claude is in PATH."""
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            assert claude_spawner.is_available() is True

    def test_claude_unavailable_when_not_in_path(
        self,
        claude_spawner: ClaudeCodeSpawner,
    ) -> None:
        """Test is_available returns False when claude is not in PATH."""
        with patch("shutil.which", return_value=None):
            assert claude_spawner.is_available() is False


# =============================================================================
# Unit Tests: ClaudeCodeSpawner
# =============================================================================


class TestClaudeCodeSpawner:
    """Tests for ClaudeCodeSpawner."""

    def test_agent_type(self, claude_spawner: ClaudeCodeSpawner) -> None:
        """Test that agent_type returns correct value."""
        assert claude_spawner.agent_type == "claude_code"

    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("os.open")
    def test_spawn_creates_process_with_correct_args(
        self,
        mock_os_open: MagicMock,
        mock_os_close: MagicMock,
        mock_popen: MagicMock,
        claude_spawner: ClaudeCodeSpawner,
        sample_task: Task,
        temp_dir: Path,
    ) -> None:
        """Test that spawn creates process with correct arguments."""
        mock_process = MagicMock()
        mock_popen.return_value = mock_process
        mock_os_open.return_value = 42  # Mock file descriptor

        log_file = temp_dir / "task.log"
        context = "Some context"
        workdir = Path(sample_task.workdir)

        # Create log file parent directory
        log_file.parent.mkdir(parents=True, exist_ok=True)

        result = claude_spawner.spawn(sample_task, context, workdir, log_file)

        # Verify os.open was called with correct flags
        mock_os_open.assert_called_once()
        open_call = mock_os_open.call_args
        assert str(log_file) in open_call[0]

        # Verify Popen was called
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args

        # Verify command structure
        cmd = call_args[0][0]
        assert cmd[0] == "claude"
        assert "--print" in cmd
        assert "--output-format" in cmd
        assert "json" in cmd
        assert "-p" in cmd

        # Verify workdir
        assert call_args[1]["cwd"] == workdir

        # Verify stdout is the fd
        assert call_args[1]["stdout"] == 42

        # Verify stderr redirected to stdout
        assert call_args[1]["stderr"] == subprocess.STDOUT

        # Verify fd was closed
        mock_os_close.assert_called_once_with(42)

        # Verify return value
        assert result == mock_process

    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("os.open")
    def test_spawn_prompt_contains_task_info(
        self,
        mock_os_open: MagicMock,
        mock_os_close: MagicMock,
        mock_popen: MagicMock,
        claude_spawner: ClaudeCodeSpawner,
        sample_task: Task,
        temp_dir: Path,
    ) -> None:
        """Test that spawn passes prompt with task information."""
        mock_popen.return_value = MagicMock()
        mock_os_open.return_value = 42

        log_file = temp_dir / "task.log"
        context = "Previous task completed"
        workdir = Path(sample_task.workdir)

        # Create log file parent directory
        log_file.parent.mkdir(parents=True, exist_ok=True)

        claude_spawner.spawn(sample_task, context, workdir, log_file)

        # Get the prompt from call args
        call_args = mock_popen.call_args
        cmd = call_args[0][0]

        # Find -p argument and get the prompt
        p_index = cmd.index("-p")
        prompt = cmd[p_index + 1]

        # Verify prompt content
        assert sample_task.title in prompt
        assert sample_task.prompt in prompt
        assert context in prompt


# =============================================================================
# Integration Tests: Spawn with Mock (Echo)
# =============================================================================


def _spawn_with_log_fd(
    cmd: list[str],
    workdir: Path,
    log_file: Path,
) -> subprocess.Popen[bytes]:
    """Helper to spawn process with proper fd management."""
    fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        process = subprocess.Popen(
            cmd,
            cwd=workdir,
            stdout=fd,
            stderr=subprocess.STDOUT,
        )
    finally:
        os.close(fd)
    return process


class TestSpawnIntegration:
    """Integration tests for spawning with real subprocess."""

    @pytest.mark.integration
    def test_spawn_with_echo_command(self, temp_dir: Path) -> None:
        """Test spawning using echo as a mock command."""

        class EchoSpawner(AgentSpawner):
            """Test spawner that uses echo instead of claude."""

            @property
            def agent_type(self) -> str:
                return "echo_test"

            def is_available(self) -> bool:
                return True

            def spawn(
                self,
                task: Task,
                context: str,
                workdir: Path,
                log_file: Path,
                retry_context: str = "",
            ) -> subprocess.Popen[bytes]:
                prompt = self.build_prompt(task, context, retry_context)
                return _spawn_with_log_fd(["echo", prompt], workdir, log_file)

        spawner = EchoSpawner()
        task = Task(
            id="echo-task",
            title="Echo Test",
            prompt="Test prompt content",
            workdir=str(temp_dir),
            agent_type=AgentType.CLAUDE_CODE,
            scope=["file1.py"],
        )

        log_file = temp_dir / "echo.log"
        context = "Context from dependency"

        # Spawn the process
        process = spawner.spawn(task, context, temp_dir, log_file)

        # Wait for completion
        return_code = process.wait()

        # Verify success
        assert return_code == 0

        # Verify log file content
        log_content = log_file.read_text()
        assert task.title in log_content
        assert task.prompt in log_content
        assert context in log_content
        assert "file1.py" in log_content

    @pytest.mark.integration
    def test_spawn_captures_stderr(self, temp_dir: Path) -> None:
        """Test that stderr is captured in log file."""

        class StderrSpawner(AgentSpawner):
            """Test spawner that writes to stderr."""

            @property
            def agent_type(self) -> str:
                return "stderr_test"

            def is_available(self) -> bool:
                return True

            def spawn(
                self,
                task: Task,
                context: str,
                workdir: Path,
                log_file: Path,
                retry_context: str = "",
            ) -> subprocess.Popen[bytes]:
                return _spawn_with_log_fd(
                    ["sh", "-c", "echo 'error message' >&2"],
                    workdir,
                    log_file,
                )

        spawner = StderrSpawner()
        task = Task(
            id="stderr-task",
            title="Stderr Test",
            prompt="Test",
            workdir=str(temp_dir),
        )

        log_file = temp_dir / "stderr.log"

        process = spawner.spawn(task, "", temp_dir, log_file)
        process.wait()

        log_content = log_file.read_text()
        assert "error message" in log_content

    @pytest.mark.integration
    def test_spawn_returns_exit_code(self, temp_dir: Path) -> None:
        """Test that spawn returns correct exit code."""

        class FailingSpawner(AgentSpawner):
            """Test spawner that returns non-zero exit code."""

            @property
            def agent_type(self) -> str:
                return "failing_test"

            def is_available(self) -> bool:
                return True

            def spawn(
                self,
                task: Task,
                context: str,
                workdir: Path,
                log_file: Path,
                retry_context: str = "",
            ) -> subprocess.Popen[bytes]:
                return _spawn_with_log_fd(
                    ["sh", "-c", "exit 42"],
                    workdir,
                    log_file,
                )

        spawner = FailingSpawner()
        task = Task(
            id="fail-task",
            title="Fail Test",
            prompt="Test",
            workdir=str(temp_dir),
        )

        log_file = temp_dir / "fail.log"

        process = spawner.spawn(task, "", temp_dir, log_file)
        return_code = process.wait()

        assert return_code == 42


# =============================================================================
# Tests: Spawner Inheritance
# =============================================================================


class TestSpawnerInheritance:
    """Tests for spawner inheritance patterns."""

    def test_custom_spawner_inherits_build_prompt(self) -> None:
        """Test that custom spawners inherit build_prompt method."""

        class CustomSpawner(AgentSpawner):
            @property
            def agent_type(self) -> str:
                return "custom"

            def is_available(self) -> bool:
                return True

            def spawn(
                self,
                task: Task,
                context: str,
                workdir: Path,
                log_file: Path,
                retry_context: str = "",
            ) -> subprocess.Popen[bytes]:
                return MagicMock()

        spawner = CustomSpawner()
        task = Task(
            id="test",
            title="Test Task",
            prompt="Test prompt",
            workdir="/tmp",
        )

        # Should be able to use inherited build_prompt
        prompt = spawner.build_prompt(task, "context")
        assert "Test Task" in prompt
        assert "Test prompt" in prompt

    def test_custom_spawner_can_override_build_prompt(self) -> None:
        """Test that custom spawners can override build_prompt."""

        class CustomSpawner(AgentSpawner):
            @property
            def agent_type(self) -> str:
                return "custom"

            def is_available(self) -> bool:
                return True

            def spawn(
                self,
                task: Task,
                context: str,
                workdir: Path,
                log_file: Path,
                retry_context: str = "",
            ) -> subprocess.Popen[bytes]:
                return MagicMock()

            def build_prompt(
                self,
                task: Task,
                context: str,
                retry_context: str = "",
            ) -> str:
                return f"CUSTOM: {task.title}"

        spawner = CustomSpawner()
        task = Task(
            id="test",
            title="Test Task",
            prompt="Test prompt",
            workdir="/tmp",
        )

        prompt = spawner.build_prompt(task, "context")
        assert prompt == "CUSTOM: Test Task"


# =============================================================================
# Test Fixtures: Additional Spawners
# =============================================================================


@pytest.fixture
def codex_spawner() -> CodexSpawner:
    """Provide a Codex spawner instance."""
    return CodexSpawner()


@pytest.fixture
def aider_spawner() -> AiderSpawner:
    """Provide an Aider spawner instance."""
    return AiderSpawner()


@pytest.fixture
def announce_spawner() -> AnnounceSpawner:
    """Provide an Announce spawner instance."""
    return AnnounceSpawner()


# =============================================================================
# Unit Tests: CodexSpawner
# =============================================================================


class TestCodexSpawner:
    """Tests for CodexSpawner."""

    def test_agent_type(self, codex_spawner: CodexSpawner) -> None:
        """Test that agent_type returns correct value."""
        assert codex_spawner.agent_type == "codex_cli"

    def test_codex_available_when_in_path(
        self,
        codex_spawner: CodexSpawner,
    ) -> None:
        """Test is_available returns True when codex is in PATH."""
        with patch(
            "maestro.spawners.codex.shutil.which",
            return_value="/usr/local/bin/codex",
        ):
            assert codex_spawner.is_available() is True

    def test_codex_unavailable_when_not_in_path(
        self,
        codex_spawner: CodexSpawner,
    ) -> None:
        """Test is_available returns False when codex is not in PATH."""
        with patch(
            "maestro.spawners.codex.shutil.which",
            return_value=None,
        ):
            assert codex_spawner.is_available() is False

    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("os.open")
    def test_spawn_creates_process_with_correct_args(
        self,
        mock_os_open: MagicMock,
        mock_os_close: MagicMock,
        mock_popen: MagicMock,
        codex_spawner: CodexSpawner,
        sample_task: Task,
        temp_dir: Path,
    ) -> None:
        """Test that spawn creates process with correct arguments."""
        mock_process = MagicMock()
        mock_popen.return_value = mock_process
        mock_os_open.return_value = 42

        log_file = temp_dir / "task.log"
        context = "Some context"
        workdir = Path(sample_task.workdir)

        result = codex_spawner.spawn(sample_task, context, workdir, log_file)

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]

        assert cmd[0] == "codex"
        assert "--quiet" in cmd
        assert "--approval-mode" in cmd
        assert "auto-edit" in cmd
        assert call_args[1]["cwd"] == workdir
        assert call_args[1]["stdout"] == 42
        assert call_args[1]["stderr"] == subprocess.STDOUT
        mock_os_close.assert_called_once_with(42)
        assert result == mock_process

    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("os.open")
    def test_spawn_prompt_contains_task_info(
        self,
        mock_os_open: MagicMock,
        mock_os_close: MagicMock,
        mock_popen: MagicMock,
        codex_spawner: CodexSpawner,
        sample_task: Task,
        temp_dir: Path,
    ) -> None:
        """Test that spawn passes prompt with task information."""
        mock_popen.return_value = MagicMock()
        mock_os_open.return_value = 42

        log_file = temp_dir / "task.log"
        context = "Previous task completed"
        workdir = Path(sample_task.workdir)

        codex_spawner.spawn(sample_task, context, workdir, log_file)

        call_args = mock_popen.call_args
        cmd = call_args[0][0]

        # Prompt is the last positional argument for codex
        prompt = cmd[-1]
        assert sample_task.title in prompt
        assert sample_task.prompt in prompt
        assert context in prompt


# =============================================================================
# Unit Tests: AiderSpawner
# =============================================================================


class TestAiderSpawner:
    """Tests for AiderSpawner."""

    def test_agent_type(self, aider_spawner: AiderSpawner) -> None:
        """Test that agent_type returns correct value."""
        assert aider_spawner.agent_type == "aider"

    def test_aider_available_when_in_path(
        self,
        aider_spawner: AiderSpawner,
    ) -> None:
        """Test is_available returns True when aider is in PATH."""
        with patch(
            "maestro.spawners.aider.shutil.which",
            return_value="/usr/local/bin/aider",
        ):
            assert aider_spawner.is_available() is True

    def test_aider_unavailable_when_not_in_path(
        self,
        aider_spawner: AiderSpawner,
    ) -> None:
        """Test is_available returns False when aider is not in PATH."""
        with patch(
            "maestro.spawners.aider.shutil.which",
            return_value=None,
        ):
            assert aider_spawner.is_available() is False

    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("os.open")
    def test_spawn_creates_process_with_correct_args(
        self,
        mock_os_open: MagicMock,
        mock_os_close: MagicMock,
        mock_popen: MagicMock,
        aider_spawner: AiderSpawner,
        sample_task: Task,
        temp_dir: Path,
    ) -> None:
        """Test that spawn creates process with correct arguments."""
        mock_process = MagicMock()
        mock_popen.return_value = mock_process
        mock_os_open.return_value = 42

        log_file = temp_dir / "task.log"
        context = "Some context"
        workdir = Path(sample_task.workdir)

        result = aider_spawner.spawn(sample_task, context, workdir, log_file)

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]

        assert cmd[0] == "aider"
        assert "--yes-always" in cmd
        assert "--no-auto-commits" in cmd
        assert "--message" in cmd
        assert call_args[1]["cwd"] == workdir
        assert call_args[1]["stdout"] == 42
        assert call_args[1]["stderr"] == subprocess.STDOUT
        mock_os_close.assert_called_once_with(42)
        assert result == mock_process

    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("os.open")
    def test_spawn_includes_scope_files(
        self,
        mock_os_open: MagicMock,
        mock_os_close: MagicMock,
        mock_popen: MagicMock,
        aider_spawner: AiderSpawner,
        sample_task: Task,
        temp_dir: Path,
    ) -> None:
        """Test that spawn includes scope files as arguments."""
        mock_popen.return_value = MagicMock()
        mock_os_open.return_value = 42

        log_file = temp_dir / "task.log"
        workdir = Path(sample_task.workdir)

        aider_spawner.spawn(sample_task, "", workdir, log_file)

        call_args = mock_popen.call_args
        cmd = call_args[0][0]

        # Scope files should be appended after --message <prompt>
        assert "src/module.py" in cmd
        assert "tests/test_module.py" in cmd

    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("os.open")
    def test_spawn_no_scope_files_when_empty(
        self,
        mock_os_open: MagicMock,
        mock_os_close: MagicMock,
        mock_popen: MagicMock,
        aider_spawner: AiderSpawner,
        sample_task_no_scope: Task,
        temp_dir: Path,
    ) -> None:
        """Test that spawn omits scope files when scope is empty."""
        mock_popen.return_value = MagicMock()
        mock_os_open.return_value = 42

        log_file = temp_dir / "task.log"
        workdir = Path(sample_task_no_scope.workdir)

        aider_spawner.spawn(sample_task_no_scope, "", workdir, log_file)

        call_args = mock_popen.call_args
        cmd = call_args[0][0]

        # Command should be: aider --yes-always --no-auto-commits
        #                     --message <prompt>
        # No extra file args
        assert len(cmd) == 5

    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("os.open")
    def test_spawn_prompt_contains_task_info(
        self,
        mock_os_open: MagicMock,
        mock_os_close: MagicMock,
        mock_popen: MagicMock,
        aider_spawner: AiderSpawner,
        sample_task: Task,
        temp_dir: Path,
    ) -> None:
        """Test that spawn passes prompt with task information."""
        mock_popen.return_value = MagicMock()
        mock_os_open.return_value = 42

        log_file = temp_dir / "task.log"
        context = "Previous task completed"
        workdir = Path(sample_task.workdir)

        aider_spawner.spawn(sample_task, context, workdir, log_file)

        call_args = mock_popen.call_args
        cmd = call_args[0][0]

        # Find --message and get the prompt
        msg_index = cmd.index("--message")
        prompt = cmd[msg_index + 1]

        assert sample_task.title in prompt
        assert sample_task.prompt in prompt
        assert context in prompt


# =============================================================================
# Unit Tests: AnnounceSpawner
# =============================================================================


class TestAnnounceSpawner:
    """Tests for AnnounceSpawner."""

    def test_agent_type(self, announce_spawner: AnnounceSpawner) -> None:
        """Test that agent_type returns correct value."""
        assert announce_spawner.agent_type == "announce"

    def test_announce_always_available(self, announce_spawner: AnnounceSpawner) -> None:
        """Test that announce spawner is always available."""
        assert announce_spawner.is_available() is True

    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("os.open")
    def test_spawn_creates_echo_process(
        self,
        mock_os_open: MagicMock,
        mock_os_close: MagicMock,
        mock_popen: MagicMock,
        announce_spawner: AnnounceSpawner,
        sample_task: Task,
        temp_dir: Path,
    ) -> None:
        """Test that spawn creates an echo process."""
        mock_process = MagicMock()
        mock_popen.return_value = mock_process
        mock_os_open.return_value = 42

        log_file = temp_dir / "task.log"
        context = "Some context"
        workdir = Path(sample_task.workdir)

        result = announce_spawner.spawn(sample_task, context, workdir, log_file)

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]

        assert cmd[0] == "echo"
        assert call_args[1]["cwd"] == workdir
        assert call_args[1]["stdout"] == 42
        assert call_args[1]["stderr"] == subprocess.STDOUT
        mock_os_close.assert_called_once_with(42)
        assert result == mock_process

    @pytest.mark.integration
    def test_announce_spawn_writes_to_log(
        self,
        announce_spawner: AnnounceSpawner,
        temp_dir: Path,
    ) -> None:
        """Integration test: announce spawner writes to log file."""
        task = Task(
            id="announce-task",
            title="Milestone Alpha",
            prompt="All alpha tasks completed",
            workdir=str(temp_dir),
            agent_type=AgentType.ANNOUNCE,
            scope=["docs/"],
        )

        log_file = temp_dir / "announce.log"
        context = "Previous milestones done"

        process = announce_spawner.spawn(task, context, temp_dir, log_file)
        return_code = process.wait()

        assert return_code == 0
        log_content = log_file.read_text()
        assert task.title in log_content
        assert task.prompt in log_content
        assert context in log_content


# =============================================================================
# Unit Tests: is_available checks (all spawners)
# =============================================================================


class TestIsAvailableAllSpawners:
    """Cross-spawner is_available tests."""

    def test_codex_checks_codex_binary(self) -> None:
        """Test that CodexSpawner checks for 'codex' binary."""
        spawner = CodexSpawner()
        with patch("maestro.spawners.codex.shutil.which") as mock_which:
            mock_which.return_value = None
            assert spawner.is_available() is False
            mock_which.assert_called_once_with("codex")

    def test_aider_checks_aider_binary(self) -> None:
        """Test that AiderSpawner checks for 'aider' binary."""
        spawner = AiderSpawner()
        with patch("maestro.spawners.aider.shutil.which") as mock_which:
            mock_which.return_value = None
            assert spawner.is_available() is False
            mock_which.assert_called_once_with("aider")

    def test_claude_checks_claude_binary(self) -> None:
        """Test that ClaudeCodeSpawner checks for 'claude' binary."""
        spawner = ClaudeCodeSpawner()
        with patch("maestro.spawners.claude_code.shutil.which") as mock_which:
            mock_which.return_value = None
            assert spawner.is_available() is False
            mock_which.assert_called_once_with("claude")

    def test_announce_needs_no_binary(self) -> None:
        """Test that AnnounceSpawner requires no external binary."""
        spawner = AnnounceSpawner()
        # Should always be available regardless of PATH
        assert spawner.is_available() is True


# =============================================================================
# Integration Tests: Spawn with Mock (Additional Spawners)
# =============================================================================


class TestSpawnIntegrationAdditional:
    """Integration tests for new spawners with real subprocess."""

    @pytest.mark.integration
    def test_codex_spawn_with_mock(self, temp_dir: Path) -> None:
        """Test CodexSpawner with mocked Popen arguments."""
        spawner = CodexSpawner()
        task = Task(
            id="codex-task",
            title="Codex Test",
            prompt="Test prompt for codex",
            workdir=str(temp_dir),
            agent_type=AgentType.CODEX,
            scope=["app.py"],
        )

        log_file = temp_dir / "codex.log"

        with (
            patch("subprocess.Popen") as mock_popen,
            patch("os.open", return_value=42),
            patch("os.close"),
        ):
            mock_process = MagicMock()
            mock_process.wait.return_value = 0
            mock_popen.return_value = mock_process

            process = spawner.spawn(task, "ctx", temp_dir, log_file)

            cmd = mock_popen.call_args[0][0]
            assert cmd[0] == "codex"
            assert "--quiet" in cmd
            assert process.wait() == 0

    @pytest.mark.integration
    def test_aider_spawn_with_mock(self, temp_dir: Path) -> None:
        """Test AiderSpawner with mocked Popen arguments."""
        spawner = AiderSpawner()
        task = Task(
            id="aider-task",
            title="Aider Test",
            prompt="Test prompt for aider",
            workdir=str(temp_dir),
            agent_type=AgentType.AIDER,
            scope=["main.py", "utils.py"],
        )

        log_file = temp_dir / "aider.log"

        with (
            patch("subprocess.Popen") as mock_popen,
            patch("os.open", return_value=42),
            patch("os.close"),
        ):
            mock_process = MagicMock()
            mock_process.wait.return_value = 0
            mock_popen.return_value = mock_process

            process = spawner.spawn(task, "ctx", temp_dir, log_file)

            cmd = mock_popen.call_args[0][0]
            assert cmd[0] == "aider"
            assert "--yes-always" in cmd
            assert "main.py" in cmd
            assert "utils.py" in cmd
            assert process.wait() == 0

    @pytest.mark.integration
    def test_announce_spawn_real_process(self, temp_dir: Path) -> None:
        """Test AnnounceSpawner with real subprocess (echo)."""
        spawner = AnnounceSpawner()
        task = Task(
            id="announce-int",
            title="Integration Announce",
            prompt="Announce integration test",
            workdir=str(temp_dir),
            agent_type=AgentType.ANNOUNCE,
        )

        log_file = temp_dir / "announce_int.log"

        process = spawner.spawn(task, "context", temp_dir, log_file)
        return_code = process.wait()

        assert return_code == 0
        log_content = log_file.read_text()
        assert "Integration Announce" in log_content
