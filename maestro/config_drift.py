"""Resume-time config drift detection (#198) — pure comparison, no I/O.

A run persists its workstream configuration when it is created, and every
later tick works from that copy. That is correct: a run must not change the
rules under itself mid-flight. The defect was the **silence** — editing
`project.yaml` and resuming looked exactly like "the edit applied and did not
help", and a green `maestro validate` on the edited file actively encouraged
that reading. One reported cycle of fix → resume → identical refusal was spent
before anyone opened `state.db`.

So this module answers one question: does `project.yaml` still say what the
run was started with? It computes the answer and nothing else — the caller
decides what a drift means. Detection is deliberately separated from the halt
because the two happen at different moments: drift is known before recovery,
and must not pre-empt it (see the orchestrator's `run`).

What is NOT here, on purpose: any way to adopt the edit. `maestro
workstream-rework <id> --refresh-from project.yaml` already refreshes
description/scope with re-validation and an audit record, at the price of a
full re-decomposition; topology fields it refuses outright. Teaching this
check to write the new config into the run would give the operator a cheap
path to change a running plan's rules, which is the thing persisting the
config protects against.
"""

from __future__ import annotations

from dataclasses import dataclass

from maestro.models import Workstream, WorkstreamConfig


#: Fields an operator can adopt into a live run, and the command that does it.
#: `rework.validate_refresh` is the authority on this set; it is mirrored here
#: only to tell the operator which half of a drift has a cheap remedy.
REFRESHABLE_FIELDS = frozenset({"description", "scope"})

REFRESH_HINT = (
    "adopt with: maestro workstream-rework <id> --refresh-from <config> "
    "(a rework — it re-decomposes and respawns the author, not a free "
    "config update)"
)
FROZEN_HINT = (
    "cannot be adopted into a live run: revert the edit in the config, or "
    "start a new run against the edited config"
)

#: Compared per workstream. `branch` is derived from `branch_prefix` rather
#: than declared per workstream, and is included because editing the prefix is
#: silently ignored in exactly the same way — the whole class, not one field.
COMPARED_FIELDS = (
    "title",
    "description",
    "scope",
    "depends_on",
    "priority",
    "backend",
    "branch",
)

#: Order carries no meaning for either: scope globs are asked "does any match"
#: (`scope_gate.find_escapes`) and dependencies are a set. Reordering is not
#: drift, and reporting it as such would train operators to ignore the check.
_ORDER_FREE_FIELDS = frozenset({"scope", "depends_on"})


@dataclass(frozen=True)
class FieldDrift:
    """One field of one workstream that the config and the run disagree on."""

    workstream_id: str
    field: str
    persisted: object
    configured: object

    @property
    def refreshable(self) -> bool:
        return self.field in REFRESHABLE_FIELDS


@dataclass(frozen=True)
class ConfigDrift:
    """Everything the config says that the run does not, and vice versa."""

    fields: tuple[FieldDrift, ...] = ()
    added_ids: tuple[str, ...] = ()
    removed_ids: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.fields or self.added_ids or self.removed_ids)

    @property
    def all_refreshable(self) -> bool:
        """True when every difference has a supported adoption path.

        An added or removed workstream never qualifies: the DAG's shape is not
        something `--refresh-from` will touch.
        """
        if self.added_ids or self.removed_ids:
            return False
        return bool(self.fields) and all(f.refreshable for f in self.fields)


def _normalize(field: str, value: object) -> object:
    if field in _ORDER_FREE_FIELDS and isinstance(value, list):
        return sorted(value)
    return value


def find_config_drift(
    configured: list[WorkstreamConfig],
    persisted: list[Workstream],
    branch_prefix: str,
    workstreams_declared: bool | None = None,
) -> ConfigDrift:
    """Compare the config's workstreams against the run's persisted copy.

    ``workstreams_declared`` says how this run's workstreams were created, and
    exists only to disambiguate an EMPTY ``configured``. The two causes are
    indistinguishable from the persisted rows alone:

    * ``False`` — auto-decomposed. Nothing was declared, so nothing can
      disagree; reporting the whole run as "removed" would be nonsense.
    * ``True`` — declared, and the section is now gone. That is the same
      silence this module exists to end, just a different edit shape, so it
      reports every workstream as removed.
    * ``None`` — a run predating migration 28. **Fails open**: treated as
      auto-decomposed. Halting every legacy auto-decomposed run on resume
      would be a worse defect than the hole left unclosed, and per-run state
      directories are short-lived enough that the unknown window closes on its
      own.
    """
    if not configured:
        if workstreams_declared and persisted:
            return ConfigDrift(removed_ids=tuple(sorted(w.id for w in persisted)))
        return ConfigDrift()

    by_id = {w.id: w for w in persisted}
    configured_by_id = {c.id: c for c in configured}

    fields: list[FieldDrift] = []
    for ws_id, entry in configured_by_id.items():
        row = by_id.get(ws_id)
        if row is None:
            continue
        expected = Workstream.from_config(entry, branch_prefix=branch_prefix)
        for field in COMPARED_FIELDS:
            want = _normalize(field, getattr(expected, field))
            have = _normalize(field, getattr(row, field))
            if want != have:
                fields.append(
                    FieldDrift(
                        workstream_id=ws_id,
                        field=field,
                        persisted=have,
                        configured=want,
                    )
                )

    return ConfigDrift(
        fields=tuple(fields),
        added_ids=tuple(sorted(set(configured_by_id) - set(by_id))),
        removed_ids=tuple(sorted(set(by_id) - set(configured_by_id))),
    )


def render_config_drift(drift: ConfigDrift, config_path: str) -> str:
    """Operator-facing explanation: what differs, and what can be done.

    States that the persisted version stays in force. The reported failure
    mode was reading "resumed and still refused" as "my edit did not help", so
    the message has to close that reading off explicitly rather than merely
    listing differences.
    """
    lines = [
        f"config drift: {config_path} no longer matches this run's persisted "
        "configuration.",
        "",
        "The run continues to use the PERSISTED version — a run does not "
        "change its own rules mid-flight. Nothing was dispatched.",
        "",
    ]

    for ws_id in sorted({f.workstream_id for f in drift.fields}):
        lines.append(f"  workstream '{ws_id}':")
        for field in (f for f in drift.fields if f.workstream_id == ws_id):
            lines.append(f"    {field.field}:")
            lines.append(f"      run:    {field.persisted!r}")
            lines.append(f"      config: {field.configured!r}")
        lines.append("")

    if drift.added_ids:
        lines.append(
            f"  declared in the config but not in this run: "
            f"{', '.join(drift.added_ids)}"
        )
        lines.append("")
    if drift.removed_ids:
        lines.append(
            f"  in this run but no longer declared in the config: "
            f"{', '.join(drift.removed_ids)}"
        )
        lines.append("")

    refreshable = sorted({f.field for f in drift.fields if f.refreshable})
    frozen = sorted({f.field for f in drift.fields if not f.refreshable})
    if refreshable:
        lines.append(f"{', '.join(refreshable)} — {REFRESH_HINT}")
    if frozen or drift.added_ids or drift.removed_ids:
        changed = ", ".join(frozen) if frozen else "the workstream set"
        lines.append(f"{changed} — {FROZEN_HINT}")

    return "\n".join(lines).rstrip()
