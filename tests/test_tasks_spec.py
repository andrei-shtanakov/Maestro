"""Dangling-dependency validation of a generated tasks.md (#165).

spec-runner owns the tasks.md format and validates dependencies at RUN time,
exiting 1. That is correct but late: by then Maestro has paid for spec
generation and spawned a process. This validator runs right after
`plan --full` and before the executor, so a rework that references a task
from the *previous* revision of the file is caught while it is still cheap.

The prompt-level instruction in the rework addendum is prevention; this is the
guarantee. Correctness must not depend on an LLM obeying an instruction.
"""

from maestro.tasks_spec import (
    DanglingDependency,
    build_dangling_dependency_error,
    find_dangling_dependencies,
)


def _tasks(*blocks: str) -> str:
    return "# Tasks\n\n" + "\n\n".join(blocks) + "\n"


def _task(task_id: str, *, depends: str | None = None) -> str:
    body = f"### {task_id}: Do the thing\n\n- [ ] step one\n"
    if depends is not None:
        body += f"\n**Depends on:** {depends}\n"
    return body


class TestFindDangling:
    def test_valid_current_revision_passes(self) -> None:
        text = _tasks(
            _task("TASK-001"),
            _task("TASK-002", depends="[TASK-001]"),
            _task("TASK-003", depends="[TASK-001], [TASK-002]"),
        )

        assert find_dangling_dependencies(text) == []

    def test_reference_to_a_previous_revision_is_dangling(self) -> None:
        """The pilot's case: rework rewrote tasks.md, the decomposer kept
        pointing at TASK-021 from the revision it replaced."""
        text = _tasks(
            _task("TASK-022", depends="[TASK-021]"),
            _task("TASK-023", depends="[TASK-022]"),
        )

        assert find_dangling_dependencies(text) == [
            DanglingDependency(task_id="TASK-022", missing="TASK-021")
        ]

    def test_no_dependencies_is_fine(self) -> None:
        assert find_dangling_dependencies(_tasks(_task("TASK-001"))) == []

    def test_em_dash_means_no_dependencies(self) -> None:
        """spec-runner writes `—` for "none"; it is not a task id."""
        text = _tasks(_task("TASK-001", depends="—"))

        assert find_dangling_dependencies(text) == []

    def test_requirement_and_design_refs_are_not_dependencies(self) -> None:
        """The format filters refs by the prefixes the headers actually use.

        Without this, every `[REQ-001]` / `[DESIGN-004]` traceability
        reference on a Depends line would be reported as dangling and the
        validator would block every well-formed spec.
        """
        text = _tasks(
            _task("TASK-001"),
            _task("TASK-002", depends="[TASK-001], [REQ-001], [DESIGN-004]"),
        )

        assert find_dangling_dependencies(text) == []

    def test_native_prefixes_are_supported(self) -> None:
        """External projects number natively (KAP-002, ABC-17), not TASK-nnn."""
        text = _tasks(_task("KAP-001"), _task("KAP-002", depends="[KAP-001]"))

        assert find_dangling_dependencies(text) == []

    def test_native_prefix_dangling_is_caught(self) -> None:
        text = _tasks(_task("KAP-002", depends="[KAP-001]"))

        assert find_dangling_dependencies(text) == [
            DanglingDependency(task_id="KAP-002", missing="KAP-001")
        ]

    def test_forward_reference_within_the_file_is_valid(self) -> None:
        """Resolution is by membership, not by order — spec-runner schedules
        by the dependency graph, not by position in the file."""
        text = _tasks(_task("TASK-001", depends="[TASK-002]"), _task("TASK-002"))

        assert find_dangling_dependencies(text) == []

    def test_every_dangling_reference_is_reported(self) -> None:
        text = _tasks(
            _task("TASK-010", depends="[TASK-001], [TASK-002]"),
            _task("TASK-011", depends="[TASK-003]"),
        )

        assert find_dangling_dependencies(text) == [
            DanglingDependency(task_id="TASK-010", missing="TASK-001"),
            DanglingDependency(task_id="TASK-010", missing="TASK-002"),
            DanglingDependency(task_id="TASK-011", missing="TASK-003"),
        ]

    def test_self_reference_is_not_dangling(self) -> None:
        """Degenerate but present in the file: spec-runner's own cycle check
        owns that verdict, this validator only answers "does it exist"."""
        text = _tasks(_task("TASK-001", depends="[TASK-001]"))

        assert find_dangling_dependencies(text) == []

    def test_empty_file_has_no_dependencies(self) -> None:
        assert find_dangling_dependencies("") == []


