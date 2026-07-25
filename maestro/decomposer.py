"""Project decomposition into independent workstreams.

This module uses Claude CLI to analyze a project description
and decompose it into independent, non-overlapping work units
(workstreams). It also generates spec files for each workstream.
"""

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from maestro._vendor.obs import child_env
from maestro.models import SPEC_PREFIX, WorkstreamConfig


if TYPE_CHECKING:
    from maestro.domain import DomainProfile


class DecomposerError(Exception):
    """Base exception for decomposition errors."""


def resolve_spec_gen_settings(
    domain: "DomainProfile | None", legacy_budget_usd: float | None
) -> tuple[float | None, float | None]:
    """Resolve (budget_usd, timeout_minutes) for spec generation (§9 SSOT).

    When ``domain.spec_gen`` is set it is the single source of truth; otherwise
    the legacy ``spec_runner.spec_gen_budget_usd`` supplies the budget and there
    is no generation timeout. Preflight guarantees the domain budget is not null
    when ``domain.spec_gen`` is configured, so wiring it here can never fall
    back to an uncapped run.
    """
    if domain is not None and domain.spec_gen is not None:
        return domain.spec_gen.budget_usd, domain.spec_gen.timeout_minutes
    return legacy_budget_usd, None


class ScopeOverlapWarning:
    """Warning about overlapping scopes between workstreams."""

    def __init__(
        self,
        workstream_a: str,
        workstream_b: str,
        overlapping_patterns: list[str],
    ) -> None:
        self.workstream_a = workstream_a
        self.workstream_b = workstream_b
        self.overlapping_patterns = overlapping_patterns

    def __str__(self) -> str:
        patterns = ", ".join(self.overlapping_patterns)
        return (
            f"Scope overlap between '{self.workstream_a}' and "
            f"'{self.workstream_b}': {patterns}"
        )


DECOMPOSE_PROMPT = """\
You are a project decomposition expert. Analyze the following \
project description and decompose it into independent, \
non-overlapping work units (workstreams).

## Project Description
{description}

## Repository Structure
{repo_tree}

## Requirements

1. Each workstream must be INDEPENDENT — it can be developed in \
parallel with others without conflicts.
2. Scopes must NOT overlap — each file/directory should belong \
to at most one workstream.
3. Each workstream should be a meaningful, self-contained feature \
or component.
4. Include 3-8 workstreams (not too granular, not too coarse).
5. Assign priorities: higher priority = should be done first.

## Output Format

Return ONLY a JSON array (no markdown, no explanation):
```json
[
  {{
    "id": "short-kebab-case-id",
    "title": "Human-readable title",
    "description": "Detailed description of what to implement",
    "scope": ["src/module/**", "tests/test_module*"],
    "depends_on": [],
    "priority": 0
  }}
]
```

Priority: higher number = higher priority (0-100).
depends_on: list of workstream IDs that must complete first.
scope: glob patterns for files/dirs this workstream will modify.
"""


