"""(workdir, scope) reservations + static per-workdir arming for Mode-1 remote.

Pure path logic (no DB, no filesystem writes). The overlap test is
deliberately conservative in *possible-path* space: it may serialize two
disjoint scopes that share an ancestor (false positive), but it never lets a
real overlap through (no false negative). Exact-path matching lives in
`ssh_collect` against actual changed paths.
"""

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
