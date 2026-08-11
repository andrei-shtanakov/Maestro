"""Dangling-dependency validation of a generated `tasks.md` (#165).

spec-runner owns the tasks.md format and validates dependencies when it runs,
exiting 1. That is correct but late: Maestro has already paid for spec
generation and spawned a process by then. A rework rewrites
`spec/maestro-tasks.md` wholesale, and the decomposer — knowing it is
continuing after TASK-021 — happily emits `**Depends on:** [TASK-021]` for a
task that exists only in the revision the rewrite replaced.

This validator runs right after `plan --full` and before the executor, so the
defect is caught while it is still cheap. The instruction in the rework
addendum is prevention; this is the guarantee, because correctness must not
depend on a model obeying an instruction.

**Vendored contract**, pinned to spec-runner `VENDORED_FROM_SPEC_RUNNER`
(`src/spec_runner/task.py`): the ID grammar, the `### <id>: <name>` header,
`[<id>]` references and the `**Depends on:**` line. Notably, references are
filtered by the prefixes the task headers actually use — that is what keeps
`[REQ-001]` / `[DESIGN-004]` traceability refs on a Depends line from being
read as dependencies. `contracts/` conventions apply: a copy inside this repo
rather than a reach into the sibling checkout.
"""

import re
from dataclasses import dataclass


VENDORED_FROM_SPEC_RUNNER = "2.24.0"
"""spec-runner release this format contract was read from.

Kept next to the copy so drift shows up in review rather than at run time,
and asserted against `SPEC_RUNNER_REQUIRED_VERSION`: raising the pin above
this version means the format was last read at an older release than the one
we now require, which is exactly when a re-read is due.
"""

_ID_PATTERN = r"[A-Z][A-Z0-9]*-\d+"
_TASK_HEADER = re.compile(rf"^### ({_ID_PATTERN}): (.+)$")
_TASK_REF = re.compile(rf"\[({_ID_PATTERN})\]")
_DEPENDS_ON = re.compile(r"\*\*Depends on:\*\* (.+)")
_NO_DEPENDENCIES = "—"


@dataclass(frozen=True)
class DanglingDependency:
    """A dependency that names a task the current revision does not contain."""

    task_id: str
    missing: str


def find_dangling_dependencies(text: str) -> list[DanglingDependency]:
    """Every dependency in `text` that no task in `text` provides.

    Resolution is by membership, not by order: a forward reference to a task
    defined later in the file is valid, because spec-runner schedules by the
    dependency graph rather than by position. Cycles are spec-runner's
    verdict, not this function's — it answers only "does the referenced task
    exist in this revision".
    """
    tasks = _parse_dependencies(text)
    known = set(tasks)
    prefixes = {task_id.split("-", 1)[0] for task_id in known}
    dangling: list[DanglingDependency] = []
    for task_id, refs in tasks.items():
        for ref in refs:
            # Only refs sharing a prefix with a real task header are
            # dependencies; `[REQ-001]`/`[DESIGN-004]` on the same line are
            # traceability, and treating them as dependencies would block
            # every well-formed spec.
            if ref.split("-", 1)[0] not in prefixes:
                continue
            if ref not in known:
                dangling.append(DanglingDependency(task_id=task_id, missing=ref))
    return dangling


def build_dangling_dependency_error(dangling: list[DanglingDependency]) -> str:
    """Operator-facing diagnosis naming each referencing task and missing id.

    States the revision boundary explicitly: without it an operator reads
    "TASK-021 not found" as a typo and goes looking in the wrong place,
    when the actual cause is a reference across a rewrite of the file.
    """
    pairs = ", ".join(f"{d.task_id} -> {d.missing}" for d in dangling)
    return (
        f"spec/maestro-tasks.md has {len(dangling)} dangling dependency "
        f"reference(s): {pairs}. Every dependency must resolve inside the "
        f"current revision of the file; these name tasks that exist only in "
        f"a previous revision (a rework rewrites the file wholesale). "
        f"spec-runner would reject this at run time — blocking now, before "
        f"the executor is spawned."
    )


def _parse_dependencies(text: str) -> dict[str, list[str]]:
    """Task id -> referenced ids, in file order.

    Mirrors the vendored parser's assignment semantics exactly: EVERY
    `**Depends on:**` line under a header assigns, so the last one wins, and a
    line reading `—` leaves whatever was already there untouched (it means "no
    dependencies", not "clear the list").

    That is not a detail to paraphrase. Agents editing tasks.md mid-run do
    duplicate meta lines, and a vendored copy that reads a file differently
    from the tool that owns the format would produce findings spec-runner
    disagrees with — the opposite of the early-diagnosis this validator exists
    for.
    """
    tasks: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        header = _TASK_HEADER.match(line)
        if header:
            current = header.group(1)
            tasks.setdefault(current, [])
            continue
        if current is None:
            continue
        depends = _DEPENDS_ON.search(line)
        if depends:
            body = depends.group(1)
            if body.strip() != _NO_DEPENDENCIES:
                tasks[current] = _TASK_REF.findall(body)
    return tasks


SELF_CONTAINED_DEPENDENCIES_INSTRUCTION = (
    "Dependency constraint: this run REGENERATES spec/maestro-tasks.md from "
    "scratch. Every `**Depends on:**` reference must name a task defined in "
    "the file you are writing now. Do not reference task ids from an earlier "
    "revision of this file, even when continuing work — they will not exist. "
    "Carry forward anything still needed as a task in this revision instead."
)
"""Prevention only (#165).

Appended to a rework decomposition's description so the model is told the
constraint up front. It is NOT the guarantee — `find_dangling_dependencies`
is, and it runs whether or not the instruction was honoured. Kept here, next
to the validator, so the two never drift into describing different rules.
"""