class ProjectDecomposer:
    """Decomposes a project into independent workstreams.

    Uses Claude CLI to analyze the project and produce
    non-overlapping task groups with spec files.
    """

    def __init__(
        self,
        repo_path: Path,
        claude_command: str = "claude",
        spec_gen_budget_usd: float | None = 1.0,
        spec_gen_timeout_minutes: float | None = None,
    ) -> None:
        """Initialize the decomposer.

        Args:
            repo_path: Path to the git repository.
            claude_command: Claude CLI command name.
            spec_gen_budget_usd: USD cap for `spec-runner plan --full`;
                None disables the cap.
            spec_gen_timeout_minutes: wall-clock cap for spec generation;
                None keeps the built-in default (30 min).
        """
        self._repo_path = repo_path
        self._claude_command = claude_command
        self._spec_gen_budget_usd = spec_gen_budget_usd
        self._spec_gen_timeout_minutes = spec_gen_timeout_minutes
        self._logger = logging.getLogger(__name__)

    def _get_repo_tree(self, max_depth: int = 3) -> str:
        """Get repository directory tree.

        Args:
            max_depth: Maximum depth for tree output.

        Returns:
            Tree-like string of the repository structure.
        """
        try:
            result = subprocess.run(
                [
                    "find",
                    ".",
                    "-maxdepth",
                    str(max_depth),
                    "-not",
                    "-path",
                    "./.git/*",
                    "-not",
                    "-path",
                    "./node_modules/*",
                    "-not",
                    "-path",
                    "./.venv/*",
                    "-not",
                    "-path",
                    "./__pycache__/*",
                ],
                cwd=self._repo_path,
                capture_output=True,
                text=True,
                check=False,
            )
            return result.stdout[:5000]  # Limit size
        except FileNotFoundError:
            return "(unable to list directory)"

    def _run_claude(self, prompt: str, timeout_minutes: int = 15) -> str:
        """Run Claude CLI with a prompt.

        Args:
            prompt: The prompt to send.
            timeout_minutes: Timeout in minutes.

        Returns:
            Claude's response text.

        Raises:
            DecomposerError: If Claude CLI fails.
        """
        cmd = [
            self._claude_command,
            "--print",
            "-p",
            prompt,
            "--disallowedTools",
            "Edit",
            "Write",
            "Bash",
            "NotebookEdit",
        ]

        try:
            result = subprocess.run(
                cmd,
                cwd=self._repo_path,
                capture_output=True,
                text=True,
                timeout=timeout_minutes * 60,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            msg = f"Claude CLI timed out after {timeout_minutes} minutes"
            raise DecomposerError(msg) from e
        except FileNotFoundError as e:
            msg = f"Claude CLI command '{self._claude_command}' not found"
            raise DecomposerError(msg) from e

        if result.returncode != 0:
            msg = (
                f"Claude CLI failed with code "
                f"{result.returncode}: {result.stderr[:500]}"
            )
            raise DecomposerError(msg)

        return result.stdout

    def _parse_decomposition(self, response: str) -> list[WorkstreamConfig]:
        """Parse Claude's JSON response into WorkstreamConfigs.

        Args:
            response: Claude's response containing JSON.

        Returns:
            List of validated WorkstreamConfig objects.

        Raises:
            DecomposerError: If parsing fails.
        """
        # Extract JSON from response (may have markdown)
        json_str = response.strip()

        # Try to find JSON array in response
        start = json_str.find("[")
        end = json_str.rfind("]")

        if start == -1 or end == -1:
            msg = "No JSON array found in Claude response"
            raise DecomposerError(msg)

        json_str = json_str[start : end + 1]

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            msg = f"Failed to parse JSON response: {e}"
            raise DecomposerError(msg) from e

        if not isinstance(data, list):
            msg = "Expected JSON array of workstreams"
            raise DecomposerError(msg)

        try:
            return [WorkstreamConfig(**item) for item in data]
        except Exception as e:
            msg = f"Failed to validate workstreams: {e}"
            raise DecomposerError(msg) from e

    def decompose(self, project_description: str) -> list[WorkstreamConfig]:
        """Decompose project into independent workstreams.

        Uses Claude CLI to analyze the project and produce
        non-overlapping task groups.

        Args:
            project_description: Text description of the project.

        Returns:
            List of WorkstreamConfig objects.

        Raises:
            DecomposerError: If decomposition fails.
        """
        repo_tree = self._get_repo_tree()

        prompt = DECOMPOSE_PROMPT.format(
            description=project_description,
            repo_tree=repo_tree,
        )

        self._logger.info("Decomposing project via Claude CLI")
        response = self._run_claude(prompt)

        workstreams = self._parse_decomposition(response)

        if not workstreams:
            msg = "Decomposition produced no workstreams"
            raise DecomposerError(msg)

        # Validate non-overlap
        warnings = self.validate_non_overlap(workstreams)
        for w in warnings:
            self._logger.warning(str(w))

        self._logger.info("Decomposed into %d workstreams", len(workstreams))
        return workstreams

    async def generate_spec(
        self,
        workstream: WorkstreamConfig,
        workspace_path: Path,
        timeout_minutes: float | None = None,
        *,
        on_pid: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        """Generate spec files by delegating to `spec-runner plan --full`.

        Writes spec/maestro-{requirements,design,tasks}.md into the workspace.
        spec-runner owns the tasks.md format (no built-in prompt copy).

        `timeout_minutes` overrides the configured spec-gen timeout for this
        call; when omitted it falls back to `spec_gen_timeout_minutes` (from
        `domain.spec_gen`), then to the built-in 30-minute default.

        Raises:
            DecomposerError: if spec-runner is missing, exits non-zero,
                times out, or exits 0 without producing spec/maestro-tasks.md.
        """
        effective_timeout = (
            timeout_minutes
            if timeout_minutes is not None
            else self._spec_gen_timeout_minutes
            if self._spec_gen_timeout_minutes is not None
            else 30
        )
        spec_dir = workspace_path / "spec"
        spec_dir.mkdir(exist_ok=True)

        description = (
            f"Title: {workstream.title}\n\n"
            f"Description: {workstream.description}\n\n"
            f"Scope: {', '.join(workstream.scope)}"
        )
        desc_file = tempfile.NamedTemporaryFile(  # noqa: SIM115
            "w", encoding="utf-8", suffix=".md", delete=False
        )
        desc_path = Path(desc_file.name)
        try:
            with desc_file:
                desc_file.write(description)

            cmd = [
                "spec-runner",
                "plan",
                "--full",
                "--from-file",
                desc_file.name,
                "--no-branch",
                "--no-commit",
                "--no-interactive",
                "--spec-prefix",
                SPEC_PREFIX,
            ]
            if self._spec_gen_budget_usd is not None:
                cmd += ["--budget", str(self._spec_gen_budget_usd)]

            self._logger.info(
                "Generating spec for workstream '%s' via spec-runner plan --full",
                workstream.id,
            )
            await self._run_spec_runner(
                cmd, workspace_path, effective_timeout, on_pid=on_pid
            )
        finally:
            desc_path.unlink(missing_ok=True)  # noqa: ASYNC240

        tasks_path = spec_dir / f"{SPEC_PREFIX}tasks.md"
        if not tasks_path.is_file():
            msg = (
                f"spec-runner plan --full exited 0 but spec/{SPEC_PREFIX}tasks.md "
                f"was not created (workstream '{workstream.id}')"
            )
            raise DecomposerError(msg)
        self._logger.info("Spec generated for workstream '%s'", workstream.id)

    async def _run_spec_runner(
        self,
        cmd: list[str],
        cwd: Path,
        timeout_minutes: float,
        *,
        on_pid: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        """Run a spec-runner subprocess; report its pid via on_pid, and
        terminate it on persist-failure/cancel/timeout."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env={**os.environ, **child_env()},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            msg = "spec-runner command not found — is spec-runner installed?"
            raise DecomposerError(msg) from e

        if on_pid is not None:
            # Persist the pid before awaiting the process.
            try:
                await on_pid(proc.pid)
            except BaseException:
                # Persist failed OR the task was cancelled mid-persist — we
                # cannot track this process, so terminate it rather than leave
                # an orphan, and propagate (re-raises CancelledError too).
                await self._terminate(proc)
                raise

        try:
            _out, err = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_minutes * 60
            )
        except (TimeoutError, asyncio.CancelledError) as e:
            await self._terminate(proc)
            if isinstance(e, asyncio.CancelledError):
                raise  # shutdown-driven; propagate so the caller can go READY
            msg = f"spec-runner plan --full timed out after {timeout_minutes} min"
            raise DecomposerError(msg) from e

        if proc.returncode != 0:
            stderr_text = err.decode("utf-8", "replace")[:500] if err else ""
            msg = (
                f"spec-runner plan --full failed with code "
                f"{proc.returncode}: {stderr_text}"
            )
            raise DecomposerError(msg)

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        """Terminate a spec-runner subprocess, escalating to kill."""
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            await proc.wait()

    @staticmethod
    def validate_non_overlap(
        workstreams: list[WorkstreamConfig],
    ) -> list[ScopeOverlapWarning]:
        """Check that workstreams scopes don't overlap.

        Args:
            workstreams: List of workstreams to check.

        Returns:
            List of warnings for overlapping scopes.
        """
        warnings: list[ScopeOverlapWarning] = []

        for i, a in enumerate(workstreams):
            for b in workstreams[i + 1 :]:
                overlaps: list[str] = []
                for pattern_a in a.scope:
                    for pattern_b in b.scope:
                        if _patterns_overlap(pattern_a, pattern_b):
                            overlaps.append(f"{pattern_a} <-> {pattern_b}")

                if overlaps:
                    warnings.append(ScopeOverlapWarning(a.id, b.id, overlaps))

        return warnings


def _patterns_overlap(pattern_a: str, pattern_b: str) -> bool:
    """Check if two glob patterns could match same files.

    Simple heuristic: check if one pattern matches the other
    or if they share a common prefix directory.

    Args:
        pattern_a: First glob pattern.
        pattern_b: Second glob pattern.

    Returns:
        True if patterns could overlap.
    """
    # Direct match
    if fnmatch(pattern_a, pattern_b) or fnmatch(pattern_b, pattern_a):
        return True

    # Same base directory with wildcards
    base_a = pattern_a.split("/")[0] if "/" in pattern_a else ""
    base_b = pattern_b.split("/")[0] if "/" in pattern_b else ""

    if base_a and base_b and base_a == base_b:
        # Same top-level directory — could overlap
        # More precise check: strip base and compare rest
        rest_a = "/".join(pattern_a.split("/")[1:])
        rest_b = "/".join(pattern_b.split("/")[1:])
        if rest_a == "**" or rest_b == "**":
            return True
        if fnmatch(rest_a, rest_b) or fnmatch(rest_b, rest_a):
            return True

    return False