class TestErrorMessage:
    def test_names_the_missing_id_and_the_referencing_task(self) -> None:
        danglings = [DanglingDependency(task_id="TASK-022", missing="TASK-021")]

        message = build_dangling_dependency_error(danglings)

        assert "TASK-022" in message
        assert "TASK-021" in message

    def test_explains_the_revision_boundary(self) -> None:
        """The operator has to know WHY it is missing, or they will look for a
        typo instead of recognising a cross-revision reference."""
        message = build_dangling_dependency_error(
            [DanglingDependency(task_id="TASK-022", missing="TASK-021")]
        )

        assert "revision" in message.lower()

    def test_reports_every_pair(self) -> None:
        message = build_dangling_dependency_error(
            [
                DanglingDependency(task_id="TASK-010", missing="TASK-001"),
                DanglingDependency(task_id="TASK-011", missing="TASK-003"),
            ]
        )

        assert "TASK-010 -> TASK-001" in message
        assert "TASK-011 -> TASK-003" in message


class TestVendoredContractProvenance:
    """Drift detection for a copy we cannot check against a live sibling.

    There is no runtime access to the spec-runner checkout — that is the point
    of vendoring — so the strongest available signal is internal consistency:
    the version this contract was read from, versus the version Maestro
    requires at run time. If the pin is raised past the vendored copy, the
    format may have moved and nobody re-read it.
    """

    def test_vendored_version_is_recorded(self) -> None:
        from maestro.tasks_spec import VENDORED_FROM_SPEC_RUNNER

        assert VENDORED_FROM_SPEC_RUNNER == "2.24.0"

    def test_both_vendored_copies_name_the_same_release(self) -> None:
        """The format and the stop-reason vocabulary were read together."""
        from maestro.retry_policy import (
            VENDORED_FROM_SPEC_RUNNER as retry_version,
        )
        from maestro.tasks_spec import VENDORED_FROM_SPEC_RUNNER as format_version

        assert retry_version == format_version

    def test_required_version_does_not_exceed_the_vendored_one(self) -> None:
        """Bumping the pin past the vendored copy must force a re-read.

        This is the #169b trigger's blast radius made visible: raising
        SPEC_RUNNER_REQUIRED_VERSION is exactly the moment to re-check both
        vendored contracts, and this assertion fails until someone does.
        """
        from maestro.spec_runner import (
            SPEC_RUNNER_REQUIRED_VERSION,
            parse_spec_runner_version,
        )
        from maestro.tasks_spec import VENDORED_FROM_SPEC_RUNNER

        required = parse_spec_runner_version(
            f"spec-runner {SPEC_RUNNER_REQUIRED_VERSION}"
        )
        vendored = parse_spec_runner_version(f"spec-runner {VENDORED_FROM_SPEC_RUNNER}")
        assert required is not None and vendored is not None
        assert required <= vendored, (
            "SPEC_RUNNER_REQUIRED_VERSION was raised above the release the "
            "vendored tasks.md/stop-reason contracts were read from; re-read "
            "them and update VENDORED_FROM_SPEC_RUNNER"
        )


class TestUpstreamTemplateShape:
    """Parse a block in the exact shape the upstream generator template emits.

    Copied from spec-runner 2.24.0
    `skills/spec-generator-skill/templates/tasks.template.md` at authoring
    time. This is the closest thing to a contract test available without
    reaching into the sibling repo: if the real format grows a line the parser
    mishandles, a faithful sample is where it shows.
    """

    TEMPLATE_BLOCK = """\
### TASK-100: Test Infrastructure Setup
🔴 P0 | ⬜ TODO | Est: 2d

**Description:**
Set up the test infrastructure.

**Checklist:**
- [ ] Test framework setup (Python: `pytest`)
- [ ] Coverage reporting

**Traces to:** [NFR-000]
**Depends on:** —
**Blocks:** All other tasks

---

## Milestone 1: MVP

### TASK-001: First real task
🟠 P1 | ⬜ TODO | Est: 1d

**Description:**
Do the thing.

**Traces to:** [REQ-001], [DESIGN-002]
**Depends on:** [TASK-100]
**Blocks:** [TASK-002]

---
"""

    def test_template_shape_parses_clean(self) -> None:
        assert find_dangling_dependencies(self.TEMPLATE_BLOCK) == []

    def test_traces_and_blocks_lines_are_not_dependencies(self) -> None:
        """`Traces to:` carries REQ/DESIGN ids and `Blocks:` carries task ids;
        neither is a dependency, and reading either would break every spec."""
        text = self.TEMPLATE_BLOCK.replace(
            "**Blocks:** [TASK-002]", "**Blocks:** [TASK-999]"
        )

        assert find_dangling_dependencies(text) == []

    def test_a_dangling_dep_in_template_shape_is_still_caught(self) -> None:
        text = self.TEMPLATE_BLOCK.replace(
            "**Depends on:** [TASK-100]", "**Depends on:** [TASK-021]"
        )

        assert find_dangling_dependencies(text) == [
            DanglingDependency(task_id="TASK-001", missing="TASK-021")
        ]
