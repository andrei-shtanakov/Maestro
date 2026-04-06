"""Project decomposition into independent zadachi.

This module uses Claude CLI to analyze a project description
and decompose it into independent, non-overlapping work units
(zadachi). It also generates spec files for each zadacha.
"""

import json
import logging
import subprocess
from fnmatch import fnmatch
from pathlib import Path

from maestro.models import ZadachaConfig


class DecomposerError(Exception):
    """Base exception for decomposition errors."""


class ScopeOverlapWarning:
    """Warning about overlapping scopes between zadachi."""

    def __init__(
        self,
        zadacha_a: str,
        zadacha_b: str,
        overlapping_patterns: list[str],
    ) -> None:
        self.zadacha_a = zadacha_a
        self.zadacha_b = zadacha_b
        self.overlapping_patterns = overlapping_patterns

    def __str__(self) -> str:
        patterns = ", ".join(self.overlapping_patterns)
        return (
            f"Scope overlap between '{self.zadacha_a}' and "
            f"'{self.zadacha_b}': {patterns}"
        )


DECOMPOSE_PROMPT = """\
You are a project decomposition expert. Analyze the following \
project description and decompose it into independent, \
non-overlapping work units (zadachi).

## Project Description
{description}

## Repository Structure
{repo_tree}

## Requirements

1. Each zadacha must be INDEPENDENT — it can be developed in \
parallel with others without conflicts.
2. Scopes must NOT overlap — each file/directory should belong \
to at most one zadacha.
3. Each zadacha should be a meaningful, self-contained feature \
or component.
4. Include 3-8 zadachi (not too granular, not too coarse).
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
depends_on: list of zadacha IDs that must complete first.
scope: glob patterns for files/dirs this zadacha will modify.
"""

SPEC_GENERATION_PROMPT = """\
Generate a tasks.md file for the following development task. \
Output ONLY the file content, no explanations or preamble.

## Task
Title: {title}
Description: {description}
Scope: {scope}

## Required Format

spec-runner parses this EXACT format — deviations break parsing.

```
# Title — Tasks Specification

## Milestone 1: Core Implementation

### TASK-001: First Task Name
🔴 P0 | ⬜ TODO | Est: 2h

Description of what this task does.

**Checklist:**
- [ ] First step
- [ ] Second step

**Depends on:**

### TASK-002: Second Task Name
🟠 P1 | ⬜ TODO | Est: 1h

Description here.

**Checklist:**
- [ ] Step one
- [ ] Step two

**Depends on:** TASK-001
```

Rules for tasks.md:
- Task headers MUST be exactly: ### TASK-NNN: Name
- Metadata line MUST be: EMOJI PRIORITY | EMOJI STATUS | Est: TIME
- Priority emojis: 🔴 P0 (critical), 🟠 P1 (high), 🟡 P2 (medium), 🟢 P3 (low)
- Status MUST be: ⬜ TODO (for all new tasks)
- Estimate format: 1h, 2h, 1-2h, 1d
- Keep tasks granular (30min-4h each). Include test tasks.
- Every task MUST have a **Checklist:** section with checkboxes
"""


class ProjectDecomposer:
    """Decomposes a project into independent zadachi.

    Uses Claude CLI to analyze the project and produce
    non-overlapping task groups with spec files.
    """

    def __init__(
        self,
        repo_path: Path,
        claude_command: str = "claude",
    ) -> None:
        """Initialize the decomposer.

        Args:
            repo_path: Path to the git repository.
            claude_command: Claude CLI command name.
        """
        self._repo_path = repo_path
        self._claude_command = claude_command
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

    def _parse_decomposition(self, response: str) -> list[ZadachaConfig]:
        """Parse Claude's JSON response into ZadachaConfigs.

        Args:
            response: Claude's response containing JSON.

        Returns:
            List of validated ZadachaConfig objects.

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
            msg = "Expected JSON array of zadachi"
            raise DecomposerError(msg)

        try:
            return [ZadachaConfig(**item) for item in data]
        except Exception as e:
            msg = f"Failed to validate zadachi: {e}"
            raise DecomposerError(msg) from e

    def decompose(self, project_description: str) -> list[ZadachaConfig]:
        """Decompose project into independent zadachi.

        Uses Claude CLI to analyze the project and produce
        non-overlapping task groups.

        Args:
            project_description: Text description of the project.

        Returns:
            List of ZadachaConfig objects.

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

        zadachi = self._parse_decomposition(response)

        if not zadachi:
            msg = "Decomposition produced no zadachi"
            raise DecomposerError(msg)

        # Validate non-overlap
        warnings = self.validate_non_overlap(zadachi)
        for w in warnings:
            self._logger.warning(str(w))

        self._logger.info("Decomposed into %d zadachi", len(zadachi))
        return zadachi

    def generate_spec(
        self,
        zadacha: ZadachaConfig,
        workspace_path: Path,
    ) -> None:
        """Generate spec files for a zadacha.

        Creates spec/requirements.md, spec/design.md, and
        spec/tasks.md in the workspace directory.

        Args:
            zadacha: The zadacha configuration.
            workspace_path: Path to the workspace directory.

        Raises:
            DecomposerError: If spec generation fails.
        """
        spec_dir = workspace_path / "spec"
        spec_dir.mkdir(exist_ok=True)

        prompt = SPEC_GENERATION_PROMPT.format(
            title=zadacha.title,
            description=zadacha.description,
            scope=", ".join(zadacha.scope),
        )

        self._logger.info("Generating spec for zadacha '%s'", zadacha.id)
        response = self._run_claude(prompt)

        # Write tasks.md directly (prompt generates only tasks.md)
        tasks_path = spec_dir / "tasks.md"
        tasks_path.write_text(response.strip() + "\n")
        self._logger.info("Wrote %s", tasks_path)

    def _write_spec_files(self, spec_dir: Path, response: str) -> None:
        """Parse and write spec files from Claude response.

        Args:
            spec_dir: Directory to write spec files.
            response: Claude's response with file markers.
        """
        files: dict[str, list[str]] = {}
        current_file: str | None = None

        for line in response.split("\n"):
            if line.startswith("--- FILE:") and line.endswith("---"):
                # Extract filename
                filename = line.replace("--- FILE:", "").replace("---", "").strip()
                # Normalize: spec/tasks.md → tasks.md
                if filename.startswith("spec/"):
                    filename = filename[5:]
                current_file = filename
                files[current_file] = []
            elif current_file is not None:
                files[current_file].append(line)

        # If no file markers found, write entire response
        # as tasks.md
        if not files:
            self._logger.warning(
                "No file markers found in response, writing as tasks.md"
            )
            tasks_path = spec_dir / "tasks.md"
            tasks_path.write_text(response)
            return

        for filename, lines in files.items():
            filepath = spec_dir / filename
            content = "\n".join(lines).strip() + "\n"
            filepath.write_text(content)
            self._logger.info("Wrote %s", filepath)

    @staticmethod
    def validate_non_overlap(
        zadachi: list[ZadachaConfig],
    ) -> list[ScopeOverlapWarning]:
        """Check that zadachi scopes don't overlap.

        Args:
            zadachi: List of zadachi to check.

        Returns:
            List of warnings for overlapping scopes.
        """
        warnings: list[ScopeOverlapWarning] = []

        for i, a in enumerate(zadachi):
            for b in zadachi[i + 1 :]:
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
