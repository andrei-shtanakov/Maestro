"""(workdir, scope) reservations + static per-workdir arming for Mode-1 remote.

Pure path logic (no DB, no filesystem writes). The overlap test is
deliberately conservative in *possible-path* space: it may serialize two
disjoint scopes that share an ancestor (false positive), but it never lets a
real overlap through (no false negative). Exact-path matching lives in
`ssh_collect` against actual changed paths.
"""

from dataclasses import dataclass
from pathlib import Path


_WILDCARD = set("*?[")


def anchor_of(glob: str) -> str:
    """Longest leading wildcard-free path prefix; '' == workdir root."""
    segments = glob.strip("/").split("/")
    literal: list[str] = []
    for seg in segments:
        if any(ch in _WILDCARD for ch in seg):
            break
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
