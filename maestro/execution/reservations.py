"""(workdir, scope) reservations + static per-workdir arming for Mode-1 remote.

Pure path logic (no DB, no filesystem writes). The overlap test is
deliberately conservative in *possible-path* space: it may serialize two
disjoint scopes that share an ancestor (false positive), but it never lets a
real overlap through (no false negative). Exact-path matching lives in
`ssh_collect` against actual changed paths.
"""

from dataclasses import dataclass
from pathlib import Path

from maestro.execution.exec_config import ExecutionConfig
from maestro.models import Task


_WILDCARD = set("*?[")


def anchor_of(glob: str) -> str:
    """Longest leading wildcard-free path prefix; '' == workdir root.

    Literal `.` segments are dropped so `./src/**` and `src/**` normalize
    to the same anchor (a bare `strip("/").split("/")` would otherwise
    keep them as distinct anchors and falsely declare overlapping scopes
    disjoint). A literal `..` segment means the scope may escape the
    workdir root — return the whole-workdir anchor `""`, the conservative
    over-approximation (reserves everything, never under-approximates).
    """
    segments = glob.strip("/").split("/")
    literal: list[str] = []
    for seg in segments:
        if any(ch in _WILDCARD for ch in seg):
            break
        if seg == ".":
            continue
        if seg == "..":
            return ""
        literal.append(seg)
    return "/".join(literal)


def _covers(a: str, b: str) -> bool:
    """Anchor `a` covers anchor `b` on segment boundaries; '' covers all."""
    if a == "":
        return True
    return b == a or b.startswith(a + "/")


def canonical_workdir(path: str | Path) -> Path:
    """Absolute, symlink-resolved workdir key (same policy everywhere)."""
    return Path(path).expanduser().resolve()


@dataclass(frozen=True)
class Reservation:
    workdir: Path
    anchors: frozenset[str]


def scope_to_reservation(workdir: str | Path, scope: list[str]) -> Reservation:
    """Empty/undeclared scope reserves the whole workdir (anchor '')."""
    anchors = frozenset(anchor_of(g) for g in scope) if scope else frozenset({""})
    return Reservation(workdir=canonical_workdir(workdir), anchors=anchors)


def overlaps(a: Reservation, b: Reservation) -> bool:
    """Check if two reservations overlap."""
    if a.workdir != b.workdir:
        return False
    return any(_covers(x, y) or _covers(y, x) for x in a.anchors for y in b.anchors)


class UnboundedRemoteScopeError(Exception):
    """A Mode-1 SSH task declared no scope — remote collect would be unbounded."""


def effective_backend_name(task: Task, execution: ExecutionConfig) -> str:
    """Resolve a task's effective backend name (task override or execution default)."""
    return task.backend or execution.default_backend


def is_ssh_task(task: Task, execution: ExecutionConfig) -> bool:
    """Check if task targets an SSH backend (by transport type, not name)."""
    spec = execution.normalized().get(effective_backend_name(task, execution))
    return spec is not None and spec.transport.type == "ssh"


def compute_armed_workdirs(tasks: list[Task], execution: ExecutionConfig) -> set[Path]:
    """Return canonical workdirs hosting ≥1 SSH task."""
    return {canonical_workdir(t.workdir) for t in tasks if is_ssh_task(t, execution)}


def validate_ssh_scopes(tasks: list[Task], execution: ExecutionConfig) -> None:
    """Raise UnboundedRemoteScopeError if any SSH task has empty scope."""
    for t in tasks:
        if is_ssh_task(t, execution) and not t.scope:
            raise UnboundedRemoteScopeError(
                f"SSH task {t.id!r} has no scope: remote Mode-1 execution "
                "requires a bounding scope (parent design §2/§7)"
            )


class ReservationRegistry:
    """In-memory owner->Reservation map with a conservative overlap gate."""

    def __init__(self) -> None:
        self._held: dict[str, Reservation] = {}

    def try_acquire(self, owner: str, r: Reservation) -> bool:
        for other, held in self._held.items():
            if other != owner and overlaps(held, r):
                return False
        self._held[owner] = r
        return True

    def reconstruct(self, owner: str, r: Reservation) -> None:
        self._held[owner] = r

    def release(self, owner: str) -> None:
        self._held.pop(owner, None)

    def holds(self, owner: str) -> bool:
        return owner in self._held
